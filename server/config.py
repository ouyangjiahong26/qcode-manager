"""运行期配置：全部通过环境变量覆盖，默认值适配单机部署。"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # 仓库根目录


def _env(name: str, default: str) -> str:
    v = os.environ.get(name, '').strip()
    return v or default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, '').strip() or default)
    except ValueError:
        return default


PORT = _env_int('QC_MANAGER_PORT', 7865)
HOST = _env('QC_MANAGER_HOST', '0.0.0.0')

# 上游 qoder2api 的基址与可选附加鉴权。PAT 仍逐请求注入，这里只在
# 上游变体要求额外 API Key 时叠加。
QC2API_BASE = _env('QC2API_BASE', 'http://127.0.0.1:8963').rstrip('/')
QC2API_KEY = _env('QC2API_KEY', '')

# 本管理端数据
DATA_DIR = Path(_env('QC_DATA_DIR', str(ROOT / 'data')))
DB_PATH = Path(_env('QC_DB', str(DATA_DIR / 'manager.db')))
USERS_FILE = Path(_env('QC_USERS_FILE', str(DATA_DIR / 'users.json')))
STATIC_DIR = Path(_env('QC_STATIC_DIR', str(ROOT / 'web' / 'out')))

# 子路径部署时的 URL 前缀，留空 = 根路径部署（默认）。
#
# 反向代理把 `/qcoder-manager/...` 剥掉前缀再转发给本服务，所以路由本身不需要
# 感知前缀；但**服务端主动发出的绝对地址**（如 RSC 兜底重定向的 Location）会
# 漏掉它，浏览器跟过去就落到域名根上。该值须与前端构建时的
# `NEXT_PUBLIC_BASE_PATH` 保持一致。
_BASE_PATH_RAW = _env('QC_BASE_PATH', '').strip('/')
BASE_PATH = f'/{_BASE_PATH_RAW}' if _BASE_PATH_RAW else ''

# 网关转发上游的超时（秒）：对话补全可能带长思考，默认放宽到 120。
UPSTREAM_TIMEOUT = _env_int('QC_UPSTREAM_TIMEOUT', 120)
TRUST_PROXY = _env('QC_TRUST_PROXY', '1') == '1'
# 可信反向代理跳数：用于从 X-Forwarded-For 右侧取真实客户端 IP。
# 前面直接是 Nginx/1Panel 时保持 1；若还挂了 CDN 则设为 CDN+反代的层数。
TRUSTED_PROXY_HOPS = _env_int('QC_TRUSTED_PROXY_HOPS', 1)
# 可信代理的**来源网段**（逗号分隔，支持 CIDR）。
#
# 为什么需要它：仅凭「配置里开了 TRUST_PROXY」就无条件采信 X-Real-IP 是错的——
# 服务直接暴露（默认 0.0.0.0）时，X-Real-IP 只是普通客户端头，任何人加一个
# `X-Real-IP: 9.9.9.9` 就能冒充任意来源 IP，从而绕过全局 IP 白/黑名单、
# 密钥 IP 白名单与 max_ips、以及登录失败按 IP 锁定。
# 现在只在**TCP 对端确实来自这些网段**时才采信转发头，否则一律用对端地址。
# 默认**只信回环**（反代与面板同机，最常见也最安全）。
TRUSTED_PROXY_CIDRS = [
    c.strip() for c in _env(
        'QC_TRUSTED_PROXY_CIDRS',
        '127.0.0.0/8,::1/128',
    ).split(',') if c.strip()
]
# 是否暴露 /docs、/openapi.json、/redoc。生产环境建议关闭（默认关闭）。
ENABLE_DOCS = _env('QC_ENABLE_DOCS', '0') == '1'
# 入站访问日志（安全页的「IP 访问日志」）是否记录**放行**的请求。
#
# 默认关闭：那张表的用途是安全审计（谁在扫我、谁被挡了），记全量会把信号淹没；
# 而表有 2 万行滚动上限，被正常流量占满后真出事时拦截记录可能已被挤掉。
# 放行的明细在「请求日志」页有完整记录，这里不重复记不丢信息。
AUDIT_ALL_ACCESS = _env('QC_AUDIT_ALL_ACCESS', '0') == '1'
# 显式出口代理（可选，如 http://127.0.0.1:7890）。
# 留空时所有请求都不使用任何代理：httpx 默认 trust_env=True 会读取系统/环境代理，
# 会把内网请求（如 127.0.0.1:8963）也交给系统代理，导致连接被劫持或长时间超时。
HTTP_PROXY = _env('QC_HTTP_PROXY', '')
# 会话**总时长**上限（天）：登录后最多维持这么久，到点必须重新登录。
#
# 下限钳到 1 天：0（或负数）会让 cookie 的 exp 等于签发时刻，即「登录成功但
# 立刻过期」——用户被锁在门外，而报错只会说「未登录」，看不出是配置写错了。
SESSION_DAYS = max(1, _env_int('QC_SESSION_DAYS', 1))
# 会话**空闲**上限（小时）：距上次活动超过它就失效（滑动续期的窗口）。
# **0 = 关闭空闲判定**，只按总时长失效。
SESSION_IDLE_HOURS = _env_int('QC_SESSION_IDLE_HOURS', 12)
COOKIE_NAME = 'qc_session'
SECURE_COOKIE = _env('QC_SECURE_COOKIE', 'auto')  # auto | true | false

# 首次启动时 admin 账号的初始密码（留空则随机生成并打在日志里）。
ADMIN_PASSWORD = _env('QC_ADMIN_PASSWORD', '')

# 网关限流：每密钥每分钟的最大请求数。
GATEWAY_RATE_PER_MIN = max(1, _env_int('QC_GATEWAY_RATE_PER_MIN', 120))
# 网关请求体上限（MB）：拦在读取 body 之前，防止超大上传占内存。
GATEWAY_MAX_BODY_MB = max(1, _env_int('QC_GATEWAY_MAX_BODY_MB', 32))

# 一键更新相关：代理、部署方式、GitHub 令牌与镜像加速。
UPDATE_PROXY = _env('QC_UPDATE_PROXY', '')
SYNC_DEPLOY = _env('QC_SYNC_DEPLOY', '')
GITHUB_TOKEN = _env('QC_GITHUB_TOKEN', '')
GITHUB_MIRROR = _env('QC_GITHUB_MIRROR', '')

# 允许的 CORS 来源（同源部署时留空即可；禁用 `*`，多个用逗号分隔）
CORS_ORIGINS = [o for o in _env('QC_CORS_ORIGINS', '').split(',') if o]


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)


def global_http_proxy() -> str:
    """全局出口代理：面板设置里配了且开启时生效，否则回落环境变量 QC_HTTP_PROXY。"""
    try:
        from . import db
        if db.get_setting('global_proxy_enabled', True) is False:
            return ''
        stored = str(db.get_setting('http_proxy', '') or '').strip()
    except Exception:            # noqa: BLE001
        stored = ''
    return stored or HTTP_PROXY


def new_secret() -> str:
    return secrets.token_urlsafe(48)


# ── 出站客户端池 ──────────────────────────────────────────
#
# 为什么不再每次新建：Windows 上构造 httpx.AsyncClient 会去加载系统证书库建 SSL
# 上下文，实测 **~1 秒**。面板的每个页面切换都要问上游几次（账号/模型/探测），
# 于是「每次出站新建客户端」直接表现成「切页面慢 1~3 秒」。共享之后同一套配置
# 只建一次。
#
# 键里带**事件循环**：httpx 的客户端（连接池）绑在创建它的那个循环上，跨循环
# 复用会炸（同步代码里 `asyncio.run` 另起循环那条路就是例子）。不同循环各建
# 一个，互不干扰。
_CLIENTS: dict[tuple, tuple] = {}
_CLIENTS_MAX = 24


class _BorrowedClient:
    """借出去用的客户端：退出 `async with` 时**不关闭**（归还给池）。

    调用点大多是 `async with config.http_client(...) as client:` 的写法，所以这里
    只把「上下文管理」这层语义接过来，避免改一堆调用点。关闭由
    `close_clients()`（应用退出）或池满淘汰时统一做。

    另外**代理属性访问**（`__getattr__`）并让 `aclose()` 变成「归还」而不是真关：
    有几处调用点是不用 `async with`、自己 `client = http_client(...)` 然后
    `finally: await client.aclose()` 的写法（网关、测活等）。共享之后，那些
    `aclose()` 一旦真关，就会把别人正在用的连接池一起关掉——并发下表现为
    「偶发请求失败」。所以借来的客户端只支持「还」，真关只发生在池淘汰与退出
    清理里。
    """

    __slots__ = ('_client',)

    def __init__(self, client) -> None:  # noqa: ANN001
        self._client = client

    def __getattr__(self, name: str):  # noqa: ANN201
        # 借用包装要能当客户端用（post/stream/headers/... 一律转发）
        return getattr(self._client, name)

    async def __aenter__(self):  # noqa: ANN201
        return self._client

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def aclose(self) -> None:
        """借用语义：只归还，不真关（共享客户端由池统一管理）。"""
        return None


def _timeout_key(tmo) -> tuple:  # noqa: ANN001
    return (tmo.connect, tmo.read, tmo.write, tmo.pool)


_INTERNAL_SUFFIXES = ('.local', '.internal', '.lan', '.docker', '.localhost')


def _is_internal_host(host: str | None) -> bool:
    """这个目标算不算「内网 / 本机」——算的话不该交给外部代理。

    覆盖三类：回环与常见内网别名、私有 / 链路本地 / 保留网段的 IP、**单标签主机名**
    （容器服务名这类；公网域名一定带点）。
    """
    h = (host or '').strip().lower().rstrip('.')
    if not h:
        return False
    if h in ('localhost', 'host.docker.internal', 'host.containers.internal'):
        return True
    if h.endswith(_INTERNAL_SUFFIXES) or '.' not in h:
        # 单标签 = 内网 DNS（容器服务名、`myhost` 之类）。公网目标必然带点。
        return True
    try:
        addr = ipaddress.ip_address(h)
    except ValueError:
        return False
    return bool(addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_unspecified)


def _proxy_transport(proxy: str):  # noqa: ANN202
    """「内网/本机直连、其余走代理」的 httpx transport。

    用自定义 transport 而不是 `mounts=`：mounts 只能按主机名模式匹配，表达不了
    「私有网段」「单标签主机名」这两类，而它们恰恰是容器部署的常态
    （`host.docker.internal`、`10.x`…）。

    类建在这里而不是模块顶层，是因为 httpx 在本模块是**按需导入**的（保持冷启动
    不加载它）；每个 (循环, 超时, 代理) 组合只建一次客户端，这点构造开销可忽略。
    """
    import httpx

    class _DirectForInternalTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self._proxied = httpx.AsyncHTTPTransport(proxy=proxy)
            self._direct = httpx.AsyncHTTPTransport()

        async def handle_async_request(self, request):  # noqa: ANN001, ANN201
            transport = (self._direct if _is_internal_host(request.url.host)
                         else self._proxied)
            return await transport.handle_async_request(request)

        async def aclose(self) -> None:
            await self._proxied.aclose()
            await self._direct.aclose()

    return _DirectForInternalTransport()


def _build_client(tmo, proxy: str | None):  # noqa: ANN001, ANN202
    import httpx

    kwargs: dict = {'timeout': tmo, 'trust_env': False}
    if proxy:
        kwargs['transport'] = _proxy_transport(proxy)
    return httpx.AsyncClient(**kwargs)


async def close_clients() -> None:
    """关掉池里所有客户端（应用退出时调用；幂等）。"""
    clients = [entry[1] for entry in _CLIENTS.values()]
    _CLIENTS.clear()
    for client in clients:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 —— 退出路径上的清理失败不该再抛
            continue


def _evict(loop) -> None:  # noqa: ANN001
    """池满了淘汰最早的一个（按插入顺序）。"""
    while len(_CLIENTS) > _CLIENTS_MAX:
        key = next(iter(_CLIENTS))
        entry = _CLIENTS.pop(key, None)
        if not entry or entry[1].is_closed:
            continue
        old_loop, client = entry
        try:
            if old_loop is loop:
                loop.create_task(client.aclose())      # 同一循环才能安全关闭
        except Exception:  # noqa: BLE001
            continue


def stored_http_proxy() -> str:
    """面板设置里存的出口代理；空串 = 没设过（回落到环境变量）。"""
    try:
        from . import db          # 延迟导入：config 被 db 反过来 import，顶层会成环
        value = db.get_setting('http_proxy', '')
    except Exception:             # noqa: BLE001 —— 读不到就当没设过，别让出站挂掉
        return ''
    return str(value or '').strip()


def effective_http_proxy() -> str:
    """当前生效的**全局**出口代理（面板设置优先、环境变量兜底，开关可关）。"""
    return global_http_proxy()


def http_client(timeout, *, connect: float | None = None,
                proxy: str | None = None):
    """统一的 httpx 客户端：默认忽略系统/环境代理，避免内网请求被代理劫持。

    需要走代理时显式设置 QC_HTTP_PROXY 或在面板设置里配置。

    timeout 可以传数字，也可以传已构造好的 httpx.Timeout。
    注意：httpx 不允许「Timeout 实例 + connect 关键字」同时传，
    因此传入实例时忽略 connect，避免 AssertionError。

    **返回的是借用语义的上下文管理器**（退出时不关客户端，见 `_BorrowedClient`）：
    同一事件循环 + 同一组超时/代理只建一次，连接池复用（Windows 上每次新建
    要 ~1 秒，正好是页面切换的耗时）。
    """
    import httpx

    if isinstance(timeout, httpx.Timeout):
        tmo = timeout
    elif connect is not None:
        tmo = httpx.Timeout(timeout, connect=connect)
    else:
        tmo = httpx.Timeout(timeout)
    selected_proxy = effective_http_proxy() if proxy is None else proxy

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 事件循环外（同步夹具/脚本场景）：不缓存，按老样子建一个即用即弃的，
        # 免得把一个绑在未知循环上的客户端留在池里。
        return _build_client(tmo, selected_proxy)

    key = (*_timeout_key(tmo), selected_proxy or '')
    entry = _CLIENTS.get(key)
    if entry is not None and entry[0] is loop and not entry[1].is_closed:
        return _BorrowedClient(entry[1])
    client = _build_client(tmo, selected_proxy)
    _CLIENTS[key] = (loop, client)
    _evict(loop)
    return _BorrowedClient(client)
