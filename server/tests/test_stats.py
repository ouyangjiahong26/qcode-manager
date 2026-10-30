"""日志与统计路由：聚合口径、分页/筛选边界、security 配置与规则 CRUD。

不 import 真实的 server.security / gateway：鉴权依赖在导入路由前用假的
server.security 模块顶上，再配合 FastAPI dependency_overrides 注入假用户，
路由逻辑本身完全按真实代码路径执行。
"""
from __future__ import annotations

import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi import FastAPI
from fastapi.testclient import TestClient  # noqa: E402

from server import config, db  # noqa: E402

# ── 用假鉴权模块顶住 server.security（真模块由其他片实现，这里不依赖它）──
_fake = types.ModuleType('server.security')
_fake_user = {'username': 'tester', 'admin': True}


def _cur_user() -> dict:
    return _fake_user


def _require_admin() -> dict:
    return _fake_user


def _require_session_admin() -> dict:
    return _fake_user


def _audit(user, action, target='', detail='', ip='') -> None:
    db.add_audit_log(str(user.get('username', '?')), action, target, detail, ip)


_fake.current_user = _cur_user
_fake.require_admin = _require_admin
_fake.require_session_admin = _require_session_admin
_fake.audit = _audit
sys.modules.setdefault('server.security', _fake)

import server  # noqa: E402

server.security = _fake

from server.routers import logs as logs_router  # noqa: E402
from server.routers import security as security_router  # noqa: E402
from server.routers import stats as stats_router  # noqa: E402


