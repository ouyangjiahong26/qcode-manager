"""多上游接入点（PAT 池分组）的配置、校验与解析。

职责与两条硬约束：
1. **默认上游不是一行数据库记录**，而是环境变量（QC2API_BASE / QC2API_KEY）
   的投影 —— id 为 None、永远可用、不可编辑、不可删除。所有「绑定了某上游」
   的数据（api_keys.upstream_id / accounts.upstream_id）里 NULL 都表示默认上游。
2. **引用完整性**：还有密钥或账号绑在某上游上时不允许删除它（返回
   (False, n) 由路由层回 409）。否则那批密钥 / 账号会立刻变成「绑定缺失」
   而全部请求失败，事后只能从日志里反推。
"""
from __future__ import annotations

import time

from . import config, db

# 默认上游的 id 约定：None（而不是某个魔法数字）。
# 为什么不用 0：0 在 SQL 里是合法 id，前端序列化时 '' / 0 / null 极易混淆，
# 而 None 在 JSON 里就是 null，语义唯一。
DEFAULT_ID = None

MAX_NAME = 64
MAX_NOTE = 200
MAX_URL = 500


class UpstreamUnavailable(Exception):
    """密钥绑定的上游不可用（已被删除或已停用）。

    网关捕获它回 502/503 级错误而不是 500：这不是面板的故障，修法在配置里
    （重新绑定 / 启用上游），把原因带给界面比堆栈有用。
    """


def _clean(value: object, limit: int) -> str:
    return str(value or '').strip()[:limit]


def normalize_base_url(value: object) -> str:
    """归一化 Base URL：去掉末尾斜杠，必须是 http(s)://。

    拼接规则是 `{base}/v1/...`，末尾多一个斜杠就会变成 `//v1`；多数网关能容错，
    但不该把正确性押在对面容错上。
    """
    url = _clean(value, MAX_URL)
    if not url.startswith(('http://', 'https://')):
        raise ValueError('上游地址必须以 http:// 或 https:// 开头')
    return url.rstrip('/')


def _row_to_dict(row) -> dict:
    return {
        'id': int(row['id']),
        'name': str(row['name']),
        'base_url': str(row['base_url']),
        'api_key': str(row['api_key'] or ''),
        'note': str(row['note'] or ''),
        'enabled': bool(row['enabled']),
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
        'is_default': False,
    }


def default_upstream() -> dict:
    """默认上游（环境变量），永远可用、不可编辑、不可删除。"""
    return {
        'id': DEFAULT_ID,
        'name': '默认上游',
        'base_url': config.QC2API_BASE,
        'api_key': config.QC2API_KEY,
        'note': '来自 QC2API_BASE 环境变量（未配置多上游时使用的接入点）',
        'enabled': True,
        'created_at': None,
        'updated_at': None,
        'is_default': True,
    }


