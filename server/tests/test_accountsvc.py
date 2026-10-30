"""PAT 账号池：加密、CRUD 掩码、轮询调度、保活时刻解析与 qoder2api 客户端。

全部用临时目录隔离（不碰真实 data/）；上游交互跑在 dev/mock_upstream.py
子进程上（同接口形态：pt- 开头且不含 invalid 的 PAT 有效）。
不 import security/gateway 等并行片模块——跨模块集成由组装片负责。
"""
from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import accountsvc, config, db, keepalive  # noqa: E402
from server.services import qoder2api  # noqa: E402

DEV_MOCK = Path(__file__).resolve().parents[2] / 'dev' / 'mock_upstream.py'


def run(coro):
    return asyncio.run(coro)


class AccountTestCase(unittest.TestCase):
    """公共夹具：数据目录与库都指到临时目录，密钥缓存/轮询指针复位。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_data = config.DATA_DIR
        self._orig_db = config.DB_PATH
        config.DATA_DIR = Path(self._tmp.name)
        config.DB_PATH = Path(self._tmp.name) / 't.db'
        db._conn = None
        db.connect()
        # unittest 运行器会 simplefilter('default') 重置警告过滤，须在每个
        # 用例内重新忽略：跨事件循环收尾连接池的 ResourceWarning 属夹具产物
        # （生产环境单事件循环长存），不该淹没真实失败信号。
        warnings.filterwarnings('ignore', category=ResourceWarning)
        accountsvc._reset_crypto_cache()
        accountsvc._PICK_CURSOR.clear()
        self._orig_sync_probe = accountsvc._test_pat_sync

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DATA_DIR = self._orig_data
        config.DB_PATH = self._orig_db
        accountsvc._reset_crypto_cache()
        accountsvc._test_pat_sync = self._orig_sync_probe
        self._tmp.cleanup()

    def mock_sync_probe(self, ok: bool = True, msg: str = '模拟测活'):
        """把建账即测的同步 HTTP 换成固定结果（离线测 CRUD 语义）。"""
        accountsvc._test_pat_sync = lambda aid: (ok, msg)  # noqa: E731

    def insert(self, label: str, pat: str, status: str = 'active',
               enabled: int = 1, upstream_id=None) -> int:
        """直接落一行账号（绕过建账即测），精确控制状态供调度测试。"""
        now = int(time.time())
        return db.execute(
            'INSERT INTO accounts(label, pat_encrypted, pat_suffix, upstream_id,'
            ' enabled, status, created_at, updated_at)'
            ' VALUES (?,?,?,?,?,?,?,?)',
            (label, accountsvc.encrypt_pat(pat), pat[-4:], upstream_id,
             enabled, status, now, now))


    def row(self, account_id: int) -> dict:
        return dict(db.query_one(
            'SELECT * FROM accounts WHERE id = ?', (account_id,)))


class CryptoTests(AccountTestCase):
    def test_roundtrip(self) -> None:
        pat = 'pt-你好世界-1234'
        enc = accountsvc.encrypt_pat(pat)
        self.assertNotEqual(enc, pat)
        self.assertNotIn(pat, enc)
        self.assertEqual(accountsvc.decrypt_pat(enc), pat)

    def test_key_file_created(self) -> None:
        self.assertFalse((config.DATA_DIR / 'secret.key').exists())
        accountsvc.encrypt_pat('pt-x')
        self.assertTrue((config.DATA_DIR / 'secret.key').is_file())

    def test_key_reused_across_cache_reset(self) -> None:
        enc = accountsvc.encrypt_pat('pt-keep')
        accountsvc._reset_crypto_cache()  # 模拟进程重启：密钥必须仍从磁盘读
        self.assertEqual(accountsvc.decrypt_pat(enc), 'pt-keep')
        # 同一密钥两次加密的密文不同（Fernet 带随机 IV），但都能解回
        self.assertEqual(accountsvc.decrypt_pat(accountsvc.encrypt_pat('pt-keep')),
                         'pt-keep')

    def test_tampered_ciphertext_rejected(self) -> None:
        enc = accountsvc.encrypt_pat('pt-good')
        with self.assertRaises(Exception):
            accountsvc.decrypt_pat(enc[:-6] + 'XXXXXX')


class CrudTests(AccountTestCase):
    def test_create_and_list_masked(self) -> None:
        self.mock_sync_probe(True)
        created = accountsvc.create_account('主号', 'pt-abcdef123456')
        self.assertEqual(created['pat_suffix'], '3456')
        self.assertEqual(created['status'], 'active')
        self.assertEqual(created['test'], {'ok': True, 'msg': '模拟测活'})
        accounts = accountsvc.list_accounts()
        self.assertEqual(len(accounts), 1)
        a = accounts[0]
        self.assertEqual(a['label'], '主号')
        self.assertEqual(a['pat_suffix'], '3456')
        # 掩码形态：明文与密文都不出接口
        self.assertNotIn('_pat', a)
        self.assertNotIn('pat_encrypted', a)
        self.assertNotIn('pt-abcdef123456', str(a))

    def test_create_marks_invalid_when_probe_fails(self) -> None:
        self.mock_sync_probe(False, '上游返回 401（PAT 无效或已过期）')
        created = accountsvc.create_account('坏号', 'pt-bad-invalid')
        self.assertEqual(created['status'], 'invalid')
        self.assertFalse(created['test']['ok'])
        row = self.row(created['id'])
        self.assertEqual(row['last_check_ok'], 0)

    def test_create_validates_input(self) -> None:
        with self.assertRaises(ValueError):
            accountsvc.create_account('', 'pt-x')
        with self.assertRaises(ValueError):
            accountsvc.create_account('名', '   ')

    def test_get_account_internal_plaintext(self) -> None:
        aid = self.insert('内部', 'pt-secret99')
        got = accountsvc.get_account(aid)
        self.assertEqual(got['_pat'], 'pt-secret99')
        self.assertEqual(got['pat_suffix'], 'et99')
        self.assertNotIn('pat_encrypted', got)
        self.assertIsNone(accountsvc.get_account(999))

    def test_update_account(self) -> None:
        aid = self.insert('旧名', 'pt-x1', status='unknown')
        out = accountsvc.update_account(
            aid, {'label': '新名', 'enabled': False, 'weight': 3, 'bogus': 1})
        self.assertEqual(out['label'], '新名')
        self.assertEqual(out['enabled'], 0)
        self.assertEqual(out['weight'], 3)
        row = self.row(aid)
        self.assertEqual(row['note'], '')  # 未提供的字段不动
        # 未知字段被忽略而不是报错（PATCH 语义）
        self.assertIsNone(accountsvc.update_account(999, {'label': 'x'}))
        with self.assertRaises(ValueError):
            accountsvc.update_account(aid, {'label': ' '})
        with self.assertRaises(ValueError):
            accountsvc.update_account(aid, {'weight': 0})

    def test_delete_account(self) -> None:
        aid = self.insert('待删', 'pt-del')
        self.assertTrue(accountsvc.delete_account(aid))
        self.assertFalse(accountsvc.delete_account(aid))

    def test_list_filters(self) -> None:
        self.insert('默认组', 'pt-a1')
        self.insert('分组7', 'pt-a2', upstream_id=7)
        self.insert('停用', 'pt-a3', enabled=0)
        self.assertEqual([a['label'] for a in accountsvc.list_accounts()],
                         ['默认组', '分组7', '停用'])
        self.assertEqual([a['label'] for a in accountsvc.list_accounts(7)], ['分组7'])
        self.assertEqual([a['label'] for a in
                          accountsvc.list_accounts(include_disabled=False)],
                         ['默认组', '分组7'])

    def test_reveal_accounts(self) -> None:
        self.insert('甲', 'pt-open1111', status='invalid')
        items = accountsvc.reveal_accounts()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['pat'], 'pt-open1111')
        self.assertEqual(items[0]['status'], 'invalid')

    def test_mark_result_and_bump_usage(self) -> None:
        aid = self.insert('计数', 'pt-u1')
        accountsvc.bump_usage(aid, 100)
        accountsvc.bump_usage(aid, 50)
        row = self.row(aid)
        self.assertEqual(row['requests'], 2)
        self.assertEqual(row['total_tokens'], 150)
        accountsvc.mark_result(aid, True, 'ok')
        self.assertEqual(self.row(aid)['status'], 'active')
        accountsvc.mark_result(aid, False, '上游返回 401')
        self.assertEqual(self.row(aid)['status'], 'invalid')


class PickTests(AccountTestCase):
    def test_round_robin_skips_invalid(self) -> None:
        a = self.insert('甲', 'pt-r1')
        b = self.insert('乙', 'pt-r2')
        self.insert('丙', 'pt-r3', status='invalid')
        seen = [accountsvc.pick_pat()[0] for _ in range(4)]
        self.assertEqual(seen, [a, b, a, b])

    def test_skips_disabled_and_decrypted(self) -> None:
        self.insert('停用', 'pt-d1', enabled=0)
        aid = self.insert('启用', 'pt-d2')
        picked = accountsvc.pick_pat()
        self.assertEqual(picked[0], aid)
        self.assertEqual(picked[1], 'pt-d2')  # 返回的是解密后的明文 PAT

    def test_unknown_status_is_candidate(self) -> None:
        aid = self.insert('未测', 'pt-unk', status='unknown')
        self.assertEqual(accountsvc.pick_pat()[0], aid)

    def test_empty_or_all_invalid_pool(self) -> None:
        self.assertIsNone(accountsvc.pick_pat())
        self.insert('坏', 'pt-i1', status='invalid')
        self.assertIsNone(accountsvc.pick_pat())  # 全部 invalid：不硬选

    def test_upstream_isolation(self) -> None:
        self.insert('默认', 'pt-g1')
        g = self.insert('组3', 'pt-g2', upstream_id=3)
        self.assertEqual(accountsvc.pick_pat(3)[0], g)
        self.assertNotEqual(accountsvc.pick_pat()[0], g)  # 默认分组看不到组3
        # 分组各自独立指针，互不干扰
        self.assertEqual(accountsvc.pick_pat(3)[0], g)


class KeepaliveTests(AccountTestCase):
    def test_parse_time(self) -> None:
        self.assertEqual(keepalive.parse_time('08:00'), 480)
        self.assertEqual(keepalive.parse_time('23:59'), 1439)
        self.assertEqual(keepalive.parse_time('8:05'), 485)  # 缺前导零也接受
        self.assertIsNone(keepalive.parse_time('24:00'))
        self.assertIsNone(keepalive.parse_time('12:60'))
        self.assertIsNone(keepalive.parse_time('abc'))
        self.assertIsNone(keepalive.parse_time(''))
        self.assertIsNone(keepalive.parse_time(None))

    def test_save_and_get_roundtrip(self) -> None:
        cfg = keepalive.save_config(
            {'enabled': True, 'times': ['09:00', '08:00', '8:00'], 'timeout': 20})
        self.assertEqual(cfg['times'], ['08:00', '09:00'])  # 归一化 + 去重 + 升序
        self.assertEqual(keepalive.get_config(), cfg)  # 落库后可读回

    def test_save_validates(self) -> None:
        with self.assertRaises(ValueError):
            keepalive.save_config({'times': ['25:00']})
        with self.assertRaises(ValueError):
            keepalive.save_config({'times': '08:00'})  # 必须是数组
        with self.assertRaises(ValueError):
            keepalive.save_config({'times': []})  # 至少一个时刻
        with self.assertRaises(ValueError):
            keepalive.save_config({'times': ['08:00'], 'timeout': 200})
        with self.assertRaises(ValueError):
            keepalive.save_config({'times': ['08:00'], 'timeout': 'abc'})

    def test_get_config_defaults_and_tolerates_corruption(self) -> None:
        self.assertEqual(keepalive.get_config(),
                         {'enabled': False, 'times': ['08:00'], 'timeout': 15})
        db.set_setting('keepalive', {'times': '坏数据', 'timeout': 'x'})
        cfg = keepalive.get_config()  # 手改库写坏时不炸，逐项回落默认
        self.assertEqual(cfg['times'], ['08:00'])
        self.assertEqual(cfg['timeout'], 15)

    def test_scheduler_lifecycle(self) -> None:
        async def scenario() -> None:
            self.assertTrue(keepalive.start_scheduler())
            self.assertTrue(keepalive.start_scheduler())  # 幂等
            keepalive.stop_scheduler()
            keepalive.stop_scheduler()  # 幂等
        run(scenario())
        self.assertFalse(keepalive.start_scheduler())  # 无事件循环时拒启


class MockUpstreamTests(AccountTestCase):
    """对 dev/mock_upstream.py 子进程验证上游客户端与真实测活链路。"""

    base_url = ''

    @classmethod
    def setUpClass(cls) -> None:
        sock = socket.socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        cls._proc = subprocess.Popen(
            [sys.executable, str(DEV_MOCK), '--port', str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.time() + 20
        while time.time() < deadline:
            probe_sock = socket.socket()
            if probe_sock.connect_ex(('127.0.0.1', port)) == 0:
                probe_sock.close()
                cls.base_url = f'http://127.0.0.1:{port}'
                return
            probe_sock.close()
            if cls._proc.poll() is not None:
                break
            time.sleep(0.2)
        cls._proc.kill()
        raise RuntimeError('mock 上游未能在 20s 内就绪')

    @classmethod
    def tearDownClass(cls) -> None:
        cls._proc.terminate()
        try:
            cls._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # noqa: PERF203
            cls._proc.kill()
        run(config.close_clients())

    def setUp(self) -> None:
        super().setUp()
        self._orig_upstream = config.QC2API_BASE
        config.QC2API_BASE = self.base_url  # 默认上游指向 mock

    def tearDown(self) -> None:
        # 每个用例各起一个事件循环，客户端池按循环分桶：用完即清，
        # 避免跨用例残留未关闭的连接触发 ResourceWarning
        run(config.close_clients())
        config.QC2API_BASE = self._orig_upstream
        super().tearDown()

    # ── qoder2api 客户端 ──
    def test_get_models_with_valid_pat(self) -> None:
        body = run(qoder2api.get_models(self.base_url, 'pt-good'))
        self.assertEqual(body['object'], 'list')
        self.assertTrue(body['data'])

    def test_get_models_rejects_invalid_pat(self) -> None:
        with self.assertRaises(qoder2api.UpstreamError) as ctx:
            run(qoder2api.get_models(self.base_url, 'pt-wrong-invalid'))
        self.assertIn('401', str(ctx.exception))

    def test_get_models_unreachable(self) -> None:
        with self.assertRaises(qoder2api.UpstreamError) as ctx:
            run(qoder2api.get_models('http://127.0.0.1:1', 'pt-x'))
        self.assertIn('无法连接', str(ctx.exception))

    def test_probe(self) -> None:
        ok, msg = run(qoder2api.probe(self.base_url))
        self.assertTrue(ok)
        ok, msg = run(qoder2api.probe('http://127.0.0.1:1'))
        self.assertFalse(ok)

    def test_chat_url(self) -> None:
        self.assertEqual(run(qoder2api.chat_url('http://x:1/')),
                         'http://x:1/v1/chat/completions')

    def test_forward_headers(self) -> None:
        h = qoder2api.forward_headers('pt-a')
        self.assertEqual(h, {'Authorization': 'Bearer pt-a'})
        h = qoder2api.forward_headers('pt-a', api_key='sk-extra')
        self.assertEqual(h['Authorization'], 'Bearer pt-a')
        self.assertEqual(h['X-Api-Key'], 'sk-extra')

    # ── 真实测活链路（accountsvc → qoder2api → mock 上游）──
    def test_test_account_active(self) -> None:
        aid = self.insert('好号', 'pt-good', status='unknown')
        result = run(accountsvc.test_account(aid))
        self.assertTrue(result['ok'])
        row = self.row(aid)
        self.assertEqual(row['status'], 'active')
        self.assertEqual(row['last_check_ok'], 1)
        self.assertTrue(row['last_check_at'])

    def test_test_account_invalid(self) -> None:
        aid = self.insert('坏号', 'pt-bad-invalid', status='active')
        result = run(accountsvc.test_account(aid))
        self.assertFalse(result['ok'])
        self.assertEqual(self.row(aid)['status'], 'invalid')
        self.assertIn('401', result['msg'])
        self.assertIsNone(accountsvc.get_account(9999))

    def test_test_all(self) -> None:
        self.insert('好', 'pt-good')
        self.insert('坏', 'pt-bad-invalid')
        self.insert('停用不测', 'pt-good', enabled=0)
        stats = run(accountsvc.test_all())
        self.assertEqual(stats['total'], 2)  # 只测 enabled
        self.assertEqual(stats['ok'], 1)
        self.assertEqual(stats['failed'], 1)

    def test_create_account_live_probe(self) -> None:
        # 不打桩：建账即测走真实同步 HTTP 到 mock 上游
        created = accountsvc.create_account('真测', 'pt-live-9999')
        self.assertTrue(created['test']['ok'])
        self.assertEqual(created['status'], 'active')

    def test_pick_pat_returns_usable_pat(self) -> None:
        self.insert('可调度', 'pt-ready', status='active')
        picked = accountsvc.pick_pat()
        self.assertEqual(picked, (picked[0], 'pt-ready'))


if __name__ == '__main__':
    unittest.main()
