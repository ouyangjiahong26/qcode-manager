"""系统更新与 PG 备份的纯逻辑测试。

网络（GitHub API）、PG 连接、子进程重启全部 mock / 假驱动，不真连：

  · 版本比较与缓存 —— check_updates 的 TTL、强制刷新、旧结果保留（stale）；
  · sha256 校验 —— 摘要不符必须当场拒绝，而不是解压到一半才炸；
  · changelog 解析 —— 喂固定格式的样本；
  · pgsync 配置 —— 读写往返、密码脱敏与「掩码不回写」，DSN 拼装。
"""
from __future__ import annotations

import io
import json
import sqlite3
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, pgsync, systemupd  # noqa: E402


class _Case(unittest.TestCase):
    """公共夹具：数据库与数据目录指到临时目录，模块级 Path 常量一并替换。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._orig = (
            config.DB_PATH, config.DATA_DIR,
            systemupd.STATUS_FILE, systemupd.LOCK_FILE, systemupd.LOG_FILE,
            systemupd.PACKAGE_FILE, systemupd._VERSION_CACHE_FILE,
            pgsync.STATUS_FILE,
        )
        config.DB_PATH = root / 't.db'
        config.DATA_DIR = root
        systemupd.STATUS_FILE = root / 'update-status.json'
        systemupd.LOCK_FILE = root / 'update.lock'
        systemupd.LOG_FILE = root / 'update.log'
        systemupd.PACKAGE_FILE = root / 'update-package.tar.gz'
        systemupd._VERSION_CACHE_FILE = root / 'version-check.json'
        pgsync.STATUS_FILE = root / 'pgsync-status.json'
        db._conn = None
        db.connect()

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        (config.DB_PATH, config.DATA_DIR,
         systemupd.STATUS_FILE, systemupd.LOCK_FILE, systemupd.LOG_FILE,
         systemupd.PACKAGE_FILE, systemupd._VERSION_CACHE_FILE,
         pgsync.STATUS_FILE) = self._orig
        self._tmp.cleanup()


# ── 版本比较 ─────────────────────────────────────────────
class VersionCompareTest(unittest.TestCase):
    def test_parse(self) -> None:
        self.assertEqual(systemupd._parse_version('v1.2.3'), (1, 2, 3))
        self.assertEqual(systemupd._parse_version('1.2'), (1, 2))
        self.assertEqual(systemupd._parse_version('v1.2.3-beta'), (1, 2, 3))
        self.assertIsNone(systemupd._parse_version(''))
        self.assertIsNone(systemupd._parse_version('abc'))
        self.assertIsNone(systemupd._parse_version('1.x.2'))

    def test_newer(self) -> None:
        self.assertTrue(systemupd._version_newer('v1.2.10', 'v1.2.9'))
        self.assertTrue(systemupd._version_newer('v2.0', 'v1.9.9'))
        self.assertFalse(systemupd._version_newer('v1.0.5', 'v1.0.5'))
        self.assertFalse(systemupd._version_newer('v1.0.4', 'v1.0.5'))
        # 补零对齐：1.2 不算新于 1.2.0
        self.assertFalse(systemupd._version_newer('v1.2', 'v1.2.0'))
        # 解析不了宁可不说有更新，也不误报
        self.assertFalse(systemupd._version_newer('latest', 'v1.0.0'))
        self.assertFalse(systemupd._version_newer('v1.0.0', 'unknown'))


# ── Release 响应解析（mock GitHub API）──────────────────
def _release(tag: str = 'v9.9.9', *, assets: list | None = None,
             fallback: bool = False) -> dict:
    return {
        'tag_name': tag,
        'html_url': f'https://github.com/{systemupd.MANAGER_REPO}/releases/tag/{tag}',
        'body': '## 更新内容\n- 修复若干问题',
        'published_at': '2026-10-01T00:00:00Z',
        'assets': [{'name': f'qcoder-manager-{tag}.tar.gz',
                    'browser_download_url': 'https://example.com/pkg.tar.gz',
                    'size': 12345, 'digest': 'sha256:' + 'a' * 64}]
        if assets is None else assets,
        **({'_fallback': True} if fallback else {}),
    }


class RemoteParseTest(unittest.TestCase):
    def test_api_shape(self) -> None:
        with mock.patch.object(systemupd, '_gh_get', return_value=_release()):
            got = systemupd._fetch_remote_versions()
        m = got['manager']
        self.assertEqual(m['latest'], 'v9.9.9')
        self.assertTrue(m['assets_ready'])
        self.assertEqual(m['error'], '')
        self.assertIn('更新内容', m['release_notes'])
        self.assertEqual(m['published_at'], '2026-10-01T00:00:00Z')

    def test_assets_not_ready(self) -> None:
        rel = _release(assets=[{'name': 'src-only.zip'}])
        with mock.patch.object(systemupd, '_gh_get', return_value=rel):
            m = systemupd._fetch_remote_versions()['manager']
        self.assertFalse(m['assets_ready'])

    def test_redirect_fallback_keeps_assets_unknown(self) -> None:
        # 302 回退拿不到资产清单：assets_ready 必须是未知（None），不能误报未就位
        with mock.patch.object(systemupd, '_gh_get', return_value=_release(fallback=True)):
            m = systemupd._fetch_remote_versions()['manager']
        self.assertIsNone(m['assets_ready'])

    def test_not_found(self) -> None:
        exc = urllib.error.HTTPError('u', 404, 'Not Found', None, None)  # type: ignore[arg-type]
        with mock.patch.object(systemupd, '_gh_get', side_effect=exc):
            m = systemupd._fetch_remote_versions()['manager']
        self.assertEqual(m['error'], '未找到 Release')


class CheckUpdateCacheTest(_Case):
    """缓存语义：默认读缓存，force 才打接口；失败保留上次成功结果并标 stale。"""

    def _patch_fetch(self, tag: str = 'v9.9.9', error: str = '') -> mock.MagicMock:
        fresh = {'checked_at': int(time.time()), 'manager': {
            'latest': tag, 'url': '', 'assets_ready': True, 'error': error,
            'release_notes': '', 'published_at': ''}}
        return mock.patch.object(systemupd, '_fetch_remote_versions',
                                 return_value=fresh)

    def _patch_current(self, v: str = 'v1.0.0') -> mock.MagicMock:
        return mock.patch.object(systemupd, 'current_version', return_value=v)

    def test_first_call_fetches_then_cache_hits(self) -> None:
        with self._patch_fetch() as fetch, self._patch_current():
            first = systemupd.check_updates()
            self.assertFalse(first['cached'])
            self.assertTrue(first['manager']['has_update'])
            second = systemupd.check_updates()
            self.assertTrue(second['cached'])
        self.assertEqual(fetch.call_count, 1)
        # 缓存文件确实落盘（下次进程冷启动也能命中）
        self.assertTrue(systemupd._VERSION_CACHE_FILE.is_file())

    def test_force_refetches(self) -> None:
        with self._patch_fetch() as fetch, self._patch_current():
            systemupd.check_updates()
            systemupd.check_updates(force=True)
        self.assertEqual(fetch.call_count, 2)

    def test_expired_cache_refetches(self) -> None:
        with self._patch_current():
            with self._patch_fetch():
                systemupd.check_updates()
            # 把缓存改成 7 小时前：超过 6 小时 TTL，必须重新拉
            data = json.loads(systemupd._VERSION_CACHE_FILE.read_text(encoding='utf-8'))
            data['checked_at'] = int(time.time()) - 7 * 3600
            systemupd._VERSION_CACHE_FILE.write_text(json.dumps(data), encoding='utf-8')
            with self._patch_fetch() as fetch:
                got = systemupd.check_updates()
            self.assertFalse(got['cached'])
        self.assertEqual(fetch.call_count, 1)

    def test_assets_pending_uses_short_ttl(self) -> None:
        with self._patch_current():
            fresh = {'checked_at': int(time.time()), 'manager': {
                'latest': 'v9.9.9', 'url': '', 'assets_ready': False, 'error': '',
                'release_notes': '', 'published_at': ''}}
            with mock.patch.object(systemupd, '_fetch_remote_versions', return_value=fresh):
                systemupd.check_updates()
            data = json.loads(systemupd._VERSION_CACHE_FILE.read_text(encoding='utf-8'))
            data['checked_at'] = int(time.time()) - 10 * 60  # 超 5 分钟短 TTL
            systemupd._VERSION_CACHE_FILE.write_text(json.dumps(data), encoding='utf-8')
            with self._patch_fetch() as fetch:
                systemupd.check_updates()
        self.assertEqual(fetch.call_count, 1)

    def test_failed_check_keeps_last_success_as_stale(self) -> None:
        with self._patch_current():
            with self._patch_fetch(tag='v9.9.9'):
                systemupd.check_updates()
            # 第二次检测失败：不能把界面打成「未知」，但必须标 stale
            with self._patch_fetch(error='HTTP 403') as fetch:
                got = systemupd.check_updates(force=True)
        m = got['manager']
        self.assertTrue(m['stale'])
        self.assertEqual(m['latest'], 'v9.9.9')
        self.assertEqual(m['error'], 'HTTP 403')
        # 拿旧缓存断言「有更新」仍然成立，但界面会据 stale 提示结论来自上次
        self.assertTrue(m['has_update'])

    def test_no_downgrade_hint(self) -> None:
        # 当前版本领先于远端：不能提示「更新」（1.0.5 → 1.0.4 是降级）
        with self._patch_fetch(tag='v1.0.4'), self._patch_current('v1.0.5'):
            got = systemupd.check_updates(force=True)
        self.assertFalse(got['manager']['has_update'])
        self.assertFalse(got['has_any'])


# ── sha256 校验（下载/离线包安装前必须核对）──────────────
class Sha256VerifyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.archive = Path(self._tmp.name) / 'pkg.tar.gz'
        self.archive.write_bytes(b'hello qcoder-manager')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _digest(self) -> str:
        import hashlib
        return hashlib.sha256(self.archive.read_bytes()).hexdigest()

    def test_digest_match_passes(self) -> None:
        info = {'pkg_digest': f'sha256:{self._digest()}'}
        self.assertEqual(systemupd.verify_download(self.archive, info), self._digest())

    def test_digest_mismatch_rejected(self) -> None:
        info = {'pkg_digest': 'sha256:' + '0' * 64}
        with self.assertRaises(RuntimeError) as ctx:
            systemupd.verify_download(self.archive, info)
        self.assertIn('不一致', str(ctx.exception))
        self.assertIn('已拒绝安装', str(ctx.exception))

    def test_size_mismatch_rejected(self) -> None:
        info = {'pkg_size': 5, 'pkg_digest': f'sha256:{self._digest()}'}
        with self.assertRaises(RuntimeError) as ctx:
            systemupd.verify_download(self.archive, info)
        self.assertIn('大小不符', str(ctx.exception))

    def test_no_digest_reports_unverified(self) -> None:
        # 回退路径拿不到发布页摘要：不炸，返回本地摘要供人工比对
        self.assertEqual(systemupd.verify_download(self.archive, {}), self._digest())


# ── 离线升级包 ───────────────────────────────────────────
def _make_package(path: Path, version: str = 'v1.2.3') -> None:
    """构造带 `.version` 的发布包（与发布流水线的目录结构一致）。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as tf:
        root = 'qcoder-manager-1.2.3'
        ver = tarfile.TarInfo(f'{root}/.version')
        data = version.encode()
        ver.size = len(data)
        tf.addfile(ver, io.BytesIO(data))
        main_py = tarfile.TarInfo(f'{root}/server/main.py')
        body = b'app = None\n'
        main_py.size = len(body)
        tf.addfile(main_py, io.BytesIO(body))
    path.write_bytes(buf.getvalue())


