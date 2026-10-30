"""对外反代网关：密钥鉴权 → IP 管控 → 模型映射 → PAT 池转发 → 计量落库。"""
from __future__ import annotations

import json
import logging
import threading
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .. import config, db, iputil, keysvc

logger = logging.getLogger('qcode.gateway')

router = APIRouter(tags=['gateway'])

# 契约常量：限流与请求体上限由 config 统一供给（环境变量 QC_GATEWAY_*）。
RATE_MAX_PER_MIN = config.GATEWAY_RATE_PER_MIN
MAX_BODY_MB = config.GATEWAY_MAX_BODY_MB

SERVICE_NAME = 'QCode Manager'


def _oai_error(message: str, status: int = 400, err_type: str = 'invalid_request_error',
               code: str | None = None, hint: str | None = None,
               retry_after: int | None = None) -> JSONResponse:
    """OpenAI 形状的错误体。

    `hint` 是与 message **并列**的网关视角补充说明（面向客户端工具链的英文
    措辞）。为空时**不写这个字段**：避免客户端拿到一个空值还要自己判断。
    """
    err: dict = {'message': message, 'type': err_type, 'code': code}
    if hint:
        err['gateway_hint'] = hint
    return JSONResponse({'error': err}, status_code=status,
                        headers=_retry_after_header(retry_after))


def _retry_after_header(seconds: int | None) -> dict[str, str] | None:
    if not seconds or seconds <= 0:
        return None
    return {'Retry-After': str(int(seconds))}


def _error_hint(data: object) -> str | None:
    """从上游错误体里取 `error.gateway_hint`（取不到返回 None）。

    协议翻译层（Anthropic / Responses）此前只取 `message` 重建错误体，这个
    字段会被丢掉——同一份上游错误，走 `/v1/chat/completions` 的客户端能看
    到建议、走 `/v1/messages` 的看不到。真实原因与可执行建议都要能到达客户
    端，故抽成共用实现，三处口径一致。
    """
    if not isinstance(data, dict):
        return None
    err = data.get('error')
    if not isinstance(err, dict):
        return None
    hint = err.get('gateway_hint')
    if isinstance(hint, str) and hint.strip():
        return hint.strip()[:300]
    return None


def _bearer(request: Request) -> str:
    auth = request.headers.get('authorization', '')
    if auth.lower().startswith('bearer '):
        return auth[7:].strip()
    return request.headers.get('x-api-key', '').strip()


# ── 模型映射（设置页「模型映射」）──────────────────────────
# 语义：精确别名 → 兜底（键 `*`）→ 原样返回。映射只影响发往上游的名字，
# 白名单判的仍是客户端发来的请求名（映射发生在鉴权之后）。
FALLBACK_KEY = '*'


