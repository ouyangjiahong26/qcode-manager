"""qoder2api 上游客户端。

上游是无状态协议桥：没有 /status、没有管理接口，只有两个可用端点——
``GET /v1/models``（模型清单）与 ``POST /v1/chat/completions``（对话）。
鉴权统一 ``Authorization: Bearer <PAT>``；个别部署变体若额外要求 API Key，
通过 ``api_key`` 参数叠加（PAT 仍逐请求注入，二者不互斥）。
"""
from __future__ import annotations

from .. import config


class UpstreamError(RuntimeError):
    """上游调用失败（网络不可达 / 非 2xx / 响应非 JSON）。

    消息面向用户（中文、含状态码或原因），调用方可直接透传给界面与测活记录。
    """


def _clean_base(base_url: str) -> str:
    """归一化上游基址：去空白与末尾斜杠，避免拼出 ``//v1/models``。"""
    return str(base_url or '').strip().rstrip('/')


def forward_headers(pat: str, *, api_key: str = '') -> dict:
    """转发到上游所需的鉴权头。

    PAT 恒为 Bearer；``api_key`` 非空时叠加 ``X-Api-Key``（上游变体可选的
    网关层鉴权，标准形态里留空即可）。
    """
    headers = {'Authorization': f'Bearer {pat}'}
    key = str(api_key or '').strip()
    if key:
        headers['X-Api-Key'] = key
    return headers


async def chat_url(base_url: str) -> str:
    """上游对话端点地址。"""
    return f'{_clean_base(base_url)}/v1/chat/completions'


async def get_models(base_url: str, pat: str | None = None,
                     *, api_key: str = '') -> dict:
    """拉取上游模型清单（真实鉴权：带 PAT 时上游会校验，401 即 PAT 失效）。

    成功返回上游 JSON（形如 ``{'object': 'list', 'data': [...]}``）；
    任何失败抛 :class:`UpstreamError`——测活、保活、模型页都靠异常分支判失败。
    """
    base = _clean_base(base_url)
    if not base:
        raise UpstreamError('上游地址为空')
    if pat:
        headers = forward_headers(pat, api_key=api_key)
    elif str(api_key or '').strip():
        headers = {'X-Api-Key': str(api_key).strip()}
    else:
        headers = {}
    try:
        async with config.http_client(15, connect=3) as client:
            resp = await client.get(f'{base}/v1/models', headers=headers)
    except Exception as exc:  # noqa: BLE001 —— 失败原因原样带给界面
        raise UpstreamError(f'无法连接上游：{exc}') from exc
    if resp.status_code >= 400:
        detail = ''
        try:
            body = resp.json()
            detail = str(body.get('detail') or body.get('error') or '')[:200]
        except Exception:  # noqa: BLE001 —— 错误体不是 JSON 也不影响判失败
            detail = resp.text[:200]
        msg = f'上游返回 {resp.status_code}'
        if detail:
            msg = f'{msg}：{detail}'
        if resp.status_code == 401:
            msg = f'{msg}（PAT 无效或已过期）'
        raise UpstreamError(msg)
    try:
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise UpstreamError('上游响应不是合法 JSON') from exc
    if not isinstance(body, dict):
        raise UpstreamError('上游响应格式异常')
    return body


async def probe(base_url: str, *, api_key: str = '') -> tuple[bool, str]:
    """探测上游是否可达。

    qoder2api 没有独立健康端点，用 ``GET /v1/models`` 是否 2xx 判活
    （不带 PAT；上游即便要求鉴权也应答 401 而非连接失败，2xx 即服务在）。
    """
    base = _clean_base(base_url)
    if not base:
        return False, '上游地址为空'
    headers: dict = {}
    key = str(api_key or '').strip()
    if key:
        headers = {'X-Api-Key': key}
    try:
        async with config.http_client(10, connect=3) as client:
            resp = await client.get(f'{base}/v1/models', headers=headers)
    except Exception as exc:  # noqa: BLE001
        return False, f'无法连接：{exc}'
    if 200 <= resp.status_code < 300:
        return True, '上游可达'
    return False, f'上游返回 {resp.status_code}'
