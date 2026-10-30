"""keysvc 全函数 + gateway 纯函数的单元测试（临时目录，不碰真实 data/）。"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config, db, keysvc  # noqa: E402
from server.routers import gateway  # noqa: E402


class TmpDbTestCase(unittest.TestCase):
    """公共夹具：把数据库指到临时目录并重建连接。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = Path(self._tmp.name) / 't.db'
        db._conn = None
        db.connect()
        db.set_setting('model_map', {})

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()


# ── keysvc ────────────────────────────────────────────────
class CreateAndResolve(TmpDbTestCase):
    def test_create_returns_plaintext_once_and_qk_prefix(self) -> None:
        created = keysvc.create_key('测试密钥', quota=1000,
                                    ip_allowlist=['10.0.0.0/8', '  ', '1.2.3.4'],
                                    models=['m1'], max_ips=2)
        self.assertTrue(created['key'].startswith('qk_'))
        self.assertEqual(created['quota'], 1000)
        self.assertEqual(created['models'], ['m1'])
        # 白名单写入前去空白、丢空项
        self.assertEqual(created['ip_allowlist'], ['10.0.0.0/8', '1.2.3.4'])
        # 列表里绝不能出现明文与哈希
        listed = keysvc.list_keys()
        self.assertEqual(len(listed), 1)
        self.assertNotIn('key', listed[0])
        self.assertNotIn('key_hash', listed[0])

    def test_resolve_by_prefix_and_hash(self) -> None:
        created = keysvc.create_key('k')
        hit = keysvc.resolve(created['key'])
        self.assertIsNotNone(hit)
        self.assertEqual(hit['id'], created['id'])
        # 错误 token / 前缀不符 / 空 → None
        self.assertIsNone(keysvc.resolve(created['key'] + 'x'))
        self.assertIsNone(keysvc.resolve('qk_' + 'a' * 40))
        self.assertIsNone(keysvc.resolve(''))
        self.assertIsNone(keysvc.resolve('other-prefix-token'))


class UpdateDeleteReset(TmpDbTestCase):
    def test_update_patch_semantics(self) -> None:
        created = keysvc.create_key('k', quota=10)
        # 未提交的字段不动
        updated = keysvc.update_key(created['id'], {'enabled': False})
        self.assertFalse(updated['enabled'])
        self.assertEqual(updated['quota'], 10)
        self.assertEqual(updated['name'], 'k')
        # 显式 null 的 upstream_id 合法（= 默认上游）
        updated = keysvc.update_key(created['id'], {'upstream_id': None})
        self.assertIsNone(updated['upstream_id'])
        # 空名单不覆盖已有值（与源仓库 PATCH 语义一致）
        updated = keysvc.update_key(created['id'], {'name': ''})
        self.assertEqual(updated['name'], 'k')
        self.assertIsNone(keysvc.update_key(99999, {'enabled': True}))

    def test_delete_and_reset(self) -> None:
        created = keysvc.create_key('k')
        keysvc.touch(created, '1.1.1.1', 50)
        self.assertTrue(keysvc.reset_usage(created['id']))
        row = keysvc.list_keys()[0]
        self.assertEqual(row['used_tokens'], 0)
        self.assertTrue(keysvc.delete_key(created['id']))
        self.assertEqual(keysvc.list_keys(), [])
        # 连带删 IP 记录
        self.assertEqual(db.query('SELECT * FROM api_key_ips'), [])
        self.assertFalse(keysvc.delete_key(created['id']))
        self.assertFalse(keysvc.reset_usage(created['id']))


