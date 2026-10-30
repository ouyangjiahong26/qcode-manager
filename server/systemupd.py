"""系统更新：版本检测、离线升级包、后台更新 worker、更新日志与自动更新巡检。

## 总体形态

更新会重启面板自身，因此不能在请求进程里同步执行（响应会被中断）。做法是：
以**脱离父进程**的 worker（`python -m server.systemupd --worker`）干活，进度
写入状态文件，前端轮询 `/api/system/update-status` 读取。

更新逻辑全部收在本模块内（worker 子进程直接运行它），不再单设根目录更新
脚本：worker 换掉 `server/` 的同时也换掉了自己所属的模块，下次更新自然用的
是新版逻辑，不会出现「面板已是新版、更新器还是旧版」的错配。

## 安全边界

  · 目标只有面板自身，更新包来源只有 GitHub Release 与面板上传的离线包
    （服务端固定路径），**不接受客户端传入的命令或路径**；
  · 下载后核对**大小与 sha256 摘要**（GitHub API 给每个资产带 digest；离线包
    核对用户填的发布页 sha256，默认必填），不符即中止；
  · 解压严格限制在临时目录内：拒绝链接成员、绝对路径与 `..` 穿越；
  · 依赖在替换代码**之前**安装：装失败就中止，安装目录一个字节都不动。

## 与设置页的分工

本模块同时供 `/api/settings/*` 的三个系统端点复用：

  · `github_token()` / `mask_token()` / `normalize_mirror()` /
    `stored_github_mirror()` —— `/api/settings/github-token` 与 `/github-mirror`；
  · `get_config()` / `save_config()` / `validate_config()` / `get_state()` /
    `server_tz_offset_minutes()` / `unsupported_reason()` ——
    `/api/settings/auto-update`。

自动更新（settings 键 `auto_update`）只做一件事：把「检测 → 发现新版本 →
点更新」这个人工动作按管理员的设置自动化，**不新增任何更新路径**——真正
干活的仍是 `start_update`（与按钮同一条路）。
"""
from __future__ import annotations

import argparse
import asyncio
import errno
import hashlib
import http.client
import json
import logging
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config, db
from .stdio_utf8 import force_utf8_stdio

logger = logging.getLogger(__name__)

USER_AGENT = 'qcoder-manager-updater'

# 面板自己的仓库（owner/name），用于查询最新 Release；可用环境变量覆盖。
MANAGER_REPO = os.environ.get('QC_MANAGER_REPO') or 'ouyangjiahong26/qcoder-manager'

# worker 的安装目录。面板拉起 worker 时显式传 QC_INSTALL_DIR，两边不会各说各话。
INSTALL_DIR = Path(os.environ.get('QC_INSTALL_DIR') or config.ROOT)

STATUS_FILE = config.DATA_DIR / 'update-status.json'
LOCK_FILE = config.DATA_DIR / 'update.lock'
LOG_FILE = config.DATA_DIR / 'update.log'

# ── 离线升级包 ───────────────────────────────────────────
# 内网 / 拿不到 GitHub 的机器：从别处下载发布包，在面板里上传，再走同一套更新流程。
# 文件名固定（客户端不参与命名，只往这一个位置覆盖写），元信息存面板设置。
PACKAGE_FILE = config.DATA_DIR / 'update-package.tar.gz'
PACKAGE_META_KEY = 'update_package'
# 发布包十几 MB，留到 63 MB（= 请求体上限 64 MB 减去 multipart 的边界与
# 表头开销）：上传是往磁盘写，没有上限就等于给了一个能把磁盘填满的入口。
# `main.py` 的请求体上限中间件必须为 /api/system/update-package 放宽到 64 MB。
PACKAGE_MAX_BYTES = 63 * 1024 * 1024

# 锁有效期：超过此时间视为异常退出遗留，允许再次更新
LOCK_TTL = 3600

# 每条 shell 步骤（pip 安装、systemctl）的超时（秒）
STEP_TIMEOUT = 900


def _err_text(exc: BaseException) -> str:
    """把异常压成一句人话：子进程/网络异常常带多层 cause，直接 str() 会把
    底层细节一并甩给界面用户。"""
    parts = [str(exc).strip()]
    seen = {exc}
    cause = exc.__cause__
    while cause is not None and cause not in seen:
        seen.add(cause)
        text = str(cause).strip()
        if text:
            parts.append(text)
        cause = cause.__cause__
    return '：'.join(p for p in parts if p) or exc.__class__.__name__


# ── 当前版本 ─────────────────────────────────────────────
def current_version() -> str:
    """当前部署版本。

    以代码里的版本号为准，`.version` 标记只作部署痕迹：两者不一致时说明旧版
    更新流程没能替换标记（代码已换、标记还是旧的），此时采信代码并顺手把标记
    纠正过来，避免界面一直显示旧版本。
    """
    code = _app_version()
    marker = config.ROOT / '.version'
    marker_ver = ''
    if marker.is_file():
        try:
            marker_ver = marker.read_text(encoding='utf-8').strip()
        except Exception:  # noqa: BLE001
            marker_ver = ''

    def norm(v: str) -> str:
        return str(v or '').strip().lstrip('vV')

    if code and code != '0.0.0' and norm(code) != norm(marker_ver):
        # 自愈：把标记对齐到实际代码版本
        try:
            marker.write_text(f'v{norm(code)}\n', encoding='utf-8')
        except Exception:  # noqa: BLE001
            pass
        return f'v{norm(code)}'
    if marker_ver:
        return marker_ver
    return f'v{code}'


def _app_version() -> str:
    try:
        from . import main  # 延迟导入避免循环

        return getattr(main.app, 'version', '0.0.0')
    except Exception:  # noqa: BLE001
        return '0.0.0'


def read_local_version() -> str:
    """读当前部署版本（不 import 面板应用，worker 侧用这条）。

    优先取部署时留下的 `.version` 标记，否则从 `server/main.py` 里解析
    `version=` 行 —— worker 换代码的前后都要读版本，import 整个应用太重。
    """
    marker = INSTALL_DIR / '.version'
    if marker.is_file():
        try:
            return marker.read_text(encoding='utf-8').strip().lstrip('vV')
        except OSError:
            pass
    main_py = INSTALL_DIR / 'server' / 'main.py'
    if main_py.is_file():
        try:
            for line in main_py.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if line.startswith('version='):
                    return line.split('=', 1)[1].strip().strip('\'"').lstrip('vV')
        except OSError:
            pass
    return ''


# ── 代理地址脱敏 ─────────────────────────────────────────
# 代理地址里的 userinfo（`http://user:pass@host`）。日志与状态文件会经
# /api/system/update-status 返回给登录用户，凭据不能就这么露出去。
_PROXY_CRED_RE = re.compile(r'(?i)(https?|socks5h?|socks4)://[^/@\s]+@')


def redact_credentials(text: str) -> str:
    """把文本里的 `scheme://user:pass@host` 脱敏成 `scheme://***@host`。

    用于更新日志的出口（读状态、tail 日志），存量文件里已经写进去的同样会被洗。
    """
    return _PROXY_CRED_RE.sub(lambda m: f'{m.group(1)}://***@', str(text))


# ── 进程探活（判断更新 worker 是否还在跑）────────────────
def _pid_alive(pid: object) -> bool:
    try:
        pid_int = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    if os.name == 'nt':
        return _pid_alive_windows(pid_int)
    try:
        os.kill(pid_int, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:  # noqa: BLE001
        return False
    # Linux 额外判僵尸：os.kill(pid, 0) 对 zombie 进程**静默成功**（PID 槽位还在，
    # 只是进程已退出、等待父进程回收）。若锁里记录的 pid 恰好变成 zombie，
    # _lock_active() 会一直判活，界面永久显示「正在更新」。
    try:
        with open(f'/proc/{pid_int}/stat', 'rb') as f:
            raw = f.read()
        # 格式：pid (comm) state ... —— comm 可含空格和右括号，故从最后一个 ')' 后切。
        state = raw.rsplit(b')', 1)[1].split()[0]
        if state == b'Z':
            return False
    except (OSError, IndexError):
        # 读不到 /proc（非 Linux、容器限制等）时退回 os.kill 的结论，保持原行为。
        pass
    return True


def _pid_alive_windows(pid_int: int) -> bool:
    """Windows 探活：OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION) + GetExitCodeProcess。

    为什么不用 os.kill(pid, 0)：在 Windows 上 sig=0 等价 CTRL_C_EVENT，走
    GenerateConsoleCtrlEvent 分支——对**已退出但内核对象尚可打开**的 pid
    静默成功（误判存活）。而「对象还在但已终止」用 GetExitCodeProcess 就能区分
    （退出码 != STILL_ACTIVE）。

    OpenProcess 失败按 GetLastError 区分：
      * 87 (ERROR_INVALID_PARAMETER) / 6 (ERROR_INVALID_HANDLE) → pid 不存在；
      * 5 (ERROR_ACCESS_DENIED) → 打不开但进程多半存在（受保护进程），视为
        存活。QUERY_LIMITED 本就是为低权限探测设计的，实际极少被拒——
        宁可保守判活，也不误清锁放进第二个并发更新。
    """
    import ctypes
    import ctypes.wintypes as wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_INVALID_PARAMETER = 87
    ERROR_INVALID_HANDLE = 6
    if pid_int > 0xFFFFFFFF:  # DWORD 上限之外必不存在（也防 ctypes 溢出报错）
        return False

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_int)
    if not handle:
        err = ctypes.get_last_error()
        if err in (ERROR_INVALID_PARAMETER, ERROR_INVALID_HANDLE):
            return False
        # 其余打不开的情形（含 5）：保守视为存活，避免误判造成并发更新
        return True
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True  # 查询失败：保守视为存活
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


# ── 更新锁（防并发更新）──────────────────────────────────
def _claim_lock() -> bool:
    """原子抢占更新锁：True = 抢到，False = 已有任务 / 无法创建。

    为什么不能「先 _lock_active() 再等 spawn 之后写锁」：两次检查之间的窗口里，
    第二个请求同样能通过检查，于是两个更新进程同时替换 server/。O_CREAT|O_EXCL
    让「检查 + 占用」成为一次原子操作。

    占位先写面板 pid，spawn 成功后改写为 worker pid —— `_lock_active` 按
    「pid 存活 + mtime 未超 TTL」判活，占位只是极短窗口内的安全值。
    """
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 顺手清掉已失效的旧锁（进程没了 / 超过 TTL）；清理与创建之间仍可能竞争，
    # 所以创建本身必须是原子的。
    _lock_active()
    try:
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    except OSError as exc:  # noqa: BLE001
        logger.warning('无法创建更新锁：%s', _err_text(exc))
        return False
    try:
        os.write(fd, str(os.getpid()).encode())
    finally:
        os.close(fd)
    return True


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _lock_active() -> bool:
    if not LOCK_FILE.is_file():
        return False
    try:
        if time.time() - LOCK_FILE.stat().st_mtime > LOCK_TTL:
            LOCK_FILE.unlink(missing_ok=True)
            return False
        pid = int(LOCK_FILE.read_text(encoding='utf-8').strip() or 0)
    except Exception:  # noqa: BLE001
        return False
    if _pid_alive(pid):
        return True
    LOCK_FILE.unlink(missing_ok=True)
    return False


