"""upstreamsvc：CRUD、引用计数删除阻断、resolve 兜底与异常、forward key；
settings：model-map 读写与管理端用户流（依赖 security 片，未就绪则跳过）。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, upstreamsvc  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class UpstreamTestCase(unittest.TestCase):
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

    # ── 辅助：造一把密钥 / 一个账号（只填必填列）────────────
    def _add_key(self, upstream_id=None) -> int:
        return db.execute(
            'INSERT INTO api_keys(name, key_hash, prefix, upstream_id) '
            "VALUES('k', 'h', 'qk_x', ?)", (upstream_id,))

    def _add_account(self, upstream_id=None) -> int:
        return db.execute(
            "INSERT INTO accounts(label, pat_encrypted, upstream_id) "
            "VALUES('a', 'enc', ?)", (upstream_id,))


class Crud(UpstreamTestCase):
    def test_create_and_get(self) -> None:
        u = upstreamsvc.create_upstream('备用', 'http://10.0.0.2:8963/',
                                        api_key='sk-1234', note='n', enabled=True)
        self.assertIsNone(upstreamsvc.get_upstream('x'))  # 非法 id 安全返回 None
        got = upstreamsvc.get_upstream(u['id'])
        self.assertEqual(got['name'], '备用')
        self.assertEqual(got['base_url'], 'http://10.0.0.2:8963')  # 去尾斜杠
        self.assertFalse(got['is_default'])
        self.assertTrue(got['enabled'])

    def test_create_rejects_bad_url_and_empty_name(self) -> None:
        with self.assertRaises(ValueError):
            upstreamsvc.create_upstream('x', 'ftp://a')
        with self.assertRaises(ValueError):
            upstreamsvc.create_upstream('  ', 'http://a')

    def test_list_order_default_first(self) -> None:
        a = upstreamsvc.create_upstream('a', 'http://a')
        b = upstreamsvc.create_upstream('b', 'http://b')
        items = upstreamsvc.list_upstreams()
        self.assertEqual([i['id'] for i in items], [None, a['id'], b['id']])
        only = upstreamsvc.list_upstreams(include_default=False)
        self.assertEqual([i['id'] for i in only], [a['id'], b['id']])

    def test_default_upstream_shape(self) -> None:
        d = upstreamsvc.default_upstream()
        self.assertIsNone(d['id'])
        self.assertEqual(d['base_url'], config.QC2API_BASE)
        self.assertEqual(d['api_key'], config.QC2API_KEY)
        self.assertTrue(d['is_default'])

    def test_update_patch_semantics(self) -> None:
        u = upstreamsvc.create_upstream('a', 'http://a', api_key='sk-1')
        # 只传 api_key 且为空串：清空要能生效（'in patch' 判定而非真值判定）
        updated = upstreamsvc.update_upstream(u['id'], {'api_key': ''})
        self.assertEqual(updated['api_key'], '')
        updated = upstreamsvc.update_upstream(u['id'], {'enabled': False, 'note': 'x'})
        self.assertFalse(updated['enabled'])
        self.assertEqual(updated['name'], 'a')  # 未传字段不动
        self.assertIsNone(upstreamsvc.update_upstream(99999, {'note': 'x'}))

    def test_delete_unreferenced(self) -> None:
        u = upstreamsvc.create_upstream('a', 'http://a')
        ok, used = upstreamsvc.delete_upstream(u['id'])
        self.assertTrue(ok)
        self.assertEqual(used, 0)
        self.assertIsNone(upstreamsvc.get_upstream(u['id']))
        ok, used = upstreamsvc.delete_upstream(u['id'])
        self.assertFalse(ok)


class RefCounts(UpstreamTestCase):
    def test_counts_and_delete_block(self) -> None:
        u = upstreamsvc.create_upstream('a', 'http://a')
        self._add_key(u['id'])
        self._add_key(u['id'])
        self._add_account(u['id'])
        self._add_key()  # 未绑定，不计入
        self.assertEqual(upstreamsvc.bound_key_count(), {u['id']: 2})
        self.assertEqual(upstreamsvc.bound_account_count(), {u['id']: 1})
        # 有引用 → 拒删，且计入密钥 + 账号总数
        ok, used = upstreamsvc.delete_upstream(u['id'])
        self.assertFalse(ok)
        self.assertEqual(used, 3)
        self.assertIsNotNone(upstreamsvc.get_upstream(u['id']))
        # 解除全部引用后可删
        db.execute('UPDATE api_keys SET upstream_id = NULL WHERE upstream_id = ?', (u['id'],))
        db.execute('UPDATE accounts SET upstream_id = NULL WHERE upstream_id = ?', (u['id'],))
        ok, used = upstreamsvc.delete_upstream(u['id'])
        self.assertTrue(ok)

    def test_account_reference_alone_blocks(self) -> None:
        u = upstreamsvc.create_upstream('a', 'http://a')
        self._add_account(u['id'])
        ok, used = upstreamsvc.delete_upstream(u['id'])
        self.assertFalse(ok)
        self.assertEqual(used, 1)


class Resolve(UpstreamTestCase):
    def test_unbound_falls_to_default(self) -> None:
        for key in (None, {}, {'upstream_id': None}, {'upstream_id': ''},
                    {'upstream_id': 0}):
            self.assertIsNone(upstreamsvc.resolve_for_key(key)['id'])

    def test_bound_upstream(self) -> None:
        u = upstreamsvc.create_upstream('a', 'http://a')
        row = upstreamsvc.resolve_for_key({'upstream_id': u['id']})
        self.assertEqual(row['id'], u['id'])

    def test_missing_and_disabled_raise(self) -> None:
        with self.assertRaises(upstreamsvc.UpstreamUnavailable):
            upstreamsvc.resolve_for_key({'upstream_id': 424242})
        u = upstreamsvc.create_upstream('a', 'http://a', enabled=False)
        with self.assertRaises(upstreamsvc.UpstreamUnavailable):
            upstreamsvc.resolve_for_key({'upstream_id': u['id']})


class ForwardKey(UpstreamTestCase):
    def test_own_key_wins(self) -> None:
        u = upstreamsvc.create_upstream('a', config.QC2API_BASE, api_key='sk-own')
        self.assertEqual(upstreamsvc.forward_api_key(u), 'sk-own')

    def test_same_base_borrows_default(self) -> None:
        # 地址与默认上游相同但 api_key 留空：沿用默认那把（同址 = 同一套实例）
        u = upstreamsvc.create_upstream('同址', f'{config.QC2API_BASE}/')
        self.assertEqual(upstreamsvc.forward_api_key(u), config.QC2API_KEY)

    def test_other_base_empty_key(self) -> None:
        u = upstreamsvc.create_upstream('a', 'http://elsewhere:1')
        self.assertEqual(upstreamsvc.forward_api_key(u), '')

    def test_default_returns_itself(self) -> None:
        d = upstreamsvc.default_upstream()
        self.assertEqual(upstreamsvc.forward_api_key(d), config.QC2API_KEY)
        self.assertEqual(upstreamsvc.forward_api_key(None), '')


class Probe(UpstreamTestCase):
    def test_empty_base(self) -> None:
        ok, msg = run(upstreamsvc.probe({}))
        self.assertFalse(ok)
        self.assertIn('为空', msg)

    def test_probe_reports_unreachable(self) -> None:
        # services/qoder2api 未就绪时返回「无法连接」而非抛出
        ok, msg = run(upstreamsvc.probe({'base_url': 'http://127.0.0.1:1'}))
        self.assertFalse(ok)


# ── settings 片（model-map / users）：依赖 security，未就绪则整组跳过 ──
try:
    from server.routers import settings as settings_router  # noqa: E402
    from server import security  # noqa: E402
    _SETTINGS_READY = True
except Exception:  # noqa: BLE001 —— security 片未就绪属并行开发的预期状态
    _SETTINGS_READY = False


@unittest.skipUnless(_SETTINGS_READY, 'security 片未就绪：users / model-map 路由测试跳过')
class ModelMap(UpstreamTestCase):
    def test_read_write_roundtrip(self) -> None:
        db.set_setting('model_map', {'gpt-x': 'glm-5.2', '*': 'glm-5.2-air'})
        got = db.get_setting('model_map', {}) or {}
        self.assertEqual(got['*'], 'glm-5.2-air')

    def test_clean_rejects_star_target(self) -> None:
        with self.assertRaises(ValueError):
            settings_router._clean_model_map({'*': '*'})

    def test_clean_drops_empty_and_keeps_fallback(self) -> None:
        clean = settings_router._clean_model_map(
            {' ': '', 'a': 'm1', 'b': None, '*': 'm2'})
        self.assertEqual(clean, {'a': 'm1', '*': 'm2'})

    def test_clean_rejects_non_dict(self) -> None:
        with self.assertRaises(ValueError):
            settings_router._clean_model_map(['a'])


@unittest.skipUnless(_SETTINGS_READY, 'security 片未就绪：users 流测试跳过')
class UsersFlow(UpstreamTestCase):
    """管理端用户流：密码策略联动 + sv 递增（真实调用 security 的纯函数）。"""

    def setUp(self) -> None:
        super().setUp()
        self._orig_users = config.USERS_FILE
        config.USERS_FILE = Path(self._tmp.name) / 'users.json'
        try:
            config.USERS_FILE.unlink()
        except FileNotFoundError:
            pass

    def tearDown(self) -> None:
        config.USERS_FILE = self._orig_users
        super().tearDown()

    def _seed(self) -> dict:
        cfg = {'users': [{
            'username': 'admin', 'role': 'admin',
            'pwd_hash': security.make_hash('AdminPass123!'), 'sv': 5,
        }]}
        security.save_users(cfg)
        return cfg

    def test_password_policy_gates_weak_pw(self) -> None:
        # 与路由同一判据：弱口令在建用户 / 改密码两处都必须被拦下
        self.assertTrue(security.password_problem('123', 'admin'))
        self.assertTrue(security.password_problem('adminadmin', 'admin'))

    def test_change_password_revokes_via_sv(self) -> None:
        cfg = self._seed()
        target = cfg['users'][0]
        target['pwd_hash'] = security.make_hash('NewPass456!')
        target['sv'] = int(target.get('sv') or 0) + 1
        security.save_users(cfg)
        loaded = security.load_users()
        self.assertEqual(loaded['users'][0]['sv'], 6)
        self.assertTrue(security.verify_pwd('NewPass456!', loaded['users'][0]['pwd_hash']))


if __name__ == '__main__':
    unittest.main()