class PackageTest(_Case):
    def test_read_package_version(self) -> None:
        _make_package(systemupd.PACKAGE_FILE)
        self.assertEqual(systemupd._read_package_version(systemupd.PACKAGE_FILE), 'v1.2.3')

    def test_package_without_version_marker(self) -> None:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode='w:gz') as tf:
            info = tarfile.TarInfo('root/file.txt')
            data = b'x'
            info.size = 1
            tf.addfile(info, io.BytesIO(data))
        systemupd.PACKAGE_FILE.write_bytes(buf.getvalue())
        self.assertEqual(systemupd._read_package_version(systemupd.PACKAGE_FILE), '')

    def test_corrupt_package_raises_value_error(self) -> None:
        systemupd.PACKAGE_FILE.write_bytes(b'not a tar.gz at all')
        with self.assertRaises(ValueError):
            systemupd._read_package_version(systemupd.PACKAGE_FILE)

    def test_meta_roundtrip(self) -> None:
        systemupd.PACKAGE_FILE.write_bytes(b'x' * 10)
        meta = systemupd.record_package(
            name='pkg.tar.gz', size=10, sha256='a' * 64,
            version='v1.2.3', expected_sha256='b' * 64, uploaded_by='admin')
        self.assertTrue(meta['present'])
        got = systemupd.package_meta()
        self.assertEqual(got['version'], 'v1.2.3')
        self.assertEqual(got['expected_sha256'], 'b' * 64)
        self.assertEqual(got['size'], 10)
        systemupd.clear_package()
        self.assertFalse(systemupd.package_meta()['present'])
        self.assertEqual(systemupd.package_meta().get('version', ''), '')