# ── 状态文件 ─────────────────────────────────────────────
def _write_initial_status(pid: int) -> None:
    """写入更新刚启动时的占位状态（running=true / ok=null）。

    字段口径与 worker 的 _WorkerReporter 对齐：read_status 会把它合并进
    返回值；子进程若瞬间死亡，「running 且 pid 已死」分支会用 _update_landed
    收敛出终止态——初始状态没有 target_version、logs 为空，正好落到
    ok=false「更新进程异常中断」，前端就能停下转圈并显示失败。
    """
    now = int(time.time())
    data = {
        'running': True,
        'ok': None,
        'target': 'manager',
        'step': '已启动更新进程',
        'logs': [],
        'started_at': now,
        'finished_at': None,
        'duration': 0,
        'pid': pid,
    }
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        # 与 _WorkerReporter 相同的「临时文件 + 原子替换」写法，
        # 避免轮询读到写了一半的 JSON
        tmp = STATUS_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        tmp.replace(STATUS_FILE)
    except Exception:  # noqa: BLE001
        # 状态写不出去也不必拦启动：worker 自己还会再写
        pass


def read_status() -> dict:
    """读取进度；附上当前版本与是否正在运行。"""
    status: dict = {
        'available': True,
        'running': False,
        'ok': None,
        'step': '',
        'logs': [],
        'version': current_version(),
    }

    if STATUS_FILE.is_file():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                status.update(data)
                # 进程已消失但状态仍标记运行中 → 需要判断是「真的崩了」还是
                # 「面板重启把更新进程一起带走了」（systemd 默认按 cgroup 收尾）。
                # 此时只要代码已换成目标版本，就说明更新其实成功了。
                if status.get('running') and not _pid_alive(status.get('pid')):
                    status['running'] = False
                    if _update_landed(data):
                        status['ok'] = True
                        status['step'] = '更新完成（服务已重启）'
                        # 被重启带走的进程来不及写结束时间，用状态文件时间兜底，
                        # 否则「上次更新」会一直显示「从未」
                        if not status.get('finished_at'):
                            try:
                                status['finished_at'] = int(STATUS_FILE.stat().st_mtime)
                            except Exception:  # noqa: BLE001
                                pass
                    else:
                        status['ok'] = False
                        status['step'] = '更新进程异常中断'
        except Exception:  # noqa: BLE001
            pass

    if _lock_active():
        status['running'] = True
    # 出口兜底：旧版本写在 update-status.json 里的日志可能带过凭据，读取时统一洗一遍。
    logs = status.get('logs')
    if isinstance(logs, list):
        # 日志条目是 dict（ts/level/text），不能整体 str()：那会把结构变成
        # Python repr 字符串，前端读 l.text 全部是 undefined。只洗 text 字段。
        cleaned = []
        for item in logs:
            if isinstance(item, dict):
                entry = dict(item)
                entry['text'] = redact_credentials(str(entry.get('text') or ''))
                cleaned.append(entry)
            else:
                cleaned.append(redact_credentials(str(item)))
        status['logs'] = cleaned
    return status


def _update_landed(status: dict) -> bool:
    """更新进程消失后，判断这次更新是否其实已经成功。

    面板更新最后一步是重启，该操作可能连带终止更新进程本身，因此
    「进程不在了」多半是正常收尾，而不是崩溃。用两条证据判断：

    1. 状态里记录了目标版本，且部署出来的版本已等于它；
    2. 日志里出现「面板已更新到 X，重启服务以生效」——该行只在
       server/ 与前端静态导出全部替换且依赖装完之后才打印，看到它
       就说明只剩重启这一步，而重启正是导致进程消失的动作。
    """
    cur = current_version().lstrip('vV')

    target = str(status.get('target_version') or '').strip().lstrip('vV')
    if target and cur == target:
        return True

    if not cur:
        return False
    for entry in status.get('logs') or []:
        text = str((entry or {}).get('text') or '')
        m = re.search(r'面板已更新到\s*v?([0-9][0-9.]*)', text)
        if m and m.group(1).strip() == cur:
            return True
    return False


def tail_log(lines: int = 80) -> str:
    """worker 的原始 stdout 尾部（管理员诊断用）。"""
    if not LOG_FILE.is_file():
        return ''
    try:
        content = LOG_FILE.read_text(encoding='utf-8', errors='replace').splitlines()
        tail = '\n'.join(content[-lines:])
        return redact_credentials(tail)
    except Exception:  # noqa: BLE001
        return ''


# ── 离线升级包 ───────────────────────────────────────────
def _read_package_version(path: Path) -> str:
    """从发布包里读出 `.version`（只解那一个成员，不展开整包）。

    上传端点与 worker 各用一次：前者靠它确认「这是本项目的发布包」并回显
    包内版本，后者靠它拿到离线包的版本号（不查 GitHub）。读 64 字节足够。
    """
    try:
        with tarfile.open(path, 'r:gz') as tf:
            for member in tf:
                name = member.name.replace('\\', '/')
                if name.count('/') == 1 and name.endswith('/.version'):
                    fh = tf.extractfile(member)
                    return fh.read(64).decode('utf-8', 'replace').strip() if fh else ''
    except (OSError, tarfile.TarError) as exc:
        raise ValueError(
            f'升级包读不了（{_err_text(exc)}）—— 确认它是发布页面下载的 .tar.gz') from exc
    return ''


def allow_unverified_package() -> bool:
    """是否允许安装**没有发布页摘要**的离线升级包（默认：不允许）。

    为什么默认拒绝：离线包没有签名链，而安装链路本身就能执行任意代码 ——
    包内 `server/requirements.txt` 会在替换代码**之前**被 `pip install -r` 执行
    （可指向任意索引/URL），随后 `server/` 整目录替换并重启进程。也就是说，
    「面板管理员」到「主机代码执行」之间原本只隔着一次摘要核对。
    `QC_ALLOW_UNVERIFIED_PACKAGE=1` 只给确实拿不到发布页摘要的离线环境用。
    """
    value = (os.environ.get('QC_ALLOW_UNVERIFIED_PACKAGE') or '').strip().lower()
    return value in ('1', 'true', 'yes', 'on')


def package_meta() -> dict:
    """已上传升级包的元信息 + 文件是否还在（界面据此决定按钮可不可点）。"""
    raw = db.get_setting(PACKAGE_META_KEY, {}) or {}
    meta = dict(raw) if isinstance(raw, dict) else {}
    meta['present'] = PACKAGE_FILE.is_file()
    if meta['present']:
        try:
            meta['size'] = PACKAGE_FILE.stat().st_size
        except OSError:
            meta['present'] = False
    return meta


def record_package(*, name: str, size: int, sha256: str, version: str,
                   expected_sha256: str = '', uploaded_by: str = '') -> dict:
    meta = {
        'name': name,
        'size': int(size),
        'sha256': sha256,
        # 用户填的「发布页 sha256」：更新时交给 worker 核对；空 = 只做了结构校验
        'expected_sha256': expected_sha256,
        'version': version,
        'uploaded_at': int(time.time()),
        'uploaded_by': uploaded_by,
    }
    db.set_setting(PACKAGE_META_KEY, meta)
    return {**meta, 'present': True}


def clear_package() -> None:
    """删掉升级包与记录（更新完成后、或用户手动清掉时调）。"""
    try:
        PACKAGE_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    db.set_setting(PACKAGE_META_KEY, {})


# ── GitHub Token / 镜像（面板设置优先、环境变量兜底）──────
def github_token() -> str:
    """生效的 GitHub Token：环境变量优先，其次「设置 → 系统」里填的那份。

    未认证的 API 配额只有 60 次/小时/IP，共享出口（TUN/系统代理）时极易被
    同出口的其他人打满，表现为「检测更新」403；认证后为 5000 次/小时。
    """
    for key in ('QC_GITHUB_TOKEN', 'GITHUB_TOKEN'):
        v = (os.environ.get(key) or '').strip()
        if v:
            return v
    try:
        return str(db.get_setting('github_token', '') or '').strip()
    except Exception:  # noqa: BLE001
        return ''


def mask_token(token: str) -> str:
    """只露尾 4 位，供界面回显（**绝不回明文**）。"""
    t = (token or '').strip()
    if not t:
        return ''
    if len(t) <= 4:
        # 短于 5 位的值全遮：照抄「露尾 4 位」会把它整串显示出来
        return '*' * len(t)
    return ('*' * (len(t) - 4)) + t[-4:]


# gh-proxy 这类镜像的用法是「镜像基址 + 完整原始地址」：
#   https://gh-proxy.com/https://api.github.com/repos/<owner>/<repo>/releases/latest
# 只在部署者显式配置时启用 —— 镜像会把请求转给第三方。
GITHUB_PREFIXES = (
    'https://github.com/',
    'https://api.github.com/',
    'https://codeload.github.com/',
    'https://raw.githubusercontent.com/',
    'https://objects.githubusercontent.com/',
)


def normalize_mirror(value: object) -> str:
    """把镜像基址规范成 `https://host/`；非法值返回空串（= 不启用）。

    带查询串 / 片段的地址直接拒绝：拼出来的必须是「基址 + 完整原始地址」，
    多一个 `?` 或 `#` 会让后半截被当成镜像自己的参数。
    """
    raw = str(value or '').strip()
    if not raw or any(c.isspace() for c in raw):
        return ''
    if '://' not in raw:
        raw = 'https://' + raw
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return ''
    if parts.scheme not in ('http', 'https') or not parts.hostname:
        return ''
    # 主机段只允许主机名/IP 字面量会出现的字符：`https://not a url` 这类
    # 输入会被 urlsplit 当成合法 hostname，拼出来的地址必然连不上。
    if not re.fullmatch(r'[A-Za-z0-9._\-:\[\]]+', parts.netloc):
        return ''
    if parts.query or parts.fragment:
        return ''
    return f'{parts.scheme}://{parts.netloc}{parts.path.rstrip("/")}/'


