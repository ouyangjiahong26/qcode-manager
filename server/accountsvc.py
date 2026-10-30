"""PAT 账号池：存储（Fernet 加密落库）、测活、轮询调度与用量累计。

qoder2api 无状态、PAT 由请求方携带，账号池完全由本面板自管：
- PAT 只以密文落库（密钥 ``data/secret.key``，不存在则自动生成，0600），
  列表/详情只出尾 4 位掩码；明文仅 ``get_account`` 的 ``_pat`` 字段供后端内部用。
- 测活即保活：带 PAT 调上游 ``GET /v1/models``（真实鉴权），回写
  ``status/last_check_*``；调度只挑 enabled 且非 invalid 的账号轮询。
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

from . import config, db
from .services import qoder2api

logger = logging.getLogger('qcode.accountsvc')

# 池内并发测活上限：上游是单机服务，全量并发打过去只会拖垮它（与保活调度同值）。
TEST_CONCURRENCY = 5

# ── Fernet 密钥管理 ────────────────────────────────────────
#
# 密钥文件与数据库同目录（DATA_DIR）。首次用到时自动生成——不放环境变量：
# 那样部署者更容易把密钥和备份一起弄丢，落盘文件反而能随数据目录一起备份/恢复。
# 按**路径**缓存实例：单测会把 DATA_DIR 指到临时目录，按路径分桶互不串味。
_FERNET_CACHE: dict[str, object] = {}
_FERNET_LOCK = threading.Lock()


def _key_path():
    return config.DATA_DIR / 'secret.key'


def _write_key_file(path, key: bytes) -> None:
    """以 0600 独占写入新密钥（O_EXCL 防并发双写；Windows 上权限收紧尽力而为）。"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows 只支持只读位


def _load_fernet():
    """取当前数据目录对应的 Fernet 实例（密钥文件不存在则生成，0600）。"""
    from cryptography import fernet

    path = _key_path()
    cache_key = str(path)
    with _FERNET_LOCK:
        hit = _FERNET_CACHE.get(cache_key)
        if hit is not None:
            return hit
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        if path.exists():
            key = path.read_bytes().strip()
        else:
            key = fernet.Fernet.generate_key()
            try:
                _write_key_file(path, key)
            except FileExistsError:
                # 另一进程刚生成了密钥：以落盘的那份为准，避免两个进程各持一把
                key = path.read_bytes().strip()
        f = fernet.Fernet(key)
        _FERNET_CACHE[cache_key] = f
        return f


def _reset_crypto_cache() -> None:
    """清掉 Fernet 实例缓存（单测切换数据目录后调用）。"""
    with _FERNET_LOCK:
        _FERNET_CACHE.clear()


def encrypt_pat(pat: str) -> str:
    return _load_fernet().encrypt(pat.encode('utf-8')).decode('ascii')


def decrypt_pat(enc: str) -> str:
    return _load_fernet().decrypt(enc.encode('ascii')).decode('utf-8')


# ── 上游解析 ───────────────────────────────────────────────
def _resolve_upstream(account: dict) -> dict:
    """账号所属分组的上游地址与附加鉴权。

    直接读 upstreams 表而不是 import upstreamsvc：本模块被网关热路径依赖，
    保持零跨模块耦合；分组被删/停用时回落默认上游，测活与调度不断链
    （账号数据本身不因分组消失而失效）。
    """
    uid = account.get('upstream_id')
    if uid is not None:
        row = db.query_one(
            'SELECT base_url, api_key, enabled FROM upstreams WHERE id = ?', (uid,))
        if row is not None and row['enabled']:
            return {'base_url': row['base_url'], 'api_key': str(row['api_key'] or '')}
    return {'base_url': config.QC2API_BASE, 'api_key': config.QC2API_KEY}


def _now() -> int:
    return int(time.time())


# 对外暴露的列：绝不含 pat_encrypted（密文也不出接口，密文+密钥才危险，
# 但密文本身就是敏感元数据，没必要给界面）。
_PUBLIC_COLUMNS = (
    'id, label, pat_suffix, upstream_id, enabled, status,'
    ' last_check_at, last_check_ok, last_check_msg,'
    ' requests, total_tokens, weight, note, created_at, updated_at'
)


