"""config 模块的基础行为：目录创建与默认值读取（不碰真实 data/）。"""
from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402


class EnsureDirs(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = (
            config.DATA_DIR, config.DB_PATH, config.USERS_FILE,
        )

    def tearDown(self) -> None:
        config.DATA_DIR, config.DB_PATH, config.USERS_FILE = self._orig
        self._tmp.cleanup()

    def test_ensure_dirs_creates_data_dir(self) -> None:
        data = Path(self._tmp.name) / 'nested' / 'data'
        config.DATA_DIR = data
        config.DB_PATH = data / 'manager.db'
        config.USERS_FILE = data / 'users.json'
        config.ensure_dirs()
        self.assertTrue(data.is_dir())
        self.assertTrue(config.DB_PATH.parent.is_dir())
        self.assertTrue(config.USERS_FILE.parent.is_dir())
        # 幂等：重复调用不抛错
        config.ensure_dirs()


class Defaults(unittest.TestCase):
    """未设置任何 QC_ 环境变量时的默认值。"""

    def setUp(self) -> None:
        self._saved = {
            k: os.environ.pop(k)
            for k in list(os.environ) if k.startswith('QC')
        }
        importlib.reload(config)

    def tearDown(self) -> None:
        os.environ.update(self._saved)
        importlib.reload(config)

    def test_core_defaults(self) -> None:
        self.assertEqual(config.PORT, 7865)
        self.assertEqual(config.HOST, '0.0.0.0')
        self.assertEqual(config.QC2API_BASE, 'http://127.0.0.1:8963')
        self.assertEqual(config.QC2API_KEY, '')
        self.assertEqual(config.UPSTREAM_TIMEOUT, 120)
        self.assertTrue(config.TRUST_PROXY)
        self.assertEqual(config.TRUSTED_PROXY_HOPS, 1)
        self.assertEqual(config.TRUSTED_PROXY_CIDRS, ['127.0.0.0/8', '::1/128'])
        self.assertFalse(config.ENABLE_DOCS)
        self.assertFalse(config.AUDIT_ALL_ACCESS)
        self.assertEqual(config.SESSION_DAYS, 1)
        self.assertEqual(config.SESSION_IDLE_HOURS, 12)
        self.assertEqual(config.COOKIE_NAME, 'qc_session')
        self.assertEqual(config.SECURE_COOKIE, 'auto')
        self.assertEqual(config.CORS_ORIGINS, [])
        self.assertEqual(config.GATEWAY_RATE_PER_MIN, 120)
        self.assertEqual(config.GATEWAY_MAX_BODY_MB, 32)
        self.assertEqual(config.BASE_PATH, '')

    def test_paths_live_under_root(self) -> None:
        self.assertEqual(config.DATA_DIR, config.ROOT / 'data')
        self.assertEqual(config.STATIC_DIR, config.ROOT / 'web' / 'out')

    def test_env_overrides(self) -> None:
        os.environ['QC_MANAGER_PORT'] = '9000'
        os.environ['QC2API_BASE'] = 'http://10.0.0.5:8963/'
        os.environ['QC_SESSION_DAYS'] = '7'
        os.environ['QC_CORS_ORIGINS'] = 'https://a.com, https://b.com'
        try:
            importlib.reload(config)
            self.assertEqual(config.PORT, 9000)
            self.assertEqual(config.QC2API_BASE, 'http://10.0.0.5:8963')
            self.assertEqual(config.SESSION_DAYS, 7)
            # 与源实现一致：只按逗号切、滤空项，不剥条目内空白
            self.assertEqual(config.CORS_ORIGINS,
                             ['https://a.com', ' https://b.com'])
        finally:
            os.environ.pop('QC_MANAGER_PORT')
            os.environ.pop('QC2API_BASE')
            os.environ.pop('QC_SESSION_DAYS')
            os.environ.pop('QC_CORS_ORIGINS')

    def test_session_days_floor(self) -> None:
        """0 会让 cookie 立刻过期，必须钳到 1。"""
        os.environ['QC_SESSION_DAYS'] = '0'
        try:
            importlib.reload(config)
            self.assertEqual(config.SESSION_DAYS, 1)
        finally:
            os.environ.pop('QC_SESSION_DAYS')


class Secrets(unittest.TestCase):
    def test_new_secret_unique(self) -> None:
        self.assertNotEqual(config.new_secret(), config.new_secret())


if __name__ == '__main__':
    unittest.main()