# ── 目录替换 ─────────────────────────────────────────────
class SwapDirTest(unittest.TestCase):
    def test_swap_replaces_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            incoming = base / 'incoming'
            incoming.mkdir()
            (incoming / 'main.py').write_text('new', encoding='utf-8')
            target = base / 'server'
            target.mkdir()
            (target / 'main.py').write_text('old', encoding='utf-8')

            systemupd.swap_dir(incoming, target)

            self.assertEqual((target / 'main.py').read_text(encoding='utf-8'), 'new')
            # staging / 备份残留不能留在安装目录里
            self.assertFalse((base / 'server.new').exists())
            self.assertFalse((base / 'server.old').exists())


# ── 脱敏与镜像 ───────────────────────────────────────────
class RedactTest(unittest.TestCase):
    def test_proxy_credentials_hidden(self) -> None:
        self.assertEqual(
            systemupd.redact_credentials('http://user:pass@host:8080/x'),
            'http://***@host:8080/x')
        self.assertEqual(
            systemupd.redact_credentials('拉取 https://a:b@gh/api 失败'),
            '拉取 https://***@gh/api 失败')

    def test_plain_text_untouched(self) -> None:
        self.assertEqual(systemupd.redact_credentials('https://github.com/x'), 'https://github.com/x')

    def test_mask_token(self) -> None:
        self.assertEqual(systemupd.mask_token('ghp_1234567890'), '**********7890')
        self.assertEqual(systemupd.mask_token('abcd'), '****')
        self.assertEqual(systemupd.mask_token(''), '')

    def test_normalize_mirror(self) -> None:
        self.assertEqual(systemupd.normalize_mirror('https://gh-proxy.com'),
                         'https://gh-proxy.com/')
        self.assertEqual(systemupd.normalize_mirror('gh-proxy.com'),
                         'https://gh-proxy.com/')
        self.assertEqual(systemupd.normalize_mirror('https://h.com/p/'), 'https://h.com/p/')
        # 查询串 / 空格 / 坏主机一律拒绝（= 不启用）
        self.assertEqual(systemupd.normalize_mirror('https://h.com/?q=1'), '')
        self.assertEqual(systemupd.normalize_mirror('not a url'), '')
        self.assertEqual(systemupd.normalize_mirror(''), '')


