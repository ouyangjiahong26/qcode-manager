"""db 模块：建表、CRUD、settings 往返、用量累计与时间口径（临时目录，不碰真实 data/）。"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402


def local_time(ts: int) -> time.struct_time:
    return time.localtime(ts)


class DbTestCase(unittest.TestCase):
    """公共夹具：把数据库指到临时目录并重建连接。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 't.db'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()


class Schema(DbTestCase):
    def test_all_tables_created(self) -> None:
        names = {
            r['name']
            for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")
        }
        expected = {
            'accounts', 'api_keys', 'api_key_ips', 'request_logs',
            'usage_daily', 'usage_hourly', 'audit_logs', 'ip_rules',
            'ip_access_logs', 'settings', 'api_tokens', 'upstreams',
        }
        self.assertTrue(expected <= names, f'缺表：{expected - names}')

    def test_accounts_columns(self) -> None:
        cols = {
            r['name']
            for r in db.query('PRAGMA table_info(accounts)')
        }
        self.assertIn('pat_encrypted', cols)
        self.assertIn('pat_suffix', cols)
        self.assertIn('upstream_id', cols)
        self.assertIn('weight', cols)
        self.assertIn('total_tokens', cols)

    def test_connect_is_singleton(self) -> None:
        self.assertIs(db.connect(), db.connect())


class Settings(DbTestCase):
    def test_roundtrip(self) -> None:
        self.assertIsNone(db.get_setting('nope'))
        self.assertEqual(db.get_setting('nope', 'dflt'), 'dflt')
        db.set_setting('proxy', {'url': 'http://127.0.0.1:7890', 'on': True})
        self.assertEqual(db.get_setting('proxy'), {'url': 'http://127.0.0.1:7890', 'on': True})
        # 中文与非 ASCII 不被转义破坏
        db.set_setting('note', '备注')
        self.assertEqual(db.get_setting('note'), '备注')

    def test_overwrite(self) -> None:
        db.set_setting('k', 1)
        db.set_setting('k', 2)
        self.assertEqual(db.get_setting('k'), 2)

    def test_corrupt_value_falls_back_to_default(self) -> None:
        db.execute("INSERT INTO settings(key, value) VALUES('bad', '{oops')")
        self.assertEqual(db.get_setting('bad', 'fallback'), 'fallback')


class Crud(DbTestCase):
    def test_execute_returns_rowid_and_query_reads_back(self) -> None:
        rid = db.execute(
            'INSERT INTO upstreams(name, base_url, created_at) VALUES(?, ?, ?)',
            ('主上游', 'http://127.0.0.1:8963', int(time.time())),
        )
        row = db.query_one('SELECT name, base_url FROM upstreams WHERE id = ?', (rid,))
        self.assertIsNotNone(row)
        self.assertEqual(row['name'], '主上游')

    def test_executemany(self) -> None:
        db.executemany(
            'INSERT INTO api_key_ips(key_id, ip, first_seen) VALUES(?, ?, ?)',
            [(1, '1.1.1.1', 0), (1, '2.2.2.2', 0)],
        )
        self.assertEqual(len(db.query('SELECT * FROM api_key_ips')), 2)

    def test_audit_log_roundtrip_and_count_clear(self) -> None:
        db.add_audit_log('admin', 'login', 'target', 'detail', '1.2.3.4')
        rows = db.list_audit_logs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['actor'], 'admin')
        self.assertEqual(db.count_audit_logs(), 1)
        db.clear_audit_logs()
        self.assertEqual(db.count_audit_logs(), 0)

    def test_audit_log_strips_newlines(self) -> None:
        """外部输入里的换行不能进入日志，否则能在界面上伪造额外的行。"""
        db.add_audit_log('a\rb\nc', 'act')
        row = db.query_one('SELECT actor FROM audit_logs')
        self.assertNotIn('\n', row['actor'])
        self.assertNotIn('\r', row['actor'])

    def test_ip_access_log(self) -> None:
        db.add_ip_access_log('9.9.9.9', '/v1/chat/completions', True, 'ua', 'missing_key')
        row = db.query_one('SELECT * FROM ip_access_logs')
        self.assertEqual(row['blocked'], 1)
        self.assertEqual(row['reason'], 'missing_key')
        # 放行且无原因 → NULL（与「记了空串」区分）
        db.add_ip_access_log('8.8.8.8', '/healthz', False, 'ua')
        row = db.query_one('SELECT * FROM ip_access_logs WHERE ip = ?', ('8.8.8.8',))
        self.assertIsNone(row['reason'])

    def test_request_log_roundtrip(self) -> None:
        db.add_request_log(
            key_id=7, ip='1.2.3.4', model='gpt-x', mapped_model='glm-4.7',
            status=200, prompt_tokens=10, completion_tokens=20, latency_ms=123,
            first_token_ms=45, ua='test', stream=1, channel='pat', account='a1',
            effort='high',
        )
        row = db.query_one('SELECT * FROM request_logs')
        self.assertEqual(row['mapped_model'], 'glm-4.7')
        self.assertEqual(row['stream'], 1)
        self.assertEqual(row['account'], 'a1')