def _norm_upstream_id(value) -> int | None:
    """空值/非法值统一收敛为 None（= 默认上游）。"""
    if value in (None, '', 0, '0'):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def create_account(label: str, pat: str, upstream_id: int | None = None,
                   note: str = '') -> dict:
    """新增 PAT 并**同步测活一次**：录入即知道死活，界面无需二次点击。

    本函数是同步的（契约约定），测活走一次性同步 HTTP 请求；调用方若在
    事件循环里，应放到线程中执行（路由层用同步 handler，由框架线程池兜底）。
    """
    label = str(label or '').strip()
    pat = str(pat or '').strip()
    if not label:
        raise ValueError('账号名称不能为空')
    if not pat:
        raise ValueError('PAT 不能为空')
    now = _now()
    account_id = db.execute(
        'INSERT INTO accounts(label, pat_encrypted, pat_suffix, upstream_id,'
        ' enabled, status, note, created_at, updated_at)'
        ' VALUES (?,?,?,?,1,?,?,?,?)',
        (label, encrypt_pat(pat), pat[-4:], _norm_upstream_id(upstream_id),
         'unknown', str(note or ''), now, now))
    ok, msg = _test_pat_sync(account_id)
    _mark(account_id, ok, msg)
    out = dict(db.query_one(
        f'SELECT {_PUBLIC_COLUMNS} FROM accounts WHERE id = ?', (account_id,)))
    out['test'] = {'ok': ok, 'msg': msg}
    return out


def _test_pat_sync(account_id: int) -> tuple[bool, str]:
    """同步测活（建账即测）：与 test_account 同一判据，仅 HTTP 栈不同。

    为什么不复用异步版：本函数运行在无事件循环的线程上下文里，
    起一次性同步客户端比搬运事件循环便宜得多。
    """
    import httpx

    row = db.query_one(
        'SELECT pat_encrypted, upstream_id FROM accounts WHERE id = ?', (account_id,))
    if row is None:
        return False, '账号不存在'
    upstream = _resolve_upstream({'upstream_id': row['upstream_id']})
    base = str(upstream['base_url'] or '').strip().rstrip('/')
    headers = qoder2api.forward_headers(
        decrypt_pat(row['pat_encrypted']), api_key=upstream['api_key'])
    try:
        with httpx.Client(timeout=httpx.Timeout(15, connect=3), trust_env=False,
                          proxy=config.effective_http_proxy() or None) as client:
            resp = client.get(f'{base}/v1/models', headers=headers)
    except Exception as exc:  # noqa: BLE001
        return False, f'无法连接上游：{exc}'
    if resp.status_code >= 400:
        msg = f'上游返回 {resp.status_code}'
        if resp.status_code == 401:
            msg = f'{msg}（PAT 无效或已过期）'
        return False, msg
    return True, '测活成功'


def update_account(account_id: int, patch: dict) -> dict | None:
    """PATCH 语义：只认 label/note/enabled/weight/upstream_id，其余忽略。"""
    current = db.query_one(
        f'SELECT {_PUBLIC_COLUMNS} FROM accounts WHERE id = ?', (account_id,))
    if current is None:
        return None
    sets: list[str] = []
    args: list = []
    if 'label' in patch:
        label = str(patch['label'] or '').strip()
        if not label:
            raise ValueError('账号名称不能为空')
        sets.append('label = ?')
        args.append(label)
    if 'note' in patch:
        sets.append('note = ?')
        args.append(str(patch['note'] or ''))
    if 'enabled' in patch:
        sets.append('enabled = ?')
        args.append(1 if patch['enabled'] in (True, 1, '1') else 0)
    if 'weight' in patch:
        try:
            weight = int(patch['weight'])
        except (TypeError, ValueError) as exc:
            raise ValueError('权重必须是整数') from exc
        if weight < 1:
            raise ValueError('权重至少为 1')
        # 权重当前存而不用：调度先保持纯轮询，字段留作后续加权调度
        sets.append('weight = ?')
        args.append(weight)
    if 'upstream_id' in patch:
        sets.append('upstream_id = ?')
        args.append(_norm_upstream_id(patch['upstream_id']))
    if sets:
        sets.append('updated_at = ?')
        args.append(_now())
        args.append(account_id)
        db.execute(
            f'UPDATE accounts SET {", ".join(sets)} WHERE id = ?', tuple(args))
    return dict(db.query_one(
        f'SELECT {_PUBLIC_COLUMNS} FROM accounts WHERE id = ?', (account_id,)))


