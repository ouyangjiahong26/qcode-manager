"""用量统计聚合。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends

from .. import db, security

router = APIRouter(prefix='/api/stats', tags=['stats'])


# days 的取值范围。**必须有上限**：超大整数在 SQLite 绑定时溢出抛错。
# 10 年足够覆盖任何正常查询，同时避免溢出与全表扫描。
_DAYS_MAX = 3650


def _clamp_days(days: int | None, default: int) -> int:
    """把 days 钳到 [1, _DAYS_MAX]；非法值退回默认。"""
    try:
        d = int(days) if days is not None else default
    except (TypeError, ValueError):
        return default
    return min(_DAYS_MAX, max(1, d))


def _since(days: int) -> str:
    """这段时间范围的**起始日期**（本地时区，含首日）。

    语义是「近 N 天」且**含今天**：`days=1` 正好就是「今天一天」（起始日 = 今天），
    `days=7` 是今天与前 6 天。所以界面上的「今日」传 1 就够，不必另开参数——
    趋势图、按模型、按密钥三处与时段选择器因此天然同一口径。
    """
    d = _clamp_days(days, 30)
    return time.strftime('%Y-%m-%d', time.localtime(time.time() - (d - 1) * 86400))


def _epoch_since(days: int) -> int:
    """与 `_since` 同一口径（本地时区、含今天）的起始时刻（epoch 秒）。"""
    return int(time.mktime(time.strptime(_since(days), '%Y-%m-%d')))


def _throughput_by_model(days: int) -> dict[str, dict]:
    """按模型算**输出速率**（tokens/秒）。

    数据源必须是 `request_logs`：`usage_daily` 只累计请求数与 token 数，没有耗时。

    口径（写清楚是因为「tok/s」很容易被算成好看但没意义的数）：

      · 生成耗时 = `latency_ms − first_token_ms`。流式请求的 first_token 是首字
        延迟，两者相减正好是「开始吐字到结束」；非流式没有中间过程，first_token
        为 NULL，用整个 latency（略低估，但不会凭空造数）；
      · 只统计成功（2xx）、有输出 token、耗时为正的记录 —— 失败请求的 latency
        含重试/排队，算进去会把速率压低成噪声；
      · 只覆盖**请求日志保留期**内的数据，返回 `throughput_samples` 让界面如实
        标注样本量（统计页的时间范围可能比日志保留期长）。
    """
    since = _epoch_since(days)
    rows = db.query(
        'SELECT model AS name, COUNT(*) AS samples, '
        'SUM(completion_tokens) AS ct, '
        'SUM(CASE WHEN latency_ms > COALESCE(first_token_ms, 0) '
        '         THEN latency_ms - COALESCE(first_token_ms, 0) ELSE 0 END) AS gen_ms '
        'FROM request_logs '
        'WHERE ts >= ? AND status >= 200 AND status < 300 AND completion_tokens > 0 '
        # 生成耗时必须为正：latency=0 或 latency<=first_token 的记录算不出速率，
        # 留在分母外也不该把它们的 token 计进分子（否则速率会虚高一倍）。
        'AND latency_ms > COALESCE(first_token_ms, 0)'
        ' GROUP BY model',
        (since,),
    )
    out: dict[str, dict] = {}
    for r in rows:
        name = r['name'] or '未知'
        gen_ms = float(r['gen_ms'] or 0)
        ct = float(r['ct'] or 0)
        out[name] = {
            'tokens_per_sec': round(ct / (gen_ms / 1000.0), 2) if gen_ms > 0 and ct > 0 else None,
            'throughput_samples': int(r['samples'] or 0),
        }
    return out


@router.get('/summary')
def summary(user: dict = Depends(security.current_user)) -> dict:
    """总览。

    附带的 `usage_health` 用于暴露「统计根本没在累计」这种情况：统计写入是
    旁路（失败只记 warning，不影响转发），一旦写入路径坏了，页面照常轮询、
    数字只是停着不动，用户侧完全静默。判据是**今天的请求日志有内容但今天的
    统计为 0**——正常部署不该出现这种组合。详见 `_usage_health`。

    另外附带 `failures`（失败请求数，按 4xx / 5xx 分档）。**它单独取自
    request_logs，不能从 usage_daily 算**——后者只累计有 token 的请求，
    被拒绝的调用与全池不可用（503，零 token）在里面**根本不存在**：全池中断
    那天的趋势图会显示为「没有请求」，用户只能去翻日志才知道出过事。
    """
    today = time.strftime('%Y-%m-%d')
    week = _since(7)

    def agg(where: str, args: tuple) -> tuple[int, int]:
        row = db.query_one(
            f'SELECT COALESCE(SUM(requests),0) AS r, '
            f'COALESCE(SUM(prompt_tokens + completion_tokens),0) AS t '
            f'FROM usage_daily WHERE {where}',
            args,
        )
        return int(row['r']), int(row['t'])

    t_req, t_tok = agg('day = ?', (today,))
    w_req, w_tok = agg('day >= ?', (week,))
    a_req, a_tok = agg('1=1', ())

    active_keys = db.query_one('SELECT COUNT(*) AS c FROM api_keys WHERE enabled = 1')['c']
    top = db.query_one(
        'SELECT model, SUM(requests + prompt_tokens + completion_tokens) AS score '
        'FROM usage_daily WHERE 1=1 GROUP BY model ORDER BY score DESC LIMIT 1',
    )
    return {
        'today_requests': t_req,
        'today_tokens': t_tok,
        'week_requests': w_req,
        'week_tokens': w_tok,
        'total_requests': a_req,
        'total_tokens': a_tok,
        'active_keys': int(active_keys),
        'top_model': top['model'] if top else None,
        'usage_health': _usage_health(today, t_req),
        'failures': _failures(),
    }


def _failures() -> dict:
    """今天 / 近 7 天的失败请求数，按 4xx 与 5xx 分档。

    为什么必须单独算：`usage_daily` 只累计**有 token** 的请求（`bump_usage` 的
    调用条件是 `if total`，刻意如此——否则「只有被拒绝的调用」的部署会被
    `_usage_health` 误报成「统计没在累计」）。代价是失败请求在用量表里
    **完全不存在**：

      · 403 / 429（密钥被拒、配额用尽、限流）→ 零 token，不累计；
      · 503（池里没有可用账号）→ 零 token，不累计；
      · 502（上游不可用）→ 同样。

    于是界面上「请求数」只反映成功的量，趋势图里失败的那段是**空白**——用户
    看不到"出过事"，只能去翻日志。

    `request_logs` 是**无条件**记录每一笔的，所以它是失败数的唯一可靠来源。
    这里不做任何「是否该累计」的过滤：凡是被拒或被上游打回的都是失败，
    正是用户想看到的。

    口径说明：只统计**带了密钥**的请求（`key_id IS NOT NULL`）。没有密钥的
    401 是扫描器/配置错误打进来的噪声，混进来会让失败数失去信号价值。
    """
    out: dict = {
        'today_4xx': 0, 'today_5xx': 0,
        'week_4xx': 0, 'week_5xx': 0,
    }
    try:
        today = time.strftime('%Y-%m-%d')
        week = _since(7)
        for label, since in (('today', today), ('week', week)):
            # 用 `ts >= 当地零点` 而不是 `day_sql(ts) >= 日期串`：两者逐行等价，
            # 但后者把 ts 包在函数里，查询计划退化成 SCAN（扫整个索引），前者是
            # SEARCH（按范围定位）。本接口在总览页被 30 秒轮询，是热路径。
            row = db.query_one(
                'SELECT SUM(CASE WHEN status >= 400 AND status < 500 THEN 1 ELSE 0 END) AS c4, '
                'SUM(CASE WHEN status >= 500 THEN 1 ELSE 0 END) AS c5 '
                'FROM request_logs WHERE ts >= ? AND key_id IS NOT NULL',
                (db.day_start_ts(since),),
            )
            if row:
                out[f'{label}_4xx'] = int(row['c4'] or 0)
                out[f'{label}_5xx'] = int(row['c5'] or 0)
    except Exception:  # noqa: BLE001
        # 统计属旁路：任何一个数算不出来都不该让总览接口失败（失败计数只是
        # 锦上添花，总览本身有更多关键数字）。返回全 0，前端按「无失败」显示。
        pass
    return out


def _usage_health(today: str, today_requests: int) -> dict:
    """检测「用量统计没有在累计」——本该累计的请求有日志、但统计为 0。

    **判据必须与「什么请求才会累计用量」完全对齐**，否则会误报：
    `request_logs` 是**无条件**写的，而 `bump_usage` 只在 `key 且 tokens 非 0`
    时调用。于是「今天只有被拒绝的调用」（403 / 429 / 无 key）或「只调了
    /v1/models」（0 token）会留下日志却没有用量行——那种部署**完全健康**，
    却会被报「统计可能没有正常写入」。

    所以这里只数**本该产生用量行**的那部分日志：有 key、且 token 非 0。
    这与 gateway._record 的判断条件是同一份口径；那边改了这里也要跟着改。

    返回里带上结构化数字，**不要让前端去正则解析中文句子**（曾经这么做，
    改文案时前端静默失效、译文形同虚设）。
    """
    try:
        # 只数「本该累计」的：与 _record 的 `if key:` 且 `if total:` 对齐
        should = (
            'key_id IS NOT NULL'
            ' AND (COALESCE(prompt_tokens,0) + COALESCE(completion_tokens,0) > 0)'
        )
        # 用 ts 的半开区间 [当地零点, 次日零点) 代替 `day_sql(ts) = 日期串`：
        # 逐行等价，但前者走索引。**等值必须给上下界**：只写 `ts >= 零点` 会把
        # 次日、下月、明年的记录全算成今天——那比慢更糟（数字直接错）。
        logs_today = db.query_one(
            f'SELECT COUNT(*) AS c FROM request_logs '
            f'WHERE ts >= ? AND ts < ? AND {should}',
            (db.day_start_ts(today), db.day_start_ts(today) + 86400),
        )
        n = int(logs_today['c']) if logs_today else 0
    except Exception:  # noqa: BLE001
        return {'ok': True, 'detail': '', 'logs_today': 0}
    if n > 0 and today_requests == 0:
        return {
            'ok': False,
            'logs_today': n,
            'detail': (f'今天已有 {n} 次调用记录，但用量统计为 0——统计可能没有 '
                       f'正常写入。可尝试「修复统计」；若仍为 0，请查看服务端日志。'),
        }
    return {'ok': True, 'detail': '', 'logs_today': 0}


@router.post('/repair-usage')
def repair_usage(user: dict = Depends(security.require_admin)) -> dict:
    """按请求日志回填用量统计的缺口（幂等，可重复执行）。

    用于修复历史缺陷导致的部分调用未计入统计。
    """
    return db.backfill_usage_from_logs()


@router.post('/rebuild-usage')
def rebuild_usage(user: dict = Depends(security.require_admin)) -> dict:
    """以请求日志为准重建用量统计（清理时区口径不一致造成的重复计数）。

    与 /repair-usage 的区别：repair 只补缺口（增量、幂等），
    本接口是**重建**——会替换 usage_daily 的内容，能删除此前多出来的行。
    """
    return db.rebuild_usage_from_logs()


@router.get('/daily')
def daily(days: int = 30,
          user: dict = Depends(security.current_user)) -> list[dict]:
    """按天聚合。

    `failed` 是当天的失败请求数（4xx / 5xx 合计），**来自 request_logs 而不是
    usage_daily**：后者不含被拒绝与零 token 的失败请求，失败数在其中恒为 0
    （详见 `_failures`）。趋势图用它把「失败」画出来——否则全池中断那天的
    曲线是空的，看不出出过事。
    """
    rows = db.query(
        'SELECT day, SUM(requests) AS requests, '
        'SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens '
        'FROM usage_daily WHERE day >= ? GROUP BY day ORDER BY day ASC',
        (_since(max(1, days)),),
    )
    failures = _daily_failures(days)
    days_seen = {r['day'] for r in rows}
    out = [
        {
            'day': r['day'],
            'requests': int(r['requests'] or 0),
            'prompt_tokens': int(r['prompt_tokens'] or 0),
            'completion_tokens': int(r['completion_tokens'] or 0),
            'failed': failures.get(r['day'], 0),
        }
        for r in rows
    ]
    # **只有失败、没有成功**的日子也要出现：那一天正是最该被看到的。
    # 只按 usage_daily 出行的话，全池中断日会整条缺失（它没有任何成功请求），
    # 趋势图上等于那天不存在——比画成 0 更糟。
    for day, n in sorted(failures.items()):
        if day not in days_seen and n > 0:
            out.append({
                'day': day, 'requests': 0, 'prompt_tokens': 0,
                'completion_tokens': 0, 'failed': n,
            })
    out.sort(key=lambda d: d['day'])
    return out


def _daily_failures(days: int) -> dict[str, int]:
    """按天统计失败请求数（4xx + 5xx）。口径与 `_failures` 一致。"""
    try:
        # 过滤用 ts 范围（走索引），**分组**仍必须用日期表达式（要按天聚合，
        # 这个没法避免）。两者分开：过滤是热路径上的大头，分组只作用于过滤后的行。
        rows = db.query(
            f"SELECT {db.day_sql('ts')} AS day, COUNT(*) AS n FROM request_logs "
            f"WHERE ts >= ? AND status >= 400 AND key_id IS NOT NULL "
            f'GROUP BY day',
            (db.day_start_ts(_since(max(1, days))),),
        )
        return {str(r['day']): int(r['n']) for r in rows if r['day']}
    except Exception:  # noqa: BLE001
        return {}


@router.get('/hourly')
def hourly(day: str | None = None,
           user: dict = Depends(security.current_user)) -> list[dict]:
    """某天**按小时**聚合（默认今天）。「今日」趋势图用它——范围只有一天时
    按天聚合只会得到一根柱子，看不出「今天什么时候忙」。

    与 `/daily` 同一套口径：
      · 用量取自 `usage_hourly`，它与 `usage_daily` 由**同一处写入**累计，
        所以这里的合计与页头卡片天然一致（清日志、日志保留期裁剪都不影响）；
      · `failed` 仍来自 `request_logs`（`usage_*` 只累计有 token 的请求，
        被拒绝与零 token 的失败在里面根本不存在）。

    **固定返回 24 个桶（补零）**：X 轴长度不随「今天走到几点」变化，图上不会
    因为还没到的时段而抖动；没数据的时段就是 0，读起来也清楚。
    """
    d = (day or '').strip() or time.strftime('%Y-%m-%d')
    # 只接受 YYYY-MM-DD：这是个查询参数，写错形态就回落到今天，而不是抛错
    # （趋势图不该因为一个手改的 URL 变成红色错误态）。
    if len(d) != 10 or d[4] != '-' or d[7] != '-':
        d = time.strftime('%Y-%m-%d')
    rows = db.query(
        'SELECT hour, SUM(requests) AS requests, '
        'SUM(prompt_tokens) AS prompt_tokens, '
        'SUM(completion_tokens) AS completion_tokens '
        'FROM usage_hourly WHERE day = ? GROUP BY hour ORDER BY hour ASC',
        (d,),
    )
    by_hour = {int(r['hour']): r for r in rows}
    failures = _hourly_failures(d)
    out: list[dict] = []
    for h in range(24):
        r = by_hour.get(h)
        out.append({
            'day': d,
            'hour': h,
            'requests': int(r['requests'] or 0) if r else 0,
            'prompt_tokens': int(r['prompt_tokens'] or 0) if r else 0,
            'completion_tokens': int(r['completion_tokens'] or 0) if r else 0,
            'failed': failures.get(h, 0),
        })
    return out


def _hourly_failures(day: str) -> dict[int, int]:
    """某天**按小时**的失败请求数（4xx / 5xx 合计）。口径与 `_failures` 一致。"""
    try:
        # 过滤用 ts 范围（走索引），分组用小时表达式（要聚合，躲不开）
        rows = db.query(
            f"SELECT {db.hour_sql('ts')} AS bucket, COUNT(*) AS n FROM request_logs "
            f"WHERE ts >= ? AND ts < ? AND status >= 400 AND key_id IS NOT NULL "
            f'GROUP BY bucket',
            (db.day_start_ts(day), db.day_start_ts(day) + 86400),
        )
        out: dict[int, int] = {}
        for r in rows:
            bucket = str(r['bucket'] or '')
            if len(bucket) >= 13:
                out[int(bucket[11:13])] = int(r['n'])
        return out
    except Exception:  # noqa: BLE001
        return {}


@router.get('/by-model')
def by_model(days: int = 30,
             user: dict = Depends(security.current_user)) -> list[dict]:
    """按模型聚合（按 token 总量降序）。"""
    rows = db.query(
        'SELECT model AS name, SUM(requests) AS requests, '
        'SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens '
        'FROM usage_daily WHERE day >= ? GROUP BY model '
        'ORDER BY SUM(prompt_tokens + completion_tokens) DESC',
        (_since(max(1, days)),),
    )
    # 输出速率来自请求日志：与上面的用量表**同区间不同源**，所以只给「两边
    # 都有」的模型补字段；日志里完全没有的模型给 null（显示 —）。
    tmap = _throughput_by_model(days)
    out = []
    for r in rows:
        name = r['name'] or '未知'
        out.append({
            'name': name,
            'requests': int(r['requests'] or 0),
            'prompt_tokens': int(r['prompt_tokens'] or 0),
            'completion_tokens': int(r['completion_tokens'] or 0),
            **tmap.get(name, {'tokens_per_sec': None, 'throughput_samples': 0}),
        })
    return out


@router.get('/by-key')
def by_key(days: int = 30,
           user: dict = Depends(security.current_user)) -> list[dict]:
    """按密钥聚合（按 token 总量降序）。已删除的密钥显示为 id 字符串。"""
    rows = db.query(
        'SELECT COALESCE(k.name, u.key_id || "") AS name, SUM(u.requests) AS requests, '
        'SUM(u.prompt_tokens) AS prompt_tokens, SUM(u.completion_tokens) AS completion_tokens '
        'FROM usage_daily u LEFT JOIN api_keys k ON k.id = u.key_id '
        'WHERE u.day >= ? GROUP BY u.key_id '
        'ORDER BY SUM(u.prompt_tokens + u.completion_tokens) DESC',
        (_since(max(1, days)),),
    )
    return [
        {
            'name': r['name'] or '未知',
            'requests': int(r['requests'] or 0),
            'prompt_tokens': int(r['prompt_tokens'] or 0),
            'completion_tokens': int(r['completion_tokens'] or 0),
        }
        for r in rows
    ]