# ── changelog 解析 ───────────────────────────────────────
CHANGELOG_SAMPLE = """# 更新日志

<!-- 说明 -->

## [未发布]

### 安全

- 修复 **token** 明文下发
- 子项示例
  - 这里是子项内容

### 计划中

- 等上游稳定后一起发版

---

## [1.0.15] - 2026-09-13

### 修复

- 请求体上限改为跟随配置 `max_body_mb`
  补充的续行说明

---

## [1.0.0] - 2026-08-01

### 新增

- 首个版本
"""


class ChangelogTest(unittest.TestCase):
    def test_parse_structure(self) -> None:
        versions = systemupd.parse_changelog(CHANGELOG_SAMPLE)
        self.assertEqual([v['version'] for v in versions],
                         ['未发布', '1.0.15', '1.0.0'])
        first = versions[0]
        self.assertTrue(first['unreleased'])
        self.assertEqual(first['date'], '')
        self.assertEqual([s['title'] for s in first['sections']], ['安全', '计划中'])
        items = first['sections'][0]['items']
        self.assertEqual(items[0]['level'], 0)
        # 「子项示例」是普通条目；缩进 2 空格的下一行才是子条目
        self.assertEqual(items[1]['text'], '子项示例')
        self.assertEqual(items[2]['level'], 1)

    def test_released_version_carries_date(self) -> None:
        versions = systemupd.parse_changelog(CHANGELOG_SAMPLE)
        self.assertFalse(versions[1]['unreleased'])
        self.assertEqual(versions[1]['date'], '2026-09-13')

    def test_continuation_line_joined(self) -> None:
        versions = systemupd.parse_changelog(CHANGELOG_SAMPLE)
        items = versions[1]['sections'][0]['items']
        self.assertEqual(items[0]['text'],
                         '请求体上限改为跟随配置 `max_body_mb` 补充的续行说明')

    def test_text_before_any_version_ignored(self) -> None:
        versions = systemupd.parse_changelog('# 标题\n说明文字\n')
        self.assertEqual(versions, [])

    def test_load_changelog_reads_file(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / 'CHANGELOG.md'
            path.write_text(CHANGELOG_SAMPLE, encoding='utf-8')
            with mock.patch.object(systemupd, '_candidate_paths', return_value=[path]):
                data = systemupd.load_changelog()
        self.assertTrue(data['available'])
        self.assertEqual(data['total'], 3)
        self.assertFalse(data['truncated'])

    def test_load_changelog_missing_file(self) -> None:
        with mock.patch.object(systemupd, '_candidate_paths',
                               return_value=[Path('Z:/no/such/CHANGELOG.md')]):
            data = systemupd.load_changelog()
        self.assertFalse(data['available'])
        self.assertEqual(data['versions'], [])
        self.assertIn('未找到更新日志文件', data['error'])


# ── 自动更新判定（纯函数）────────────────────────────────
class AutoUpdateDecideTest(unittest.TestCase):
    def _cfg(self) -> dict:
        return systemupd.default_config()

    def test_in_window(self) -> None:
        self.assertTrue(systemupd.in_window(3, 3, 6))
        self.assertTrue(systemupd.in_window(5, 3, 6))
        self.assertFalse(systemupd.in_window(6, 3, 6))
        self.assertFalse(systemupd.in_window(2, 3, 6))
        # 跨午夜
        self.assertTrue(systemupd.in_window(23, 22, 2))
        self.assertTrue(systemupd.in_window(1, 22, 2))
        self.assertFalse(systemupd.in_window(3, 22, 2))
        # 相等 = 全天
        self.assertTrue(systemupd.in_window(0, 5, 5))

    def test_check_due(self) -> None:
        cfg = self._cfg()
        now = 1_000_000.0
        self.assertTrue(systemupd.check_due(cfg, {'last_check_at': 0}, now))
        self.assertFalse(systemupd.check_due(
            cfg, {'last_check_at': now - 60}, now))
        # next_check_at 优先（失败后的短间隔重试）
        self.assertTrue(systemupd.check_due(
            cfg, {'last_check_at': now, 'next_check_at': now - 1}, now))
        self.assertFalse(systemupd.check_due(
            cfg, {'last_check_at': 0, 'next_check_at': now + 3600}, now))

    def _check(self, **kw) -> dict:
        base = {'latest': 'v2.0.0', 'has_update': True, 'error': '',
                'stale': False, 'assets_ready': True}
        base.update(kw)
        return {'manager': base}

    def test_decide_none_cases(self) -> None:
        cfg = self._cfg()
        state: dict = {}
        kw = {'hour': 4, 'running': False, 'state': state, 'now': 0.0}
        self.assertEqual(systemupd.decide(cfg, self._check(error='HTTP 403'), **kw)[0], 'none')
        self.assertEqual(systemupd.decide(cfg, self._check(stale=True), **kw)[0], 'none')
        self.assertEqual(systemupd.decide(cfg, self._check(has_update=False), **kw)[0], 'none')

    def test_decide_wait_cases(self) -> None:
        cfg = self._cfg()
        kw = {'hour': 4, 'running': False, 'state': {}, 'now': 0.0}
        self.assertEqual(systemupd.decide(
            cfg, self._check(assets_ready=False), **kw)[0], 'wait')
        self.assertEqual(systemupd.decide(
            cfg, self._check(), running=True, state={}, hour=4, now=0.0)[0], 'wait')
        state = {'last_attempt_version': 'v2.0.0', 'last_attempt_at': time.time() - 60}
        self.assertEqual(systemupd.decide(
            cfg, self._check(), hour=4, running=False, state=state, now=time.time())[0], 'wait')
        # 时间窗外（默认 3-6 点）
        action, reason = systemupd.decide(
            cfg, self._check(), hour=12, running=False, state={}, now=0.0)
        self.assertEqual(action, 'wait')
        self.assertIn('时间窗', reason)

    def test_decide_update(self) -> None:
        cfg = self._cfg()
        action, reason = systemupd.decide(
            cfg, self._check(), hour=4, running=False, state={}, now=0.0)
        self.assertEqual(action, 'update')
        self.assertIn('v2.0.0', reason)

    def test_validate_and_normalize(self) -> None:
        clean, err = systemupd.validate_config(
            {'enabled': True, 'interval_hours': 12, 'window_start': 1, 'window_end': 5})
        self.assertEqual(err, '')
        self.assertTrue(clean['enabled'])
        self.assertEqual(clean['target'], 'manager')
        # 越界直接报错，而不是悄悄夹值
        _, err = systemupd.validate_config({'interval_hours': 0})
        self.assertIn('检测间隔', err)
        _, err = systemupd.validate_config({'interval_hours': 6, 'window_start': 24})
        self.assertIn('0–23', err)
        # 收敛：未知字段丢弃、越界夹到边界、字符串布尔按字面值判断
        cfg = systemupd.normalize_config(
            {'enabled': 'false', 'interval_hours': 999, 'window_start': -3,
             'evil': 'x'})
        self.assertFalse(cfg['enabled'])
        self.assertEqual(cfg['interval_hours'], systemupd.MAX_INTERVAL_HOURS)
        self.assertEqual(cfg['window_start'], 0)
        self.assertNotIn('evil', cfg)

    def test_config_roundtrip_via_settings(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            orig = config.DB_PATH
            config.DB_PATH = Path(name) / 't.db'
            db._conn = None
            db.connect()
            try:
                systemupd.save_config({'enabled': True, 'interval_hours': 8})
                self.assertTrue(systemupd.get_config()['enabled'])
                self.assertEqual(systemupd.get_config()['interval_hours'], 8)
                self.assertEqual(systemupd.get_state(), {})
            finally:
                if db._conn is not None:
                    db._conn.close()
                db._conn = None
                config.DB_PATH = orig


# ── pgsync：配置读写与脱敏（假 DSN，不真连 PG）───────────
class PgsyncConfigTest(_Case):
    def test_save_and_public_masks_password(self) -> None:
        cfg = pgsync.save_config({'host': 'db.local', 'password': 's3cret',
                                  'user': 'qc', 'dbname': 'qcoder'})
        self.assertEqual(cfg['password'], 's3cret')   # 存的是明文（导出要用）
        public = pgsync.public_config()
        self.assertEqual(public['password'], '********')
        # 端到端再读一次：库里那份也是明文，只有出口脱敏
        self.assertEqual(pgsync.get_config()['password'], 's3cret')

    def test_mask_placeholder_does_not_overwrite(self) -> None:
        pgsync.save_config({'host': 'db.local', 'password': 's3cret'})
        # 界面把掩码原样提交（改了别的字段）：密码必须保持原值
        pgsync.save_config({'password': '********', 'interval_minutes': 30})
        self.assertEqual(pgsync.get_config()['password'], 's3cret')
        self.assertEqual(pgsync.get_config()['interval_minutes'], 30)

    def test_empty_password_keeps_existing(self) -> None:
        pgsync.save_config({'host': 'db.local', 'password': 's3cret'})
        pgsync.save_config({'password': ''})
        self.assertEqual(pgsync.get_config()['password'], 's3cret')

    def test_unknown_keys_ignored(self) -> None:
        pgsync.save_config({'host': 'db.local', 'drop_tables': True})
        self.assertNotIn('drop_tables', pgsync.get_config())

    def test_interval_and_port_clamped(self) -> None:
        cfg = pgsync.save_config({'host': 'h', 'interval_minutes': -5, 'port': 'x'})
        self.assertEqual(cfg['interval_minutes'], 0)
        self.assertEqual(cfg['port'], 5432)

    def test_invalid_sslmode_rejected_on_save(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            pgsync.save_config({'host': 'h', 'sslmode': 'super'})
        self.assertIn('SSL 模式', str(ctx.exception))

    def test_merge_form_prefers_submitted(self) -> None:
        saved = pgsync.get_config()
        merged = pgsync.merge_form(saved, {'host': 'new.local', 'password': ''})
        self.assertEqual(merged['host'], 'new.local')
        self.assertEqual(merged['password'], saved['password'])


class PgsyncDsnTest(unittest.TestCase):
    def test_basic(self) -> None:
        dsn = pgsync.build_dsn({'host': 'db.local', 'port': 5432, 'dbname': 'qc',
                                'user': 'qc', 'password': 'pw', 'sslmode': ''})
        self.assertEqual(dsn, 'postgresql://qc:pw@db.local:5432/qc')

    def test_password_special_chars_quoted(self) -> None:
        # 密码里的 @ / : / / 会让 libpq 解析错位，必须百分号转义
        dsn = pgsync.build_dsn({'host': 'h', 'dbname': 'd', 'user': 'u',
                                'password': 'p@ss:w/d', 'sslmode': 'prefer'})
        self.assertEqual(dsn, 'postgresql://u:p%40ss%3Aw%2Fd@h:5432/d')

    def test_sslmode_only_when_not_default(self) -> None:
        dsn = pgsync.build_dsn({'host': 'h', 'dbname': 'd', 'sslmode': 'require'})
        self.assertTrue(dsn.endswith('?sslmode=require'))
        self.assertNotIn('sslmode', pgsync.build_dsn({'host': 'h', 'dbname': 'd'}))

    def test_empty_host_rejected(self) -> None:
        with self.assertRaises(ValueError):
            pgsync.build_dsn({'host': '   ', 'dbname': 'd'})

    def test_bad_sslmode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            pgsync.build_dsn({'host': 'h', 'sslmode': 'maybe'})


class PgsyncConnectionTest(unittest.TestCase):
    """探测接口的三类失败都以 {ok: False, message} 返回，绝不 500。"""

    def test_missing_driver(self) -> None:
        def boom():
            raise RuntimeError('未安装 PostgreSQL 驱动。请执行 pip install "psycopg[binary]"')
        with mock.patch.object(pgsync, '_require_psycopg', side_effect=boom):
            got = pgsync.test_connection({'host': 'h', 'dbname': 'd'})
        self.assertFalse(got['ok'])
        self.assertIn('未安装', got['message'])

    def test_empty_host_reported_first(self) -> None:
        got = pgsync.test_connection({'host': ''})
        self.assertFalse(got['ok'])
        self.assertIn('不能为空', got['message'])

    def test_connect_failure_converted(self) -> None:
        class _FakePsycopg:
            @staticmethod
            def connect(*_a, **_k):
                raise OSError('connection refused')
        with mock.patch.object(pgsync, '_require_psycopg', return_value=_FakePsycopg):
            got = pgsync.test_connection({'host': 'h', 'dbname': 'd',
                                          'user': 'u', 'password': 'p'})
        self.assertFalse(got['ok'])
        self.assertIn('连接失败', got['message'])
        self.assertIn('connection refused', got['message'])


class PgsyncSettingsAllowlistTest(unittest.TestCase):
    def test_only_allowlisted_keys_travel(self) -> None:
        cols = ['key', 'value']
        rows = [('model_map', '{}'), ('security', '{}'), ('keepalive', '{}'),
                ('github_token', 'ghp_secret'), ('auto_update', '{}')]
        kept = pgsync._filter_settings_rows('settings', cols, rows)
        self.assertEqual([r[0] for r in kept], ['model_map', 'security', 'keepalive'])

    def test_other_tables_pass_through(self) -> None:
        rows = [(1, 'a'), (2, 'b')]
        self.assertEqual(pgsync._filter_settings_rows('accounts', ['id', 'x'], rows), rows)

    def test_settings_without_key_column_drops_all(self) -> None:
        # 结构不认识就不搬：避免把可能含凭据的整表写进镜像
        self.assertEqual(pgsync._filter_settings_rows('settings', ['value'], [('x',)]), [])


class PgsyncCopyTest(unittest.TestCase):
    """搬运语义：SQLite 文件当假 PG，全量覆盖 + 列交集 + settings 白名单。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.src_path = Path(self._tmp.name) / 'src.db'
        conn = sqlite3.connect(str(self.src_path))
        conn.executescript(
            "CREATE TABLE accounts(id INTEGER PRIMARY KEY, label TEXT NOT NULL);"
            "INSERT INTO accounts VALUES (1, 'a'), (2, 'b');"
            "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);"
            "INSERT INTO settings VALUES ('model_map', '{}'),"
            " ('github_token', 'ghp_secret');"
        )
        conn.commit()
        conn.close()
        self.dst_path = Path(self._tmp.name) / 'dst.db'
        conn = sqlite3.connect(str(self.dst_path))
        conn.executescript(
            # 本地多一列：恢复时取交集，多的列保持默认值
            "CREATE TABLE accounts(id INTEGER PRIMARY KEY, label TEXT NOT NULL,"
            " note TEXT DEFAULT '');"
            "INSERT INTO accounts VALUES (9, 'stale', 'old');"
            "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT);"
            "INSERT INTO settings VALUES ('github_token', 'ghp_local'),"
            " ('auto_update', '{}');"
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_overwrite_not_merge_and_settings_filtered(self) -> None:
        src = pgsync._SqliteSource(self.src_path)
        dst_conn = sqlite3.connect(str(self.dst_path))
        sink = pgsync._SqliteSink(dst_conn)
        # 恢复语义：先清空再灌入（settings 只清白名单 key）
        with dst_conn:
            for table in sorted(src.tables()):
                cols = src.columns(table)
                names = [c['name'] for c in cols]
                wanted = sink.accepts(table, names)
                if table == 'settings':
                    keys = sorted(pgsync._SETTING_ALLOWLIST)
                    ph = ', '.join('?' * len(keys))
                    dst_conn.execute(
                        f'DELETE FROM "settings" WHERE "key" IN ({ph})', keys)
                else:
                    dst_conn.execute(f'DELETE FROM "{table}"')
                for chunk in src.rows(table, wanted):
                    sink.write(table, wanted, chunk)
        rows = dst_conn.execute('SELECT id, label FROM accounts ORDER BY id').fetchall()
        self.assertEqual([tuple(r) for r in rows], [(1, 'a'), (2, 'b')])  # 旧行被覆盖
        settings = dict(dst_conn.execute('SELECT key, value FROM settings').fetchall())
        # 白名单 key 已被镜像内容替换；凭据 / 更新链路 key 原样保留
        self.assertEqual(settings['model_map'], '{}')
        self.assertEqual(settings['github_token'], 'ghp_local')
        self.assertIn('auto_update', settings)
        src.close()
        dst_conn.close()


if __name__ == '__main__':
    unittest.main()