def delete_account(account_id: int) -> bool:
    # db.execute 返回 lastrowid（对 DELETE 恒 0），不能用返回值判是否删到，
    # 先查存在性再删。
    row = db.query_one('SELECT id FROM accounts WHERE id = ?', (account_id,))
    if row is None:
        return False
    db.execute('DELETE FROM accounts WHERE id = ?', (account_id,))
    return True


def list_accounts(upstream_id: int | None = None, *,
                  include_disabled: bool = True) -> list[dict]:
    """账号列表（掩码形态）。upstream_id 为 None 时不过滤分组。"""
    conds: list[str] = []
    args: list = []
    if upstream_id is not None:
        conds.append('upstream_id = ?')
        args.append(upstream_id)
    if not include_disabled:
        conds.append('enabled = 1')
    sql = f'SELECT {_PUBLIC_COLUMNS} FROM accounts'
    if conds:
        sql += ' WHERE ' + ' AND '.join(conds)
    sql += ' ORDER BY id'
    return [dict(r) for r in db.query(sql, tuple(args))]


def get_account(account_id: int) -> dict | None:
    """含 ``_pat`` 明文——**仅后端内部使用**（网关注入、导出），绝不直接回接口。"""
    row = db.query_one(
        f'SELECT {_PUBLIC_COLUMNS}, pat_encrypted FROM accounts WHERE id = ?',
        (account_id,))
    if row is None:
        return None
    out = {k: row[k] for k in row.keys() if k != 'pat_encrypted'}
    try:
        out['_pat'] = decrypt_pat(row['pat_encrypted'])
    except Exception as exc:  # noqa: BLE001 —— 密钥文件丢失/密文损坏时不炸调用方
        out['_pat'] = ''
        logger.error('PAT 解密失败 account=%s: %s', account_id, exc)
    return out


def reveal_accounts() -> list[dict]:
    """管理员导出用：label + PAT 明文（调用方必须走最高鉴权并落审计）。"""
    rows = db.query('SELECT id, label, pat_encrypted, upstream_id, enabled,'
                    ' status, note, created_at FROM accounts ORDER BY id')
    out = []
    for r in rows:
        item = {k: r[k] for k in ('id', 'label', 'upstream_id', 'enabled',
                                  'status', 'note', 'created_at')}
        try:
            item['pat'] = decrypt_pat(r['pat_encrypted'])
        except Exception:  # noqa: BLE001 —— 单条坏密文不阻断整份导出
            item['pat'] = ''
        out.append(item)
    return out


def _mark(account_id: int, ok: bool, msg: str) -> None:
    """回写测活结果。首版直接按结果置状态（连续失败阈值留作后续扩展）。"""
    status = 'active' if ok else 'invalid'
    db.execute(
        'UPDATE accounts SET status = ?, last_check_at = ?, last_check_ok = ?,'
        ' last_check_msg = ?, updated_at = ? WHERE id = ?',
        (status, _now(), 1 if ok else 0, str(msg or '')[:500], _now(), account_id))


def mark_result(account_id: int, ok: bool, msg: str = '') -> None:
    _mark(account_id, ok, msg)