class ValidateBranches(TmpDbTestCase):
    def _key(self, **kw) -> dict:
        return keysvc.create_key('k', **kw)

    def test_enabled_and_expires(self) -> None:
        key = self._key()
        r = keysvc.validate(key, '1.1.1.1', 'm1')
        self.assertIsNone(r)
        disabled = keysvc.update_key(key['id'], {'enabled': False})
        r = keysvc.validate(disabled, '1.1.1.1', 'm1')
        self.assertIsInstance(r, keysvc.Rejection)
        self.assertEqual(r.status, 403)

        expired = self._key(expires_at=int(time.time()) - 10)
        r = keysvc.validate(expired, '1.1.1.1', 'm1')
        self.assertEqual(r.status, 403)
        self.assertEqual(r.code, 'key_expired')
        # 未过期的时间戳放行
        future = self._key(expires_at=int(time.time()) + 3600)
        self.assertIsNone(keysvc.validate(future, '1.1.1.1', 'm1'))

    def test_quota_exhausted_429(self) -> None:
        key = self._key(quota=100)
        keysvc.touch(key, '1.1.1.1', 100)
        key['used_tokens'] = 100
        r = keysvc.validate(key, '1.1.1.1', 'm1')
        self.assertEqual(r.status, 429)
        self.assertEqual(r.code, 'quota_exhausted')

    def test_ip_allowlist_400(self) -> None:
        key = self._key(ip_allowlist=['10.0.0.0/8'])
        r = keysvc.validate(key, '9.9.9.9', 'm1')
        self.assertEqual(r.status, 400)
        self.assertEqual(r.code, 'ip_not_allowed')
        self.assertIsNone(keysvc.validate(key, '10.1.2.3', 'm1'))

    def test_max_ips_400(self) -> None:
        key = self._key(max_ips=1)
        self.assertIsNone(keysvc.validate(key, '1.1.1.1', 'm1'))
        keysvc.touch(key, '1.1.1.1')
        r = keysvc.validate(key, '2.2.2.2', 'm1')
        self.assertEqual(r.status, 400)
        self.assertEqual(r.code, 'too_many_ips')
        # 已见过的 IP 不占新名额
        self.assertIsNone(keysvc.validate(key, '1.1.1.1', 'm1'))

    def test_model_whitelist(self) -> None:
        key = self._key(models=['m1'])
        self.assertIsNone(keysvc.validate(key, '1.1.1.1', 'm1'))
        r = keysvc.validate(key, '1.1.1.1', 'm2')
        self.assertEqual(r.status, 400)
        self.assertEqual(r.code, 'model_not_allowed')
        # 缺 model 且有白名单 → 拒（不能因缺失就跳过检查）
        r = keysvc.validate(key, '1.1.1.1', None)
        self.assertEqual(r.status, 400)
        # 模型发现请求（/v1/models）跳过白名单
        self.assertIsNone(keysvc.validate(key, '1.1.1.1', None, is_model_list=True))

    def test_model_allowed(self) -> None:
        key = {'models': []}
        self.assertTrue(keysvc.model_allowed(key, 'anything'))
        key = {'models': ['m1', 'm2']}
        self.assertTrue(keysvc.model_allowed(key, 'm1'))
        self.assertTrue(keysvc.model_allowed(key, ' m1 '))
        self.assertFalse(keysvc.model_allowed(key, 'm3'))
        self.assertFalse(keysvc.model_allowed(key, None))
        self.assertFalse(keysvc.model_allowed(key, ''))


class TouchAndEntries(TmpDbTestCase):
    def test_touch_accumulates(self) -> None:
        created = keysvc.create_key('k')
        keysvc.touch(created, '1.1.1.1', 10)
        keysvc.touch(created, '1.1.1.1', 5)
        row = keysvc.list_keys()[0]
        self.assertEqual(row['used_tokens'], 15)
        self.assertIsNotNone(row['last_used_at'])
        ips = db.query('SELECT ip FROM api_key_ips WHERE key_id = ?', (created['id'],))
        self.assertEqual([r['ip'] for r in ips], ['1.1.1.1'])

    def test_unknown_whitelist_entries(self) -> None:
        known = {'m1', 'm2'}
        self.assertEqual(
            keysvc.unknown_whitelist_entries(['m1', 'typo', ' m2 '], known), ['typo'])
        # 清单拿不到 → 不判
        self.assertEqual(
            keysvc.unknown_whitelist_entries(['anything'], None), [])
        # 别名无条件认
        self.assertEqual(
            keysvc.unknown_whitelist_entries(['alias'], known, ['alias']), [])