class BumpUsage(DbTestCase):
    def test_accumulates_same_day(self) -> None:
        db.bump_usage(1, 'glm-4.7', 100, 50)
        db.bump_usage(1, 'glm-4.7', 10, 5)
        row = db.query_one(
            'SELECT * FROM usage_daily WHERE key_id = 1 AND model = ?', ('glm-4.7',))
        self.assertEqual(row['requests'], 2)
        self.assertEqual(row['prompt_tokens'], 110)
        self.assertEqual(row['completion_tokens'], 55)
        # 小时表同步累计
        hrow = db.query_one(
            'SELECT SUM(requests) AS r FROM usage_hourly WHERE key_id = 1')
        self.assertEqual(hrow['r'], 2)

    def test_separate_models(self) -> None:
        db.bump_usage(1, 'm1', 1, 1)
        db.bump_usage(1, 'm2', 1, 1)
        self.assertEqual(len(db.query('SELECT * FROM usage_daily')), 2)

    def test_bump_account_usage(self) -> None:
        aid = db.execute(
            "INSERT INTO accounts(label, pat_encrypted, requests, total_tokens) "
            "VALUES('a', 'enc', 0, 0)")
        db.bump_account_usage(aid, 500)
        db.bump_account_usage(aid, 30)
        row = db.query_one('SELECT requests, total_tokens, updated_at FROM accounts WHERE id = ?', (aid,))
        self.assertEqual(row['requests'], 2)
        self.assertEqual(row['total_tokens'], 530)
        self.assertIsNotNone(row['updated_at'])
        # 不存在的账号不报错（静默无操作）
        db.bump_account_usage(9999, 1)


class TimeBuckets(DbTestCase):
    """时间口径：Python 与 SQL 两侧必须一致，跨小时/跨天边界归入正确桶。"""

    def test_day_of_matches_sql(self) -> None:
        # 取本地零点后 30 秒：任何时区下都是「当天最边缘」的样本
        lt = time.localtime()
        ts = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 30, 0, 0, -1)))
        via_py = db.day_of(ts)
        via_sql = db.connect().execute(
            f"SELECT {db.day_sql('?')} AS d", (ts,)).fetchone()['d']
        self.assertEqual(via_py, via_sql)

    def test_hour_of_matches_sql(self) -> None:
        lt = time.localtime()
        ts = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 14, 59, 59, 0, 0, -1)))
        self.assertEqual(db.hour_of(ts), 14)
        via_sql = db.connect().execute(
            f"SELECT {db.hour_sql('?')} AS h", (ts,)).fetchone()['h']
        self.assertEqual(via_sql.split(' ')[1], '14')

    def test_hour_boundary_crossing(self) -> None:
        """小时边界前后一秒必须落在相邻的两个桶里。"""
        lt = time.localtime()
        base = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 10, 0, 0, 0, 0, -1)))
        self.assertEqual(db.hour_of(base - 1), 9)
        self.assertEqual(db.hour_of(base), 10)
        # SQL 侧同样按本地小时分桶
        before = db.connect().execute(
            f"SELECT {db.hour_sql('?')} AS h", (base - 1,)).fetchone()['h']
        after = db.connect().execute(
            f"SELECT {db.hour_sql('?')} AS h", (base,)).fetchone()['h']
        self.assertEqual(int(before[-2:]), 9)
        self.assertEqual(int(after[-2:]), 10)

    def test_day_start_ts_is_local_midnight(self) -> None:
        day = db.day_of()
        start = db.day_start_ts(day)
        self.assertEqual(db.day_of(start), day)
        self.assertEqual(db.day_of(start - 1), db.day_of(start - 1))  # 不越界抛错
        # 零点前 1 秒属于前一天
        prev = db.day_of(start - 1)
        self.assertNotEqual(prev, day)
        self.assertEqual(db.day_start_ts(prev) + 86400, start)

    def test_hour_start_ts(self) -> None:
        day = db.day_of()
        h = db.hour_start_ts(day, 15)
        self.assertEqual(db.hour_of(h), 15)
        self.assertEqual(db.day_of(h), day)
        self.assertEqual(h, db.day_start_ts(day) + 15 * 3600)


