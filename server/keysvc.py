"""网关密钥的生成、校验与限额判定（网关与管理端共用）。"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time

from . import db
from .iputil import ip_matches

TOKEN_PREFIX = 'qk_'


class Rejection(str):
    """拒绝原因 + 该用哪个 HTTP 状态码回。

    为什么不是纯字符串：一批客户端会把 401/403 的真实报文整段丢掉，只显示
    本地化的「API 密钥无效」。于是「密钥限定了模型白名单、请求却带了别的
    模型」这种**配置问题**被显示成密钥坏了，用户就反复新建密钥。

    所以按「该怪谁」分流，而不是一律 403：

      · 请求与密钥不匹配（模型白名单 / 缺 model / IP 白名单 / max_ips）→
        ``400 invalid_request_error``：这是「这次请求的参数不对」，客户端会把
        原因原样显示出来，用户一眼看到该改什么。
      · 凭据本身不可用（已停用 / 已过期）→ ``403``：显示成「密钥无效」是贴切的。
      · 用量类（配额用尽）→ ``429``：避免被当成认证失败（OpenAI 同语义用
        429 ``insufficient_quota``）。

    继承 str 是为了兼容既有调用方——它们只做 `if reason:` 真假判断或把原因
    当文本用，`str` 子类在这两种用法下行为不变。
    """

    def __new__(cls, message: str, status: int = 403,
                err_type: str = 'permission_error', code: str = 'forbidden'):
        self = super().__new__(cls, message)
        self.status = status
        self.err_type = err_type
        self.code = code
        return self


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _norm_upstream_id(value: object) -> int | None:
    """归一化密钥绑定的上游 id：空 / 非法一律当「未绑定」（= 默认上游）。"""
    try:
        uid = int(value)
    except (TypeError, ValueError):
        return None
    return uid if uid > 0 else None


def _norm_cidrs(items: object) -> list[str]:
    """归一化 IP 白名单：逐项去空白、丢弃空项。

    写入前归一化意味着**校验与存储是同一份数据**（见 routers/keys.py 的
    `_check_ip_allowlist`：它拦的是非法 CIDR，这里保证存进去的就是通过
    校验的那个形态），读侧不必再做一遍同样的清洗。
    """
    out: list[str] = []
    for raw in items or []:
        s = str(raw).strip()
        if s and s not in out:
            out.append(s)
    return out


def _json_list(raw: object) -> list[str]:
    """把库里存的 JSON 数组文本解析成列表；**解析不了按「该列未设置」处理**。

    为什么必须容错：`ip_allowlist` / `models` 是 JSON 文本列，而读取它的
    `_parse` 被**列表接口**用到——只要**任意一把**密钥的这两列存了非法 JSON
    （历史版本写入过、手工改过库、写入被截断），整个 `GET /api/keys` 就抛
    JSONDecodeError → 500，界面上**一把密钥都看不到**。

    而这会伪装成「创建失败」：新建密钥的 POST 本身是成功的（数据已入库），
    紧接着前端刷新列表才炸——用户看到 500 以为没建成，再点一次就多一把
    重复的。所以这里宁可少显示一个字段，也绝不让一列坏数据拖垮整个列表。

    解析失败按空列表返回，而 `validate` 里是 `if allow and ...`——空列表
    意味着**跳过该项检查**（不限制 IP / 不限制模型）。坏数据只可能来自已经
    损坏的库，修好列表（能打开密钥页）才是当务之急；密钥本身仍有哈希校验、
    启停、有效期、配额与限流兜底，IP 与模型白名单是附加收紧项。
    """
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x) for x in parsed if x]


def _parse(row) -> dict:
    return {
        'id': row['id'],
        'name': row['name'],
        'prefix': row['prefix'],
        'enabled': bool(row['enabled']),
        'expires_at': row['expires_at'],
        'max_ips': row['max_ips'],
        'ip_allowlist': _json_list(row['ip_allowlist']),
        'models': _json_list(row['models']),
        # 绑定的上游分组（多上游隔离）。None = 默认上游 —— 默认上游不是数据
        # 库里的某一行，所以保持 None 而不是补 0。
        'upstream_id': _norm_upstream_id(row['upstream_id']),
        'quota': row['quota'],
        'used_tokens': row['used_tokens'],
        'created_at': row['created_at'],
        'last_used_at': row['last_used_at'],
    }


def list_keys() -> list[dict]:
    """全部密钥（新建设的排前面）。"""
    return [_parse(r) for r in
            db.query('SELECT * FROM api_keys ORDER BY id DESC')]


def create_key(
    name: str,
    expires_at: int | None = None,
    max_ips: int = 0,
    ip_allowlist: list[str] | None = None,
    models: list[str] | None = None,
    quota: int = 0,
    upstream_id: int | None = None,
    *,
    _conn: sqlite3.Connection | None = None,
) -> dict:
    """创建一个密钥。返回含**明文 token** 的字典（库里只存哈希）。

    `_conn`：传入一个**已开启事务**的连接时，本函数在它上面执行且**不自行
    提交**，由调用方负责 commit/rollback，用于「一批密钥整批原子」的场合。
    默认 None 时行为完全一致（自己提交）。做成参数而不是让调用方抄一份
    INSERT：SQL 抄第二遍就是第二份事实，改一处漏一处。
    """
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    sql = ('INSERT INTO api_keys(name, key_hash, prefix, enabled, expires_at, '
           'max_ips, ip_allowlist, models, quota, used_tokens, created_at, '
           'upstream_id) VALUES(?, ?, ?, 1, ?, ?, ?, ?, ?, 0, ?, ?)')
    args = (
        name,
        _hash(token),
        token[:12],
        expires_at,
        max_ips,
        json.dumps(_norm_cidrs(ip_allowlist)),
        json.dumps(models or []),
        quota,
        int(time.time()),
        _norm_upstream_id(upstream_id),
    )
    if _conn is not None:
        key_id = int(_conn.execute(sql, args).lastrowid or 0)
        row = _conn.execute('SELECT * FROM api_keys WHERE id = ?', (key_id,)).fetchone()
    else:
        key_id = db.execute(sql, args)
        row = db.query_one('SELECT * FROM api_keys WHERE id = ?', (key_id,))
    out = _parse(row)
    out['key'] = token  # 仅此一次返回明文
    return out


def update_key(key_id: int, patch: dict) -> dict | None:
    """PATCH 语义：只动 patch 里**真的提交了**的字段（`'x' in patch` 判定）。"""
    row = db.query_one('SELECT * FROM api_keys WHERE id = ?', (key_id,))
    if not row:
        return None
    fields: dict[str, object] = {}
    if 'name' in patch and patch['name']:
        fields['name'] = str(patch['name'])
    if 'enabled' in patch:
        fields['enabled'] = 1 if patch['enabled'] else 0
    if 'expires_at' in patch:
        fields['expires_at'] = patch['expires_at']
    if 'max_ips' in patch:
        fields['max_ips'] = int(patch['max_ips'] or 0)
    if 'ip_allowlist' in patch:
        # 与创建同一条归一化路径：校验与存储必须是同一份数据（见 _norm_cidrs）
        fields['ip_allowlist'] = json.dumps(_norm_cidrs(patch['ip_allowlist']))
    if 'models' in patch:
        fields['models'] = json.dumps(patch['models'] or [])
    if 'quota' in patch:
        fields['quota'] = int(patch['quota'] or 0)
    if 'upstream_id' in patch:
        # 显式传 null / 0 是**允许**的：管理员可以把密钥改回「默认上游」。
        # 所以这里不能用真值判断（否则改不回默认上游）。
        fields['upstream_id'] = _norm_upstream_id(patch['upstream_id'])
    if fields:
        assignments = ', '.join(f'{k} = ?' for k in fields)
        db.execute(f'UPDATE api_keys SET {assignments} WHERE id = ?', (*fields.values(), key_id))
    return _parse(db.query_one('SELECT * FROM api_keys WHERE id = ?', (key_id,)))


def delete_key(key_id: int) -> bool:
    if not db.query_one('SELECT id FROM api_keys WHERE id = ?', (key_id,)):
        return False
    db.execute('DELETE FROM api_keys WHERE id = ?', (key_id,))
    db.execute('DELETE FROM api_key_ips WHERE key_id = ?', (key_id,))
    return True


def reset_usage(key_id: int) -> bool:
    """把已用 Token 归零。返回是否真的命中了密钥。

    返回布尔值是为了让路由能对「不存在的 id」报 404 —— 静默成功会让前端
    提示「已重置」，而实际什么都没发生。
    """
    if not db.query_one('SELECT id FROM api_keys WHERE id = ?', (key_id,)):
        return False
    db.execute('UPDATE api_keys SET used_tokens = 0 WHERE id = ?', (key_id,))
    return True


def resolve(token: str) -> dict | None:
    """按前缀定位后比对哈希，避免全表扫描。"""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    rows = db.query('SELECT * FROM api_keys WHERE prefix = ?', (token[:12],))
    digest = _hash(token)
    for row in rows:
        if secrets.compare_digest(row['key_hash'], digest):
            return _parse(row)
    return None


def model_allowed(key: dict, model: object) -> bool:
    """该密钥的**模型白名单**是否放行这个模型。

    **调用侧与 `/v1/models` 裁剪共用这一份判据**：两处各写一遍迟早漂移，
    而漂移的表现正是「列表里给出的模型，调用时却被白名单拒掉」（客户端把
    它当模型选择器，于是「能选、一选就失败」）。

    白名单为空 = 不限制，一律放行。
    """
    allow = key.get('models') or []
    if not allow:
        return True
    if not isinstance(model, str) or not model.strip():
        return False
    return model.strip() in {str(x).strip() for x in allow}


def unknown_whitelist_entries(models: list[str], known: set[str] | None,
                              aliases: list[str] | set[str] = ()) -> list[str]:
    """白名单里**匹配不到任何已知模型**的条目（多半是拼错了）。

    白名单是自由文本框，填错了不会报错，只会在下游表现为「模型列表是空的」。
    空列表本身看不出原因，所以在**编辑处**当场点出来比事后裁剪更根本。

    `known` 为 None 表示**清单拿不到**——此时不判（宁可这次不提示）：
    「上游暂时读不到」被显示成「你这个名字写错了」是假警报，比不提示更糟。
    别名要算：它就是给下游用的合法名字（鉴权判请求名，映射在其后）。
    """
    if not models:
        return []
    if known is None:
        return []
    alias_set = {str(x).strip() for x in aliases}
    known_set = {str(x).strip() for x in known}
    out: list[str] = []
    for raw in models:
        entry = str(raw).strip()
        if not entry or entry in alias_set or entry in known_set:
            continue
        out.append(entry)
    return out


def validate(key: dict, ip: str, model: str | None,
             *, is_model_list: bool = False,
             mapped_model: str | None = None) -> Rejection | None:
    """返回 None 表示放行，否则返回拒绝原因（`Rejection`，自带状态码）。

    is_model_list：请求是 `/v1/models`（模型发现，不带 model）。模型白名单
    在这种请求上不拦——它没有模型可言，拦了会让限定白名单的密钥连「我
    有哪些模型」都问不到；真正的隔离由调用时的模型名把关。

    mapped_model：`model` 经「模型映射」后的名字。目前保留参数以对齐网关
    调用形态（映射先于鉴权），判定只关心请求名。
    """
    if not key['enabled']:
        return Rejection('密钥已停用', 403, 'permission_error', 'key_disabled')
    if key['expires_at'] and key['expires_at'] < time.time():
        return Rejection('密钥已过期', 403, 'permission_error', 'key_expired')
    if key['quota'] and key['used_tokens'] >= key['quota']:
        # 配额用尽与认证无关，用 429 才对（403 会被客户端读成「密钥无效」：
        # 用户于是去查密钥、而不是去调额度，方向就被带偏了）。
        return Rejection('密钥配额已用尽', 429, 'insufficient_quota', 'quota_exhausted')

    allow = key['ip_allowlist']
    if allow and not any(ip_matches(ip, c) for c in allow):
        # 观感上像认证失败，但它不是「密钥错」而是「来源不对」，客户端原样
        # 显示才能让用户知道要加白名单，故用 400 让报文得以透出。
        return Rejection(f'来源 IP {ip} 不在密钥白名单内', 400,
                         'invalid_request_error', 'ip_not_allowed')

    if key['max_ips']:
        known = db.query('SELECT ip FROM api_key_ips WHERE key_id = ?', (key['id'],))
        ips = {r['ip'] for r in known}
        if ip not in ips and len(ips) >= key['max_ips']:
            return Rejection(
                f'密钥已绑定 {len(ips)} 个 IP，超出上限 {key["max_ips"]}', 400,
                'invalid_request_error', 'too_many_ips')

    # 模型白名单：**不能因为 model 缺失就跳过检查**。若缺失就整段跳过，限定
    # 单模型的密钥可用「不带 model」的请求走上游默认模型——白名单形同虚设。
    # 请求侧已在网关把缺失/非法的 model 拦成 400；这里再兜一层，任何非字符
    # 串或空值一律拒绝。
    if key['models'] and not is_model_list:
        if not isinstance(model, str) or not model.strip():
            return Rejection('请求未指定 model，而该密钥启用了模型白名单', 400,
                             'invalid_request_error', 'model_not_allowed')
        # 判据见 `model_allowed`：与 `/v1/models` 的裁剪共用同一份。
        if not model_allowed(key, model):
            return Rejection(f'模型 {model} 不在密钥白名单内', 400,
                             'invalid_request_error', 'model_not_allowed')
    return None


def touch(key: dict, ip: str, tokens: int = 0) -> None:
    """记账：来源 IP、最近使用与已用 token。"""
    db.execute(
        'INSERT OR IGNORE INTO api_key_ips(key_id, ip, first_seen) VALUES(?, ?, ?)',
        (key['id'], ip, int(time.time())),
    )
    db.execute('UPDATE api_keys SET last_used_at = ? WHERE id = ?', (int(time.time()), key['id']))
    if tokens:
        db.execute('UPDATE api_keys SET used_tokens = used_tokens + ? WHERE id = ?', (tokens, key['id']))
