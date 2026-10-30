"""security/tokensvc 单元测试：密码哈希、密码策略、签名会话、防爆破、管理 Token。

用 tempfile 目录隔离 users.json 与 SQLite，不触碰真实 data/。
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from server import config, db, security, tokensvc


class IsolatedEnvMixin:
    """每个用例一套独立的 DATA_DIR/USERS_FILE/DB_PATH。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._orig = (config.DATA_DIR, config.DB_PATH, config.USERS_FILE)
        config.DATA_DIR = tmp
        config.DB_PATH = tmp / 'manager.db'
        config.USERS_FILE = tmp / 'users.json'
        # 复位进程级状态：登录失败计数 / 连接缓存
        security._fail.clear()
        security._user_fail.clear()
        db._close_connection_locked()

    def tearDown(self) -> None:
        db._close_connection_locked()
        config.DATA_DIR, config.DB_PATH, config.USERS_FILE = self._orig
        security._fail.clear()
        security._user_fail.clear()
        self._tmp.cleanup()


class TestPasswordHash(IsolatedEnvMixin, unittest.TestCase):
    def test_roundtrip(self) -> None:
        stored = security.make_hash('s3cret-passw0rd')
        self.assertTrue(stored.startswith('pbkdf2_sha256$260000$'))
        self.assertTrue(security.verify_pwd('s3cret-passw0rd', stored))

    def test_wrong_password(self) -> None:
        stored = security.make_hash('s3cret-passw0rd')
        self.assertFalse(security.verify_pwd('s3cret-passw1rd', stored))

    def test_malformed_hash(self) -> None:
        self.assertFalse(security.verify_pwd('x', 'not-a-hash'))
        self.assertFalse(security.verify_pwd('x', 'md5$1$aa$bb'))

    def test_dummy_verify_noop(self) -> None:
        security.dummy_verify('whatever')  # 不抛错即可


class TestPasswordPolicy(unittest.TestCase):
    def test_short(self) -> None:
        self.assertIn('至少', security.password_problem('a1!short'))

    def test_weak_blacklist(self) -> None:
        self.assertEqual(security.password_problem('admin123456789'), '密码过于常见，请换一个')
        self.assertEqual(security.password_problem('password1234'), '密码过于常见，请换一个')

    def test_username_plus_digits(self) -> None:
        self.assertIn('用户名', security.password_problem('admin20240101', 'admin'))

    def test_all_digits(self) -> None:
        self.assertIn('全是数字', security.password_problem('918273645501'))

    def test_repeated_char(self) -> None:
        self.assertIn('重复字符', security.password_problem('aaaaaaaaaaaa'))

    def test_ok(self) -> None:
        self.assertIsNone(security.password_problem('s3cret-passw0rd'))


class TestUserStore(IsolatedEnvMixin, unittest.TestCase):
    def test_bootstrap_creates_admin(self) -> None:
        cfg = security.load_users()
        self.assertIn('secret', cfg)
        self.assertEqual(cfg['users'][0]['username'], 'admin')
        self.assertTrue(config.USERS_FILE.exists())

    def test_bootstrap_env_password(self) -> None:
        old = config.ADMIN_PASSWORD
        config.ADMIN_PASSWORD = 'env-passw0rd-123'
        try:
            cfg = security.load_users()
            self.assertTrue(security.verify_pwd('env-passw0rd-123',
                                                cfg['users'][0]['pwd_hash']))
            self.assertFalse(security.initial_password_path().exists())
        finally:
            config.ADMIN_PASSWORD = old

    def test_bootstrap_rejects_weak_env_password(self) -> None:
        old = config.ADMIN_PASSWORD
        config.ADMIN_PASSWORD = 'admin123456789'
        try:
            with self.assertRaises(RuntimeError):
                security.load_users()
        finally:
            config.ADMIN_PASSWORD = old

    def test_random_password_written_to_file(self) -> None:
        security.load_users()
        pwd = security.initial_password_path().read_text(encoding='utf-8').strip()
        self.assertTrue(len(pwd) >= 12)
        security.clear_initial_password_file()
        self.assertFalse(security.initial_password_path().exists())

    def test_corrupt_file_refuses_rebuild(self) -> None:
        security.load_users()
        before = config.USERS_FILE.read_text(encoding='utf-8')
        config.USERS_FILE.write_text('{ broken json', encoding='utf-8')
        with self.assertRaises(RuntimeError):
            security.load_users()
        # 现场保留：文件内容未被覆盖
        self.assertEqual(config.USERS_FILE.read_text(encoding='utf-8'), '{ broken json')
        config.USERS_FILE.write_text(before, encoding='utf-8')

    def test_save_users_atomic_no_tmp_left(self) -> None:
        cfg = security.load_users()
        cfg['users'].append({'username': 'ops', 'role': 'viewer',
                             'pwd_hash': security.make_hash('ops-passw0rd-99')})
        security.save_users(cfg)
        leftovers = [p.name for p in config.DATA_DIR.iterdir() if p.name.endswith('.tmp')]
        self.assertEqual(leftovers, [])
        self.assertEqual(len(security.load_users()['users']), 2)


