"""PAT 保活调度：按时刻表对全部 enabled 账号定时测活。

唯一保留的定时任务类型：PAT 长期不用可能被上游回收，定时带真实鉴权打一遍
``GET /v1/models`` 既证明 PAT 活着、又刷新库内状态（status/last_check_*），
供轮询调度剔除失效账号。配置存 settings 表 key ``keepalive``：
``{'enabled': bool, 'times': ['HH:MM', ...], 'timeout': 秒}``。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time

from . import accountsvc, db

logger = logging.getLogger('qcoder.keepalive')

_KEY = 'keepalive'
_DEFAULTS = {'enabled': False, 'times': ['08:00'], 'timeout': 15}

# 苏醒频率：时刻表精度是分钟，20s 检查一次足够贴合且空转开销可忽略。
_CHECK_INTERVAL = 20

# 时刻表条目上限：防把配置当存储滥用（每条每天触发一轮全量测活）。
_MAX_TIMES = 24

_TIME_RE = re.compile(r'^(\d{1,2}):(\d{2})$')

# 任务句柄 + 每个时刻最近一次触发的日期（本地时区），保证同一天同一时刻只触发一轮。
_task: asyncio.Task | None = None
_last_fired: dict[str, str] = {}


def parse_time(value: object) -> int | None:
    """``'HH:MM'`` → 当天分钟数；非法（小时≥24、分钟≥60、格式错）返回 None。

    ``'8:05'`` 这类缺前导零的写法接受并归一化——用户手输时刻表时这是高频形态。
    """
    m = _TIME_RE.match(str(value or '').strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def _normalize_times(raw: object) -> list[str]:
    """收敛成去重、升序的合法 ``'HH:MM'`` 列表；含非法项抛 ValueError。"""
    if not isinstance(raw, list):
        raise ValueError('times 必须是字符串数组')
    seen: dict[int, str] = {}
    for item in raw:
        minutes = parse_time(item)
        if minutes is None:
            raise ValueError(f'非法时刻：{item!r}（应为 HH:MM）')
        seen[minutes] = f'{minutes // 60:02d}:{minutes % 60:02d}'
    return [seen[m] for m in sorted(seen)]


def get_config() -> dict:
    """当前保活配置（库里没有/字段缺失时逐项回落默认值）。"""
    raw = db.get_setting(_KEY)
    cfg = dict(_DEFAULTS)
    if isinstance(raw, dict):
        cfg['enabled'] = bool(raw.get('enabled', cfg['enabled']))
        try:
            cfg['times'] = _normalize_times(raw.get('times', cfg['times']))
        except ValueError:
            pass  # 手改库写坏的时刻表：回落默认，不让整个面板读配置报错
        try:
            cfg['timeout'] = int(raw.get('timeout', cfg['timeout']))
        except (TypeError, ValueError):
            pass
    cfg['timeout'] = max(5, min(120, cfg['timeout']))
    return cfg


def save_config(payload: dict) -> dict:
    """保存保活配置；校验失败抛 ValueError（路由层转 400）。"""
    cfg = {
        'enabled': bool(payload.get('enabled', _DEFAULTS['enabled'])),
        'times': _normalize_times(payload.get('times', _DEFAULTS['times'])),
        'timeout': int(payload.get('timeout', _DEFAULTS['timeout'])),
    }
    if not cfg['times']:
        raise ValueError('至少需要一个保活时刻')
    if len(cfg['times']) > _MAX_TIMES:
        raise ValueError(f'保活时刻最多 {_MAX_TIMES} 个')
    if not 5 <= cfg['timeout'] <= 120:
        raise ValueError('单次测活超时须在 5–120 秒之间')
    db.set_setting(_KEY, cfg)
    return cfg


async def run_round(cfg: dict | None = None) -> dict:
    """对全部 enabled 账号并发跑一轮测活（信号量 5，单账号受 timeout 上限）。"""
    if cfg is None:
        cfg = get_config()
    rows = accountsvc.list_accounts(include_disabled=False)
    results = await accountsvc._run_tests([r['id'] for r in rows],
                                          timeout=cfg['timeout'])
    ok = sum(1 for r in results if r.get('ok'))
    stats = {'total': len(results), 'ok': ok, 'failed': len(results) - ok}
    logger.info('保活轮次完成：%s', stats)
    return stats


async def _loop() -> None:
    """常驻循环：到点的时刻触发一轮（当天已触发的时刻不重复）。"""
    await asyncio.sleep(5)  # 首轮稍等，避开启动期的初始化
    while True:
        try:
            cfg = get_config()
            if cfg['enabled'] and cfg['times']:
                now = time.localtime()
                today = time.strftime('%Y-%m-%d', now)
                hhmm = f'{now.tm_hour:02d}:{now.tm_min:02d}'
                for slot in cfg['times']:
                    # 字符串比较即可：零填充的 HH:MM 字典序 == 时间序。
                    # 服务在时刻过后才启动/恢复时，会在下一次苏醒补跑当天的
                    # 错过时刻（迟到好过不跑）。
                    if hhmm >= slot and _last_fired.get(slot) != today:
                        _last_fired[slot] = today
                        logger.info('保活时刻到期：%s', slot)
                        await run_round(cfg)
                        break  # 每次苏醒至多跑一轮，多个到期时刻顺延消化
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 单轮异常不能杀死调度
            logger.warning('保活轮次异常: %s', exc)
        await asyncio.sleep(_CHECK_INTERVAL)


def start_scheduler() -> bool:
    """启动保活调度（幂等）。无运行中事件循环时返回 False（CLI 场景）。"""
    global _task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if _task is None or _task.done():
        _task = loop.create_task(_loop())
    return True


def stop_scheduler() -> None:
    """停止保活调度（幂等；应用关闭时调用）。"""
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None