def _load_map(mapping: object) -> dict[str, str]:
    """把数据库里的原始值归一成 `{别名: 目标}`。

    坏数据（存成非对象）按「没有映射」处理，不能让整个网关 5xx：映射是
    可选的便利功能，坏数据只该影响它自己那一项。
    """
    if not isinstance(mapping, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in mapping.items():
        name = str(key).strip()
        target = str(value).strip() if value is not None else ''
        if name and target:
            out[name] = target
    return out


def _map_model(model: str | None) -> str:
    """「模型映射」查表：别名 → 真名；没命中落到兜底模型；两者都没有原样返回。"""
    table = _load_map(db.get_setting('model_map', {}) or {})
    if not model:
        return model or ''
    exact = table.get(model)
    if exact:
        return exact
    return table.get(FALLBACK_KEY) or model


def _alias_names() -> list[str]:
    """真实别名（不含兜底 `*`）：`*` 不是模型名。"""
    return [name for name in _load_map(db.get_setting('model_map', {}) or {})
            if name != FALLBACK_KEY]


# ── max_tokens 上限钳制 ────────────────────────────────────
#
# 越界的输出上限会被部分客户端「按剩余额度做减法」算出来（约 99 亿），上游
# 按整数读它、拒绝时又常包成 5xx——客户端据此以为「服务暂时不可用」而重试，
# 但参数越界重试多少次都一样失败。阈值取 1e6：正常模型的最大输出上限远低于
# 它，不会误伤，又能挡住这类减法式产物。
MAX_TOKENS_CEILING = 1_000_000

# 一并钳制的字段：三种协议各自的输出上限字段名（OpenAI 新旧名 + Responses）。
_MAX_TOKENS_FIELDS = ('max_tokens', 'max_completion_tokens', 'max_output_tokens')


def clamp_max_tokens(body: dict) -> None:
    """原地钳制请求体里的输出上限字段（见 _MAX_TOKENS_FIELDS）。

    为什么是**原地改**而不是返回新 dict：调用方拿到 body 后直接转发，原地把
    越界值压回上限，无需改动其余逻辑。非数字一律**原样保留**（不猜测、不置
    默认）：把它交回上游去判，网关不替上游做协议决定。
    """
    if not isinstance(body, dict):
        return
    for field in _MAX_TOKENS_FIELDS:
        value = body.get(field)
        # bool 是 int 的子类，必须排除；None 交给上游按「未提供」处理
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value <= MAX_TOKENS_CEILING:
            continue
        body[field] = MAX_TOKENS_CEILING


def _payload_too_large() -> JSONResponse:
    mb = MAX_BODY_MB
    return _oai_error(
        f'请求体超过 {mb} MB 上限：请压缩内容（精简上下文或附件），'
        f'或提高环境变量 QC_GATEWAY_MAX_BODY_MB（改完要重启面板）',
        413, 'invalid_request_error', 'payload_too_large',
    )


async def _read_json_body(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """读取并解析网关请求体，带大小与格式校验。

    大小校验必须落在**实际读取**上，不能只看 Content-Length：分块传输
    （chunked）时该头缺失，伪造该头也可以偏小，只看头会让超大请求被整段
    读进内存（少量并发即可耗尽内存）。
    """
    limit = MAX_BODY_MB * 1024 * 1024
    try:
        declared = int(request.headers.get('content-length') or 0)
    except ValueError:
        declared = 0
    if declared > limit:
        return None, _payload_too_large()

    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                return None, _payload_too_large()
            chunks.append(chunk)
    except Exception:  # noqa: BLE001
        return None, _oai_error('读取请求体失败', 400)

    try:
        body = json.loads(b''.join(chunks))
    except Exception:  # noqa: BLE001
        return None, _oai_error('请求体不是合法 JSON', 400)
    if not isinstance(body, dict):
        return None, _oai_error('请求体必须是 JSON 对象', 400)
    return body, None


# ── 简单的每密钥速率限制（滑动窗口）─────────────────────
# 目的：单个密钥被打爆时保护上游 PAT 池，避免拖垮其他调用方。
# 计数放在进程内存，单实例足够；多实例部署时可换成 Redis。
_rate: dict[int, list[float]] = {}
RATE_WINDOW = 60


def _rate_limited(key: dict) -> bool:
    """滑动窗口内**放行**了多少次。

    **只有放行的请求才占用窗口额度**。此前不分放行与拒绝一律 `append()`，
    被拒的请求同样计入，于是与客户端重试构成正反馈：客户端以高于阈值的
    速率持续重试时，窗口计数永远降不回阈值以下，表现为「凑满一窗之后持续
    全拒、永不恢复」——限流器本该限速，却变成了熔断。超出的请求被拒且不占
    额度，因此把速率降到阈值以下能立刻恢复。
    """
    kid = int(key['id'])
    if RATE_MAX_PER_MIN <= 0:
        return False
    now = time.time()
    hits = [t for t in _rate.get(kid, []) if now - t < RATE_WINDOW]
    limited = len(hits) >= RATE_MAX_PER_MIN
    if not limited:
        hits.append(now)
    # 拒绝的分支也要回写：窗口内已过期的记录要顺手清掉，否则长期被限的密钥
    # 会一直拖着一串陈旧时间戳，内存白占。
    _rate[kid] = hits
    if len(_rate) > 2000:
        for k in [k for k, v in _rate.items() if not v or now - v[-1] > RATE_WINDOW]:
            _rate.pop(k, None)
    return limited


# ── 未鉴权入口的入站日志采样 ─────────────────────────────
# 「没有密钥」「密钥不认识」这两条路径**在校验密钥之前**就写库，任何匿名者
# 都能触发。攻击者只要写个循环用随机 token 打过来，就能以每请求一行的速度
# 把 `ip_access_logs` 的滚动窗口顶掉——真正该看的扫描记录反而被挤了出去。
# 做法：按 (来源 IP, 原因) 做窗口内计数，前 N 次照常逐条记录，超出的折叠，
# 并在本窗口第一次折叠时写一条 `key_auth_flood` 汇总行。
_AUTH_FAIL_REASONS = frozenset({'missing_key', 'invalid_key'})
_AUTH_LOG_WINDOW = 60
_AUTH_LOG_PER_WINDOW = 5
_AUTH_LOG_MAX_ENTRIES = 8192
_auth_log_state: dict[tuple[str, str], list] = {}
_auth_log_lock = threading.Lock()


def _auth_log_verdict(ip: str, reason: str) -> str:
    """返回 'log'（逐条记录）/ 'flood'（写一条汇总行）/ 'drop'（折叠掉）。"""
    now = time.time()
    key = (ip, reason)
    with _auth_log_lock:
        state = _auth_log_state.get(key)
        if not state or now - state[0] >= _AUTH_LOG_WINDOW:
            state = [now, 0, False]
            _auth_log_state[key] = state
        state[1] += 1
        # 硬上限：大量不同来源 IP 各打一次时，按计数的过期清理永远轮不到，
        # 宁可少采样一小段，也不让外部输入把进程内存撑爆。
        if len(_auth_log_state) > _AUTH_LOG_MAX_ENTRIES:
            _auth_log_state.clear()
            state = [now, 1, False]
            _auth_log_state[key] = state
        if state[1] <= _AUTH_LOG_PER_WINDOW:
            return 'log'
        if not state[2]:
            state[2] = True
            return 'flood'
        return 'drop'


def _log_ip(ip: str, path: str, blocked: bool, ua: str | None,
            reason: str | None = None) -> None:
    """记录一次入站访问。**默认只记拦截**，放行不记。

    reason 是**被拦的原因**（稳定短码）：只有一个 blocked 布尔时，界面上
    显示「已拦截」却看不出是哪一关拦的：没带密钥 / 密钥不认识 / IP 规则
    拦的，三者的处置方式完全不同。

    放行不记（默认）：这张表的用途是安全审计——回答「谁在扫我、谁被挡了」。
    记全量会把信号淹没，而表有滚动上限，被正常流量占满后真出事时拦截记录
    可能已被挤掉。放行的明细在「请求日志」页有完整记录，不重复记不丢信息。
    例外：QC_AUDIT_ALL_ACCESS=1 时恢复全量记录。
    """
    if not blocked and not config.AUDIT_ALL_ACCESS:
        return
    if reason and reason in _AUTH_FAIL_REASONS:
        verdict = _auth_log_verdict(ip, reason)
        if verdict == 'drop':
            return
        if verdict == 'flood':
            reason = 'key_auth_flood'
    db.add_ip_access_log(ip, path, blocked, ua, reason)


def client_channel(ua: str | None) -> str:
    """客户端渠道归类：只用于日志可观测性，**不改变转发行为**。

    为什么要它：出问题时最想知道「是哪类客户端在打」。UA 是唯一无需客户端
    配合就能拿到的来源维度；归类宁可粗、不可猜错：认不出就记 other。
    """
    text = (ua or '').strip().lower()
    if not text:
        return 'unknown'
    if 'codex' in text:
        return 'codex'
    if 'claude' in text:
        return 'claude-code'
    if 'qoder' in text:
        return 'qoder'
    if 'vscode' in text or 'visual studio code' in text or 'cursor' in text:
        return 'vscode'
    if 'jetbrains' in text or 'intellij' in text:
        return 'jetbrains'
    if 'mozilla' in text or 'chrome/' in text or 'safari/' in text or 'edg/' in text:
        return 'web'
    if any(k in text for k in ('python', 'httpx', 'requests/', 'curl/', 'node-fetch',
                               'axios', 'okhttp', 'go-http-client')):
        return 'api'
    return 'other'


async def _record(key: dict | None, ip: str, model: str, mapped_model: str,
                  status: int, *, usage: dict | None = None, latency_ms: int = 0,
                  first_token_ms: int | None = None, stream: bool = False,
                  error: str | None = None, account: tuple[int, str] | None = None,
                  effort: str | None = None, ua: str | None = None) -> None:
    """记录调用日志与用量（request_logs + usage 累计 + 密钥/账号记账）。

    `usage` 直接把上游那份 usage 传进来，token 数从里面取。`account` 是本次
    实际使用的 PAT `(账号 id, label)`：请求成败要回写账号状态（上游 401/403
    多半是 PAT 失效），token 也要累计到账号维度供池调度展示。

    注意：日志/统计属于旁路，任何异常都不能影响用户请求本身（曾因统计函数
    缺失导致流式响应在收尾阶段中断），因此这里整体兜底。
    """
    pt = ct = 0
    if isinstance(usage, dict):
        pt = int(usage.get('prompt_tokens') or 0)
        ct = int(usage.get('completion_tokens') or 0)

    try:
        # **所有外部来源的文本都要清洗 + 截断**（`db._clean`）：model 来自请求
        # 体、error 含上游响应原文、ua 来自请求头，带换行就能在日志页伪造出
        # 额外行；长度无上限的 model 更能单次请求放大数 MB。清洗只影响入库
        # 文本，不影响转发给上游的内容（body 早已发走）。
        model_clean = db._clean(model, 128)
        mapped_clean = db._clean(mapped_model, 128)
        db.add_request_log(
            ts=int(time.time()),
            key_id=key['id'] if key else None,
            ip=db._clean(ip, 64),
            model=model_clean,
            mapped_model=mapped_clean,
            status=status,
            prompt_tokens=pt,
            completion_tokens=ct,
            latency_ms=latency_ms,
            first_token_ms=first_token_ms,
            ua=db._clean(ua, 512),
            error=db._clean(error, 500),
            stream=1 if stream else 0,
            channel=client_channel(ua),
            account=account[1] if account else None,
            # 思考强度只作展示：客户端传什么记什么，不做推断（NULL=没传）。
            effort=db._clean(effort, 16) if effort else None,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning('写入请求日志失败（不影响请求）: %s', exc)
        return

    if key:
        try:
            total = pt + ct
            keysvc.touch(key, ip, total)
            if total:
                db.bump_usage(key['id'], mapped_clean or model_clean, pt, ct)
        except Exception as exc:  # noqa: BLE001
            logger.warning('累计用量失败（不影响请求）: %s', exc)

    if account:
        try:
            from .. import accountsvc
            if status < 400:
                accountsvc.mark_result(account[0], True)
            elif status in (401, 403):
                # 上游拒绝鉴权：PAT 大概率失效，回写状态让池调度避开它。
                accountsvc.mark_result(account[0], False, db._clean(error, 200))
            if pt + ct:
                accountsvc.bump_usage(account[0], pt + ct)
        except Exception as exc:  # noqa: BLE001
            logger.warning('账号记账失败（不影响请求）: %s', exc)


def _global_ip_blocked(ip: str) -> bool:
    """全局入站 IP 规则是否拦截该来源（未启用安全策略时恒为放行）。

    从 `_authorize` 抽出：`/v1/usage` 与调用路径必须同口径——它虽是只读端
    点，但同样暴露密钥元数据与余量，被全局黑名单拦住的来源不该换条路就能查。
    """
    sec = db.get_setting('security', {}) or {}
    if not isinstance(sec, dict) or not sec.get('enabled'):
        return False
    rules = [
        {'kind': r['kind'], 'cidr': r['cidr']}
        for r in db.query('SELECT kind, cidr FROM ip_rules')
    ]
    return not iputil.evaluate(ip, rules, sec.get('mode', 'blacklist'))


def _key_reject_code(reason: object) -> str:
    """把密钥层的拒绝原因归成一个稳定的短码，供入站日志显示。

    为什么不用 keysvc 的原话：那是给调用方客户端看的完整句子（含具体数
    字），直接塞进日志列表会撑爆列宽。这里只归类别，展示侧按语言翻译。
    取不到 code 时回落到 'key_rejected'（宁可笼统，也不要漏记成「已放行」）。
    """
    code = getattr(reason, 'code', '') or ''
    return code if isinstance(code, str) and code else 'key_rejected'


async def _authorize(request: Request, model: str | None,
                     *, is_model_list: bool = False,
                     mapped: str | None = None) -> tuple[dict | None, JSONResponse | None]:
    """返回 (key, error_response)；error 非 None 时 key 为 None。

    mapped：`model` 经「模型映射」后的名字，调用方算好了可以传进来，漏传
    时这里自己算一次（记录日志要用它）。
    """
    ip = iputil.client_ip(request)
    ua = request.headers.get('user-agent')
    path = request.url.path

    token = _bearer(request)
    if not token:
        _log_ip(ip, path, True, ua, 'missing_key')
        return None, _oai_error('缺少 API Key，请在 Authorization 头中提供 Bearer 令牌',
                                401, 'authentication_error', 'missing_api_key')

    key = keysvc.resolve(token)
    if not key:
        _log_ip(ip, path, True, ua, 'invalid_key')
        return None, _oai_error('API Key 无效', 401, 'authentication_error', 'invalid_api_key')

    if mapped is None:
        mapped = _map_model(model)

    # 全局入站 IP 规则
    if _global_ip_blocked(ip):
        _log_ip(ip, path, True, ua, 'ip_blocked')
        await _record(key, ip, model or '', mapped or '', 403, ua=ua, error='IP 被拦截')
        return None, _oai_error(f'来源 IP {ip} 被安全策略拦截', 403,
                                'permission_error', 'ip_blocked')

    reason = keysvc.validate(key, ip, model, is_model_list=is_model_list,
                             mapped_model=mapped)
    if reason:
        # 状态码由 keysvc 决定，不再一律 403：一批客户端把 401/403 统一显示
        # 成「API 密钥无效」，一律 403 会把「白名单不匹配」这种配置问题说成
        # 密钥坏了，用户便反复重建密钥。
        reject_status = getattr(reason, 'status', 403)
        await _record(key, ip, model or '', mapped or '', reject_status,
                      ua=ua, error=str(reason))
        _log_ip(ip, path, True, ua, _key_reject_code(reason))
        return None, _oai_error(
            str(reason), reject_status,
            getattr(reason, 'err_type', 'permission_error'),
            getattr(reason, 'code', 'forbidden'),
            retry_after=60 if reject_status == 429 else None,
        )

    if _rate_limited(key):
        msg = f'请求过于频繁（{RATE_WINDOW}s 内超过 {RATE_MAX_PER_MIN} 次）'
        await _record(key, ip, model or '', mapped or '', 429, ua=ua, error=msg)
        _log_ip(ip, path, True, ua, 'rate_limited')
        return None, _oai_error(msg, 429, 'rate_limit_error', 'rate_limit_exceeded',
                                retry_after=RATE_WINDOW)

    _log_ip(ip, path, False, ua)
    return key, None


def _resolve_upstream(key: dict | None) -> dict:
    """按密钥解析本次请求该走哪个上游；不可用时抛 UpstreamUnavailable。

    不静默回落默认上游：那把密钥本应只碰某个分组的账号，回落到默认上游会让
    它打到全池——隔离失效而且无人察觉。
    """
    from .. import upstreamsvc
    return upstreamsvc.resolve_for_key(key)


def _upstream_hint(upstream: dict | None) -> str:
    """502/503 时补一句「本次打的是哪个上游、这一跳会不会走出口代理」。

    最常见的两种成因恰恰要靠这两条信息才能自查：上游地址/端口填错；上游
    地址写成公网域名、同时又配了出口代理（代理绕不回本机就全 502，而内网
    与本机地址是自动直连的）。
    """
    from .. import upstreamsvc
    up = upstream or upstreamsvc.default_upstream()
    base = str(up.get('base_url') or '').strip()
    if not base:
        return ''
    try:
        host = urlsplit(base).hostname
    except ValueError:
        host = None
    proxy = config.effective_http_proxy()
    if proxy and host:
        if config._is_internal_host(host):  # noqa: SLF001 —— 与转发判断同一份实现
            return f'（本次请求的上游：{base}；该地址属内网/本机，这一跳直连）'
        masked = proxy.split('@')[-1]
        return (f'（本次请求的上游：{base}；这一跳会经出口代理 {masked} —— '
                f'代理到不了这个地址就会 502）')
    return f'（本次请求的上游：{base}）'


def _pick_account(upstream: dict) -> tuple[int, str] | None:
    """从该上游分组的 PAT 池轮询选一个账号；池空返回 None。"""
    from .. import accountsvc
    picked = accountsvc.pick_pat(upstream.get('id'))
    return (picked[0], picked[1]) if picked else None


async def _open_chat(upstream: dict, pat: str, payload: dict):
    """对上游发起 chat 请求，返回 `(client, resp)`。

    resp 是 `stream=True` 的 httpx 响应：流式路径用 `aiter_bytes()` 逐块读；
    非流式路径 `await resp.aread()`。**用完必须** `await resp.aclose()` 并
    `await client.aclose()`（后者只是归还共享连接池，不真关）。连接绑在池中
    的 client 上，close 之前 resp 一直可用。
    """
    from ..services import qoder2api
    client = config.http_client(config.UPSTREAM_TIMEOUT, connect=5)
    url = await qoder2api.chat_url(upstream['base_url'])
    headers = qoder2api.forward_headers(
        pat, api_key=upstream.get('api_key') or config.QC2API_KEY)
    req = client.build_request('POST', url, json=payload, headers=headers)
    resp = await client.send(req, stream=True)
    return client, resp


async def _forward_chat(upstream: dict, pat: str, payload: dict, *,
                        started: float | None = None,
                        on_finish=None):
    """实际转发 POST {base}/v1/chat/completions，SSE / JSON 双模式。

    返回 fastapi Response：上游 2xx 流式 → StreamingResponse（边转发边扫
    usage / 首字延迟）；其余（含错误）→ JSONResponse 原样透传。`on_finish`
    是可选的异步回调 `on_finish(status, usage, first_token_ms, latency_ms,
    error)`，在响应体收尾后调用——`_chat` 用它落日志，playground 等复用方
    可以不传。
    """
    if started is None:
        started = time.time()
    try:
        client, resp = await _open_chat(upstream, pat, payload)
    except Exception as exc:  # noqa: BLE001
        if on_finish:
            await on_finish(502, {}, None, int((time.time() - started) * 1000), str(exc))
        return _oai_error(f'上游不可用: {exc}' + _upstream_hint(upstream), 502,
                          'api_error', 'upstream_unavailable')

    status_code = resp.status_code
    if status_code >= 400:
        # 上游的错误不是 SSE（正文是一小段 JSON），读完整段后按 JSON 返回，
        # 状态码与文案保持上游原样，客户端能拿到结构化错误。
        raw = await resp.aread()
        await resp.aclose()
        await client.aclose()
        text = raw.decode('utf-8', errors='replace')
        try:
            payload_out: object = json.loads(text)
        except Exception:  # noqa: BLE001
            payload_out = None
        if on_finish:
            await on_finish(status_code, {}, None,
                            int((time.time() - started) * 1000), text[:500])
        if payload_out is not None:
            return JSONResponse(payload_out, status_code=status_code)
        return JSONResponse({'error': {'message': text[:1000], 'type': 'api_error'}},
                            status_code=status_code)

    # 非流式：上游回的是普通 JSON completion，SSE 扫描器只认 `data:` 帧、
    # 从里面永远提不出 usage —— 必须单独解析，否则 request_logs 的 token 数
    # 恒为 0、usage_daily 不累计、按 token 的配额也不扣减。
    content_type = resp.headers.get('content-type', '')
    if not payload.get('stream') and 'text/event-stream' not in content_type:
        raw = await resp.aread()
        await resp.aclose()
        await client.aclose()
        usage_ns: dict = {}
        data_ns = None
        try:
            data_ns = json.loads(raw.decode('utf-8', errors='replace'))
        except Exception:  # noqa: BLE001
            data_ns = None
        if isinstance(data_ns, dict) and isinstance(data_ns.get('usage'), dict):
            usage_ns = data_ns['usage']
        if on_finish:
            await on_finish(status_code, usage_ns, None,
                            int((time.time() - started) * 1000), None)
        if data_ns is not None:
            return JSONResponse(data_ns, status_code=status_code)
        return Response(content=raw, status_code=status_code,
                        media_type=content_type or 'application/json')

    async def generator():
        usage: dict = {}
        pending = ''
        error_text: str | None = None
        # 首字延迟：只记一次，取「首个含正文的 delta」到达时刻——OpenAI 流的
        # 第一块通常只有 role，用「收到首块」当首字会把建连时间也算进去。
        first_token_ms: int | None = None
        try:
            async for chunk in resp.aiter_bytes():
                pending += chunk.decode('utf-8', errors='ignore')
                pending, saw_content = _scan_sse(pending, usage)
                if saw_content and first_token_ms is None:
                    first_token_ms = int((time.time() - started) * 1000)
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()
            if on_finish:
                await on_finish(status_code, usage, first_token_ms,
                                int((time.time() - started) * 1000), error_text)

    return StreamingResponse(generator(), status_code=status_code,
                             media_type=resp.headers.get('content-type', 'text/event-stream'))


def _scan_sse(pending: str, usage: dict) -> tuple[str, bool]:
    """扫描 SSE 文本片段：提取 usage，并判断是否已出现首个正文。

    返回 (未处理完的残留缓冲, 本次是否见到正文 delta)。
    """
    saw_content = False
    while '\n' in pending:
        line, pending = pending.split('\n', 1)
        line = line.strip()
        if not line.startswith('data:'):
            continue
        payload = line[5:].strip()
        if not payload or payload == '[DONE]':
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get('usage'), dict):
            usage.update(obj['usage'])
        choices = obj.get('choices')
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                delta = choice.get('delta') or choice.get('message') or {}
                if isinstance(delta, dict) and (
                    delta.get('content') or delta.get('reasoning_content')
                ):
                    saw_content = True
    return pending, saw_content


# ── 密钥自查用量 ─────────────────────────────────────────
# 拿密钥自身鉴权、只回这把密钥自己的数字，**只读面板已记录的值、不触发任何
# 上游请求**（否则「查一下还剩多少」会变成一次真实调用，反过来加剧额度消耗
# 与限流）。有意**不套用**调用时的那套校验（额度用尽 / 已过期 / 被停用）：
# 恰恰是在调不动的时候，用户最需要看到「还剩多少、为什么调不动」。
@router.get('/v1/usage')
async def key_usage(request: Request):
    ip = iputil.client_ip(request)
    ua = request.headers.get('user-agent')
    path = request.url.path
    token = _bearer(request)
    if not token:
        _log_ip(ip, path, True, ua, 'missing_key')
        return _oai_error('缺少 API Key，请在 Authorization 头中提供 Bearer 令牌',
                          401, 'authentication_error', 'missing_api_key')
    key = keysvc.resolve(token)
    if not key:
        _log_ip(ip, path, True, ua, 'invalid_key')
        return _oai_error('API Key 无效', 401, 'authentication_error', 'invalid_api_key')

    # 全局入站 IP 规则：与其他 /v1 入口同口径（见 _global_ip_blocked 的说明）。
    if _global_ip_blocked(ip):
        _log_ip(ip, path, True, ua, 'ip_blocked')
        await _record(key, ip, '', '', 403, ua=ua, error='IP 被拦截')
        return _oai_error(f'来源 IP {ip} 被安全策略拦截', 403,
                          'permission_error', 'ip_blocked')

    now = int(time.time())
    expires_at = int(key.get('expires_at') or 0)
    # 记一行**请求日志**（与 /v1/models 同口径，0 token）：否则一把密钥被监
    # 控脚本每分钟轮询查用量时，运维在「请求日志」里完全看不出流量从哪来。
    # token 消耗一分不记（记账口径只认真实调用）。
    await _record(key, ip, '', '', 200, ua=ua)
    limit = int(key.get('quota') or 0)
    used = int(key.get('used_tokens') or 0)
    return {
        'object': 'key.usage',
        'key': {
            'name': key.get('name') or '',
            'prefix': key.get('prefix') or '',
            'enabled': bool(key.get('enabled')),
            'expires_at': expires_at,
            'expired': bool(expires_at and now >= expires_at),
            'models': list(key.get('models') or []),
        },
        'tokens': {
            'used': used,
            'limit': limit or None,          # None = 不限制
            'remaining': max(0, limit - used) if limit else None,
        },
        'last_used_at': int(key.get('last_used_at') or 0),
        'snapshot_at': now,
        'data_source': 'panel-recorded',
    }


# ── 模型列表 ─────────────────────────────────────────────
@router.get('/v1/models')
async def list_models(request: Request):
    key, err = await _authorize(request, None, is_model_list=True)
    if err:
        return err
    ip = iputil.client_ip(request)
    ua = request.headers.get('user-agent')
    started = time.time()
    try:
        upstream = _resolve_upstream(key)
    except Exception as exc:  # noqa: BLE001
        await _record(key, ip, '', '', 503, ua=ua, error=str(exc))
        return _oai_error(str(exc), 503, 'api_error', 'upstream_unavailable')

    from ..services import qoder2api
    picked = _pick_account(upstream)
    pat = picked[1] if picked else ''
    try:
        async with config.http_client(30, connect=3) as client:
            resp = await client.get(
                f'{upstream["base_url"]}/v1/models',
                headers=qoder2api.forward_headers(
                    pat, api_key=upstream.get('api_key') or config.QC2API_KEY))
        latency = int((time.time() - started) * 1000)
        await _record(key, ip, '', '', resp.status_code,
                      latency_ms=latency, ua=ua, account=picked)
        payload = _scope_models(resp.json(), key)
        # Anthropic 客户端（Claude Code 等）也会调这个路径，但期望的结构不同：
        # 只能在同一个路径上按请求头分流（各注册一个路由的话先注册的永远赢）。
        if request.headers.get('anthropic-version'):
            payload = _as_anthropic_models(payload)
        return JSONResponse(payload, status_code=resp.status_code)
    except Exception as exc:  # noqa: BLE001
        latency = int((time.time() - started) * 1000)
        await _record(key, ip, '', '', 502, latency_ms=latency, ua=ua, error=str(exc),
                      account=picked)
        return _oai_error(f'上游不可用: {exc}' + _upstream_hint(upstream), 502,
                          'api_error', 'upstream_unavailable')


def _as_anthropic_models(payload: object) -> object:
    """把 OpenAI 形状的模型列表翻成 Anthropic 的形状；认不出的结构原样返回。"""
    items = payload.get('data') if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return payload
    data = [
        {
            'type': 'model',
            'id': str(m.get('id') or ''),
            'display_name': str(m.get('name') or m.get('id') or ''),
            # 协议要求 ISO8601；上游给的是 created(epoch)，缺省时用纪元起点占位
            'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ',
                                        time.gmtime(m.get('created') or 0)),
        }
        for m in items
        if isinstance(m, dict) and m.get('id')
    ]
    return {
        'data': data,
        'has_more': False,
        'first_id': data[0]['id'] if data else None,
        'last_id': data[-1]['id'] if data else None,
    }


def _alias_entries(items: list, allow: list[str]) -> list[dict]:
    """白名单里写的是**别名**时，补一条以别名为 id 的模型条目。

    别名（设置页「模型映射」）是给下游用的名字，它**不在上游模型清单里**。
    若只按清单字面裁剪，白名单填成别名的密钥会拿到**空列表**——而那个别名
    明明能调用（鉴权判的是请求里的名字，模型映射发生在鉴权**之后**）。

    条目取别名所指模型的信息，只把 `id` 换成别名——那才是客户端要发的
    名字（发清单里的真名会被白名单拒掉）。配了兜底（`*`）后白名单里任意
    名字都调得通，同样要补条目，否则用户会以为白名单没生效。
    """
    mapping = _load_map(db.get_setting('model_map', {}) or {})
    if not mapping:
        return []
    by_name: dict[str, dict] = {}
    for m in items:
        if isinstance(m, dict) and m.get('id'):
            by_name.setdefault(str(m['id']).strip(), m)
    out: list[dict] = []
    seen: set[str] = set()
    for raw in allow:
        name = str(raw).strip()
        # 本身就是清单里的模型 → 白名单裁剪已保留它，不重复添加
        if not name or name in seen or name in by_name:
            continue
        target = _map_model(name)
        src = by_name.get(target.strip()) if target else None
        if not src:
            continue          # 别名指向的模型不在清单里 → 调不通，不给
        out.append({**src, 'id': name})
        seen.add(name)
    return out


def _scope_models(payload: object, key: dict | None) -> object:
    """按密钥的**模型白名单**裁剪模型列表。

    这个接口要回答的是「**你能用**什么模型」——客户端普遍拿它当模型选择
    器。只按清单透传会出现「列表里有、一选就失败」（白名单外的模型照旧列
    出来，选中就 400）。白名单判据与调用侧**共用 `keysvc.model_allowed`**，
    不另写一份：两处各写一遍迟早漂移。白名单为空 = 不限制。

    结构不是预期的 `{data: [...]}` 时**原样透传**——不该因为我们认不出结
    构就去改动上游的响应。
    """
    key = key or {}
    allow = [str(x).strip() for x in (key.get('models') or []) if str(x).strip()]
    if not allow:
        return payload
    if not isinstance(payload, dict):
        return payload
    items = payload.get('data')
    if not isinstance(items, list):
        return payload

    scoped = [m for m in items
              if isinstance(m, dict) and keysvc.model_allowed(key, m.get('id'))]
    # 别名：白名单可能写的是下游熟悉的别名，它不在清单里，要补进来
    scoped += _alias_entries(items, allow)
    return {**payload, 'data': scoped}


# ── 对话补全（v1 / v2）──────────────────────────────────
async def _chat(request: Request):
    body, err = await _read_json_body(request)
    if err:
        return err

    requested_model = body.get('model') if isinstance(body, dict) else None
    # model 必须是字符串：非字符串（对象/数组/数字）会让映射与上游处理出现
    # 意外行为，这里直接拒掉，也顺带让模型白名单的判定有确定的输入。
    if requested_model is not None and not isinstance(requested_model, str):
        return _oai_error('model 必须是字符串', 400, 'invalid_request_error', 'invalid_model')
    # 映射**先于鉴权**：白名单判的是客户端发来的请求名，而真正发往上游的
    # 名字在这里就定下来（供鉴权与日志统一口径）。
    mapped = _map_model(requested_model)
    key, err = await _authorize(request, requested_model, mapped=mapped)
    if err:
        return err
    ip = iputil.client_ip(request)
    ua = request.headers.get('user-agent')

    if mapped:
        body['model'] = mapped

    # 越界的 max_tokens 会被上游包装成 5xx（见 clamp_max_tokens 的说明），
    # 鉴权后、转发前压回上限。
    clamp_max_tokens(body)

    # 思考强度（reasoning_effort）：仅用于日志展示，**不改写请求体**。
    effort = None
    raw_effort = body.get('reasoning_effort')
    if isinstance(raw_effort, str) and raw_effort.strip():
        effort = raw_effort.strip()[:16]

    stream = bool(body.get('stream'))
    if stream:
        # 让上游在最后一个 chunk 返回 usage，便于精确计量
        body.setdefault('stream_options', {})
        if isinstance(body['stream_options'], dict):
            body['stream_options'].setdefault('include_usage', True)

    started = time.time()
    try:
        upstream = _resolve_upstream(key)
    except Exception as exc:  # noqa: BLE001
        await _record(key, ip, requested_model or '', mapped or '', 503, ua=ua,
                      error=str(exc), effort=effort)
        return _oai_error(str(exc), 503, 'api_error', 'upstream_unavailable')

    picked = _pick_account(upstream)
    if picked is None:
        msg = (f'上游「{upstream.get("name") or upstream.get("base_url")}」的 '
               f'PAT 账号池为空或没有可用账号，请先在账号页添加并测活')
        await _record(key, ip, requested_model or '', mapped or '', 503, ua=ua,
                      error=msg, effort=effort)
        return _oai_error(msg, 503, 'api_error', 'no_account_available')

    async def on_finish(status, usage, first_token_ms, latency_ms, error):
        await _record(key, ip, requested_model or '', mapped or '', status,
                      usage=usage, latency_ms=latency_ms,
                      first_token_ms=first_token_ms, stream=stream,
                      error=error, account=picked, effort=effort, ua=ua)

    return await _forward_chat(upstream, picked[1], body,
                               started=started, on_finish=on_finish)


@router.post('/v1/chat/completions')
async def chat_v1(request: Request):
    return await _chat(request)


@router.post('/v2/chat/completions')
async def chat_v2(request: Request):
    return await _chat(request)


# ── 存活探测 ─────────────────────────────────────────────
@router.get('/healthz')
async def gateway_health() -> dict:
    """未认证的存活探测：`{ok, service, upstream}`。

    **只回布尔**：不透传上游响应体——那可能带池子规模等信息，探测方只需要
    「上游是否可用」。异常也不回详情：那会暴露上游地址与网络拓扑。
    """
    try:
        from ..services import qoder2api
        ok, _ = await qoder2api.probe(config.QC2API_BASE, api_key=config.QC2API_KEY)
    except Exception:  # noqa: BLE001
        ok = False
    return {'ok': True, 'service': SERVICE_NAME, 'upstream': bool(ok)}