def stored_github_mirror() -> str:
    """面板设置里存的镜像；空串 = 没设过。"""
    try:
        return str(db.get_setting('github_mirror', '') or '').strip()
    except Exception:  # noqa: BLE001
        return ''


def github_mirror() -> str:
    """生效的镜像基址：面板设置优先，其次环境变量 QC_GITHUB_MIRROR。"""
    return normalize_mirror(stored_github_mirror()
                            or os.environ.get('QC_GITHUB_MIRROR') or '')


def _mirror_url(url: str, mirror: str) -> str:
    if not mirror or not url.startswith(GITHUB_PREFIXES):
        return url
    return f'{mirror}{url}'


# ── GitHub JSON 获取（检测与 worker 共用一套候选/回退规则）──
def _fetch_json(url: str, *, timeout: int, token: str, mirror: str,
                opener: urllib.request.OpenerDirector | None = None) -> object:
    """取一个 GitHub JSON；配了镜像就先走镜像，失败再直连。

    **走镜像时不带 Authorization**：镜像会把请求转给第三方，Token 不该为了
    提速离开本机；镜像按自己的出口访问 GitHub，也不需要我们的配额。

    **404 不回退**：镜像明确回答「仓库/Release 不存在」时，直连只会给出同一个
    结论（或更糟 —— 被限流的 403 把真实原因盖掉）。403/429 这类「可能换条路
    就好」的才值得回退。
    """
    candidates = [url]
    mirrored = _mirror_url(url, mirror)
    if mirrored != url:
        candidates.insert(0, mirrored)

    last: BaseException | None = None
    for candidate in candidates:
        headers = {
            'Accept': 'application/vnd.github+json',
            'User-Agent': USER_AGENT,
        }
        if candidate == url and token:
            headers['Authorization'] = f'Bearer {token}'
        req = urllib.request.Request(candidate, headers=headers)
        try:
            op = opener if opener is not None else urllib.request.build_opener()
            with op.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            last = exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # ValueError 涵盖「镜像返回了非 JSON 错误页」；两类都该换下一条候选
            last = exc
    assert last is not None
    raise last


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """不让 urllib 自动跟随 302：我们要读 Location 里的 tag。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def _latest_tag_via_redirect(repo: str) -> str:
    """API 不可用时的回退：读 github.com/.../releases/latest 的 302 Location。

    很多企业网关会 TLS 拦截 api.github.com 并返回 HTML 提示页，但放行
    github.com 的网页与资产下载。这条路径不消耗 API 配额，只需要一个 tag。
    """
    page = f'https://github.com/{repo}/releases/latest'
    req = urllib.request.Request(page, headers={'User-Agent': USER_AGENT})
    try:
        with _NO_REDIRECT_OPENER.open(req, timeout=15):
            raise RuntimeError('releases/latest 没有返回重定向')
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise
        location = str(exc.headers.get('Location') or '')
        tag = ''
        if '/tag/' in location:
            tag = location.split('/tag/', 1)[1].split('?', 1)[0].strip('/')
        if not tag or '/' in tag:
            raise RuntimeError('releases/latest 的 Location 里没有有效 tag')
        return tag


def _gh_get(url: str, timeout: int = 15) -> object:
    """面板侧的 GitHub JSON 获取：面板设置的 token/镜像 + 302 回退。"""
    try:
        return _fetch_json(url, timeout=timeout,
                           token=github_token(), mirror=github_mirror())
    except BaseException as exc:  # noqa: BLE001 —— 回退也失败时保留原始错误
        if '/releases/latest' in url:
            try:
                repo = MANAGER_REPO
                m = re.match(r'https://api\.github\.com/repos/([^/]+)/([^/]+)/releases/latest$',
                             url)
                if m:
                    repo = f'{m.group(1)}/{m.group(2)}'
                tag = _latest_tag_via_redirect(repo)
                return {
                    'tag_name': tag,
                    'html_url': f'https://github.com/{repo}/releases/tag/{tag}',
                    'assets': [],
                    '_fallback': True,   # 没有 assets 信息：assets_ready 保持未知
                }
            except Exception:  # noqa: BLE001
                pass
        raise exc


# ── 版本检测（是否有新版本可更新）────────────────────────
# GitHub 未认证 API 限流为每小时 60 次/IP，因此结果做长缓存，
# 避免每次打开页面都去请求；用户可手动强制刷新。
_VERSION_CACHE_FILE = config.DATA_DIR / 'version-check.json'
VERSION_CACHE_TTL = 6 * 3600          # 6 小时
# 「新版本已发布但产物还没传完」时的短 TTL：这时结论随时会变（维护者传完就好），
# 用 5 分钟换来界面及时恢复。
_ASSETS_PENDING_TTL = 5 * 60

# Release 正文最多回传多少字符（界面折叠展示，不做 Markdown 渲染）
_RELEASE_NOTES_LIMIT = 4000


def _parse_version(v: str) -> tuple[int, ...] | None:
    """把 v1.2.3 / 1.2 解析成可比较的数字元组；含非数字段则返回 None。"""
    s = re.split(r'[-+]', str(v or '').strip().lstrip('vV'), 1)[0]
    parts = [p for p in s.split('.') if p != '']
    if not parts:
        return None
    out: list[int] = []
    for p in parts:
        if not p.isdigit():
            return None
        out.append(int(p))
    return tuple(out)


def _version_newer(remote: str, current: str) -> bool:
    """remote 是否**严格新于** current。

    这里必须是「大于」而不是「不相等」：低于或等于当前版本都不能提示更新，
    否则回滚/降级场景会冒出「v1.0.5 → v1.0.4」这种把降级当更新的提示。
    两端有一个解析不了时返回 False——宁可不提示，也不误报。
    """
    r = _parse_version(remote)
    c = _parse_version(current)
    if r is None or c is None:
        return False
    n = max(len(r), len(c))
    return r + (0,) * (n - len(r)) > c + (0,) * (n - len(c))


def _read_cache() -> dict:
    if not _VERSION_CACHE_FILE.is_file():
        return {}
    try:
        data = json.loads(_VERSION_CACHE_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_cache(data: dict) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        _VERSION_CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    except Exception:  # noqa: BLE001
        pass


def clear_version_cache() -> None:
    """清掉版本检测缓存（更新完成后调：缓存里存的是更新前的 latest，
    留着会让界面拿旧 latest 跟新版本比较，出现把降级当更新的提示）。"""
    try:
        _VERSION_CACHE_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _fetch_remote_versions() -> dict:
    """向 GitHub 查询最新 Release（不做缓存判断）。"""
    result: dict = {
        'checked_at': int(time.time()),
        'manager': {'latest': '', 'url': '', 'assets_ready': None, 'error': '',
                    'release_notes': '', 'published_at': ''},
    }

    try:
        rel = _gh_get(f'https://api.github.com/repos/{MANAGER_REPO}/releases/latest')
        if isinstance(rel, dict):
            result['manager']['latest'] = str(rel.get('tag_name') or '')
            result['manager']['url'] = str(rel.get('html_url') or '')
            body = str(rel.get('body') or '')
            # Release 正文：更新前就能看到「本版本改了什么」，不必等装完再翻
            # 本地 CHANGELOG。只回传纯文本，界面不渲染 Markdown。
            result['manager']['release_notes'] = body[:_RELEASE_NOTES_LIMIT]
            result['manager']['published_at'] = str(rel.get('published_at') or '')
            # 产物是否就位：Release 一般由 CI 连产物一起创建，但「手工建了
            # Release 还没上传产物」也会被 releases/latest 看到 —— 在版本检查
            # 里就说清，别让用户点下去才拿到 404。
            assets = rel.get('assets') or []
            # 302 回退拿不到资产清单：标成未知，不能误报「产物未就位」。
            result['manager']['assets_ready'] = None if rel.get('_fallback') else any(
                str((a or {}).get('name') or '').endswith('.tar.gz')
                for a in assets if isinstance(a, dict))
    except urllib.error.HTTPError as exc:
        result['manager']['error'] = '未找到 Release' if exc.code == 404 else f'HTTP {exc.code}'
    except Exception as exc:  # noqa: BLE001
        result['manager']['error'] = str(exc)[:120]

    return result


def check_updates(force: bool = False) -> dict:
    """检测是否有新版本。结果缓存 6 小时（GitHub 未认证 API 限流较严）。"""
    cache = _read_cache()
    age = time.time() - float(cache.get('checked_at') or 0)
    # 已知「该版本产物还没就位」时用短 TTL：产物一上传（通常几分钟）界面就该恢复
    # 正常，而不是继续拿旧结论劝退用户。
    ttl = (_ASSETS_PENDING_TTL if (cache.get('manager') or {}).get('assets_ready') is False
           else VERSION_CACHE_TTL)
    if force or not cache or age > ttl:
        fresh = _fetch_remote_versions()
        # 保留上次成功结果：临时网络故障不应让界面显示「未知」。
        #
        # 但**必须标记这是旧结果**：把上一次的 latest 当成这次检测的结论，
        # 界面就会在 403 限流时照样报「已是最新版本」—— 把一次失败说成了
        # 结论，用户会以为确实没有新版本。stale/stale_at 让界面能如实说
        # 「这次没拿到，下面是 X 时刻的旧结果」。
        if fresh['manager'].get('error') and (cache.get('manager') or {}).get('latest'):
            old_at = int(cache.get('checked_at') or 0)
            fresh['manager'] = {**cache['manager'], 'error': fresh['manager']['error'],
                                'stale': True, 'stale_at': old_at}
        _write_cache(fresh)
        cache = fresh

    current_manager = current_version()
    m_latest = str((cache.get('manager') or {}).get('latest') or '')
    # 只有远端确实更新才提示；不能只判「不相等」，否则当前版本领先于缓存里的
    # 旧 latest 时会冒出降级提示（如 v1.0.5 → v1.0.4）
    manager_has = bool(m_latest) and _version_newer(m_latest, current_manager)

    return {
        'checked_at': int(cache.get('checked_at') or 0),
        'cached': not force and age <= VERSION_CACHE_TTL,
        'manager': {
            'current': current_manager,
            'latest': m_latest,
            'has_update': manager_has,
            'url': str((cache.get('manager') or {}).get('url') or ''),
            'assets_ready': (cache.get('manager') or {}).get('assets_ready'),
            'release_notes': str((cache.get('manager') or {}).get('release_notes') or ''),
            'published_at': str((cache.get('manager') or {}).get('published_at') or ''),
            'error': str((cache.get('manager') or {}).get('error') or ''),
            # stale=True：这一节的值来自**上一次**成功检测（本次失败了），
            # 界面不能据此下「已是最新」的结论。
            'stale': bool((cache.get('manager') or {}).get('stale')),
            'stale_at': int((cache.get('manager') or {}).get('stale_at') or 0),
            'repo': MANAGER_REPO,
        },
        'has_any': manager_has,
    }


# ── 更新日志（CHANGELOG.md）解析 ─────────────────────────
# 版本标题：## [1.0.15] - 2026-09-13  或  ## [未发布]
_VERSION_RE = re.compile(r'^##\s+\[([^\]]+)\]\s*(?:-\s*(.+))?$')
# 小节标题：### 修复
_SECTION_RE = re.compile(r'^###\s+(.+?)\s*$')
# 条目：- xxx（缩进 2 空格为子条目）
_ITEM_RE = re.compile(r'^(\s*)-\s+(.*)$')

# 最多返回多少个版本（界面是折叠列表，太多无意义）
_MAX_VERSIONS = 30


def _candidate_paths() -> list:
    """按优先级列出可能的日志文件位置（发布包把 CHANGELOG.md 放在根目录）。"""
    return [config.ROOT / 'CHANGELOG.md', config.ROOT / 'server' / 'CHANGELOG.md']


def _strip_inline(text: str) -> str:
    """去掉首尾空白；粗体/行内代码这类内联标记留给前端做轻量处理。"""
    return text.strip()


def parse_changelog(text: str) -> list[dict]:
    """把 CHANGELOG.md 解析成 [{version, date, unreleased, sections:[{title, items:[{level,text}]}]}]。

    不引入 markdown 库：CHANGELOG 的格式是固定的三级结构
    （版本 → 小节 → 条目），解析出来即可。
    """
    versions: list[dict] = []
    cur: dict | None = None
    section: dict | None = None
    # 续行：上一条目的后续行（CHANGELOG 里长条目会换行并缩进）
    pending: dict | None = None

    def flush_pending() -> None:
        nonlocal pending
        pending = None

    for raw in (text or '').splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        m = _VERSION_RE.match(line)
        if m:
            flush_pending()
            name = m.group(1).strip()
            date = (m.group(2) or '').strip()
            cur = {
                'version': name,
                'date': date,
                'unreleased': name in ('未发布', 'Unreleased'),
                'sections': [],
            }
            versions.append(cur)
            section = None
            continue

        if cur is None:
            continue

        m = _SECTION_RE.match(line)
        if m:
            flush_pending()
            section = {'title': m.group(1).strip(), 'items': []}
            cur['sections'].append(section)
            continue

        m = _ITEM_RE.match(line)
        if m and section is not None:
            flush_pending()
            indent = len(m.group(1))
            item = {
                # 缩进 ≥2 视为子条目，前端用小圆点区分
                'level': 1 if indent >= 2 else 0,
                'text': _strip_inline(m.group(2)),
            }
            section['items'].append(item)
            pending = item
            continue

        # 普通文本行：若是上一条目的续行（缩进对齐），拼回去
        if pending is not None and section is not None and line.startswith('  '):
            pending['text'] = f"{pending['text']} {line.strip()}"
            continue
        # 版本间的分隔线或说明文字，忽略
        flush_pending()

    return versions


def load_changelog() -> dict:
    """读取并解析 CHANGELOG.md。读不到时返回 available=False 并说明原因。

    离线环境同样可用：文件随发布包一起分发，无需访问 GitHub。
    """
    candidates = _candidate_paths()
    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        tried = '、'.join(str(p) for p in candidates)
        return {
            'available': False,
            'error': f'未找到更新日志文件（已尝试：{tried}）',
            'versions': [],
        }
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except Exception as exc:  # noqa: BLE001
        return {
            'available': False,
            'error': f'读取更新日志失败（{path}）：{exc}',
            'versions': [],
        }

    versions = parse_changelog(text)
    return {
        'available': True,
        'path': str(path),
        'total': len(versions),
        'versions': versions[:_MAX_VERSIONS],
        'truncated': len(versions) > _MAX_VERSIONS,
    }


# ── 启动更新（面板侧：拉起 worker）───────────────────────
def start_update(*, source: str = 'release') -> tuple[bool, str]:
    """启动更新（后台脱离运行）。返回 (是否已启动, 说明)。

    `source`：
      · `release` —— 从 GitHub 拉最新发布包（默认）；
      · `uploaded` —— 用面板里已上传的离线升级包（内网机器用）。
        文件路径由服务端给出（固定文件名），客户端永远不传路径。
    """
    if source not in ('release', 'uploaded'):
        return False, '参数不合法'
    package_expected = ''
    if source == 'uploaded':
        meta = package_meta()
        if not meta.get('present'):
            return False, '还没有上传升级包（在「离线升级包」里选一个发布包上传后再试）'
        package_expected = str(meta.get('expected_sha256') or '')
        # 早于「摘要必填」的那版面板可能已经存下了一个无摘要的包：这里再拦一次，
        # 不能只靠上传端点（那层只挡新上传）。判定与上传端点同源，见
        # allow_unverified_package 的说明。
        if not package_expected and not allow_unverified_package():
            return False, (
                '这个离线升级包没有发布页 sha256 可供核对，已拒绝安装。'
                '请重新上传并填写发布页上那串 64 位 sha256；'
                '确实无法核对的离线环境，设 QC_ALLOW_UNVERIFIED_PACKAGE=1 后重试。'
            )
    if _lock_active():
        return False, '已有更新任务正在执行'

    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 清理上次状态，避免前端读到旧进度
    STATUS_FILE.unlink(missing_ok=True)

    env = dict(os.environ)
    # 面板设置里配的出口代理 / 更新代理交给 worker：它是个独立进程，只认环境变量
    #（更新器自己的下载只认 http/https，非 http 的写法那边会忽略并记一条日志）。
    stored_http_proxy = config.stored_http_proxy()
    stored_update_proxy = db.get_setting('update_proxy', '') or ''
    if stored_http_proxy:
        env['QC_HTTP_PROXY'] = stored_http_proxy
    if stored_update_proxy:
        env['QC_UPDATE_PROXY'] = str(stored_update_proxy)
    # GitHub Token / 镜像一并传给 worker：它拉 Release / 校验也走 GitHub API，
    # 未认证同样吃 60/h 的限流。空值不传（避免把空变量覆盖掉部署环境里的那份）。
    _token = github_token()
    if _token:
        env['QC_GITHUB_TOKEN'] = _token
    # 走镜像时 worker **不会**把 Token 发出去（见 _fetch_json）。
    _mirror = github_mirror()
    if _mirror:
        env['QC_GITHUB_MIRROR'] = _mirror
    env.update({
        'QC_INSTALL_DIR': str(config.ROOT),
        'QC_MANAGER_PORT': str(config.PORT),
        'QC_DATA_DIR': str(config.DATA_DIR),
        # worker 结束面板进程后由替补进程按同一条命令重启（见 _restart_panel）
        'QC_PANEL_PID': str(os.getpid()),
        'QC_PANEL_ARGV': json.dumps(sys.argv),
        'QC_SERVICE_NAME': os.environ.get('QC_SERVICE_NAME', 'qcoder-manager'),
        # 子进程 stdio 与默认编码强制 UTF-8：中文 Windows 上重定向 stdout 的
        # 默认编码是 cp936（GBK），print('✓'/'⚠️') 会 UnicodeEncodeError 直接
        # 杀死更新进程。
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONUTF8': '1',
    })
    env['PYTHONPATH'] = str(config.ROOT) + (
        os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    if source == 'uploaded':
        # 离线升级包：worker 不查 GitHub，版本号从包里读；用户填过发布页 sha256
        # 的话一并传下去让它核对（没填就只做结构校验，worker 会在日志里说明）。
        env['QC_UPDATE_PACKAGE'] = str(PACKAGE_FILE)
        env.pop('QC_UPDATE_PACKAGE_SHA256', None)
        if package_expected:
            env['QC_UPDATE_PACKAGE_SHA256'] = package_expected

    # 原子抢占：到这里所有前置校验都过了，下一行就是 spawn。抢不到说明另一个
    # 请求刚好抢先（上面的 _lock_active 只是快速失败，两个请求可以同时通过）。
    if not _claim_lock():
        return False, '已有更新任务正在执行'

    logfh = None
    try:
        logfh = open(LOG_FILE, 'ab')
    except OSError:
        logfh = None

    python = sys.executable or shutil.which('python3') or 'python3'
    try:
        proc = subprocess.Popen(
            [python, '-m', 'server.systemupd', '--worker', '--source', source],
            cwd=str(config.ROOT),
            env=env,
            stdout=logfh if logfh is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            **({'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
               if os.name == 'nt' else {'start_new_session': True}),
        )
    except Exception as exc:  # noqa: BLE001
        _release_lock()   # 抢到锁但没起来：必须放掉，否则界面永远显示「已有任务」
        return False, f'启动更新失败：{_err_text(exc)}'
    finally:
        if logfh is not None:
            try:
                logfh.close()
            except OSError:
                pass

    _write_initial_status(proc.pid)

    try:
        # 锁里换成 worker 的 pid：_lock_active 靠它判断「更新进程还在不在」。
        LOCK_FILE.write_text(str(proc.pid), encoding='utf-8')
    except Exception:  # noqa: BLE001
        pass

    return True, f'更新已开始（pid={proc.pid}）'


# ─────────────────────────────────────────────────────────
# 更新 worker（`python -m server.systemupd --worker` 独立进程运行）
# ─────────────────────────────────────────────────────────
def _detach_kwargs() -> dict:
    if os.name == 'nt':
        return {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {'start_new_session': True}


class _WorkerReporter:
    """worker 的进度上报：状态原子写入 STATUS_FILE，日志同时打到 stdout。"""

    def __init__(self) -> None:
        self.start = time.time()
        self.state: dict = {
            'running': True,
            'ok': None,
            'target': 'manager',
            'step': '初始化',
            'logs': [],
            'started_at': int(self.start),
            'finished_at': None,
            'duration': 0,
            'pid': os.getpid(),
        }
        self.flush()

    def log(self, message: str, level: str = 'info') -> None:
        self.state['logs'].append(
            {'ts': int(time.time()), 'level': level, 'text': str(message)[:500]})
        # 终端输出便于命令行单独运行（stdout 已重定向到 update.log）
        print(f'[{time.strftime("%H:%M:%S")}] {message}', flush=True)
        self.flush()

    def step(self, name: str) -> None:
        self.state['step'] = name
        self.log(f'== {name} ==')
        self.flush()

    def finish(self, ok: bool) -> None:
        self.state['running'] = False
        self.state['ok'] = ok
        self.state['finished_at'] = int(time.time())
        self.state['duration'] = round(time.time() - self.start, 1)
        self.flush()

    def set_target_version(self, tag: str) -> None:
        """记录本次要更新到的版本。

        面板重启可能把本进程一并终止（systemd 按 cgroup 收尾）；面板读到
        「运行中但进程已不在」时，用这个版本号核对代码是否已就位，从而区分
        「更新成功、只是被重启带走」与「真的崩了」。
        """
        self.state['target_version'] = str(tag or '').strip().lstrip('vV')
        self.flush()

    def set_integrity(self, status: str, digest: str = '') -> None:
        """记录发布包完整性校验结果，供界面显示。

        status: digest（sha256 与发布页/资产摘要一致）/ none（回退路径拿不到
        摘要，只打印了本地摘要供人工比对）。「这次装的东西有没有被核对过」
        不该只留在日志里。
        """
        self.state['integrity'] = {'status': status, 'digest': digest}
        self.flush()

    def flush(self) -> None:
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATUS_FILE.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding='utf-8')
            tmp.replace(STATUS_FILE)
        except Exception:  # noqa: BLE001
            pass


def _run(cmd: list[str], cwd: Path | None = None, rep: _WorkerReporter | None = None,
         timeout: int = STEP_TIMEOUT, check: bool = True) -> tuple[int, str]:
    """跑一条命令，输出尾部并进更新日志。"""
    if rep:
        rep.log(f'$ {" ".join(cmd)}')
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError as exc:
        if rep:
            rep.log(f'命令不存在: {exc}', 'error')
        if check:
            raise
        return 127, str(exc)
    except subprocess.TimeoutExpired:
        if rep:
            rep.log(f'命令超时（{timeout}s）', 'error')
        if check:
            raise
        return 124, 'timeout'

    out = (proc.stdout or '') + (proc.stderr or '')
    if rep and out.strip():
        for line in out.strip().splitlines()[-30:]:
            rep.log(f'  {line}')
    if proc.returncode != 0 and check:
        raise RuntimeError(f'命令失败（exit {proc.returncode}）: {" ".join(cmd)}')
    return proc.returncode, out


# ── 下载（仅标准库 urllib；更新过程中依赖可能被替换）─────
def _update_proxy() -> str:
    """更新专用出口代理：QC_UPDATE_PROXY 优先，其次面板的 QC_HTTP_PROXY。

    urllib 只认 http/https，非 http 的写法（socks5 等）一律忽略——把 socks5
    交给它只会得到一条看不懂的报错。
    """
    raw = (os.environ.get('QC_UPDATE_PROXY')
           or os.environ.get('QC_HTTP_PROXY') or '').strip()
    if raw and not raw.lower().startswith(('http://', 'https://')):
        return ''
    return raw


def _opener() -> urllib.request.OpenerDirector:
    """按配置构造 opener：设了更新代理就用它，否则用环境变量默认。

    本机与内网不走代理：worker 要读本地状态文件，代理配置不该把内网也带偏。
    """
    handlers: list[urllib.request.BaseHandler] = []
    proxy = _update_proxy()
    if proxy:
        handlers.append(urllib.request.ProxyHandler(
            {'http': proxy, 'https': proxy}))
        os.environ.setdefault('no_proxy', '127.0.0.1,localhost')
        os.environ.setdefault('NO_PROXY', os.environ['no_proxy'])
    handlers.append(urllib.request.HTTPSHandler())
    return urllib.request.build_opener(*handlers)


def _worker_token() -> str:
    return (os.environ.get('QC_GITHUB_TOKEN')
            or os.environ.get('GITHUB_TOKEN') or '').strip()


def _worker_mirror() -> str:
    return normalize_mirror(os.environ.get('QC_GITHUB_MIRROR') or '')


def http_json(url: str, timeout: int = 20) -> object:
    """worker 侧的 GitHub JSON 获取（带代理 opener）。"""
    return _fetch_json(url, timeout=timeout,
                       token=_worker_token(), mirror=_worker_mirror(),
                       opener=_opener())


def _download_once(url: str, dest: Path, rep: _WorkerReporter) -> BaseException | None:
    """下载到 dest；成功返回 None，失败返回最后一次异常（不抛出）。

    timeout=120 是**单次 socket 操作**的超时（只在无数据流动时计时），不是
    总时长——代理链路慢时全量下载远超 120 秒属正常。对偶发网络失败
    （连接重置、响应中断、5xx）自动重试一次；4xx 是请求本身的问题，
    重试不会变好，直接抛出。
    """
    last: BaseException | None = None
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            with _opener().open(req, timeout=120) as resp, open(dest, 'wb') as fh:
                shutil.copyfileobj(resp, fh)
            last = None
            break
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                return exc
            last = exc
        except (OSError, http.client.HTTPException) as exc:
            # URLError/socket.timeout/ConnectionReset 等都是 OSError 子类；
            # IncompleteRead 走 HTTPException。两类都值得再试一次。
            last = exc
        if attempt == 1:
            rep.log(f'下载失败（{last}），自动重试一次…', 'warn')
    if last is not None:
        return last

    size = dest.stat().st_size
    rep.log(f'  完成（{size / 1024 / 1024:.2f} MB）')
    if size < 100_000:
        # 镜像有时会返回一张「仓库不存在 / 请稍后再试」的 HTML 错误页
        return RuntimeError('下载内容异常偏小，可能是错误页面')
    return None


def _download(url: str, dest: Path, rep: _WorkerReporter) -> None:
    """下载 Release 资产到 dest。配了镜像先走镜像，失败再直连——镜像本身
    也会抖动，不能因为它挂了就让整个更新不可用。"""
    mirror = _worker_mirror()
    candidates = [url]
    mirrored = _mirror_url(url, mirror)
    if mirrored != url:
        candidates.insert(0, mirrored)
    last: BaseException | None = None
    for candidate in candidates:
        if candidate != url:
            rep.log(f'经镜像下载 {candidate}')
        else:
            rep.log(f'下载 {candidate}')
        last = _download_once(candidate, dest, rep)
        if last is None:
            return
        if candidate != url:
            rep.log(f'镜像下载失败（{last}），回退直连…', 'warn')
    assert last is not None
    raise last


def _sha256_of(path: Path) -> str:
    """分块算 sha256（发布包十几 MB，分块读更省内存）。"""
    digest = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def verify_download(archive: Path, info: dict,
                    rep: _WorkerReporter | None = None) -> str:
    """核对发布包：文件大小 + sha256 摘要（API/发布页给的），不符即中止。

    为什么保留这两项（而不是「下载完直接装」）：

      · **大小** —— 最常出的事故是**下载被截断**（CDN / 代理链路），截断的
        tar.gz 往往解压到一半才报错，报错位置与真实原因毫无关系；
      · **摘要** —— 防的是「传输途中被换掉」，而且它就是发布页上给用户的
        同一个哈希：想人工复核的人拿它就能对得上。

    回退路径（只有 302 重定向）拿不到摘要，那时打印本地 sha256 供人工比对，
    并**如实记成未比对** —— 不假装校验过。返回本地摘要。
    """
    size = archive.stat().st_size
    expected_size = info.get('pkg_size')
    if isinstance(expected_size, int) and expected_size > 0 and size != expected_size:
        raise RuntimeError(
            f'发布包大小不符，已拒绝安装：下载到 {size} 字节，发布页是 {expected_size} 字节。\n'
            '  通常是下载被中断（网络或代理不稳）—— 再点一次更新即可。'
        )

    digest = _sha256_of(archive)
    expected = str(info.get('pkg_digest') or '')
    if expected.startswith('sha256:'):
        want = expected.split(':', 1)[1].strip().lower()
        if digest != want:
            raise RuntimeError(
                '发布包 sha256 与发布页不一致，已拒绝安装。\n'
                f'  发布页：{want}\n'
                f'  下载到：{digest}\n'
                '  多为下载不完整（重试一次）；若始终不一致，'
                '请到项目 Releases 页面自行核对后再决定是否手动安装。'
            )
        if rep:
            rep.log(f'✓ 发布包校验通过（sha256 {digest}，与发布页一致）')
            rep.set_integrity('digest', digest)
        return digest

    if rep:
        rep.log(f'发布包 sha256：{digest}')
        rep.log('本次未比对发布页摘要（当前走的是回退路径），只校验了下载是否完整', 'warn')
        rep.set_integrity('none', digest)
    return digest


# ── Release 元信息（API 优先、302 回退、离线包不联网）─────
def fetch_release_via_api() -> dict:
    """经 GitHub API 取最新 Release（认证后 5000 次/小时，未认证 60 次/小时/IP）。"""
    rel = http_json(f'https://api.github.com/repos/{MANAGER_REPO}/releases/latest')
    if not isinstance(rel, dict):
        raise RuntimeError('Release 响应格式异常')
    assets = rel.get('assets') or []
    pkg = next((a for a in assets if str(a.get('name', '')).endswith('.tar.gz')), None)
    if not pkg:
        raise RuntimeError('该 Release 未提供 .tar.gz 产物')
    return {
        'tag': rel.get('tag_name') or '',
        'pkg_url': pkg['browser_download_url'],
        # 名字按同名原则净化（Path(...).name 丢掉任何目录成分）：下载地址来自
        # 网络响应，「用远端数据拼本地路径」是路径穿越的同一类错误。
        'pkg_name': Path(str(pkg.get('name') or 'pkg.tar.gz')).name,
        # 下载后据此核对（见 verify_download）：GitHub 给每个资产带 size 与
        # sha256 摘要（形如 `sha256:…`；老响应里可能没有 → 那时只能比对大小）。
        'pkg_size': pkg.get('size'),
        'pkg_digest': pkg.get('digest'),
    }


def fetch_release_via_redirect() -> dict:
    """不走 GitHub API 的回退：releases/latest 的 302 重定向拿最新 tag。

    API 未认证限额只有 60 次/小时/IP，共享出口极易被同出口用户打满，表现为
    偶发 HTTP 403；而 github.com 的网页端与 releases/download 资产下载不受该
    配额限制。资产名按发布惯例 `qcoder-manager-<tag>.tar.gz` 构造。

    **这条路径不走镜像**：gh-proxy 一类加速服务只放行资源下载，对网页正文
    直接返回 403。镜像模式下的主路径是 API，只有 API 完全不可用时才落到这里，
    那时直连 github.com 本来就是唯一选择。
    """
    tag = _latest_tag_via_redirect(MANAGER_REPO)
    base = f'https://github.com/{MANAGER_REPO}/releases/download/{tag}'
    pkg_name = f'qcoder-manager-{tag}.tar.gz'
    # 这条路径拿不到资产的 sha256（只有重定向里的 tag），下载后只校验完整性、
    # 并把本地摘要打进日志供人工比对 —— 见 verify_download。
    return {
        'tag': tag,
        'pkg_url': f'{base}/{pkg_name}',
        'pkg_name': pkg_name,
        'pkg_size': None,
        'pkg_digest': '',
    }


def offline_package_info(rep: _WorkerReporter) -> dict:
    """离线升级包的元信息（不联网）。

    版本号取自包内 `.version` 而不是文件名：文件名谁都能改，而 `.version` 是
    打包时写进去的（面板上传端点也按同一份读，两边口径一致）。
    """
    archive = Path((os.environ.get('QC_UPDATE_PACKAGE') or '').strip())
    if not archive.is_file():
        raise RuntimeError(f'找不到离线升级包：{archive}')
    size = archive.stat().st_size
    if size < 100_000:
        raise RuntimeError('离线升级包过小，不像是完整的发布包（下载可能中断了）')
    try:
        version = _read_package_version(archive)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if not version:
        raise RuntimeError('离线升级包里没有 .version —— 不是本项目的发布包，已中止')
    rep.log(f'使用离线升级包：{archive.name}（{size / 1024 / 1024:.1f} MB，包内版本 {version}）')
    sha = (os.environ.get('QC_UPDATE_PACKAGE_SHA256') or '').strip().lower()
    if not sha:
        rep.log('未提供发布页 sha256：本次只校验包能不能解开、内容结构对不对，'
                '包本身的可信度取决于你从哪拿到它', 'warn')
    return {
        'tag': version,
        'pkg_url': '',
        'pkg_name': archive.name,
        'pkg_size': None,          # 没有独立的「期望大小」（文件就在本机）
        'pkg_digest': f'sha256:{sha}' if sha else '',
        'local_package': str(archive),
    }


def _fetch_release_info(rep: _WorkerReporter, source: str) -> dict:
    """取发布包元信息（**不下载**）。API 优先；失败回退 302 重定向；离线包不联网。"""
    if source == 'uploaded' or (os.environ.get('QC_UPDATE_PACKAGE') or '').strip():
        return offline_package_info(rep)

    api_error = ''
    try:
        return fetch_release_via_api()
    except Exception as exc:  # noqa: BLE001
        api_error = f'{type(exc).__name__}: {exc}'
        rep.log(f'GitHub API 不可用（{api_error}），回退到 releases/latest 重定向…', 'warn')
    if _worker_mirror():
        rep.log(f'已配置 GitHub 镜像：{_worker_mirror()}（下载失败会自动回退直连）')
    try:
        return fetch_release_via_redirect()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f'获取 Release 失败：API {api_error}；回退路径也失败：{exc}') from exc


# ── 解压（严格限制在临时目录内）──────────────────────────
def _safe_extract(tf: tarfile.TarFile, dest: Path) -> None:
    """解压发布包，严格限制在 dest 之内。

    安全（防路径逃逸，属供应链风险）：更新 worker 可能以较高权限运行、包来自
    Release，一旦有人能控制该 Release，包里的路径逃逸就等于任意文件写入。

      1. **符号链接成员一律拒绝** —— 检查阶段链接还没创建，`resolve()` 看不穿
         它；解压时先建 `link -> /任意/目录`，再写 `link/x` 就落到了外面。
      2. 绝对路径与 `..` 直接拒绝（比「算出来再比较」更早、更明确）；
      3. 包含性判断用 `Path.is_relative_to`，不用字符串前缀比较；
      4. 优先用 `filter='data'`（Python 3.12+）由标准库再兜一层。
    """
    base = dest.resolve()
    for member in tf.getmembers():
        if member.issym() or member.islnk():
            raise RuntimeError(f'压缩包含链接成员，已拒绝：{member.name}')
        name = member.name
        if name.startswith('/') or name.startswith('\\') or '..' in Path(name).parts:
            raise RuntimeError(f'压缩包含非法路径：{name}')
        target = (base / name).resolve()
        if target != base and not target.is_relative_to(base):
            raise RuntimeError(f'压缩包含非法路径：{name}')

    try:
        tf.extractall(dest, filter='data')  # Python 3.12+：再由标准库校验一遍
    except TypeError:
        # 老版本 Python 无 filter 参数；上面的逐成员校验已是等价防护
        tf.extractall(dest)
    # 兜底：清掉可能被带进来的特殊权限位
    for root, _dirs, files in os.walk(dest):
        for fn in files:
            p = Path(root) / fn
            try:
                mode = p.stat().st_mode
                if mode & (stat.S_ISUID | stat.S_ISGID):
                    os.chmod(p, stat.S_IMODE(mode) & ~(stat.S_ISUID | stat.S_ISGID))
            except OSError:
                pass


def _release_root(rep: _WorkerReporter, info: dict, tmp: Path) -> Path:
    """下载/就位 + 校验 + 解压发布包，返回包的单一顶层目录。

    校验必须在解压之前：先确认「这个包下全了、没被换过」，再谈包里的内容。
    """
    archive = tmp / info['pkg_name']
    local = str(info.get('local_package') or '')
    if local:
        # 离线升级包：文件就在本机，复制一份到临时目录只是让后续步骤共用
        # 同一条路径（解压、清理都在 tmp 里，不动用户上传的那份原件）。
        shutil.copyfile(local, archive)
        rep.log('离线升级包已就位（跳过下载）')
    else:
        _download(info['pkg_url'], archive, rep)
    verify_download(archive, info, rep)

    rep.log('解压…')
    with tarfile.open(archive, 'r:gz') as tf:
        _safe_extract(tf, tmp)
    roots = [p for p in tmp.iterdir() if p.is_dir()]
    if len(roots) != 1:
        raise RuntimeError('压缩包结构异常（应含单一顶层目录）')
    return roots[0]


# ── 依赖安装（必须在替换代码之前）────────────────────────
def _install_requirements(req: Path, rep: _WorkerReporter) -> None:
    """安装 Python 依赖，失败则抛错。

    依赖必须在**替换代码之前**装：装失败就中止，安装目录一个字节都不动，
    旧版本继续运行。先带 `--break-system-packages`（PEP 668 的系统 Python
    不带它会拒装），不认这个参数的旧 pip 会失败，再不带它重试一次。
    """
    py = sys.executable
    rep.log('检查 / 安装 Python 依赖…')
    code, out = 0, ''
    for extra in (['--break-system-packages'], []):
        code, out = _run([py, '-m', 'pip', 'install', '-q', *extra, '-r', str(req)],
                         rep=rep, check=False)
        if code == 0:
            return
    raise RuntimeError(f'Python 依赖安装失败（exit {code}），已中止更新，当前版本保持不变')


# ── 目录替换（同目录 staging + 原子换名）─────────────────
def swap_dir(incoming: Path, target: Path) -> None:
    """用 incoming 目录**原子替换** target（同目录 staging + os.replace）。

    为什么不能 rmtree + copytree：复制中途失败（磁盘满、权限、被杀）会留下
    不完整的目录，服务再也起不来，而备份不会自动恢复。这里先把新内容完整
    复制到 `target.new`，成功后再交换；交换失败会把旧目录换回去。

    注意：覆盖粒度是**整个目录**，所以调用方给的 incoming 必须是完整的一份
    （发布包解压出来的那份），不能是增量。
    """
    target = Path(target)
    old = target.with_name(target.name + '.old')
    staging = target.with_name(target.name + '.new')
    # 清理上次中断留下的残留（它们已不可信）
    for stale in (old, staging):
        shutil.rmtree(stale, ignore_errors=True)
    shutil.copytree(incoming, staging)
    had_old = target.exists()
    if had_old:
        try:
            os.replace(target, old)
        except OSError as exc:
            # 容器重建后目录可能位于 overlayfs 的 lowerdir，对目录 rename(2)
            # 会返回 EXDEV。退化为「复制成备份 + 删除原目录」：备份完整，
            # 后续换入/回滚逻辑不变。
            if exc.errno != errno.EXDEV:
                raise
            shutil.copytree(target, old)
            shutil.rmtree(target)
    try:
        os.replace(staging, target)
    except Exception:
        if had_old and old.exists():
            os.replace(old, target)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(old, ignore_errors=True)


# ── 重启 ─────────────────────────────────────────────────
def in_container() -> bool:
    """是否运行在容器里（Docker/K8s 等）。

    判据用标准做法：`/.dockerenv` 存在，或 cgroup 里出现容器运行时标识。
    这是**探测**而不是配置项，减少用户配错的概率；`QC_RUN_MODE` 可显式覆盖
    （systemd / container / local / auto）。
    """
    mode = (os.environ.get('QC_RUN_MODE') or 'auto').strip().lower()
    if mode in ('docker', 'container'):
        return True
    if mode in ('systemd', 'host', 'local'):
        return False
    if Path('/.dockerenv').exists():
        return True
    try:
        cg = Path('/proc/1/cgroup').read_text(encoding='utf-8')
        if any(k in cg for k in ('docker', 'containerd', 'kubepods', 'podman')):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _terminate_pid(pid: int) -> None:
    """结束面板进程。Windows 用 taskkill 连子进程树一起收（os.kill 的
    CTRL 事件到不了脱离的控制组）；posix 用 SIGTERM 让 uvicorn 优雅退出。"""
    if pid <= 0:
        return
    if os.name == 'nt':
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                       capture_output=True)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _panel_argv() -> list[str]:
    """面板的启动命令（start_update 存进 QC_PANEL_ARGV 的 sys.argv 快照）。"""
    try:
        raw = json.loads(os.environ.get('QC_PANEL_ARGV') or '[]')
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    return [str(a) for a in raw]


def _relaunch_and_stop(rep: _WorkerReporter, panel_pid: int) -> None:
    """无 systemd 的环境：先安排替补进程，再结束旧面板。

    替补进程（--relaunch）轮询旧面板 pid，等它退出后用**同一条命令**把面板
    重新拉起（新代码此刻已在磁盘上）。先拉替补再杀旧进程，顺序不能反——
    反过来的任何失败都会让面板停在「没人负责重启」的状态。
    """
    argv = _panel_argv()
    if argv:
        try:
            subprocess.Popen(
                [sys.executable, '-m', 'server.systemupd', '--relaunch', str(max(panel_pid, 0))],
                cwd=str(INSTALL_DIR), env=dict(os.environ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                **_detach_kwargs())
            rep.log('已安排替补进程：面板退出后将自动以新代码重启')
        except Exception as exc:  # noqa: BLE001
            rep.log(f'替补进程启动失败（{_err_text(exc)}）：请在更新完成后手动重启面板', 'warn')
    else:
        rep.log('拿不到面板启动命令（QC_PANEL_ARGV 未设置）：请在更新完成后手动重启面板', 'warn')
    rep.log('结束当前面板进程…')
    _terminate_pid(panel_pid)


def _restart_panel(rep: _WorkerReporter) -> None:
    """重启面板使新代码生效。

    几种运行形态的重启方式**本质不同**，必须分开处理：

      * systemd（宿主机安装）：`systemctl restart`。该操作会连同本 worker
        一起终止，所以调用方已先把终态写好（rep.finish(True) 在前）；
      * 容器：**无法自我重启**——由 compose 的 `restart` 策略在进程退出后
        自动拉起，因此这里的做法是结束面板进程，把重启交给编排层；
      * 其它（Windows / 裸进程）：替补进程等旧面板退出后按原命令重启。
    """
    panel_pid = 0
    try:
        panel_pid = int(os.environ.get('QC_PANEL_PID') or 0)
    except ValueError:
        panel_pid = 0
    mode = (os.environ.get('QC_RUN_MODE') or 'auto').strip().lower()

    if mode in ('container', 'docker') or (mode == 'auto' and in_container()):
        rep.log('容器环境：新代码已就位，结束面板进程，由容器重启策略用新代码拉起…')
        rep.log('（容器无法自我重启；若长时间未恢复，请在宿主机执行：docker compose restart）')
        _terminate_pid(panel_pid)
        return

    if os.name != 'nt':
        service = os.environ.get('QC_SERVICE_NAME') or 'qcoder-manager'
        have_systemctl = shutil.which('systemctl') is not None
        if mode == 'systemd' or (mode == 'auto' and have_systemctl):
            code, _ = _run(['systemctl', 'restart', service], rep=rep, check=False)
            if code == 0:
                rep.log('服务已重启')
                return
            if mode == 'systemd':
                raise RuntimeError(
                    f'重启服务失败（systemctl 返回 {code}）。新代码已就位，'
                    f'请在服务器上手动执行：systemctl restart {service}')
            rep.log(f'systemctl 重启未成功（返回 {code}），改用替补进程方式重启', 'warn')

    _relaunch_and_stop(rep, panel_pid)


# ── 更新主体 ─────────────────────────────────────────────
def _update_manager(rep: _WorkerReporter, source: str) -> None:
    """面板自身的更新：取包 → 校验 → 解压 → 装依赖 → 备份 → 替换 → 重启。"""
    rep.step('获取发布包信息')
    info = _fetch_release_info(rep, source)
    tag = str(info['tag'] or '')
    rep.set_target_version(tag)

    if info.get('local_package'):
        # 离线包是用户**显式指定**的：即使版本号与当前相同也继续装（重装/修复
        # 正是它最常见的用法之一），不套用「已是最新就跳过」。
        rep.log('离线升级包：按你指定的包安装，不比较版本新旧')
    else:
        rep.log(f'最新版本：{tag or "(未知)"}')
        if tag and read_local_version() == tag.lstrip('vV'):
            rep.log(f'当前已是最新版本（{tag}）')
            return

    with tempfile.TemporaryDirectory(prefix='qcm-update-') as tmpname:
        tmp = Path(tmpname)
        new_root = _release_root(rep, info, tmp)

        new_server = new_root / 'server'
        new_out = new_root / 'web' / 'out'
        if not (new_server / 'main.py').is_file():
            raise RuntimeError('新包缺少 server/main.py，中止更新')
        if not (new_out / 'index.html').is_file():
            raise RuntimeError('新包缺少 web/out/index.html，中止更新')

        # 依赖必须在**替换代码之前**装（见 _install_requirements 的说明）
        new_req = new_server / 'requirements.txt'
        if new_req.is_file():
            _install_requirements(new_req, rep)

        # 备份当前版本：替换失败时用户还有一份能手工回退的现场
        ts = time.strftime('%Y%m%d-%H%M%S')
        backup = INSTALL_DIR / f'backup-{ts}'
        backup.mkdir(parents=True, exist_ok=True)
        rep.log(f'备份当前版本到 {backup}')
        shutil.copytree(INSTALL_DIR / 'server', backup / 'server', dirs_exist_ok=True)
        if config.STATIC_DIR.is_dir():
            shutil.copytree(config.STATIC_DIR, backup / 'web-out', dirs_exist_ok=True)

        rep.log('替换 server/')
        swap_dir(new_server, INSTALL_DIR / 'server')
        # 清掉旧字节码，避免新进程加载到过期模块
        for pc in (INSTALL_DIR / 'server').rglob('__pycache__'):
            shutil.rmtree(pc, ignore_errors=True)

        rep.log('替换前端静态导出/')
        config.STATIC_DIR.parent.mkdir(parents=True, exist_ok=True)
        swap_dir(new_out, config.STATIC_DIR)

        # 文档同步：界面「更新日志」直接读 CHANGELOG.md，若不同步会一直停在
        # 旧版内容；版本标记则是「当前版本」与更新提醒的依据，必须一并替换。
        for name in ('CHANGELOG.md', 'README.md'):
            src = new_root / name
            if src.is_file():
                shutil.copyfile(src, INSTALL_DIR / name)
                rep.log(f'同步 {name}')
        new_marker = new_root / '.version'
        if new_marker.is_file():
            shutil.copyfile(new_marker, INSTALL_DIR / '.version')
            rep.log(f'更新版本标记：{new_marker.read_text(encoding="utf-8").strip()}')

        rep.log(f'面板已更新到 {tag or "新版本"}，重启服务以生效')

    # 清掉版本检测缓存：缓存里存的是「更新前」查到的 latest，留着会让界面
    # 拿旧 latest 跟新版本比较，出现把降级当更新的提示。
    clear_version_cache()

    # 先把终态落盘，再重启：systemd 按 cgroup 收尾时会连同本 worker 一起终止，
    # 若等重启之后再写状态就永远写不到了。
    rep.finish(True)
    _restart_panel(rep)


def run_worker(source: str) -> int:
    """worker 入口：跑一次完整更新，任何退出路径都把终态落盘。"""
    force_utf8_stdio()
    rep = _WorkerReporter()
    ok = True
    rep.log(f'开始更新（安装目录={INSTALL_DIR}）')
    mirror = _worker_mirror()
    if mirror:
        # 日志里出现它很重要：镜像不是「隐身加速」，用户得知道流量经过了第三方
        rep.log(f'GitHub 镜像已启用：{mirror}（凭据不会发给镜像）')
    proxy = _update_proxy()
    if proxy and not proxy.startswith(('http://', 'https://')):
        rep.log(f'已忽略下载代理（{proxy.split("://", 1)[0]}）：更新器只认 http/https。'
                f'需要更新也走代理时，设 QC_UPDATE_PROXY=http://…；面板自身的代理不受影响', 'warn')
    try:
        _update_manager(rep, source)
    except BaseException as exc:  # noqa: BLE001
        # KeyboardInterrupt（控制台 Ctrl+C/关窗）、SystemExit 等 BaseException
        # 也要捕获并留下终态：只捕 Exception 的话中断会直接穿出去，状态文件
        # 永远停在 running=true，前端就一直显示「正在更新」。
        if isinstance(exc, KeyboardInterrupt):
            rep.log('更新被中断（收到 Ctrl+C / 控制台关闭信号）', 'error')
        else:
            rep.log(f'更新失败：{_err_text(exc)}', 'error')
        ok = False
    finally:
        # 收尾兜底：正常成功路径已在 _update_manager 内 rep.finish(True) 写过
        # （running=false），这里幂等跳过；其余情形在此补写 ok=false。
        if rep.state.get('running'):
            rep.finish(ok)
    rep.log('更新完成' if ok else '更新未完成，请检查上方日志')
    return 0 if ok else 1


def _run_relaunch(pid: int) -> int:
    """替补进程入口：等旧面板退出，然后按原命令把它重新拉起。"""
    force_utf8_stdio()
    deadline = time.time() + 180
    while time.time() < deadline and _pid_alive(pid):
        time.sleep(0.5)
    if _pid_alive(pid):
        # 旧面板迟迟不退（优雅关闭被卡住）：不再等，留给人工处理
        return 1
    # 给端口释放留一点余量，避免新进程 bind 冲突
    time.sleep(1.0)
    argv = _panel_argv()
    if not argv:
        return 2
    install = os.environ.get('QC_INSTALL_DIR') or str(config.ROOT)
    log_path = config.DATA_DIR / 'update-relaunch.log'
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        fh = open(log_path, 'ab')  # noqa: SIM115
    except OSError:
        fh = None
    try:
        subprocess.Popen([sys.executable, *argv], cwd=install, env=dict(os.environ),
                         stdin=subprocess.DEVNULL,
                         stdout=fh if fh is not None else subprocess.DEVNULL,
                         stderr=subprocess.STDOUT,
                         **_detach_kwargs())
    finally:
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='QCoder Manager 更新')
    parser.add_argument('--worker', action='store_true', help='作为更新 worker 运行')
    parser.add_argument('--source', choices=['release', 'uploaded'], default='release',
                        help='更新包来源：GitHub Release 或面板上传的离线包')
    parser.add_argument('--relaunch', type=int, default=0, metavar='PID',
                        help='替补进程：等 PID 退出后重启面板')
    args = parser.parse_args(argv)
    if args.relaunch:
        return _run_relaunch(args.relaunch)
    if args.worker:
        return run_worker(args.source)
    parser.print_help()
    return 2


if __name__ == '__main__':
    raise SystemExit(main())


# ─────────────────────────────────────────────────────────
# 自动更新巡检（settings 键 auto_update；端点在 /api/settings/auto-update）
# ─────────────────────────────────────────────────────────
AUTO_CONFIG_KEY = 'auto_update'
AUTO_STATE_KEY = 'auto_update_state'

MIN_INTERVAL_HOURS = 1
MAX_INTERVAL_HOURS = 7 * 24
# qcoder 只更新面板自身，没有上游容器可连着一起更新
TARGETS = ('manager',)

# 同一版本自动更新失败后，多久内不再自动重试
RETRY_SAME_VERSION_SECONDS = 24 * 3600
# 检测**失败**后的重试间隔：一次网络抖动/限流不该把下一轮检测推到几小时甚至
# 一周后（间隔最大 168 小时）。成功一轮才重新按间隔排。
ERROR_RETRY_SECONDS = 3600

_TICK_SECONDS = 300
_FIRST_DELAY_SECONDS = 120

_auto_task = None  # asyncio.Task | None（注解拆开写避免模块级导入 asyncio）


def unsupported_reason() -> str:
    """当前部署形态不支持网页一键更新时返回原因；支持时返回空串。

    各形态（systemd / 容器 / Windows / 裸进程）都有对应的重启路径（见
    _restart_panel），因此恒为空串；保留这个入口是让设置页与 start_update
    的「前置拒绝」只有一个判断来源，将来新增受限形态时只改这里。
    """
    return ''


def default_config() -> dict:
    return {
        'enabled': False,
        'interval_hours': 6,
        # 默认凌晨 3 点到 6 点（服务器本地时间）才动手：避开白天的请求高峰
        'window_start': 3,
        'window_end': 6,
        # qcoder 只有面板一个更新对象
        'target': 'manager',
    }


def _auto_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _auto_bool(value: object, default: bool) -> bool:
    """严格的布尔收敛。

    不能直接 `bool(value)`：HTTP 客户端把开关写成字符串 `"false"` 时它是 truthy，
    于是「关闭自动更新」的请求反而会**开启**它 —— 这是会让面板无人值守重启的
    开关，必须按字面值判断，认不出来就退回默认（关）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value) if value in (0, 1) else default
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ('1', 'true', 'on', 'yes'):
            return True
        if text in ('0', 'false', 'off', 'no', ''):
            return False
    return default