class Backfill(DbTestCase):
    def _seed_log(self, ts: int, pt: int, ct: int) -> None:
        db.execute(
            'INSERT INTO request_logs(ts, key_id, model, status, prompt_tokens, completion_tokens) '
            'VALUES(?, 1, ?, 200, ?, ?)',
            (ts, 'glm-4.7', pt, ct),
        )

    def test_backfill_fills_gap_and_is_idempotent(self) -> None:
        ts = int(time.time())
        self._seed_log(ts, 100, 50)
        res = db.backfill_usage_from_logs()
        self.assertEqual(res['repaired'], 1)
        row = db.query_one('SELECT * FROM usage_daily WHERE key_id = 1')
        self.assertEqual(row['requests'], 1)
        # 再跑一遍：没有缺口，不重复计数
        res2 = db.backfill_usage_from_logs()
        self.assertEqual(res2['repaired'], 0)
        self.assertEqual(res2['requests'], 0)

    def test_bump_then_backfill_no_gap(self) -> None:
        """bump_usage 已记过的调用，回填不应再补出第二行。"""
        db.bump_usage(1, 'glm-4.7', 100, 50)
        self._seed_log(int(time.time()), 100, 50)
        db.backfill_usage_from_logs()
        self.assertEqual(len(db.query('SELECT * FROM usage_daily WHERE key_id = 1')), 1)

    def test_ensure_hourly_today(self) -> None:
        self._seed_log(int(time.time()), 10, 5)
        self.assertTrue(db.ensure_hourly_today())
        # 幂等：已有数据后不再补
        self.assertFalse(db.ensure_hourly_today())
        rows = db.hourly_rows(db.day_of())
        self.assertEqual(sum(r['requests'] for r in rows), 1)

    def test_rebuild_from_logs(self) -> None:
        self._seed_log(int(time.time()), 100, 50)
        self._seed_log(int(time.time()), 30, 20)
        res = db.rebuild_usage_from_logs()
        self.assertEqual(res['rows_after'], 1)
        row = db.query_one('SELECT * FROM usage_daily')
        self.assertEqual(row['requests'], 2)
        self.assertEqual(row['prompt_tokens'], 130)
        # 小时表一并重建
        self.assertEqual(sum(r['requests'] for r in db.hourly_rows(db.day_of())), 2)

    def test_hourly_rows_orders_by_hour(self) -> None:
        db.execute(
            "INSERT INTO usage_hourly(day, hour, key_id, model, requests, prompt_tokens, completion_tokens) "
            "VALUES(?, 9, 1, 'm', 1, 0, 0)", (db.day_of(),))
        db.execute(
            "INSERT INTO usage_hourly(day, hour, key_id, model, requests, prompt_tokens, completion_tokens) "
            "VALUES(?, 8, 1, 'm', 1, 0, 0)", (db.day_of(),))
        self.assertEqual([r['hour'] for r in db.hourly_rows(db.day_of())], [8, 9])


class ClampDays(unittest.TestCase):
    def test_clamp(self) -> None:
        self.assertIsNone(db.clamp_days(None))
        self.assertIsNone(db.clamp_days('abc'))
        self.assertEqual(db.clamp_days(0), 1)
        self.assertEqual(db.clamp_days(7), 7)
        self.assertEqual(db.clamp_days(10 ** 9), 3650)


if __name__ == '__main__':
    unittest.main()