async def test_account(account_id: int) -> dict:
    """对单个账号执行真实测活（上游会校验 PAT），并回写状态。"""
    row = db.query_one(
        'SELECT pat_encrypted, upstream_id FROM accounts WHERE id = ?', (account_id,))
    if row is None:
        return {'ok': False, 'missing': True, 'msg': '账号不存在'}
    upstream = _resolve_upstream({'upstream_id': row['upstream_id']})
    try:
        pat = decrypt_pat(row['pat_encrypted'])
    except Exception as exc:  # noqa: BLE001
        ok, msg = False, f'PAT 解密失败：{exc}'
    else:
        try:
            await qoder2api.get_models(upstream['base_url'], pat,
                                       api_key=upstream['api_key'])
            ok, msg = True, '测活成功'
        except qoder2api.UpstreamError as exc:
            ok, msg = False, str(exc)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f'测活异常：{exc}'
    _mark(account_id, ok, msg)
    out = dict(db.query_one(
        f'SELECT {_PUBLIC_COLUMNS} FROM accounts WHERE id = ?', (account_id,)))
    out['ok'] = ok
    out['msg'] = msg
    return out


async def _run_tests(account_ids: list[int], timeout: float) -> list[dict]:
    """并发测一批账号（信号量限流 + 单账号超时上限）。"""
    sem = asyncio.Semaphore(TEST_CONCURRENCY)

    async def one(aid: int) -> dict:
        async with sem:
            try:
                return await asyncio.wait_for(test_account(aid), timeout)
            except asyncio.TimeoutError:
                _mark(aid, False, f'测活超时（>{int(timeout)}s）')
                return {'ok': False, 'id': aid, 'msg': '测活超时'}
            except Exception as exc:  # noqa: BLE001 —— 单账号失败不拖垮整批
                return {'ok': False, 'id': aid, 'msg': f'测活异常：{exc}'}

    return list(await asyncio.gather(*(one(a) for a in account_ids)))


async def test_all(upstream_id: int | None = None) -> dict:
    """批量测活（enabled 账号；单账号 15s 超时上限），返回汇总。"""
    rows = list_accounts(upstream_id, include_disabled=False)
    results = await _run_tests([r['id'] for r in rows], timeout=15)
    ok = sum(1 for r in results if r.get('ok'))
    return {
        'total': len(results),
        'ok': ok,
        'failed': len(results) - ok,
        'results': results,
    }


# ── 轮询调度 ───────────────────────────────────────────────
#
# 指针按分组各自独立（key = upstream_id，None 表示默认分组）：不同分组的
# 轮询互不干扰；进程重启指针归零（重新从最小编号开始，无持久化必要）。
_PICK_CURSOR: dict[object, int] = {}


def _pick_candidates(upstream_id: int | None, status_clause: str):
    sql = ('SELECT id, pat_encrypted FROM accounts WHERE enabled = 1'
           f' AND status {status_clause}'
           ' AND upstream_id IS ?')
    return db.query(sql, (upstream_id,))


def pick_pat(upstream_id: int | None = None) -> tuple[int, str] | None:
    """为本次转发挑选 PAT：候选集 = enabled 且 status != 'invalid'。

    候选集本就包含 unknown（未测活不歧视，即「全部 invalid 时退而选
    enabled 且 unknown」的放宽）；池空（无 enabled 或全部 invalid）返回
    None，由调用方报「无可用 PAT」。纯轮询；weight 存而不用，留作加权扩展。
    """
    rows = _pick_candidates(upstream_id, "!= 'invalid'")
    if not rows:
        return None
    idx = _PICK_CURSOR.get(upstream_id, 0)
    row = rows[idx % len(rows)]
    _PICK_CURSOR[upstream_id] = (idx + 1) % len(rows)
    try:
        pat = decrypt_pat(row['pat_encrypted'])
    except Exception as exc:  # noqa: BLE001
        # 解密失败（密钥文件被换/密文损坏）视同该账号不可用，本轮放弃调度
        logger.error('PAT 解密失败 account=%s: %s', row['id'], exc)
        return None
    return int(row['id']), pat


def bump_usage(account_id: int, tokens: int) -> None:
    """转发成功后累计账号用量（请求数 +1、token 累加）。"""
    db.bump_account_usage(account_id, int(tokens or 0))
