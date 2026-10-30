"""SQLite 存储层：账号、密钥、日志、用量、IP 规则与全局设置。"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import config

logger = logging.getLogger('qcode.db')

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def day_of(ts: int | float | None = None) -> str:
    """把时间戳换算成「哪一天」，全库统一用本地时区。

    必须统一口径：写入用量（bump_usage）与回填用量（backfill_usage_from_logs）
    过去一个用本地日期、一个用 UTC 日期，在 UTC+8 机器上凌晨 00:00-08:00 的调用
    会被算进两个不同的 day，导致回填把同一次调用重复计数。
    展示层（stats 的 today/_since）也用本地日期，故此处一律取本地。
    """
    t = time.time() if ts is None else float(ts)
    return time.strftime('%Y-%m-%d', time.localtime(t))


def day_sql(column: str = 'ts') -> str:
    """在 SQL 里按本地时区取日期的表达式（与 day_of 口径一致）。

    注意 SQLite 的 date(ts,'unixepoch') 是 UTC，不能直接用它——那正是
    之前造成口径不一致的原因。这里用 'unixepoch','localtime' 两个修饰符。

    **当心索引失效**：这个表达式把 `ts` 包在函数里，SQLite 无法再走 `ts` 上的
    索引（查询计划会退化成 SCAN，即全表扫描）。

    所以：**只按天过滤**（`day >= X` / `day = X`）的场景请改用
    `day_start_ts()` 把它换算成时间戳范围（见那里的说明）；只有确实要**按天
    分组聚合**（GROUP BY 日期）时才用它——那种场景没法避免表达式。
    """
    return f"strftime('%Y-%m-%d', {column}, 'unixepoch', 'localtime')"


def hour_of(ts: float | None = None) -> int:
    """本地小时（0-23）。与 day_of 同一口径：**本地时区**，不是 UTC。"""
    t = time.time() if ts is None else float(ts)
    return int(time.strftime('%H', time.localtime(t)))


def hour_sql(column: str = 'ts') -> str:
    """在 SQL 里按本地小时分桶（'2026-09-29 09'）——「今日」按小时聚合用。

    与 day_sql 同样的注意事项：表达式包住列就没法走索引，所以过滤条件仍应写成
    `ts >= ?`（用 day_start_ts / hour_start_ts 换算），只有 GROUP BY 才用它。
    """
    return f"strftime('%Y-%m-%d %H', {column}, 'unixepoch', 'localtime')"


def hour_start_ts(day: str | None = None, hour: int = 0) -> int:
    """某天某小时（本地时区）的**起点时间戳**。day 为 None 时取今天。"""
    d = day or day_of()
    y, m, dd = (int(x) for x in d.split('-'))
    return int(time.mktime((y, m, dd, max(0, min(23, int(hour))), 0, 0, 0, 0, -1)))


def day_start_ts(day: str | None = None) -> int:
    """某一天（本地时区）的**零点时间戳**；day 为 None 时取今天。

    为什么需要它：`day_sql(ts) >= '2026-09-15'` 这种写法会把 `ts` 包在函数里，
    导致 `ts` 上的索引失效、退化成全表扫描。而「本地日期 >= X」与
    「ts >= X 当地零点」在语义上**完全等价**（两者都是「本地日期不早于 X」），
    换成后者就能走索引。

    ## 等价性的边界（已逐行验证过，不要凭直觉改）

    两种写法都只有**下界**、没有上界 —— 也就是说未来的时间戳两边都算进来。
    `day_sql` 的写法看起来像「日期字符串比较」，但因为它同样没有上界，所以
    「本地日期 == 明天」的行在两种口径下都被包含。

    ## 时区

    用 `time.mktime(time.strptime(...))` 按**本地时区**解释 —— 与 `day_sql` 的
    `'localtime'` 修饰符同口径。两者都跟随本机时区，所以部署到不同时区的机器上
    仍然一致（这也是当初统一到本地时区的原因，见 `day_of` 的说明）。
    """
    if day is None:
        day = day_of()
    return int(time.mktime(time.strptime(day, '%Y-%m-%d')))


# days 参数的统一上限。**必须有上限**：超大整数在 SQLite 绑定时会溢出抛错
# （实测 /api/logs?days=999999999999999 返回 500）。db 层是唯一的收敛点，
# 在这里钳一次就覆盖了所有调用方（logs/stats 各处）。
_DAYS_MAX = 3650


def clamp_days(days: int | None) -> int | None:
    """把 days 钳到 [1, _DAYS_MAX]；None/非法值返回 None（= 不按时间过滤）。"""
    if days is None:
        return None
    try:
        d = int(days)
    except (TypeError, ValueError):
        return None
    return min(_DAYS_MAX, max(1, d))


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  label TEXT NOT NULL,
  pat_encrypted TEXT NOT NULL,
  pat_suffix TEXT DEFAULT '',
  upstream_id INTEGER,
  enabled INTEGER DEFAULT 1,
  status TEXT DEFAULT 'unknown',
  last_check_at INTEGER,
  last_check_ok INTEGER,
  last_check_msg TEXT DEFAULT '',
  requests INTEGER DEFAULT 0,
  total_tokens INTEGER DEFAULT 0,
  weight INTEGER DEFAULT 1,
  note TEXT DEFAULT '',
  created_at INTEGER,
  updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_accounts_upstream ON accounts(upstream_id);

CREATE TABLE IF NOT EXISTS api_keys(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT, key_hash TEXT, prefix TEXT, enabled INTEGER DEFAULT 1,
  expires_at INTEGER, max_ips INTEGER DEFAULT 0,
  ip_allowlist TEXT DEFAULT '[]', models TEXT DEFAULT '[]',
  quota INTEGER DEFAULT 0, used_tokens INTEGER DEFAULT 0,
  created_at INTEGER, last_used_at INTEGER, upstream_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_keys_prefix ON api_keys(prefix);

CREATE TABLE IF NOT EXISTS api_key_ips(key_id INTEGER, ip TEXT, first_seen INTEGER,
  PRIMARY KEY(key_id, ip));

CREATE TABLE IF NOT EXISTS request_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, key_id INTEGER, ip TEXT,
  model TEXT, mapped_model TEXT, status INTEGER DEFAULT 0,
  prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
  latency_ms INTEGER DEFAULT 0, first_token_ms INTEGER, ua TEXT, error TEXT,
  stream INTEGER DEFAULT 0, channel TEXT, account TEXT, effort TEXT
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON request_logs(ts);

CREATE TABLE IF NOT EXISTS usage_daily(
  day TEXT, key_id INTEGER, model TEXT,
  requests INTEGER DEFAULT 0, prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
  PRIMARY KEY(day, key_id, model));
CREATE TABLE IF NOT EXISTS usage_hourly(
  day TEXT, hour INTEGER, key_id INTEGER, model TEXT,
  requests INTEGER DEFAULT 0, prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
  PRIMARY KEY(day, hour, key_id, model));
CREATE INDEX IF NOT EXISTS idx_usage_hourly_day ON usage_hourly(day);

CREATE TABLE IF NOT EXISTS audit_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, actor TEXT, action TEXT,
  target TEXT DEFAULT '', detail TEXT DEFAULT '', ip TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(ts);

CREATE TABLE IF NOT EXISTS ip_rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, cidr TEXT, note TEXT, created_at INTEGER);

CREATE TABLE IF NOT EXISTS ip_access_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, ip TEXT, path TEXT,
  blocked INTEGER DEFAULT 0, ua TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS idx_ip_logs_ts ON ip_access_logs(ts);

CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS api_tokens(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, token_hash TEXT, prefix TEXT,
  scope TEXT, enabled INTEGER DEFAULT 1, expires_at INTEGER, created_at INTEGER,
  created_by TEXT, last_used_at INTEGER, last_used_ip TEXT);
CREATE INDEX IF NOT EXISTS idx_tokens_prefix ON api_tokens(prefix);

CREATE TABLE IF NOT EXISTS upstreams(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, base_url TEXT, api_key TEXT DEFAULT '',
  note TEXT DEFAULT '', enabled INTEGER DEFAULT 1, created_at INTEGER, updated_at INTEGER);
"""