class RouterTestCase(unittest.TestCase):
    """公共夹具：临时库 + 挂三个路由 + 覆盖全部鉴权依赖。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 't.db'
        db._conn = None
        db.connect()

        app = FastAPI()
        app.include_router(logs_router.router)
        app.include_router(stats_router.router)
        app.include_router(security_router.router)
        app.dependency_overrides[logs_router.security.current_user] = _cur_user
        app.dependency_overrides[logs_router.security.require_admin] = _require_admin
        app.dependency_overrides[logs_router.security.require_session_admin] = _require_session_admin
        self.client = TestClient(app)

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()

    # ── 造数工具 ─────────────────────────────────────────
    def add_key(self, key_id: int, name: str, enabled: int = 1) -> None:
        db.execute(
            'INSERT INTO api_keys(id, name, key_hash, prefix, enabled) VALUES(?, ?, ?, ?, ?)',
            (key_id, name, 'h', 'qk_x', enabled),
        )

    def add_log(self, *, ts: int, status: int = 200, key_id: int | None = 1,
                model: str = 'glm-4.7', ip: str = '1.2.3.4',
                prompt: int = 10, completion: int = 20,
                latency_ms: int = 1200, first_token_ms: int | None = 200,
                error: str = '') -> None:
        db.add_request_log(
            ts=ts, key_id=key_id, ip=ip, model=model, status=status,
            prompt_tokens=prompt, completion_tokens=completion,
            latency_ms=latency_ms, first_token_ms=first_token_ms,
            ua='pytest', error=error, stream=0, channel='api', account='', effort=None,
        )

    def add_usage(self, day: str, key_id: int, model: str,
                  requests: int, prompt: int, completion: int) -> None:
        db.execute(
            'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, completion_tokens) '
            'VALUES(?, ?, ?, ?, ?, ?)',
            (day, key_id, model, requests, prompt, completion),
        )


class Summary(RouterTestCase):
    def test_counts_and_failures(self) -> None:
        today = db.day_of()
        yesterday = db.day_of(time.time() - 86400)
        self.add_key(1, 'k1')
        self.add_key(2, 'k2', enabled=0)
        self.add_usage(today, 1, 'glm-4.7', 3, 100, 200)
        self.add_usage(yesterday, 1, 'glm-4.6', 5, 1000, 2000)
        # 今日失败：429、500 各一条；另有无 key 的 401（不算失败）
        t0 = db.day_start_ts(today)
        self.add_log(ts=t0 + 60, status=429, key_id=1, prompt=0, completion=0)
        self.add_log(ts=t0 + 120, status=500, key_id=1, prompt=0, completion=0)
        self.add_log(ts=t0 + 180, status=401, key_id=None, prompt=0, completion=0)

        body = self.client.get('/api/stats/summary').json()
        self.assertEqual(body['today_requests'], 3)
        self.assertEqual(body['today_tokens'], 300)
        self.assertEqual(body['week_requests'], 8)
        self.assertEqual(body['total_tokens'], 3300)
        self.assertEqual(body['active_keys'], 1)      # 只数启用中的
        self.assertEqual(body['top_model'], 'glm-4.6')
        self.assertEqual(body['failures']['today_4xx'], 1)
        self.assertEqual(body['failures']['today_5xx'], 1)
        self.assertEqual(body['failures']['week_4xx'], 1)
        self.assertTrue(body['usage_health']['ok'])

    def test_usage_health_reports_silent_breakage(self) -> None:
        t0 = db.day_start_ts()
        # 今天有「本该累计」的日志（带 key、有 token），但 usage_daily 是空的
        self.add_log(ts=t0 + 60, key_id=1, prompt=5, completion=6)
        body = self.client.get('/api/stats/summary').json()
        self.assertFalse(body['usage_health']['ok'])
        self.assertEqual(body['usage_health']['logs_today'], 1)

        # 零 token 的调用（如 /v1/models）不该触发误报
        db.execute('DELETE FROM request_logs')
        self.add_log(ts=t0 + 60, key_id=1, prompt=0, completion=0)
        body = self.client.get('/api/stats/summary').json()
        self.assertTrue(body['usage_health']['ok'])


class DailyHourly(RouterTestCase):
    def test_daily_buckets_and_failure_only_day(self) -> None:
        today = db.day_of()
        d2 = db.day_of(time.time() - 2 * 86400)
        d3 = db.day_of(time.time() - 3 * 86400)
        self.add_usage(today, 1, 'glm-4.7', 4, 10, 20)
        self.add_usage(d2, 1, 'glm-4.7', 1, 1, 1)
        # 三天前只有失败（全池中断日）——也必须出现一根柱子
        self.add_log(ts=db.day_start_ts(d3) + 60, status=503, key_id=1,
                     prompt=0, completion=0)

        rows = self.client.get('/api/stats/daily?days=7').json()
        by_day = {r['day']: r for r in rows}
        self.assertEqual(by_day[today]['requests'], 4)
        self.assertEqual(by_day[d2]['requests'], 1)
        self.assertIn(d3, by_day)                       # 失败-only 日补行
        self.assertEqual(by_day[d3]['failed'], 1)
        self.assertEqual(by_day[d3]['requests'], 0)
        self.assertEqual([r['day'] for r in rows], sorted(r['day'] for r in rows))

    def test_hourly_24_buckets(self) -> None:
        today = db.day_of()
        db.bump_usage(1, 'glm-4.7', 7, 8)               # 当前小时
        # 固定桶选一个必不与当前小时重合的小时，否则裸 INSERT 撞
        # bump_usage 已写的行（UNIQUE day+hour+key_id+model）
        other_hour = (db.hour_of() + 12) % 24
        db.execute(
            'INSERT INTO usage_hourly(day, hour, key_id, model, requests, '
            'prompt_tokens, completion_tokens) VALUES(?, ?, 1, ?, 2, 5, 6)',
            (today, other_hour, 'glm-4.7'),
        )
        # 当前小时的一条失败
        now_hour = db.hour_of()
        self.add_log(ts=int(time.time()), status=429, key_id=1,
                     prompt=0, completion=0)

        rows = self.client.get('/api/stats/hourly').json()
        self.assertEqual(len(rows), 24)
        self.assertEqual([r['hour'] for r in rows], list(range(24)))
        by_hour = {r['hour']: r for r in rows}
        self.assertEqual(by_hour[other_hour]['requests'], 2)
        self.assertEqual(by_hour[now_hour]['requests'], 1)
        self.assertEqual(by_hour[now_hour]['failed'], 1)

        # day 形态非法 → 回落到今天，不报错
        rows = self.client.get('/api/stats/hourly?day=not-a-date').json()
        self.assertEqual(rows[0]['day'], today)


class ByModelByKey(RouterTestCase):
    def test_by_model_ranking(self) -> None:
        today = db.day_of()
        self.add_usage(today, 1, 'glm-4.7', 1, 100, 200)
        self.add_usage(today, 1, 'glm-4.6', 1, 10, 20)
        rows = self.client.get('/api/stats/by-model').json()
        self.assertEqual([r['name'] for r in rows[:2]], ['glm-4.7', 'glm-4.6'])

    def test_by_model_throughput(self) -> None:
        t0 = db.day_start_ts()
        # 生成 100 token 用了 1s（latency 1200ms - first 200ms）→ 100 tok/s
        self.add_log(ts=t0 + 60, key_id=1, model='glm-4.7',
                     prompt=0, completion=100, latency_ms=1200, first_token_ms=200)
        # 速率来自 request_logs，但模型行来自 usage_daily（同区间不同源）
        self.add_usage(db.day_of(), 1, 'glm-4.7', 1, 0, 100)
        rows = self.client.get('/api/stats/by-model?days=1').json()
        row = next(r for r in rows if r['name'] == 'glm-4.7')
        self.assertEqual(row['tokens_per_sec'], 100.0)
        self.assertEqual(row['throughput_samples'], 1)

    def test_by_key_name_resolution(self) -> None:
        today = db.day_of()
        self.add_key(1, '生产密钥')
        self.add_usage(today, 1, 'glm-4.7', 2, 10, 20)
        self.add_usage(today, 99, 'glm-4.7', 1, 1, 1)   # 已删除的密钥 → 显示 id
        rows = self.client.get('/api/stats/by-key').json()
        self.assertEqual(rows[0]['name'], '生产密钥')
        self.assertEqual(rows[1]['name'], '99')


class RepairRebuild(RouterTestCase):
    def test_rebuild_from_logs(self) -> None:
        t0 = db.day_start_ts()
        self.add_key(1, 'k1')
        self.add_log(ts=t0 + 60, key_id=1, model='glm-4.7', prompt=3, completion=4)
        self.add_log(ts=t0 + 120, key_id=1, model='glm-4.7', prompt=30, completion=40)

        self.assertEqual(self.client.get('/api/stats/summary').json()['today_requests'], 0)
        r = self.client.post('/api/stats/rebuild-usage')
        self.assertEqual(r.status_code, 200)
        body = self.client.get('/api/stats/summary').json()
        self.assertEqual(body['today_requests'], 2)
        self.assertEqual(body['today_tokens'], 77)

        # repair 是幂等补缺口：重建后再修，不应重复计数
        self.client.post('/api/stats/repair-usage')
        self.assertEqual(
            self.client.get('/api/stats/summary').json()['today_requests'], 2)


class LogsEndpoint(RouterTestCase):
    def seed(self) -> None:
        t0 = db.day_start_ts()
        self.add_key(1, 'k1')
        for i in range(5):
            self.add_log(ts=t0 + i, status=200, key_id=1, model='glm-4.7',
                         ip='10.0.0.1')
        self.add_log(ts=t0 + 99, status=500, key_id=2, model='glm-4.6',
                     ip='192.168.1.9', error='boom')

    def test_pagination_clamps(self) -> None:
        self.seed()
        body = self.client.get('/api/logs?page=1&size=9999').json()
        self.assertEqual(len(body['items']), 6)
        self.assertEqual(body['total'], 6)
        # 超大页码：不 500，返回空页
        r = self.client.get('/api/logs?page=100000&size=20')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['items'], [])
        # 超大 days：不触发 SQLite 溢出
        r = self.client.get('/api/logs?days=999999999999')
        self.assertEqual(r.status_code, 200)
        # 越界分页参数被钳到下界：page=1、size=1 → 只回一行
        body = self.client.get('/api/logs?page=0&size=-5').json()
        self.assertEqual(len(body['items']), 1)

    def test_filters(self) -> None:
        self.seed()
        base = '/api/logs'
        self.assertEqual(self.client.get(base + '?status=ok').json()['total'], 5)
        self.assertEqual(self.client.get(base + '?status=error').json()['total'], 1)
        self.assertEqual(self.client.get(base + '?model=4.6').json()['total'], 1)
        self.assertEqual(self.client.get(base + '?ip=192.168').json()['total'], 1)
        self.assertEqual(self.client.get(base + '?key_id=1').json()['total'], 5)
        self.assertEqual(self.client.get(base + '?key_id=all').json()['total'], 6)
        self.assertEqual(
            self.client.get(base + '?key_id=abc').status_code, 400)
        item = self.client.get(base + '?status=error').json()['items'][0]
        self.assertIsNone(item['key_name'])        # key 2 已不存在 → join 不到名
        self.assertEqual(item['key_id'], 2)
        self.assertEqual(item['error'], 'boom')
        ok = self.client.get(base + '?key_id=1').json()['items'][0]
        self.assertEqual(ok['key_name'], 'k1')

    def test_clear(self) -> None:
        self.seed()
        self.assertEqual(self.client.post('/api/logs/clear').status_code, 200)
        self.assertEqual(self.client.get('/api/logs').json()['total'], 0)


class SecurityConfigAndRules(RouterTestCase):
    def test_config_roundtrip(self) -> None:
        # 默认：关、黑名单
        self.assertEqual(
            self.client.get('/api/security/config').json(),
            {'enabled': False, 'mode': 'blacklist'},
        )
        r = self.client.post('/api/security/config',
                             json={'enabled': True, 'mode': 'whitelist'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            self.client.get('/api/security/config').json(),
            {'enabled': True, 'mode': 'whitelist'},
        )
        # 非法模式被 pydantic 拦下
        self.assertEqual(
            self.client.post('/api/security/config',
                             json={'enabled': True, 'mode': 'nope'}).status_code, 422)

    def test_rules_crud(self) -> None:
        r = self.client.post('/api/security/rules',
                             json={'kind': 'deny', 'cidr': '10.0.0.0/8', 'note': '内网'})
        self.assertEqual(r.status_code, 200)
        rule_id = r.json()['id']
        rules = self.client.get('/api/security/rules').json()
        self.assertEqual(len(rules), 1)
        self.assertEqual(rules[0]['cidr'], '10.0.0.0/8')
        # 非法 CIDR → 400
        self.assertEqual(
            self.client.post('/api/security/rules',
                             json={'kind': 'allow', 'cidr': 'not-a-cidr'}).status_code, 400)
        self.assertEqual(
            self.client.delete(f'/api/security/rules/{rule_id}').status_code, 200)
        self.assertEqual(self.client.get('/api/security/rules').json(), [])
        # 删不存在的 → 404
        self.assertEqual(
            self.client.delete('/api/security/rules/{0}'.format(rule_id)).status_code, 404)

    def test_access_logs_limit_and_clear(self) -> None:
        for i in range(5):
            db.add_ip_access_log('1.2.3.4', '/v1/chat/completions', True, 't', 'invalid_key')
        rows = self.client.get('/api/security/logs?limit=3').json()
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0]['blocked'])
        self.assertEqual(rows[0]['reason'], 'invalid_key')
        # limit 钳到 [1, 1000]：0 → 1
        self.assertEqual(len(self.client.get('/api/security/logs?limit=0').json()), 1)
        self.assertEqual(self.client.post('/api/security/logs/clear').status_code, 200)
        self.assertEqual(self.client.get('/api/security/logs').json(), [])


if __name__ == '__main__':
    unittest.main()