class TestSessionSign(IsolatedEnvMixin, unittest.TestCase):
    def test_issue_and_unsign_roundtrip(self) -> None:
        security.load_users()
        token = security.issue_token('admin', 'admin')
        cfg = security.load_users()
        obj = security._unsign(token, cfg['secret'])
        self.assertIsNotNone(obj)
        self.assertEqual(obj['username'], 'admin')
        self.assertEqual(obj['sv'], 0)

    def test_tampered_token_rejected(self) -> None:
        cfg = security.load_users()
        token = security.issue_token('admin', 'admin')
        # 篡改 payload 后签名不再匹配
        obj = security._unsign(token[:-4] + 'AAAA', cfg['secret'])
        self.assertIsNone(obj)

    def test_wrong_secret_rejected(self) -> None:
        security.load_users()
        token = security.issue_token('admin', 'admin')
        self.assertIsNone(security._unsign(token, config.new_secret()))

    def test_expired_token_rejected(self) -> None:
        cfg = security.load_users()
        payload = json.dumps({'username': 'admin', 'role': 'admin', 'sv': 0,
                              'iat': int(time.time()), 'orig': int(time.time()),
                              'exp': int(time.time()) - 10})
        token = security._sign(payload, cfg['secret'])
        self.assertIsNone(security._unsign(token, cfg['secret']))

    def test_revoke_increments_sv(self) -> None:
        security.load_users()
        self.assertEqual(security.revoke_sessions('admin'), 1)
        self.assertEqual(security.revoke_sessions('admin'), 2)
        self.assertEqual(
            security.session_version(security.load_users(), 'admin'), 2)

    def test_idle_expired(self) -> None:
        old = config.SESSION_IDLE_HOURS
        try:
            config.SESSION_IDLE_HOURS = 1
            fresh = {'iat': int(time.time())}
            stale = {'iat': int(time.time()) - 2 * 3600}
            self.assertFalse(security.idle_expired(fresh))
            self.assertTrue(security.idle_expired(stale))
            config.SESSION_IDLE_HOURS = 0
            self.assertFalse(security.idle_expired(stale))  # 关闭空闲判定
        finally:
            config.SESSION_IDLE_HOURS = old

    def test_needs_renewal_third_of_window(self) -> None:
        old = config.SESSION_IDLE_HOURS
        try:
            config.SESSION_IDLE_HOURS = 3
            recent = {'iat': int(time.time())}
            aged = {'iat': int(time.time()) - 3600 - 60}  # 超过 1/3 窗口
            self.assertFalse(security.needs_renewal(recent))
            self.assertTrue(security.needs_renewal(aged))
        finally:
            config.SESSION_IDLE_HOURS = old


class TestBruteForce(IsolatedEnvMixin, unittest.TestCase):
    def test_lock_after_max_fails(self) -> None:
        for _ in range(security.MAX_FAILS):
            security.record_fail('1.2.3.4', 'admin')
        self.assertTrue(security.login_blocked('1.2.3.4', 'admin'))
        self.assertTrue(security.login_blocked('1.2.3.4'))

    def test_below_threshold_not_locked(self) -> None:
        for _ in range(security.MAX_FAILS - 1):
            security.record_fail('1.2.3.4', 'admin')
        self.assertFalse(security.login_blocked('1.2.3.4', 'admin'))

    def test_clear_fail_unlocks(self) -> None:
        for _ in range(security.MAX_FAILS):
            security.record_fail('1.2.3.4', 'admin')
        security.clear_fail('1.2.3.4', 'admin')
        self.assertFalse(security.login_blocked('1.2.3.4', 'admin'))

    def test_window_expiry_unlocks(self) -> None:
        # 直接注入过期的失败记录：窗口一过即解锁
        security._fail['1.2.3.4'] = [security.MAX_FAILS,
                                     time.time() - security.LOCK_SECONDS - 1]
        self.assertFalse(security.login_blocked('1.2.3.4'))

    def test_username_lock_does_not_hurt_clean_ip(self) -> None:
        # 攻击者换 IP 爆破 admin：admin 维度满 5 次
        for i in range(security.MAX_FAILS):
            security.record_fail(f'10.0.0.{i}', 'admin')
        # 干净 IP 从未失败过：不被按用户名锁定牵连（防远程锁定 DoS）
        self.assertFalse(security.login_blocked('9.9.9.9', 'admin'))
        # 但失败过的 IP 仍被拦
        self.assertTrue(security.login_blocked('10.0.0.1', 'admin'))

    def test_prune_never_clears_active_locks(self) -> None:
        # 灌满超过 MAX_TRACKED 且全部仍在锁定窗口内：不得清掉任何条目
        for i in range(security.MAX_TRACKED + 50):
            security.record_fail(f'172.16.0.{i % 256}-{i}', f'user{i}')
        # 被灌满后，锁定窗口内的真实锁定依然生效
        for _ in range(security.MAX_FAILS):
            security.record_fail('172.16.0.250', 'admin')
        self.assertGreater(len(security._fail), security.MAX_TRACKED)
        self.assertTrue(security.login_blocked('172.16.0.250'))