def _restrict_db_permissions() -> None:
    """把数据库文件（含 WAL/SHM 伴生文件）收紧到仅属主可读写。

    为什么：库里存着 **API 密钥的哈希与前缀、加密后的 PAT、全部请求日志（含来源
    IP 与 UA）、审计日志**。默认创建的 SQLite 文件是 0644——同主机的其他用户，
    或任何能读到该目录的进程，都能直接读走：密钥前缀可用于针对性爆破，日志则
    暴露调用方与内部拓扑。

    WAL 模式下还有 `-wal` / `-shm` 两个伴生文件，同样含尚未落盘的数据，
    必须一并收紧（只 chmod 主库文件是不够的）。

    Windows 上 chmod 语义有限，失败静默忽略——不影响功能。
    """
    import stat as _stat
    for suffix in ('', '-wal', '-shm'):
        p = Path(str(config.DB_PATH) + suffix)
        try:
            if p.exists():
                os.chmod(p, _stat.S_IRUSR | _stat.S_IWUSR)
        except OSError:
            pass


def _explain_db_open_failure(exc: Exception) -> str:
    """把 `unable to open database file` 翻译成「该怎么做」。

    这个报错的原始信息**完全无法定位**：它既不说哪个路径，也不说为什么。
    实测最常见的原因是**目录/文件的属主不对** —— 容器以非 root 用户运行，
    而 bind mount（`./data:/app/data`）的属主由宿主机决定：docker 首次自动
    创建 `./data` 时归 root，容器内的应用用户就写不进去。
    """
    path = config.DB_PATH
    parent = path.parent
    lines = [f'无法打开数据库文件：{path}']
    reasons: list[str] = []

    try:
        if not parent.exists():
            reasons.append(f'目录不存在：{parent}')
        elif not os.access(str(parent), os.W_OK):
            reasons.append(f'目录不可写：{parent}')
        elif path.exists() and not os.access(str(path), os.W_OK):
            reasons.append(f'数据库文件不可写：{path}')
    except OSError:
        pass

    if reasons:
        lines.append('  原因：' + '；'.join(reasons))
    lines.append(f'  原始错误：{exc}')
    lines.append(
        '  最常见的成因是**目录属主不对**：容器以非 root 用户运行，而 bind mount\n'
        '  （compose 里的 `./data:/app/data`）属主由宿主机决定 —— docker 首次自动\n'
        '  创建该目录时归 root，容器内的应用用户写不进去。宿主机执行一次即可：\n'
        '    chown -R <容器内应用用户> ./data\n'
        '  （在 docker-compose.yml 所在目录执行）\n'
        '  若目录是网络文件系统（NFS/SMB）且不支持属主修改，改用命名卷。'
    )
    return '\n'.join(lines)


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.ensure_dirs()
        try:
            _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute('PRAGMA journal_mode=WAL')
            _conn.execute('PRAGMA synchronous=NORMAL')
            _conn.executescript(SCHEMA)
        except sqlite3.OperationalError as exc:
            # 失败时不要留下半开的连接：否则后续调用会拿到一个不能用的 _conn，
            # 报出更莫名的错误（例如「attempt to write a readonly database」）
            if _conn is not None:
                try:
                    _conn.close()
                except Exception:  # noqa: BLE001
                    pass
                _conn = None
            raise RuntimeError(_explain_db_open_failure(exc)) from exc
        _conn.commit()
        # 建表之后再收紧权限：库文件此刻才确定存在，WAL 伴生文件也在初始化后出现
        _restrict_db_permissions()
        # 升级兜底：usage_hourly 若还是空的（新库自然为空，存量库刚加表）——
        # 用户点开「今日」趋势图会是一片空白，而卡片数字明明有值。用请求日志
        # 补一次今天（幂等、只补今天、失败不影响启动）。
        ensure_hourly_today()
        # 启动时也清一次侧表：低流量部署可能很久攒不够触发周期清理的写入量。
        _prune_side_tables()
    return _conn