# ── gateway 纯函数 ───────────────────────────────────────
class MapModel(TmpDbTestCase):
    def test_passthrough_without_mapping(self) -> None:
        self.assertEqual(gateway._map_model('m1'), 'm1')
        self.assertEqual(gateway._map_model(''), '')

    def test_alias_and_fallback(self) -> None:
        db.set_setting('model_map', {'claude-x': 'real-model', '*': 'default-model'})
        self.assertEqual(gateway._map_model('claude-x'), 'real-model')
        # 未命中别名 → 兜底
        self.assertEqual(gateway._map_model('typo'), 'default-model')
        # 兜底自己不是模型名：命中 `*` 键时按字面处理（用户不该这么配）
        self.assertEqual(gateway._alias_names(), ['claude-x'])

    def test_bad_mapping_is_ignored(self) -> None:
        db.set_setting('model_map', 'not-a-dict')
        self.assertEqual(gateway._map_model('m1'), 'm1')

    def test_alias_entries(self) -> None:
        db.set_setting('model_map', {'alias-a': 'real-a'})
        items = [{'id': 'real-a', 'name': 'A'}, {'id': 'real-b'}]
        entries = gateway._alias_entries(items, ['alias-a', 'real-b', 'alias-x'])
        # real-b 本身在清单里不重复补；alias-x 指向的模型不在清单不补
        self.assertEqual([e['id'] for e in entries], ['alias-a'])
        self.assertEqual(entries[0]['name'], 'A')

    def test_scope_models(self) -> None:
        payload = {'data': [{'id': 'm1'}, {'id': 'm2'}], 'object': 'list'}
        key = {'models': ['m1']}
        scoped = gateway._scope_models(payload, key)
        self.assertEqual([m['id'] for m in scoped['data']], ['m1'])
        # 白名单为空 → 原样
        self.assertIs(gateway._scope_models(payload, {'models': []}), payload)


class OaiErrorShape(unittest.TestCase):
    def test_shape_and_headers(self) -> None:
        resp = gateway._oai_error('bad', 400, 'invalid_request_error', 'invalid_model')
        self.assertEqual(resp.status_code, 400)
        body = json_loads(resp)
        self.assertEqual(body['error']['message'], 'bad')
        self.assertEqual(body['error']['type'], 'invalid_request_error')
        self.assertEqual(body['error']['code'], 'invalid_model')
        self.assertNotIn('gateway_hint', body['error'])
        # hint / retry_after 只在非空时出现
        resp = gateway._oai_error('slow', 429, retry_after=30, hint='wait')
        body = json_loads(resp)
        self.assertEqual(body['error']['gateway_hint'], 'wait')
        self.assertEqual(resp.headers.get('retry-after'), '30')

    def test_error_hint(self) -> None:
        self.assertEqual(
            gateway._error_hint({'error': {'gateway_hint': ' do this '}}), 'do this')
        self.assertIsNone(gateway._error_hint({'error': {}}))
        self.assertIsNone(gateway._error_hint('not-a-dict'))

        body = {'max_tokens': 10 ** 10,
                'max_completion_tokens': 999_999,
                'max_output_tokens': 123}
        gateway.clamp_max_tokens(body)
        self.assertEqual(body['max_tokens'], gateway.MAX_TOKENS_CEILING)
        self.assertEqual(body['max_completion_tokens'], 999_999)
        self.assertEqual(body['max_output_tokens'], 123)

    def test_non_numeric_left_alone(self) -> None:
        body = {'max_tokens': 'huge', 'max_completion_tokens': None,
                'max_output_tokens': True}
        gateway.clamp_max_tokens(body)
        self.assertEqual(body['max_tokens'], 'huge')
        self.assertIsNone(body['max_completion_tokens'])
        # bool 是 int 的子类，不能被钳成数字
        self.assertIs(body['max_output_tokens'], True)


class ScanSse(unittest.TestCase):
    def test_extracts_usage_and_content(self) -> None:
        stream = (
            'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
            'data: {"choices":[{"delta":{"content":"你好"}}]}\n\n'
            'data: {"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n'
            'data: [DONE]\n\n'
        )
        usage: dict = {}
        pending, saw = gateway._scan_sse(stream, usage)
        self.assertTrue(saw)
        self.assertEqual(usage, {'prompt_tokens': 7, 'completion_tokens': 3})
        self.assertEqual(pending, '')

    def test_role_only_frame_is_not_first_token(self) -> None:
        stream = 'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
        _, saw = gateway._scan_sse(stream, {})
        self.assertFalse(saw)

    def test_split_chunk_and_garbage(self) -> None:
        usage: dict = {}
        first = 'data: {"usage":{"prompt_tok'
        pending, _ = gateway._scan_sse(first, usage)
        self.assertEqual(pending, first)
        self.assertEqual(usage, {})
        pending, _ = gateway._scan_sse(pending + 'ens":2}}\n', usage)
        self.assertEqual(usage, {'prompt_tokens': 2})
        # 坏 JSON / 非 data 行安静跳过
        _, saw = gateway._scan_sse('event: ping\ndata: not-json\n', {})
        self.assertFalse(saw)


def json_loads(resp) -> dict:
    import json
    return json.loads(resp.body.decode('utf-8'))


if __name__ == '__main__':
    unittest.main()