def normalize_config(raw: object) -> dict:
    """把任意输入收敛成合法配置（未知字段丢弃，越界值夹到边界）。"""
    base = default_config()
    src = raw if isinstance(raw, dict) else {}
    cfg = dict(base)
    cfg['enabled'] = _auto_bool(src.get('enabled'), base['enabled'])
    cfg['interval_hours'] = min(MAX_INTERVAL_HOURS, max(
        MIN_INTERVAL_HOURS, _auto_int(src.get('interval_hours'), base['interval_hours'])))
    for key in ('window_start', 'window_end'):
        cfg[key] = min(23, max(0, _auto_int(src.get(key), base[key])))
    target = str(src.get('target') or base['target'])
    cfg['target'] = target if target in TARGETS else base['target']
    return cfg


def validate_config(raw: object) -> tuple[dict | None, str]:
    """保存前的严格校验：越界直接报错（而不是悄悄夹值），让管理员知道填错了。"""
    if not isinstance(raw, dict):
        return None, '参数不合法'
    interval = _auto_int(raw.get('interval_hours'), -1)
    if not MIN_INTERVAL_HOURS <= interval <= MAX_INTERVAL_HOURS:
        return None, f'检测间隔需在 {MIN_INTERVAL_HOURS}–{MAX_INTERVAL_HOURS} 小时之间'
    for key in ('window_start', 'window_end'):
        v = _auto_int(raw.get(key), -1)
        if not 0 <= v <= 23:
            return None, '时间窗的小时需在 0–23 之间'
    if str(raw.get('target') or 'manager') not in TARGETS:
        return None, '更新对象不合法'
    return normalize_config(raw), ''