def _close_connection_locked() -> None:
    """关掉共享连接。调用方必须持有 `_lock`。

    先做一次 TRUNCATE checkpoint：WAL 模式下未合并的页还在 `-wal` 里，只 close
    不 checkpoint 的话，随后删除伴生文件会连带丢掉最后那批已提交的数据。
    """
    global _conn
    conn, _conn = _conn, None
    if conn is None:
        return
    try:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    except sqlite3.Error:
        pass
    try:
        conn.close()
    except sqlite3.Error:
        pass


def replace_database(staging: Path) -> None:
    """用 *staging* 替换当前数据库，并重新打开共享连接。

    **调用方不得持有 `_lock`**：这里是「先关闭、再改名、再重开」的窗口，
    只在替换的瞬间持锁（毫秒级）；耗时的那部分（把镜像写进临时库）由调用方
    在锁外完成 —— 这正是恢复不再阻塞网关的原因。

    staging 必须是本进程独占的完整 SQLite 库（调用方负责关闭它的连接）。
    """
    staging = Path(staging)
    if not staging.is_file():
        raise FileNotFoundError(f'待替换的数据库不存在：{staging}')

    target = Path(config.DB_PATH)
    old = Path(str(target) + '.swap-old')
    wal = Path(str(target) + '-wal')
    shm = Path(str(target) + '-shm')

    with _lock:
        # 上一次替换在第 1、2 步之间崩溃时，正式库不在、.swap-old 才是最后
        # 一份成功数据。先把它放回去再继续，绝不能当成残骸删掉。
        if not target.exists() and old.exists():
            os.replace(old, target)
        # 清掉上一次异常中断留下的残骸（正常路径下不存在）。
        try:
            old.unlink()
        except FileNotFoundError:
            pass

        _close_connection_locked()
        # 关闭连接时 SQLite 会做最后一次 checkpoint 并删除 WAL。若还剩非空
        # WAL，说明还有未合并的已提交数据（异常关闭/别的连接占着），此时
        # 删掉它就等于丢数据 —— 重开连接、放弃换库，让旧库连同 WAL 原样保留。
        if wal.exists() and wal.stat().st_size > 0:
            connect()
            raise RuntimeError('数据库 WAL 未能合并，已放弃替换以免丢数据')
        # WAL/SHM 必须在连接关闭之后删：Windows 上打开着的句柄不能删，
        # 硬删会抛 WinError 32；POSIX 上虽然能删，但旧 WAL 留在原地更危险。
        for path in (wal, shm):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        moved = False
        try:
            if target.exists():
                os.replace(target, old)
                moved = True
            os.replace(staging, target)
            # 旧库的 WAL/SHM **绝不能**留给新库：文件名相同，SQLite 会把它们
            # 当成新库自己的日志重放，轻则数据对不上，重则直接损坏。
            for path in (wal, shm):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            connect()
            if moved:
                try:
                    old.unlink()
                except OSError:
                    pass
        except Exception:
            # 回滚：关掉可能已经打开的新库，把旧文件放回去再重开。
            # 这一步失败意味着磁盘/权限出了大问题，只能留日志让人工处理。
            _close_connection_locked()
            try:
                if moved:
                    if target.exists():
                        target.unlink()
                    if old.exists():
                        os.replace(old, target)
                connect()
            except Exception:
                logger.exception('数据库替换回滚失败，需要人工检查：%s', target)
            raise