def get_upstream(upstream_id: object) -> dict | None:
    try:
        uid = int(upstream_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    row = db.query_one('SELECT * FROM upstreams WHERE id = ?', (uid,))
    return _row_to_dict(row) if row else None


def list_upstreams(*, include_default: bool = True) -> list[dict]:
    """全部上游：默认上游排在最前（id=None），其余按创建顺序。

    顺序稳定是有意的：前端下拉的「默认」总在第一位，而新增的上游不会突然插到
    最前面把已有选择顶掉。
    """
    out: list[dict] = []
    if include_default:
        out.append(default_upstream())
    for row in db.query('SELECT * FROM upstreams ORDER BY id ASC'):
        out.append(_row_to_dict(row))
    return out


def create_upstream(name: object, base_url: object, api_key: object = '',
                    note: object = '', enabled: object = True) -> dict:
    nm = _clean(name, MAX_NAME)
    if not nm:
        raise ValueError('上游名称不能为空')
    url = normalize_base_url(base_url)
    now = int(time.time())
    uid = db.execute(
        'INSERT INTO upstreams(name, base_url, api_key, note, enabled, '
        'created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)',
        (nm, url, _clean(api_key, MAX_URL), _clean(note, MAX_NOTE),
         1 if enabled else 0, now, now),
    )
    row = db.query_one('SELECT * FROM upstreams WHERE id = ?', (uid,))
    return _row_to_dict(row)


def update_upstream(upstream_id: object, patch: dict) -> dict | None:
    """局部更新（PATCH 语义）：只改传进来的字段。

    与 keysvc.update_key 同口径——`'key' in patch` 判定而不是真值判定，
    这样「把 api_key 清空」是能生效的动作，而不是被 `or` 吞掉的空操作。
    """
    current = get_upstream(upstream_id)
    if current is None:
        return None
    fields: dict[str, object] = {}
    if 'name' in patch:
        nm = _clean(patch['name'], MAX_NAME)
        if not nm:
            raise ValueError('上游名称不能为空')
        fields['name'] = nm
    if 'base_url' in patch:
        fields['base_url'] = normalize_base_url(patch['base_url'])
    if 'api_key' in patch:
        fields['api_key'] = _clean(patch['api_key'], MAX_URL)
    if 'note' in patch:
        fields['note'] = _clean(patch['note'], MAX_NOTE)
    if 'enabled' in patch:
        fields['enabled'] = 1 if patch['enabled'] else 0
    if fields:
        fields['updated_at'] = int(time.time())
        assignments = ', '.join(f'{k} = ?' for k in fields)
        db.execute(f'UPDATE upstreams SET {assignments} WHERE id = ?',
                   (*fields.values(), current['id']))
    return get_upstream(current['id'])


def bound_key_count() -> dict[int, int]:
    """每个上游各被多少把网关密钥引用：{id: n}（不含未绑定的默认池）。

    一次 GROUP BY 取回（而不是每个上游查一遍）：列表接口要逐行显示，N+1 查询
    没有必要。
    """
    counts: dict[int, int] = {}
    for row in db.query(
            'SELECT upstream_id, COUNT(*) AS n FROM api_keys '
            'WHERE upstream_id IS NOT NULL GROUP BY upstream_id'):
        counts[int(row['upstream_id'])] = int(row['n'])
    return counts


def bound_account_count() -> dict[int, int]:
    """每个上游各绑了多少个 PAT 账号：{id: n}（不含未绑定的默认池）。"""
    counts: dict[int, int] = {}
    for row in db.query(
            'SELECT upstream_id, COUNT(*) AS n FROM accounts '
            'WHERE upstream_id IS NOT NULL GROUP BY upstream_id'):
        counts[int(row['upstream_id'])] = int(row['n'])
    return counts


def delete_upstream(upstream_id: object) -> tuple[bool, int]:
    """删除上游，返回 (是否删除, 引用它的密钥与账号总数)。

    **有引用时拒绝删除**（返回 (False, n)，由路由层回 409）：删除本身是合法
    动作，但它会让那批密钥 / 账号立刻变成「绑定缺失」而全部 5xx——先把引用
    清掉，或者让管理员显式改绑，比事后排查一堆报错强。计数与删除在同一个
    事务里做，避免「刚数完就被新建的密钥绑上」的竞态。
    """
    try:
        uid = int(upstream_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False, 0
    with db._lock:
        # 连接在锁内取：备份恢复会在锁内关闭并重开共享连接，锁外拿到的句柄
        # 可能在随后使用时已经被关掉。
        conn = db.connect()
        try:
            conn.execute('BEGIN')
            row = conn.execute(
                'SELECT (SELECT COUNT(*) FROM api_keys WHERE upstream_id = ?) + '
                '(SELECT COUNT(*) FROM accounts WHERE upstream_id = ?) AS n',
                (uid, uid)).fetchone()
            used = int(row['n']) if row else 0
            if used:
                conn.rollback()
                return False, used
            cur = conn.execute('DELETE FROM upstreams WHERE id = ?', (uid,))
            deleted = int(cur.rowcount or 0) > 0
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return deleted, 0


def resolve_for_key(key: dict | None) -> dict:
    """解析本次请求该用哪个上游：密钥绑定优先，未绑定走默认上游。

    绑定缺失 / 停用时抛 `UpstreamUnavailable`——默认上游不是「兜底」，它只是
    「未绑定」时的正常去处；绑了却不可用必须报错，静默回落会掩盖配置问题。
    """
    uid = (key or {}).get('upstream_id')
    if uid in (None, '', 0, '0'):
        return default_upstream()
    row = get_upstream(uid)
    if row is None:
        raise UpstreamUnavailable(
            f'密钥绑定的上游 #{uid} 不存在（可能已被删除）——'
            '请到「设置 → 上游」重新绑定，或改用未绑定上游的密钥')
    if not row['enabled']:
        raise UpstreamUnavailable(
            f'密钥绑定的上游「{row["name"]}」已停用 —— 启用它，或把密钥改绑到其它上游')
    return row


def _same_base(a: object, b: object) -> bool:
    """两个 Base URL 是否指同一套实例（去首尾空白与末尾斜杠后逐字比较）。"""
    return str(a or '').strip().rstrip('/') == str(b or '').strip().rstrip('/')


def forward_api_key(upstream: dict | None) -> str:
    """跟这套上游说话时该带的附加 api_key：自己填了用自己那份；留空且与默认
    上游同址的，沿用默认上游那份。

    为什么有后一条：账号页「添加分组」的默认形态就是**只填名称**——地址默认
    沿用默认上游的地址，而 api_key 留空（默认上游的钥匙明文不出接口，前端填
    不进去）。同址 = 同一套实例，钥匙本就是同一把：不沿用的话，绑定该分组的
    密钥调用、该分组的测活都会吃上游 401。

    地址不同的留空维持「不带附加鉴权头」：既支持不鉴权的自建实例，也不把
    默认钥匙发去别的地址。
    """
    key = str((upstream or {}).get('api_key') or '')
    if key or not upstream or upstream.get('is_default'):
        return key
    if _same_base(upstream.get('base_url'), default_upstream().get('base_url')):
        return str(default_upstream().get('api_key') or '')
    return ''


async def probe(upstream: dict) -> tuple[bool, str]:
    """探测某个上游是否可达（GET /v1/models，2xx 即活）。"""
    base = str(upstream.get('base_url') or '')
    if not base:
        return False, '上游地址为空'
    # 延迟导入：qoder2api 属于上游服务片，避免本模块在服务片未就绪时不可加载；
    # 导入失败与网络失败同样按「探测不可用」返回，不让异常冒到路由层变 500。
    try:
        from .services import qoder2api
        return await qoder2api.probe(base, api_key=forward_api_key(upstream))
    except Exception as exc:           # noqa: BLE001 —— 探测失败原因原样返回给界面
        return False, f'无法连接：{exc}'