def get_config() -> dict:
    return normalize_config(db.get_setting(AUTO_CONFIG_KEY, None))


def save_config(cfg: dict) -> dict:
    clean = normalize_config(cfg)
    db.set_setting(AUTO_CONFIG_KEY, clean)
    return clean


def get_state() -> dict:
    raw = db.get_setting(AUTO_STATE_KEY, None)
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict) -> None:
    db.set_setting(AUTO_STATE_KEY, state)


def server_tz_offset_minutes() -> int:
    """服务器本地时区相对 UTC 的偏移（分钟），界面据此说明时间窗按哪个时区算。"""
    lt = time.localtime()
    # tm_gmtoff 在 CPython 3.6+（含 Windows）的 struct_time 上都有；取不到就退回
    # 0，不能因为读时区把整个设置页打挂（这个值只用于界面说明）。
    return int((getattr(lt, 'tm_gmtoff', 0) or 0) // 60)


# ── 判定（纯函数，便于测试）──────────────────────────────
def in_window(hour: int, start: int, end: int) -> bool:
    """整点时间窗 [start, end)，支持跨午夜；start == end 表示全天。"""
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def check_due(cfg: dict, state: dict, now: float) -> bool:
    """是否到了下一次**强制**检测的时刻。

    优先用 `next_check_at`（检测失败时按短间隔排的下一轮）；旧状态只有
    `last_check_at`，按当前间隔现算，升级后不必再等旧间隔走完。
    """
    next_at = state.get('next_check_at')
    if next_at is not None:
        try:
            return now >= float(next_at)
        except (TypeError, ValueError):
            pass
    last = float(state.get('last_check_at') or 0)
    return now - last >= cfg['interval_hours'] * 3600


def decide(cfg: dict, check: dict, *, hour: int, running: bool, state: dict,
           now: float) -> tuple[str, str]:
    """根据一次检测结果决定动作。返回 (action, reason)。

    action：
      · ``update`` —— 应该现在更新；
      · ``wait``   —— 有新版本但现在不动手（时间窗外 / 产物未就位 / 最近失败过）；
      · ``none``   —— 没有新版本或检测不可信。

    只在**确定**有新版本时动手：检测失败 / 结果是旧缓存（stale）/ 发布产物
    还没上传完（assets_ready=False）都只记录、不更新——宁可晚一轮，不能在
    半成品 Release 上自动升级。
    """
    m = (check or {}).get('manager') or {}
    if m.get('error') or m.get('stale'):
        return 'none', f"检测失败：{m.get('error') or '结果来自旧缓存'}"
    if not m.get('has_update'):
        return 'none', '已是最新版本'
    latest = str(m.get('latest') or '')
    if m.get('assets_ready') is False:
        return 'wait', f'发现 {latest}，但发布产物还没就位'
    if running:
        return 'wait', '已有更新任务正在执行'
    if (state.get('last_attempt_version') == latest
            and now - float(state.get('last_attempt_at') or 0) < RETRY_SAME_VERSION_SECONDS):
        return 'wait', f'{latest} 上次自动更新未成功，24 小时内不再自动重试'
    if not in_window(hour, cfg['window_start'], cfg['window_end']):
        return 'wait', (
            f"发现 {latest}，等待时间窗 {cfg['window_start']:02d}:00–{cfg['window_end']:02d}:00")
    return 'update', f'发现 {latest}，开始自动更新'


def run_once(now: float | None = None, *, force: bool = False) -> dict:
    """执行一轮：到期就检测，满足条件就启动更新。返回本轮写入的状态。

    更新会重启面板，所以这里的一切都以「宁可不动、不可误动」为原则。
    """
    cfg = get_config()
    state = get_state()
    now = time.time() if now is None else now
    if not cfg['enabled'] and not force:
        return state
    blocked = unsupported_reason()
    if blocked:
        state.update(last_action='unsupported', last_message=blocked, pending_version='')
        _save_state(state)
        return state

    # 上一轮自动启动的更新已经落地：清掉失败重试保护
    if state.get('last_attempt_version') and \
            current_version().lstrip('vV') == str(state.get('last_attempt_version')).lstrip('vV'):
        state.pop('last_attempt_version', None)
        state.pop('last_attempt_at', None)

    has_pending = bool(state.get('pending_version'))
    if not force and not check_due(cfg, state, now) and not has_pending:
        return state

    due = force or check_due(cfg, state, now)
    before = json.dumps(state, sort_keys=True, default=str)
    try:
        # 到期才强制刷新；只是「等时间窗」的轮次用缓存即可，不必每 5 分钟打 GitHub
        check = check_updates(force=due)
    except Exception as exc:  # noqa: BLE001
        state.update(last_check_at=now, next_check_at=now + ERROR_RETRY_SECONDS,
                     last_action='error', last_message=str(exc)[:200])
        _save_state(state)
        return state

    if due:
        state['last_check_at'] = now
        manager = (check or {}).get('manager') or {}
        # 检测失败（网络 / 限流）不按管理员设的间隔排下一轮：间隔最大 168 小时，
        # 一次抖动就会让自动更新停摆一周。成功的检测才重新按间隔排。
        failed = bool(manager.get('error') or manager.get('stale'))
        state['next_check_at'] = now + (
            ERROR_RETRY_SECONDS if failed else cfg['interval_hours'] * 3600)
    running = bool(read_status().get('running'))
    hour = time.localtime(now).tm_hour
    action, reason = decide(cfg, check, hour=hour, running=running, state=state, now=now)

    latest = str(((check or {}).get('manager') or {}).get('latest') or '')
    state['pending_version'] = latest if action == 'wait' else ''
    state['last_action'] = action
    state['last_message'] = reason

    if action == 'update':
        ok, message = start_update(source='release')
        state['last_attempt_version'] = latest
        state['last_attempt_at'] = now
        state['last_action'] = 'started' if ok else 'error'
        state['last_message'] = message if not ok else reason
        if ok:
            # 自动动作的审计直接落库：巡检在后台线程里跑，不该依赖 Web 层
            try:
                db.add_audit_log('system', 'auto_update', latest, '自动更新已开始')
            except Exception:  # noqa: BLE001 —— 审计失败不打断更新
                pass
            logger.info('自动更新已开始：%s → %s', current_version(), latest)
        else:
            logger.warning('自动更新未能启动：%s', message)
    # 「等时间窗」的轮次每 5 分钟走一遍，状态没变就不必回写数据库。
    if json.dumps(state, sort_keys=True, default=str) != before:
        _save_state(state)
    return state


async def _auto_loop() -> None:
    await asyncio.sleep(_FIRST_DELAY_SECONDS)
    while True:
        try:
            if get_config()['enabled']:
                # check_updates / start_update 都是阻塞调用（网络 + 子进程），
                # 丢给线程，避免卡住事件循环
                await asyncio.get_running_loop().run_in_executor(None, run_once)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 巡检绝不能因一次异常退出
            logger.warning('自动更新巡检异常: %s', exc)
        await asyncio.sleep(_TICK_SECONDS)


def start_scheduler() -> bool:
    """启动自动更新巡检（幂等）。无事件循环时返回 False（如 CLI 场景）。"""
    global _auto_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if _auto_task is None or _auto_task.done():
        _auto_task = loop.create_task(_auto_loop())
    return True


def stop_scheduler() -> None:
    global _auto_task
    if _auto_task is not None and not _auto_task.done():
        _auto_task.cancel()