def query(sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _lock:
        return list(connect().execute(sql, tuple(args)).fetchall())


def query_one(sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
    with _lock:
        return connect().execute(sql, tuple(args)).fetchone()


def execute(sql: str, args: Iterable[Any] = ()) -> int:
    with _lock:
        conn = connect()
        cur = conn.execute(sql, tuple(args))
        conn.commit()
        return int(cur.lastrowid or 0)


def executemany(sql: str, seq: Iterable[Iterable[Any]]) -> None:
    with _lock:
        conn = connect()
        conn.executemany(sql, [tuple(x) for x in seq])
        conn.commit()


# ── settings ─────────────────────────────────────────────
def get_setting(key: str, default: Any = None) -> Any:
    row = query_one('SELECT value FROM settings WHERE key = ?', (key,))
    if not row:
        return default
    try:
        return json.loads(row['value'])
    except Exception:
        return default


def set_setting(key: str, value: Any) -> None:
    execute(
        'INSERT INTO settings(key, value) VALUES(?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (key, json.dumps(value, ensure_ascii=False)),
    )


# ── 用量累计 ─────────────────────────────────────────────
def bump_usage(
    key_id: int,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
) -> None:
    """累计当日用量（按密钥 × 模型）。"""
    day = day_of()
    execute(
        'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, completion_tokens) '
        'VALUES(?, ?, ?, 1, ?, ?) '
        'ON CONFLICT(day, key_id, model) DO UPDATE SET '
        '  requests = requests + 1, '
        '  prompt_tokens = prompt_tokens + excluded.prompt_tokens, '
        '  completion_tokens = completion_tokens + excluded.completion_tokens',
        (day, key_id, model, prompt_tokens, completion_tokens),
    )
    # 同一份数据的**小时**维度，给「今日」趋势图用。两处必须一起写：只写一处
    # 就会出现「卡片有数、小时图没有」或反过来的分歧（口径一致性见 day_of）。
    execute(
        'INSERT INTO usage_hourly(day, hour, key_id, model, requests, prompt_tokens, completion_tokens) '
        'VALUES(?, ?, ?, ?, 1, ?, ?) '
        'ON CONFLICT(day, hour, key_id, model) DO UPDATE SET '
        '  requests = requests + 1, '
        '  prompt_tokens = prompt_tokens + excluded.prompt_tokens, '
        '  completion_tokens = completion_tokens + excluded.completion_tokens',
        (day, hour_of(), key_id, model, prompt_tokens, completion_tokens),
    )


def bump_account_usage(account_id: int, tokens: int) -> None:
    """账号维度的用量累计：请求数 +1、token 总量累加（PAT 池调度与展示用）。"""
    execute(
        'UPDATE accounts SET requests = requests + 1, '
        'total_tokens = total_tokens + ?, updated_at = ? WHERE id = ?',
        (int(tokens), int(time.time()), int(account_id)),
    )


def _clean(text: object, limit: int = 500) -> str:
    """把外部文本清成单行：去控制字符并截断。

    为什么必须做：登录取的用户名、网关记的 UA/路径都来自外部输入，若含换行
    就能在日志/审计里**伪造出额外的行**，污染排查与事后追溯。日志是给人看的，
    单行是硬要求。
    """
    t = str(text if text is not None else '')
    # 先按字符过滤控制字符（含 \r \n），再兜底替换残留的转义序列
    t = ''.join(ch for ch in t if ch >= ' ')
    t = t.replace(chr(13), ' ').replace(chr(10), ' ')
    return t[:limit]


def add_audit_log(actor: str, action: str, target: str = '',
                  detail: str = '', ip: str = '') -> None:
    """写一条管理端审计日志。由 security.audit 调用（那里已兜底异常）。"""
    execute(
        'INSERT INTO audit_logs(ts, actor, action, target, detail, ip) VALUES(?, ?, ?, ?, ?, ?)',
        (int(time.time()), _clean(actor, 64), _clean(action, 32),
         _clean(target, 128), _clean(detail, 500), _clean(ip, 64)),
    )


def list_audit_logs(limit: int = 200, offset: int = 0) -> list[dict]:
    rows = query(
        'SELECT * FROM audit_logs ORDER BY id DESC LIMIT ? OFFSET ?',
        (min(1000, max(1, limit)), max(0, int(offset))),
    )
    return [dict(r) for r in rows]


def count_audit_logs() -> int:
    row = query_one('SELECT COUNT(*) AS n FROM audit_logs')
    return int(row['n']) if row else 0


def clear_audit_logs() -> None:
    execute('DELETE FROM audit_logs')


# 入站访问日志（ip_access_logs）保留的最大行数。
#
# 为什么必须有上限：这张表的写入点在网关鉴权**之前**（缺密钥 / 密钥无效
# 都会记一行），也就是**未鉴权可达**。它若既无清洗也无上限、更没有清理机制，
# 任何匿名者都能用「无效 key + 超长 UA」反复写库把磁盘灌满。
#
# 取 2 万行：按每次拒绝一行估算足够回溯近期攻击，占用约几 MB；超出后按最旧丢弃
# （滚动窗口），写入成本是常数级的。
_IP_ACCESS_LOG_MAX = 20000

# 清理的触发间隔：不必每次写入都查一次 COUNT（那是一趟全表扫描）。
# 每 500 次写入检查一次，超限时一次删到上限以下，均摊成本可忽略。
_IP_ACCESS_LOG_CHECK_EVERY = 500
_ip_log_writes = 0


# 请求日志（request_logs）的保留期（天）。
#
# 为什么需要：这张表**每笔请求都写一行**（无条件，区别于用量汇总），若没有任何
# 清理机制会随调用量线性膨胀，`/api/logs` 的查询与页面都会越来越慢。
#
# 取 90 天：统计页最长的展示窗口是 30 天，「修复统计 / 重建统计」也只需覆盖到
# 用量表还有意义的时段；再久的明细对用户没有用途（真要长期留存应导出）。
#
# 注意与 `rebuild_usage_from_logs` 的关系：重建以 request_logs 为唯一依据，
# 删掉 90 天前的日志后，那段时期的用量统计**不能再重建**（现有数据不受影响，
# 只是无法重算）。这是刻意的：90 天前的用量早已定稿，不值得为它永久保留明细。
_REQUEST_LOG_RETAIN_DAYS = 90

# 清理间隔：按行数触发（每 2000 次写入检查一次），与 ip 日志同款思路——
# 不必每次写入都查一次。检查本身用索引列 ts，成本可忽略。
_REQUEST_LOG_CHECK_EVERY = 2000
_request_log_writes = 0


# 侧表保留策略：这几张表以前只增不减。
#   · usage_hourly：每天 × 每密钥 × 每模型 × 24 行，长期跑会拖慢查询与备份；
#   · audit_logs：**未认证路径也能写**（失败令牌会留痕），没有上限等于给了一个
#     填磁盘的入口（请求日志有 90 天保留，审计反而没有）。
_HOURLY_RETAIN_DAYS = 90
_AUDIT_LOG_MAX_ROWS = 20000


def _prune_side_tables() -> None:
    """给只增不减的侧表加保留期 / 条数上限。旁路操作，失败绝不影响主流程。"""
    hourly_cutoff = time.strftime(
        '%Y-%m-%d',
        time.localtime(int(time.time()) - _HOURLY_RETAIN_DAYS * 86400),
    )
    for sql, args in (
        ('DELETE FROM usage_hourly WHERE day < ?', (hourly_cutoff,)),
        # 保留最新 N 行：删除 id <= 第 N+1 新那行的 id；不足 N 行时子查询为 NULL，
        # `id <= NULL` 不成立，什么都不会删。
        ('DELETE FROM audit_logs WHERE id <= (SELECT id FROM audit_logs '
         'ORDER BY id DESC LIMIT 1 OFFSET ?)', (_AUDIT_LOG_MAX_ROWS,)),
    ):
        try:
            execute(sql, args)
        except Exception:  # noqa: BLE001
            pass


def _prune_request_logs() -> None:
    """按保留期清理请求日志（滚动删除最旧的），顺手清理侧表。

    旁路操作：任何失败都不能影响写入本身（更不能影响转发）。
    """
    cutoff = int(time.time()) - _REQUEST_LOG_RETAIN_DAYS * 86400
    try:
        execute('DELETE FROM request_logs WHERE ts < ?', (cutoff,))
    except Exception:  # noqa: BLE001
        pass
    _prune_side_tables()


def add_request_log(**fields: object) -> None:
    """写一条请求日志，并按保留期做滚动清理。

    参数用 kwargs（调用方能按名字传，列多时不易错位），但**传给 SQLite 时必须
    展开成位置元组**：`execute()` 内部做 `tuple(args)`，直接塞 dict 会把它当成
    单个参数，于是键名被当值写进库。这里显式按列序展开。
    """
    global _request_log_writes
    execute(
        'INSERT INTO request_logs(ts, key_id, ip, model, mapped_model, status, '
        'prompt_tokens, completion_tokens, latency_ms, first_token_ms, ua, error, '
        'stream, channel, account, effort) '
        'VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (fields.get('ts'), fields.get('key_id'), fields.get('ip'),
         fields.get('model'), fields.get('mapped_model'), fields.get('status'),
         fields.get('prompt_tokens'), fields.get('completion_tokens'),
         fields.get('latency_ms'), fields.get('first_token_ms'), fields.get('ua'),
         fields.get('error'), fields.get('stream'), fields.get('channel'),
         fields.get('account'), fields.get('effort')),
    )
    _request_log_writes += 1
    if _request_log_writes >= _REQUEST_LOG_CHECK_EVERY:
        _request_log_writes = 0
        _prune_request_logs()


def add_ip_access_log(ip: object, path: object, blocked: bool, ua: object,
                      reason: object = None) -> None:
    """写入一条入站访问日志：清洗 + 截断 + 行数上限。

    清洗与截断是**硬要求**（不只是节省空间）：`ua` / `path` / `reason` 来自
    外部输入，含换行就能在日志界面上伪造出额外行，污染事后排查（审计表一直
    这么做，见 `add_audit_log`）。

    reason 是**被拦的原因**：只有 blocked 布尔时，界面上的「已拦截」无法区分
    「没带密钥」「密钥不认识」「IP 规则拦的」——三者的处置方式完全不同。
    """
    global _ip_log_writes
    # 空原因存 NULL 而不是空串：读的那一侧要区分「没记原因」（历史记录 / 放行）
    # 与「记了一个空字符串」，两者在库里的语义不同。
    cleaned_reason = _clean(reason, 200) or None
    execute(
        'INSERT INTO ip_access_logs(ts, ip, path, blocked, ua, reason) '
        'VALUES(?, ?, ?, ?, ?, ?)',
        (int(time.time()), _clean(ip, 64), _clean(path, 256),
         1 if blocked else 0, _clean(ua, 512), cleaned_reason),
    )
    _ip_log_writes += 1
    if _ip_log_writes < _IP_ACCESS_LOG_CHECK_EVERY:
        return
    _ip_log_writes = 0
    try:
        row = query_one('SELECT COUNT(*) AS n FROM ip_access_logs')
        n = int(row['n']) if row else 0
        if n > _IP_ACCESS_LOG_MAX:
            # 删掉最旧的一批，留出余量（避免下一次写入又立刻触发清理）
            execute(
                'DELETE FROM ip_access_logs WHERE id IN ('
                '  SELECT id FROM ip_access_logs ORDER BY id LIMIT ?'
                ')',
                (n - _IP_ACCESS_LOG_MAX + _IP_ACCESS_LOG_MAX // 10,),
            )
    except Exception:  # noqa: BLE001
        # 清理是旁路，失败不能影响写入本身（更不能影响转发）
        pass


# ── 用量回填 ─────────────────────────────────────────────
# 归一化表达式：**必须同时**用在 SELECT 与 GROUP BY，且空串要与 Python 的
# `or ''` 同一规则。为什么这么讲究，见 rebuild_usage_from_logs 的长注释
# （GROUP BY 复用别名会绑定到源列 → 撞 usage_daily 主键 → 接口 500）。
# 收成常量是为了让「按天」与「按小时」两条回填路径共用同一份规则 —— 分别写
# 就会漂移。
_USAGE_MODEL_EXPR = "COALESCE(NULLIF(model,''),'')"


def _usage_expected_rows(bucket_expr: str) -> list[sqlite3.Row]:
    """按 `bucket_expr`（`day_sql('ts')` 或 `hour_sql('ts')`）聚合请求日志。

    返回的每一行是「这个桶 × 密钥 × 模型」应有的用量 —— 回填与重建都以
    它为准（请求日志是唯一的原始事实）。
    """
    return query(
        f"SELECT {bucket_expr} AS bucket, key_id, "
        f"{_USAGE_MODEL_EXPR} AS model, "
        "COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS pt, "
        "COALESCE(SUM(completion_tokens),0) AS ct "
        "FROM request_logs WHERE key_id IS NOT NULL "
        f"GROUP BY {bucket_expr}, key_id, {_USAGE_MODEL_EXPR}"
    )


def backfill_usage_from_logs() -> dict:
    """把 request_logs 里尚未计入 usage_daily 的用量补进统计。

    用途：修复历史缺陷（曾因统计函数缺失，导致部分调用的用量没有累计）。
    以「已记录的调用」推算应有用量，再把差额写入 usage_daily，
    因此可重复执行而不会重复计数。
    """
    # 应有用量（按天 × 密钥 × 模型）
    # 必须与 bump_usage 用同一时区口径（本地），否则凌晨的调用会被算成两天
    #
    # 归一化表达式同样要**同时**用在 SELECT 与 GROUP BY，且空串也要折成默认值
    # （原因见 rebuild_usage_from_logs 里的说明：GROUP BY 复用别名会绑定到源列；
    #   SQL 的 COALESCE 不处理空串、Python 的 `or` 会 —— 两边规则必须一致）。
    model_expr = _USAGE_MODEL_EXPR
    day_expr = day_sql('ts')
    expected = query(
        f"SELECT {day_expr} AS day, key_id, {model_expr} AS model, "
        "COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS pt, "
        "COALESCE(SUM(completion_tokens),0) AS ct "
        f"FROM request_logs WHERE key_id IS NOT NULL "
        f"GROUP BY {day_expr}, key_id, {model_expr}"
    )
    current = {
        (r['day'], r['key_id'], r['model']): r
        for r in query(
            'SELECT day, key_id, model, requests, prompt_tokens, '
            'completion_tokens FROM usage_daily'
        )
    }

    fixed = 0
    added_requests = added_tokens = 0
    for row in expected:
        key = (row['day'], row['key_id'], row['model'])
        cur = current.get(key)
        cur_req = int(cur['requests']) if cur else 0
        cur_pt = int(cur['prompt_tokens']) if cur else 0
        cur_ct = int(cur['completion_tokens']) if cur else 0

        d_req = int(row['requests']) - cur_req
        d_pt = int(row['pt']) - cur_pt
        d_ct = int(row['ct']) - cur_ct
        if d_req <= 0 and d_pt <= 0 and d_ct <= 0:
            continue
        execute(
            'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, completion_tokens) '
            'VALUES(?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(day, key_id, model) DO UPDATE SET '
            '  requests = MAX(requests, excluded.requests), '
            '  prompt_tokens = MAX(prompt_tokens, excluded.prompt_tokens), '
            '  completion_tokens = MAX(completion_tokens, excluded.completion_tokens)',
            (row['day'], row['key_id'], row['model'], int(row['requests']),
             int(row['pt']), int(row['ct'])),
        )
        fixed += 1
        added_requests += max(0, d_req)
        added_tokens += max(0, d_pt) + max(0, d_ct)

    # 小时维度用同一套规则再补一遍（「今日」趋势图读这张表）。与上面**同源同规则**：
    # 请求日志 → 应有值 → 与现值比差额 → MAX 写入，所以重复执行不会重复计数。
    fixed_h = _backfill_hourly_from_logs()

    return {
        'repaired': fixed,
        'repaired_hourly': fixed_h,
        'requests': added_requests,
        'tokens': added_tokens,
    }


def _backfill_hourly_from_logs() -> int:
    """按小时回填 usage_hourly（幂等，MAX 语义，与按天那条同一规则）。返回修复桶数。"""
    expected = _usage_expected_rows(hour_sql('ts'))
    current = {
        (r['day'], r['hour'], r['key_id'], r['model']): r
        for r in query('SELECT day, hour, key_id, model, requests, prompt_tokens, '
                       'completion_tokens FROM usage_hourly')
    }
    fixed = 0
    for row in expected:
        bucket = str(row['bucket'] or '')
        if len(bucket) < 13:            # 'YYYY-MM-DD HH' —— 形态不对就跳过（不猜）
            continue
        day, hour = bucket[:10], int(bucket[11:13])
        key = (day, hour, row['key_id'], row['model'])
        cur = current.get(key)
        cur_req = int(cur['requests']) if cur else 0
        cur_pt = int(cur['prompt_tokens']) if cur else 0
        cur_ct = int(cur['completion_tokens']) if cur else 0
        if (int(row['requests']) <= cur_req and int(row['pt']) <= cur_pt
                and int(row['ct']) <= cur_ct):
            continue
        execute(
            'INSERT INTO usage_hourly(day, hour, key_id, model, requests, prompt_tokens, '
            'completion_tokens) VALUES(?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(day, hour, key_id, model) DO UPDATE SET '
            '  requests = MAX(requests, excluded.requests), '
            '  prompt_tokens = MAX(prompt_tokens, excluded.prompt_tokens), '
            '  completion_tokens = MAX(completion_tokens, excluded.completion_tokens)',
            (day, hour, row['key_id'], row['model'], int(row['requests']),
             int(row['pt']), int(row['ct'])),
        )
        fixed += 1
    return fixed


def ensure_hourly_today() -> bool:
    """今天的小时表若还是空的，就用请求日志补一次（幂等）。

    为什么需要它：新库刚建好或用量表被重置后，小时表可能一行都没有 —— 用户点开
    「今日」趋势图会看到一片空白，而卡片数字明明有值（那正是最不想看到的观感）。
    只补**今天**：它只被「今日」用到，补历史没有意义；代价是一次聚合查询。
    返回是否真的补了。
    """
    try:
        today = day_of()
        if query_one('SELECT 1 FROM usage_hourly WHERE day = ? LIMIT 1', (today,)):
            return False
        if not query_one('SELECT 1 FROM request_logs WHERE ts >= ? LIMIT 1',
                         (day_start_ts(today),)):
            return False                # 今天还没有任何请求（或日志被清过）：无从补起
        _backfill_hourly_from_logs()
        return True
    except Exception:                   # noqa: BLE001
        return False                    # 旁路操作：失败不影响启动


def hourly_rows(day: str) -> list[dict]:
    """某天**按小时**的用量（`/api/stats/hourly` 用）。"""
    return query(
        'SELECT hour, SUM(requests) AS requests, '
        'SUM(prompt_tokens) AS prompt_tokens, '
        'SUM(completion_tokens) AS completion_tokens '
        'FROM usage_hourly WHERE day = ? GROUP BY hour ORDER BY hour ASC',
        (day,),
    )


def rebuild_usage_from_logs() -> dict:
    """以请求日志为准**重建**用量统计（会替换 usage_daily 的内容）。

    用途：修复历史时区口径不一致造成的污染——凌晨的调用曾被同时算进
    本地日与 UTC 日两行，导致总量偏高。回填（MAX 语义）只能补缺口、
    无法删除多出来的行，因此需要一次重建。

    注意：本操作以 request_logs 为唯一依据。若请求日志曾被清空，
    那部分历史汇总会随之丢失（接口上已明确标注）。
    """
    # 归一化表达式必须**同时**用在 SELECT 与 GROUP BY 上，不能只在 SELECT 里写
    # 别名、GROUP BY 里复用别名。
    #
    # 原因（线上 bug 形态）：SQLite 解析 `GROUP BY model` 时，因为 FROM 的表里
    # **也有**名为 model 的列，该名字绑定到**源列**而不是输出别名
    # `COALESCE(model,'')`。于是 `model IS NULL` 与 `model=''` 被分成两组，但
    # 两组的输出值都是 '' —— 随后 INSERT 就撞上 usage_daily 的主键
    # (day,key_id,model)，报 `UNIQUE constraint failed`，接口 500。
    #
    # 历史日志里 model 为 NULL/空串是常态，所以这不是理论风险。
    #
    # 第二处必须对齐：**空串也要归一**。SQL 的 COALESCE 只处理 NULL，而 Python
    # 的 `x or ''` 连空串一起兜住 —— 两边规则不同就会出现「SQL 分成两组、写库时
    # 都变成空串」的第二次撞键。所以 SQL 侧用 NULLIF 把空串也折成 NULL，
    # 与 Python 的 `or ''` 完全一致。
    model_expr = _USAGE_MODEL_EXPR
    day_expr = day_sql('ts')
    expected = query(
        f"SELECT {day_expr} AS day, key_id, {model_expr} AS model, "
        "COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS pt, "
        "COALESCE(SUM(completion_tokens),0) AS ct "
        f"FROM request_logs WHERE key_id IS NOT NULL "
        f"GROUP BY {day_expr}, key_id, {model_expr}"
    )
    before = query_one('SELECT COUNT(*) AS c, COALESCE(SUM(requests),0) AS r, '
                       'COALESCE(SUM(prompt_tokens+completion_tokens),0) AS t FROM usage_daily')
    # 删除与重建必须在**同一个事务**里。若是「先 DELETE（已提交）再逐条 INSERT」，
    # 一旦插入中途失败，统计表已经被清空、只剩半份数据，而 request_logs 完好 ——
    # 用户看到的就是「今天有 N 次调用、统计却是 0」，且**每次点重建都在继续破坏
    # 数据**。原子化之后，失败就整体回滚，原有的统计原样保留（宁可暂时不准，
    # 也不能把仅有的数据弄丢）。
    conn = connect()
    with _lock:
        try:
            conn.execute('BEGIN')
            conn.execute('DELETE FROM usage_daily')
            conn.executemany(
                'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, '
                'completion_tokens) VALUES(?, ?, ?, ?, ?, ?)',
                [(row['day'], row['key_id'], row['model'], int(row['requests']),
                  int(row['pt']), int(row['ct'])) for row in expected],
            )
            # 小时维度与按天**同一事务**重建：只重建一处会让「今日」图与卡片
            # 在重建后对不上，而那正是分两张表要避免的事。
            conn.execute('DELETE FROM usage_hourly')
            rows_h = _usage_expected_rows(hour_sql('ts'))
            conn.executemany(
                'INSERT INTO usage_hourly(day, hour, key_id, model, requests, prompt_tokens, '
                'completion_tokens) VALUES(?, ?, ?, ?, ?, ?, ?)',
                [(str(row['bucket'])[:10], int(str(row['bucket'])[11:13]), row['key_id'],
                  row['model'], int(row['requests']), int(row['pt']), int(row['ct']))
                 for row in rows_h if len(str(row['bucket'] or '')) >= 13],
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    after = query_one('SELECT COUNT(*) AS c, COALESCE(SUM(requests),0) AS r, '
                      'COALESCE(SUM(prompt_tokens+completion_tokens),0) AS t FROM usage_daily')
    return {
        'rows_before': int(before['c']) if before else 0,
        'rows_after': int(after['c']) if after else 0,
        # 差值可正可负：负数说明此前确实被重复计数了
        'requests_delta': int(after['r'] or 0) - int(before['r'] or 0),
        'tokens_delta': int(after['t'] or 0) - int(before['t'] or 0),
    }
