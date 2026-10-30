"""QCoder Manager 入口：管理 API + 对外反代网关 + 静态前端托管。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as _StarletteHTTPException

import logging

from . import config, db, keepalive, pgsync, security, systemupd
from .stdio_utf8 import force_utf8_stdio

from .routers import (
    accounts, anthropic, auth, gateway, keys, logs, models, playground,
    pgsync as pgsync_router, responses, settings, stats, system,
    security as security_router, tokens, upstreams,
)


logger = logging.getLogger('qcoder.main')

# 版本号唯一出处：/api/sysinfo、/api/system/* 与一键更新流程共用
# （systemupd._app_version 延迟 import 本模块读 app.version）。
__version__ = '0.1.0'


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    db.connect()
    security.load_users()  # 首次启动会自动生成 admin 并把随机密码落盘
    weak_admins = security.weak_admin_passwords()
    if weak_admins:
        # 不拒绝启动：用户需要先进面板改密码。日志必须足够醒目。
        logger.error('管理员仍在使用常见弱口令：%s。请立即在「设置 → 管理用户」中修改。',
                     '、'.join(weak_admins))
    _warn_if_exposed()
    # 三个后台调度器全部幂等、默认关闭（配置开启才真正干活）：
    #   keepalive —— 按时刻表对 PAT 池逐个测活续期
    #   systemupd —— 自动更新巡检（时间窗内检测并执行一键更新）
    #   pgsync    —— PostgreSQL 定时异地备份
    keepalive.start_scheduler()
    systemupd.start_scheduler()
    pgsync.start_scheduler()
    try:
        yield
    finally:
        keepalive.stop_scheduler()
        systemupd.stop_scheduler()
        pgsync.stop_scheduler()
        # 出站客户端是共享的（池上限 24，见 config.http_client），
        # 退出时统一关掉，别把连接池与 SSL 上下文丢在原地。
        await config.close_clients()


def _warn_if_exposed() -> None:
    """监听所有网卡时提醒：确保前面有反代，不要把 7865 直接暴露到公网。

    为什么只警告不自动改：标准部署（反代到本机端口）与「直连公网」用的是
    同一个 0.0.0.0，自动改成 127.0.0.1 会把前者一起改坏。IP 伪造的问题已在
    `iputil.client_ip` 从源头修掉（只在 TCP 对端来自可信网段时才采信转发头），
    这里的提示是纵深防御——少一层暴露就少一类风险。
    """
    if config.HOST not in ('0.0.0.0', '::'):
        return
    logger.warning(
        '服务监听在 %s（所有网卡）：请确认 7865 端口没有直接暴露到公网，'
        '仅在反向代理后使用；直连会让来源 IP 类管控与登录锁定失去意义。',
        config.HOST,
    )


app = FastAPI(
    title='QCoder Manager',
    version=__version__,
    lifespan=lifespan,
    # 生产环境默认关闭交互式文档与 OpenAPI 描述：
    # 它们会把管理接口全貌（路径、参数、结构）暴露给任何未认证访问者，
    # 便于攻击者摸清面。需要时设 QC_ENABLE_DOCS=1 打开。
    docs_url='/docs' if config.ENABLE_DOCS else None,
    redoc_url='/redoc' if config.ENABLE_DOCS else None,
    openapi_url='/openapi.json' if config.ENABLE_DOCS else None,
)


def _validate_cors_origins(origins: list[str]) -> None:
    """`*` 与凭据模式互斥：命中直接抛错，让服务起不来（配置错误要在启动时暴露）。"""
    if any(o.strip() == '*' for o in origins):
        raise RuntimeError(
            'QC_CORS_ORIGINS 含 `*` 且管理端启用 Cookie 凭据：任意网站都能冒用'
            '管理员身份读写 /api/*。请改为列出具体来源（逗号分隔），或留空'
            '（同源部署，默认）。'
        )


if config.CORS_ORIGINS:
    # 只允许**明确列出的**来源。绝不要把 QC_CORS_ORIGINS 设成 `*`：
    # 本应用用 Cookie 认证管理端，而 Starlette 在 allow_credentials=True 时
    # 会把 `*` 回显成请求方 Origin（不是字面 `*`），于是**任意网站**都能带着
    # 管理员的 Cookie 调 /api/* 并读到响应 —— 等于把控制台交给任何网页。
    # 留空（默认）即同源部署，安全。
    #
    # 这是「误配即全面接管」的开关，因此检测到 `*` 直接拒绝启动，而不是只记
    # 一条日志继续跑：日志拦不住照抄配置的人，历史上确有人这么配过。
    _validate_cors_origins(config.CORS_ORIGINS)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
    )

# 管理端请求体上限。网关那条路有自己的逐块校验（gateway._read_json_body），
# 但 `/api/*` 此前**没有任何上限** —— 未鉴权的 /api/login 就能用大 body
# 把内存占住（Starlette 的 Request.body() 逐块追加，uvicorn 自身也不设限）。
# 这里统一兜住；取得比网关默认宽松些：管理端有「保存上游配置」这类正常大请求。
MAX_API_BODY_BYTES = 16 * 1024 * 1024

# 唯一需要更大请求体的管理端路径：上传离线升级包。发布包现在约 11 MB，将来
# 会变大，所以这里给到 64 MB —— 仍然是个**硬上限**：上传本身还要过会话管理员
# 鉴权，且落盘后立刻做 tar.gz 结构与 .version 校验。
MAX_PACKAGE_BODY_BYTES = 64 * 1024 * 1024
PACKAGE_UPLOAD_PATHS = ('/api/system/update-package',)


@app.middleware('http')
async def limit_api_body(request: Request, call_next):
    """给 `/api/*` 的请求体加上限（上传升级包那条路放宽，见上面的常量）。

    只看 `Content-Length`：本中间件拦的是「声明了超大长度」这条最容易发起、
    也最容易自动化的路径（无鉴权即可打 /api/login），不用读流就能立即拒绝。
    不带该头的分块请求在这里声明为 0、拦不住，由下面的
    `ApiBodyLimitMiddleware` 在**实际读取层**强制。
    """
    if request.url.path.startswith('/api/'):
        try:
            declared = int(request.headers.get('content-length') or 0)
        except ValueError:
            declared = 0
        limit = (MAX_PACKAGE_BODY_BYTES
                 if request.url.path in PACKAGE_UPLOAD_PATHS else MAX_API_BODY_BYTES)
        if declared > limit:
            return JSONResponse(
                {'detail': f'请求体过大（上限 {limit // 1024 // 1024} MB）'},
                status_code=413,
            )
    return await call_next(request)


class _ApiBodyTooLarge(Exception):
    """内部信号：/api/* 请求体在读取层超过上限。

    注意：路由里 `except Exception` 很常见（/api/login 解析失败就转 400），
    所以它不一定能到达中间件变成 413 —— 被就地转成 400 也是**安全**的：
    超限时读取已经中止，内存不再增长。声明 413 只在这条异常成功穿透路由时生效。
    """


class ApiBodyLimitMiddleware:
    """在 ASGI receive 层强制 `/api/*` 的请求体上限。

    为什么上面的 Content-Length 预检不够：分块传输（chunked）没有该头，
    `declared` 为 0 直接放行；而 handler 里的 `await request.json()` 会把整个
    body 无上限地聚合进内存。/api/login 是未鉴权可达的，少量并发分块大请求
    就能把这个进程（管理面、/v1 网关、静态托管同体）的内存吃光。

    实现：包住 receive 通道逐块计数，超限即抛内部异常中止 handler，由本层
    （异常向外传播的必经之路）直接发 413。此时 handler 还在读 body、尚未
    开始写响应，所以 413 是该连接上唯一的响应，不会出现「半个响应 + 413」。
    """

    def __init__(self, app, *, limit: int = MAX_API_BODY_BYTES,
                 package_limit: int = MAX_PACKAGE_BODY_BYTES,
                 package_paths: tuple = PACKAGE_UPLOAD_PATHS) -> None:
        self.app = app
        self.limit = limit
        self.package_limit = package_limit
        self.package_paths = package_paths

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http' or not scope.get('path', '').startswith('/api/'):
            await self.app(scope, receive, send)
            return
        limit = (self.package_limit
                 if scope['path'] in self.package_paths else self.limit)
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message.get('type') == 'http.request':
                received += len(message.get('body') or b'')
                if received > limit:
                    raise _ApiBodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _ApiBodyTooLarge:
            response = JSONResponse(
                {'detail': f'请求体过大（上限 {limit // 1024 // 1024} MB）'},
                status_code=413,
            )
            await response(scope, receive, send)


app.add_middleware(ApiBodyLimitMiddleware)

# ── 路由注册顺序很重要：先 API / 网关，最后挂静态文件 ──
app.include_router(auth.router)
app.include_router(accounts.router)
app.include_router(keys.router)
app.include_router(logs.router)
app.include_router(stats.router)
app.include_router(security_router.router)
app.include_router(settings.router)
# PostgreSQL 异地备份：把本地数据镜像出去、需要时拉回来
app.include_router(pgsync_router.router)
# 多上游（PAT 池分组）：密钥绑定上游 = 请求走那个池，见 upstreamsvc
app.include_router(upstreams.router)
app.include_router(system.router)
app.include_router(tokens.router)
app.include_router(models.router)
app.include_router(playground.router)
app.include_router(gateway.router)
# Anthropic Messages API 兼容层（/v1/messages）——给只认该协议的客户端用
app.include_router(anthropic.router)
# OpenAI Responses API 兼容层（/v1/responses、/responses）
app.include_router(responses.router)


@app.middleware('http')
async def cache_headers(request: Request, call_next):
    """按内容性质设置缓存策略与安全响应头。

    - /_next/static/**：文件名含内容哈希，可长期强缓存（immutable）
    - /api/**、/v1/**、/v2/**、/healthz：动态数据，禁止任何缓存
    - 图标 / 字体等静态资源：内容基本不变，缓存一天
    - 其余（HTML 文档）：no-cache，每次回源校验，避免拿到旧页面

    安全头说明：
    - X-Content-Type-Options: 阻止浏览器嗅探类型（防内容被当作脚本执行）
    - X-Frame-Options / frame-ancestors: 禁止被其他站点内嵌（防点击劫持）
    - Referrer-Policy: 跨站请求不带完整 URL（避免泄露路径）
    - CSP: 只允许同源资源与内联样式（前端使用内联样式属性）
    """
    response = await call_next(request)

    # 会话滑动续期：current_user 判定「该续了」时在这里重签 cookie。
    #
    # 为什么放中间件而不是 current_user 里：那个函数是 FastAPI 依赖，只负责
    # **解析身份**，在它里面写响应会耦合出不必要的层，而且它被大量接口调用、
    # 有的还是只读的。中间件是唯一能看到「响应对象 + 请求状态」的地方。
    if getattr(request.state, 'session_renew', False):
        try:
            token = request.cookies.get(config.COOKIE_NAME) or ''
            cfg = security.load_users()
            obj = security._unsign(token, cfg['secret']) or {}
            username = str(obj.get('username') or '')
            if username:
                # orig 原样延续 —— 续期只推后**空闲截止**，不延长**总寿命**。
                # role 传空：鉴权一律以用户表为准（载荷里的 role 仅作展示参考）。
                fresh = security.issue_token(username, '', orig=int(obj.get('orig') or 0) or None)
                response.set_cookie(
                    config.COOKIE_NAME, fresh,
                    max_age=config.SESSION_DAYS * 86400, httponly=True,
                    samesite='lax', secure=security.cookie_secure(request), path='/',
                )
        except Exception as exc:  # noqa: BLE001
            # 续期失败不能让请求失败 —— 用户这次照常，只是下次要重登
            logger.warning('会话续期失败（不影响本次请求）: %s', exc)

    path = request.url.path
    if path.startswith('/_next/static/'):
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    elif path.startswith(('/api/', '/v1/', '/v2/', '/healthz')):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
    elif path.endswith(('.png', '.ico', '.svg', '.woff2', '.webmanifest')):
        response.headers['Cache-Control'] = 'public, max-age=86400'
    else:
        response.headers['Cache-Control'] = 'no-cache'

    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'DENY')
    response.headers.setdefault('Referrer-Policy', 'no-referrer')
    response.headers.setdefault('Permissions-Policy', 'geolocation=(), microphone=(), camera=()')
    response.headers.setdefault(
        'Content-Security-Policy',
        "default-src 'self'; "
        "img-src 'self' data: blob:; "
        "font-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'",
    )
    return response


@app.get('/api/sysinfo')
def sysinfo(user: dict = Depends(security.current_user)) -> dict:
    """服务信息。需要登录 —— 版本类信息不应对未认证访问者随意暴露。"""
    return {
        'service': 'qcoder-manager',
        'version': app.version,
    }


# ── 静态前端（Next.js 静态导出）────────────────────────
def _looks_like_traversal(full_path: str) -> bool:
    """判断请求路径是否像目录穿越尝试（用于记日志，不参与拦截决策）。

    拦截一律由 `_safe_static_path` 的包含性校验负责，这里只是让安全事件
    留下痕迹：静态路径原本不记录任何访问日志，万一被利用了也无从发现。
    """
    p = (full_path or '').replace(chr(92), '/')
    return '..' in p.split('/') or p.startswith('/') or ':' in p


def _safe_static_path(full_path: str) -> Path | None:
    """把请求路径解析为静态目录下的真实文件；越界或非法一律返回 None。

    安全（重要）：绝不能把请求路径直接拼到 STATIC_DIR 上而不做越界校验。
    ASGI 会先对 %2f 解码，`/..%2f..%2fdata%2fusers.json` 这类请求在拼接后
    会指向部署目录**之外**，导致任意文件读取（users.json 里有签发会话的
    secret，可据此伪造 admin 会话）。

    修法：用 `resolve()` 归一化后，强制要求结果仍位于 STATIC_DIR 之内。
    这是防目录穿越的标准做法，能同时覆盖 `..`、编码斜杠、绝对路径与
    符号链接等各变体；不合规的直接当作 404，不泄露任何信息。
    """
    if not full_path:
        return None
    try:
        root = config.STATIC_DIR.resolve()
        # 绝对路径（如 full_path 以 / 开头或形如 C:\...）会被 Path 当作新根，
        # 这里先剥掉前导分隔符，再统一在后面做包含性校验。
        candidate = (root / full_path.lstrip('/\\')).resolve()
    except (OSError, ValueError, RuntimeError):
        # resolve() 在符号链接成环等情况下可能抛错：一律视为不可访问
        return None
    if candidate != root and root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def _is_document_request(request: Request) -> bool:
    """这次请求是不是「把结果当页面显示」的文档型导航。

    浏览器的地址栏导航会带 `Accept: text/html,...`；而客户端路由抓 RSC 数据
    用的是 `fetch`（`Accept: */*` 或 `text/x-component`）并带 `RSC: 1` 头。
    两者必须分开：前者拿到的若是 flight 文本，用户看到的就是满屏原始数据。
    """
    if request.headers.get('rsc'):
        return False
    return 'text/html' in (request.headers.get('accept') or '').lower()


def _rsc_page_for(full_path: str) -> str | None:
    """文档型请求命中 Next 静态导出的 RSC 数据文件时，返回它对应的页面路径。

    `output: 'export'` 会把每个页面的 RSC（flight）数据写成
    `<页>/index.txt`。正常客户端拿它做客户端路由的数据源；但 router 的
    兜底分支会把浏览器**整页导航**到这个地址，于是浏览器按 `text/plain`
    渲染那份 flight 数据、地址栏也变成 `.txt`，刷新只会继续显示它。

    **只有对应页面确实存在时才返回**：`.txt` 也可能是真实静态文件
    （如 robots.txt），那种没有同名页面，不能一并重定向。

    两种导出形态都要认：`<页>/index.html`（绝大多数页面）与 `<页>.html`
    （根页面就是这种：`index.html` + `index.txt`）。少认一种，对应形态的
    页面在真机上就会继续显示原始数据。
    """
    p = (full_path or '').strip('/')
    if not p.endswith('.txt'):
        return None
    stem = p[:-4]
    if stem.endswith('/index'):
        stem = stem[: -len('/index')]
    elif stem == 'index':
        stem = ''
    candidates = (f'{stem}/index.html' if stem else 'index.html',
                  f'{stem}.html' if stem else 'index.html')
    if not any(_safe_static_path(c) is not None for c in candidates):
        return None          # 没有同名页面 → 是真实文件，按普通静态资源处理
    return f'/{stem}' if stem else '/'


@app.exception_handler(_StarletteHTTPException)
async def _json_http_error(request: Request, exc: _StarletteHTTPException) -> JSONResponse:
    """`/v1`、`/v2` 下的 404/405 一律回 OpenAI 形状 JSON。

    以前 `GET /v1/chat/completions` 会落到 SPA 兜底、返回整页 HTML，客户端
    解析 HTML 只能得到更难排查的错误。其余路径保持原样。
    """
    headers = getattr(exc, 'headers', None)
    if request.url.path.startswith(('/v1/', '/v2/')):
        code = 'method_not_allowed' if exc.status_code == 405 else 'not_found'
        return JSONResponse({'error': {'message': str(exc.detail),
                                       'type': 'invalid_request_error',
                                       'code': code}},
                            status_code=exc.status_code, headers=headers)
    return JSONResponse({'detail': exc.detail}, status_code=exc.status_code, headers=headers)


if config.STATIC_DIR.is_dir():
    # `_next` 子目录只在完整的 Next.js 导出里存在；挂载不存在的目录会让
    # StaticFiles 在启动期抛错、整个应用起不来（哪怕 index.html 就在根上）。
    if (config.STATIC_DIR / '_next').is_dir():
        app.mount('/_next', StaticFiles(directory=str(config.STATIC_DIR / '_next')),
                  name='next-assets')
    if (config.STATIC_DIR / 'favicon.ico').exists():
        @app.get('/favicon.ico', include_in_schema=False)
        def favicon() -> FileResponse:
            return FileResponse(config.STATIC_DIR / 'favicon.ico')

    @app.get('/', include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(config.STATIC_DIR / 'index.html')

    @app.get('/{full_path:path}', include_in_schema=False)
    def spa(full_path: str, request: Request):
        # 网关路径绝不该落到 SPA 外壳：宁可回一条 JSON 404，也不要把首页 HTML
        # 当成接口响应发给客户端。
        if full_path.startswith(('v1/', 'v2/')):
            return JSONResponse({'error': {'message': f'未知的接口路径 /{full_path}',
                                           'type': 'invalid_request_error',
                                           'code': 'not_found'}}, status_code=404)
        # 优先命中导出的静态页面 / 资源，否则回退到 404 页面。
        # 所有路径都必须先通过 _safe_static_path（越界即 None → 404）。
        #
        # 越界尝试会记一条 WARN 日志：这类请求 100% 是攻击或扫描行为，
        # 之前不记录任何痕迹，出事后无从追溯。日志只写路径，不含内容。
        if _looks_like_traversal(full_path):
            logger.warning('拦截疑似路径穿越请求: %r', full_path[:300])
        # ① 文档型请求命中 RSC 数据文件 → 送回对应页面。
        # 见 `_rsc_page_for` 的说明：客户端路由的兜底分支会把浏览器整页导航到
        # `<页>/index.txt`，那边返回的是 flight 文本，用户看到满屏原始数据。
        if _is_document_request(request):
            page = _rsc_page_for(full_path)
            if page is not None:
                # 补上子路径前缀：反代剥掉前缀后才到这里，但 Location 是发回浏览器
                # 的绝对地址，不带前缀就会跳到域名根（另一个站点）上。
                return RedirectResponse(f'{config.BASE_PATH}{page}', status_code=302)
        target = _safe_static_path(full_path)
        if target is not None:
            return FileResponse(target)
        index_candidate = _safe_static_path(f'{full_path}/index.html')
        if index_candidate is not None:
            return FileResponse(index_candidate)
        not_found = config.STATIC_DIR / '404.html'
        if not_found.is_file():
            return FileResponse(not_found, status_code=404)
        return JSONResponse({'error': 'not found'}, status_code=404)


# ── 子路径部署（反向代理前缀）────────────────────────────────────────
#
# 反向代理把本站挂在 /qoder 这类前缀下、且**不剥离**前缀时，后端收到的路径
# 仍带着前缀 —— 路由、静态挂载与 SPA 兜底全都匹配不上，页面会 404。
# 这里在最外层统一剥掉，让内部逻辑只看到「前缀之后的路径」。
#
# 反向代理若已经剥离了前缀（proxy_pass 带 URI 的常见写法），本中间件找不到
# 前缀、原样放行 —— 两种反代配置都能工作，nginx 侧不必改。
#
# 响应侧同步处理：SPA 的 RSC 兜底等场景会 302 到站内绝对路径（`/xxx`），
# 不补回前缀就会把用户带出子路径、落到站点根目录。
#
# 与 `config.BASE_PATH` 的分工：那个值被用来**主动构造**带前缀的绝对地址
# （见上面 SPA 兜底的 RedirectResponse）；本中间件负责「进来的路径带前缀」与
# 「出去的 Location 漏前缀」这两件事。两者都指同一个 QC_BASE_PATH。
class StripBasePathMiddleware:
    """纯 ASGI 中间件：剥离请求前缀，并把响应里的站内绝对位置补回前缀。"""

    def __init__(self, app, prefix: str) -> None:
        self.app = app
        self.prefix = prefix
        self._prefix_slash = prefix + '/'

    async def __call__(self, scope, receive, send):
        if scope.get('type') != 'http':
            await self.app(scope, receive, send)
            return

        path = scope.get('path') or '/'
        if path == self.prefix or path.startswith(self._prefix_slash):
            stripped = path[len(self.prefix):] or '/'
            scope = dict(scope)
            scope['path'] = stripped
            scope['raw_path'] = stripped.encode('utf-8')

        async def send_with_prefix(message):
            if message.get('type') == 'http.response.start' and message.get('headers'):
                message = dict(message)
                message['headers'] = [
                    (k, self._rewrite_location(v) if k.lower() == b'location' else v)
                    for k, v in message['headers']
                ]
            await send(message)

        await self.app(scope, receive, send_with_prefix)

    def _rewrite_location(self, value: bytes) -> bytes:
        try:
            text = value.decode('latin-1')
        except Exception:  # noqa: BLE001
            return value
        # 只处理站内绝对路径（/ 开头，且不是 //host 这种协议相对写法）
        if not text.startswith('/') or text.startswith('//'):
            return value
        # 已经带前缀的不重复叠加（SPA 兜底那处已自己补过）
        if text == self.prefix or text.startswith(self._prefix_slash):
            return value
        return (self.prefix + text).encode('latin-1')


if config.BASE_PATH:
    # add_middleware 后注册的在外层，所以这行放在文件末尾：请求进来先过它，
    # 后面的限流 / 缓存头 / 路由看到的都是剥离后的路径。
    app.add_middleware(StripBasePathMiddleware, prefix=config.BASE_PATH)
    logger.info('子路径部署：已启用前缀 %s（反代可保留或自行剥离，两者都可用）', config.BASE_PATH)
