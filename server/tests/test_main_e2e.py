"""main.py 端到端验证：真实起进程（面板 + mock 上游）跑通八组冒烟场景。

不使用 TestClient：这里的验收对象是「进程组装 + lifespan + 中间件栈 + 路由注册
+ 静态托管」的整体，TestClient 会绕开 uvicorn 的进程边界，覆盖不到真实启动
路径（stdio、后台调度器、优雅退出）。mock 上游以子进程方式拉起，测试结束回收。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
MODEL = 'GLM-5.2'  # mock 上游模型表里的任意一个


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class _Proc:
    """一个被测试托管的子进程（面板或 mock 上游）。"""

    def __init__(self, args: list[str], env: dict[str, str], log_path: Path) -> None:
        # stderr 落文件：进程起不来时（启动期异常）这里是唯一的线索
        self.log_path = log_path
        self._log = open(log_path, 'wb')
        self.proc = subprocess.Popen(
            args, cwd=str(ROOT), env=env,
            stdout=self._log, stderr=subprocess.STDOUT,
        )

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        self._log.close()


def _wait_http(url: str, timeout: float = 40.0, *, log_path: Path | None = None) -> None:
    """轮询直到该 URL 可返回任意 HTTP 响应（uvicorn 起来即算活）。"""
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=10.0)
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.3)
    detail = ''
    if log_path and log_path.is_file():
        detail = log_path.read_text(encoding='utf-8', errors='replace')[-2000:]
    raise AssertionError(f'服务未就绪: {url}（{last}）\n--- 进程日志 ---\n{detail}')


class _Panel:
    """按给定环境变量启动的面板实例（数据目录/静态目录均在临时目录里隔离）。"""

    def __init__(self, *, data_dir: Path, static_dir: Path, upstream: str | None) -> None:
        self.port = _free_port()
        env = dict(os.environ)
        env.update({
            'QC_DATA_DIR': str(data_dir),
            'QC_STATIC_DIR': str(static_dir),
            'QC_MANAGER_HOST': '127.0.0.1',
            # 测试里要验证「5 次错误密码后锁定」，把窗口内的其他管理请求隔离开
            'QC_SESSION_IDLE_HOURS': '0',
        })
        if upstream:
            env['QC2API_BASE'] = upstream
        self.data_dir = data_dir
        self.static_dir = static_dir
        self.proc = _Proc([PY, '-m', 'uvicorn', 'server.main:app',
                           '--host', '127.0.0.1', '--port', str(self.port)],
                          env, log_path=data_dir.parent / 'panel.log')
        self.base = f'http://127.0.0.1:{self.port}'
        _wait_http(f'{self.base}/healthz', log_path=self.proc.log_path)
        # 面板会话与上游 mock 都在本机回环上，直接共享一个带 cookie 的客户端
        self.client = httpx.Client(base_url=self.base, timeout=30.0)

    def stop(self) -> None:
        self.client.close()
        self.proc.stop()


def _start_mock() -> tuple[_Proc, str]:
    port = _free_port()
    proc = _Proc([PY, str(ROOT / 'dev' / 'mock_upstream.py'),
                  '--host', '127.0.0.1', '--port', str(port)],
                 dict(os.environ), log_path=Path(tempfile.gettempdir()) / 'qcoder-mock.log')
    base = f'http://127.0.0.1:{port}'
    _wait_http(f'{base}/v1/models', log_path=proc.log_path)
    return proc, base


class TestMainE2E(unittest.TestCase):
    """场景 1–7 共用一个面板实例：场景之间有数据依赖（密钥/PAT/会话）。"""

    panel: _Panel
    mock: _Proc
    admin_password: str
    api_key: str

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix='qcoder-e2e-')
        tmp = Path(cls._tmp.name)
        cls.mock, mock_base = _start_mock()
        # 静态目录指向不存在的路径：本组场景验证纯 API 面，SPA 兜底由场景 8 单测
        cls.panel = _Panel(data_dir=tmp / 'data', static_dir=tmp / 'no-web',
                           upstream=mock_base)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.panel.stop()
        cls.mock.stop()
        try:
            cls._tmp.cleanup()
        except OSError:
            pass  # Windows 句柄释放延迟时交给系统临时目录回收

    # ── 场景 1：首启 ────────────────────────────────────────
    def test_01_first_boot(self) -> None:
        r = self.panel.client.get('/healthz')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()['ok'])
        # 空数据目录首启：自动建 users.json，随机密码落 initial-admin-password.txt
        self.assertTrue((self.panel.data_dir / 'users.json').is_file())
        pwd_file = self.panel.data_dir / 'initial-admin-password.txt'
        self.assertTrue(pwd_file.is_file())
        TestMainE2E.admin_password = pwd_file.read_text(encoding='utf-8').strip()
        self.assertGreaterEqual(len(self.admin_password), 12)

    # ── 场景 2：登录与防爆破 ────────────────────────────────
    def test_02_login_and_lockout(self) -> None:
        # 先用正确密码登录拿到会话（防爆破锁定按 IP 计，先把合法会话备好）
        r = self.panel.client.post('/api/login', json={
            'username': 'admin', 'password': self.admin_password})
        self.assertEqual(r.status_code, 200)
        self.assertIn('qc_session', r.cookies)
        r = self.panel.client.get('/api/me')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['username'], 'admin')
        # /api/sysinfo 顺带验证版本口径来自 main.__version__
        r = self.panel.client.get('/api/sysinfo')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['service'], 'qcoder-manager')
        # 错密码 5 次 → 第 6 次被锁定 429
        for _ in range(5):
            bad = self.panel.client.post('/api/login', json={
                'username': 'admin', 'password': 'definitely-wrong'})
            self.assertEqual(bad.status_code, 401)
        blocked = self.panel.client.post('/api/login', json={
            'username': 'admin', 'password': self.admin_password})
        self.assertEqual(blocked.status_code, 429)

    def _login(self) -> None:
        r = self.panel.client.post('/api/login', json={
            'username': 'admin', 'password': self.admin_password})
        # setUpClass 场景 2 已把本 IP 锁定 600s —— 锁定只影响 /api/login，
        # 既有 cookie 依旧有效，所以这里只在未持会话时兜底（正常不会走到）。
        if r.status_code == 200 and 'qc_session' in r.cookies:
            return
        self.assertTrue(self.panel.client.cookies.get('qc_session'), '应有会话 cookie')

    # ── 场景 3：PAT 账号增测 ───────────────────────────────
    def test_03_accounts(self) -> None:
        self._login()
        good_pat = 'pt-e2e-good-1234567890abcdef'
        bad_pat = 'pt-xx-invalid-1'
        r = self.panel.client.post('/api/accounts', json={'label': '好账号', 'pat': good_pat})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['status'], 'active')
        r = self.panel.client.post('/api/accounts', json={'label': '坏账号', 'pat': bad_pat})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['status'], 'invalid')
        # 列表必须掩码：任何响应文本里都不该出现完整明文 PAT
        r = self.panel.client.get('/api/accounts')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(good_pat, r.text)
        self.assertNotIn(bad_pat, r.text)
        self.assertIn('pat_suffix', r.text)  # 列表带尾缀掩码字段，不带明文
        # 批量测活可重复执行
        r = self.panel.client.post('/api/accounts/test-all', json={})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json().get('ok') or 'total' in r.json())

    # ── 场景 4：密钥 ────────────────────────────────────────
    def test_04_keys(self) -> None:
        self._login()
        r = self.panel.client.post('/api/keys', json={'name': 'e2e-key'})
        self.assertEqual(r.status_code, 200)
        key = r.json().get('key') or r.json().get('token')
        self.assertTrue(key and key.startswith('qk_'))
        TestMainE2E.api_key = key
        # 网关模型列表带密钥可达（依赖场景 3 建好的可用 PAT）
        r = self.panel.client.get('/v1/models',
                                  headers={'Authorization': f'Bearer {key}'})
        self.assertEqual(r.status_code, 200)
        ids = [m['id'] for m in r.json()['data']]
        self.assertIn(MODEL, ids)

    def _chat(self, key: str, *, stream: bool = False):
        return self.panel.client.post(
            '/v1/chat/completions',
            headers={'Authorization': f'Bearer {key}'},
            json={'model': MODEL,
                  'messages': [{'role': 'user', 'content': '你好'}],
                  'stream': stream})

    # ── 场景 5：网关（非流式 / 流式 / 鉴权 / 配额）─────────
    def test_05_gateway(self) -> None:
        # 非流式：200 + usage 计量
        r = self._chat(self.api_key)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn('choices', body)
        self.assertGreater(body['usage']['total_tokens'], 0)
        # 用量落 request_logs（/api/logs）与 stats summary
        r = self.panel.client.get('/api/logs')
        self.assertEqual(r.status_code, 200)
        rows = r.json().get('items') or r.json()
        self.assertTrue(any((row.get('model') == MODEL and row.get('status') == 200)
                            for row in rows))
        r = self.panel.client.get('/api/stats/summary')
        self.assertEqual(r.status_code, 200)
        self.assertGreaterEqual(r.json()['total_requests'], 1)
        # 流式 SSE：能收到 chunk 与 [DONE]
        r = self._chat(self.api_key, stream=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/event-stream', r.headers.get('content-type', ''))
        self.assertIn('data:', r.text)
        self.assertIn('[DONE]', r.text)
        # 无效密钥：401 + OpenAI 形状错误体
        r = self.panel.client.post('/v1/chat/completions',
                                   headers={'Authorization': 'Bearer qk-not-a-real-key'},
                                   json={'model': MODEL, 'messages': [
                                       {'role': 'user', 'content': 'x'}]})
        self.assertEqual(r.status_code, 401)
        err = r.json()['error']
        self.assertIn('message', err)
        self.assertIn('type', err)
        # 配额 1 token 的密钥：第一次放行，第二次 429
        r = self.panel.client.post('/api/keys', json={'name': 'quota-key', 'quota': 1})
        self.assertEqual(r.status_code, 200)
        quota_key = r.json().get('key') or r.json().get('token')
        r1 = self._chat(quota_key)
        self.assertEqual(r1.status_code, 200)
        r2 = self._chat(quota_key)
        self.assertEqual(r2.status_code, 429)

    # ── 场景 6：Anthropic 兼容 ─────────────────────────────
    def test_06_anthropic(self) -> None:
        r = self.panel.client.post(
            '/v1/messages',
            headers={'x-api-key': self.api_key},
            json={'model': MODEL, 'max_tokens': 64, 'stream': True,
                  'messages': [{'role': 'user', 'content': '你好'}]})
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/event-stream', r.headers.get('content-type', ''))
        # Anthropic 事件流：message_start … content_block_delta … message_stop
        self.assertIn('event: message_start', r.text)
        self.assertIn('message_stop', r.text)
        self.assertIn('content_block_delta', r.text)

    # ── 场景 7：IP 管控 ────────────────────────────────────
    def test_07_ip_rules(self) -> None:
        # 开启黑名单模式 + 封本机回环 → /v1 被拒（/api 管理面不受影响）
        r = self.panel.client.post('/api/security/config',
                                   json={'enabled': True, 'mode': 'blacklist'})
        self.assertEqual(r.status_code, 200)
        r = self.panel.client.post('/api/security/rules',
                                   json={'kind': 'deny', 'cidr': '127.0.0.1',
                                         'note': 'e2e 封禁测试'})
        self.assertEqual(r.status_code, 200)
        rule_id = r.json()['id']
        r = self.panel.client.get('/v1/models',
                                  headers={'Authorization': f'Bearer {self.api_key}'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()['error'].get('code'), 'ip_blocked')
        # 拦截动作记入 ip_access_logs
        r = self.panel.client.get('/api/security/logs')
        self.assertEqual(r.status_code, 200)
        logs = r.json()
        if isinstance(logs, dict):
            logs = logs.get('items') or logs.get('logs') or []
        self.assertTrue(any(e.get('blocked') for e in logs))
        # 删规则恢复
        r = self.panel.client.delete(f'/api/security/rules/{rule_id}')
        self.assertEqual(r.status_code, 200)
        r = self.panel.client.get('/v1/models',
                                  headers={'Authorization': f'Bearer {self.api_key}'})
        self.assertEqual(r.status_code, 200)


class TestStaticHosting(unittest.TestCase):
    """场景 8：QC_STATIC_DIR 指向临时假前端目录时的静态托管与 SPA 兜底。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix='qcoder-static-')
        tmp = Path(cls._tmp.name)
        web = tmp / 'web'
        web.mkdir()
        (web / 'index.html').write_text('<html>qcoder-static-index</html>', encoding='utf-8')
        (web / '404.html').write_text('<html>qcoder-static-404</html>', encoding='utf-8')
        # 上游指向必然拒绝连接的回环端口：/healthz 的上游探测立刻失败返回，
        # 不会因探测一个「黑洞」地址拖住整个健康检查
        cls.panel = _Panel(data_dir=tmp / 'data', static_dir=web,
                           upstream='http://127.0.0.1:9')

    @classmethod
    def tearDownClass(cls) -> None:
        cls.panel.stop()
        # Windows 上 SQLite 句柄释放略有延迟，清理失败就交给系统临时目录回收，
        # 不让清理噪音把测试结果染红
        try:
            cls._tmp.cleanup()
        except OSError:
            pass

    def test_index_and_spa_fallback(self) -> None:
        r = self.panel.client.get('/')
        self.assertEqual(r.status_code, 200)
        self.assertIn('qcoder-static-index', r.text)
        # 未命中路径走 SPA 兜底（404 页面），绝不 500
        r = self.panel.client.get('/some/unknown/page', follow_redirects=True)
        self.assertIn(r.status_code, (200, 404))
        # 网关路径兜底回 JSON 404，不吐 HTML
        r = self.panel.client.get('/v1/nonexistent')
        self.assertEqual(r.status_code, 404)
        self.assertIn('error', r.json())


if __name__ == '__main__':
    unittest.main()