class TestTokenSvc(IsolatedEnvMixin, unittest.TestCase):
    def test_create_and_resolve(self) -> None:
        out = tokensvc.create_token('面板', 'readonly', created_by='admin')
        self.assertTrue(out['token'].startswith('qct_'))
        row = tokensvc.resolve(out['token'])
        self.assertIsNotNone(row)
        self.assertEqual(row['name'], '面板')
        self.assertTrue(tokensvc.usable(row))

    def test_resolve_rejects_forgery(self) -> None:
        out = tokensvc.create_token('t', 'admin')
        self.assertIsNone(tokensvc.resolve(out['token'] + 'x'))
        self.assertIsNone(tokensvc.resolve('qk_' + out['token'][4:]))  # 网关前缀不认
        self.assertIsNone(tokensvc.resolve(''))

    def test_scope_degrades_to_readonly(self) -> None:
        self.assertEqual(tokensvc.normalize_scope('ADMIN'), 'admin')
        self.assertEqual(tokensvc.normalize_scope('typo'), 'readonly')
        self.assertEqual(tokensvc.normalize_scope(None), 'readonly')
        self.assertEqual(tokensvc.scope_to_role('admin'), 'admin')
        self.assertEqual(tokensvc.scope_to_role('typo'), 'viewer')
        out = tokensvc.create_token('t', 'superuser')
        self.assertEqual(out['scope'], 'readonly')

    def test_usable_respects_enabled_and_expiry(self) -> None:
        out = tokensvc.create_token('t', 'admin')
        tid = out['id']
        updated = tokensvc.update_token(tid, {'enabled': False})
        self.assertIsNotNone(updated)
        self.assertFalse(updated['enabled'])
        self.assertFalse(tokensvc.usable(tokensvc.resolve(out['token'])))
        tokensvc.update_token(tid, {'enabled': True, 'expires_at': int(time.time()) - 1})
        self.assertFalse(tokensvc.usable(tokensvc.resolve(out['token'])))
        tokensvc.update_token(tid, {'expires_at': None})  # null = 永不过期
        self.assertTrue(tokensvc.usable(tokensvc.resolve(out['token'])))

    def test_delete_token(self) -> None:
        out = tokensvc.create_token('t')
        self.assertTrue(tokensvc.delete_token(out['id']))
        self.assertIsNone(tokensvc.resolve(out['token']))
        self.assertFalse(tokensvc.delete_token(out['id']))

    def test_touch_throttled(self) -> None:
        out = tokensvc.create_token('t')
        tokensvc.touch(out['id'], '1.1.1.1')
        first = db.query_one('SELECT last_used_at, last_used_ip FROM api_tokens WHERE id = ?',
                             (out['id'],))
        self.assertEqual(first['last_used_ip'], '1.1.1.1')
        # 节流窗口内的第二次 touch 不更新
        tokensvc.touch(out['id'], '2.2.2.2')
        second = db.query_one('SELECT last_used_ip FROM api_tokens WHERE id = ?',
                              (out['id'],))
        self.assertEqual(second['last_used_ip'], '1.1.1.1')

    def test_list_tokens_no_plaintext(self) -> None:
        out = tokensvc.create_token('t')
        listed = tokensvc.list_tokens()
        self.assertEqual(len(listed), 1)
        self.assertNotIn('token', listed[0])
        self.assertNotIn('token_hash', listed[0])


if __name__ == '__main__':
    unittest.main()
