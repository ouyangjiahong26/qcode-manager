"""内置聊天测试台：用登录会话直接试调上游，无需先发一把 API 密钥。

为什么单独做一条通道、而不是让页面直接打 `/v1/chat/completions`：
网关那条路要求 `Bearer qk_...`，而测试台的使用者还没建密钥（正是想先试一下）。
这里用管理端登录态鉴权，服务端从账号池选 PAT 转发，因此**不经过**密钥/IP/
配额校验——它只用于调试，不是对外入口。
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .. import iputil, security
from . import models as models_router

router = APIRouter(prefix='/api/playground', tags=['playground'])


class ChatIn(BaseModel):
    """测试台请求。messages 直接透传给上游，不做内容校验。"""
    model: str = Field(min_length=1, max_length=128)
    messages: list[dict] = Field(min_length=1, max_length=200)
    # 推理档位：off/minimal/low/medium/high/xhigh/max；空串表示不指定
    reasoning_effort: str = Field(default='', max_length=16)
    stream: bool = True
    temperature: float | None = None
    max_tokens: int | None = None
    # 指定走哪个上游（不传 = 默认上游）；测试台界面切了分组、请求却仍走
    # 默认上游，就会出现「切了分组却调用别的池」的错配。
    upstream_id: int | None = None


@router.post('/chat')
async def chat(body: ChatIn, request: Request, user: dict = Depends(security.require_admin)):
    """转发一次对话到上游。

    管理员限定：测试台会**真实消耗账号调用**，不该开放给所有登录用户。
    """
    from .. import accountsvc, upstreamsvc
    from . import gateway

    upstream = (upstreamsvc.get_upstream(body.upstream_id)
                if body.upstream_id is not None else upstreamsvc.default_upstream())
    if not upstream:
        raise HTTPException(status_code=404, detail='上游不存在')
    if not upstream.get('enabled'):
        raise HTTPException(status_code=503, detail='该上游已停用')
    picked = accountsvc.pick_pat(upstream.get('id'))
    if picked is None:
        raise HTTPException(status_code=503, detail='该上游没有可用账号（PAT 池为空或全部失效）')
    account_id, pat = picked
    _acct = accountsvc.get_account(account_id)
    account_label = str((_acct or {}).get('label') or f'#{account_id}')

    ip = iputil.client_ip(request)
    started = time.time()
    # 模型名先过映射（与网关同一条规则），日志的 mapped 列才有真实值。
    mapped = gateway._map_model(body.model) or ''
    model_sent = mapped or body.model
    payload: dict = {
        'model': model_sent,
        'messages': body.messages,
        'stream': body.stream,
    }
    if body.reasoning_effort:
        payload['reasoning_effort'] = body.reasoning_effort
    if body.temperature is not None:
        payload['temperature'] = body.temperature
    if body.max_tokens is not None:
        payload['max_tokens'] = body.max_tokens
    if body.stream:
        # 让上游在末帧返回 usage，收尾时记进请求日志
        payload['stream_options'] = {'include_usage': True}

    try:
        client, resp = await gateway._open_chat(upstream, pat, payload)
    except Exception as exc:  # noqa: BLE001
        await gateway._record(None, ip, body.model, mapped, 502,
                              latency_ms=int((time.time() - started) * 1000),
                              stream=body.stream, error=str(exc),
                              account=(account_id, account_label))
        raise HTTPException(status_code=502, detail=f'上游不可用：{exc}') from exc

    async def _finish(status: int, usage: dict, error: str | None,
                      first_token_ms: int | None = None) -> None:
        """收尾记账：key 传 None（测试台没有对外密钥），日志页显示成「—」，
        据此可区分「测试台调用」与「某把密钥的调用」。账号的成败回写与
        token 累计由 gateway._record 统一处理（account 元组）。"""
        await gateway._record(
            None, ip, body.model, mapped, status,
            usage=usage,
            latency_ms=int((time.time() - started) * 1000),
            first_token_ms=first_token_ms, stream=body.stream,
            error=error, account=(account_id, account_label),
        )

    if not body.stream:
        try:
            raw = await resp.aread()
        finally:
            await resp.aclose()
            await client.aclose()
        usage: dict = {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and isinstance(parsed.get('usage'), dict):
                usage = parsed['usage']
        except Exception:  # noqa: BLE001
            pass
        await _finish(resp.status_code, usage,
                      None if resp.status_code < 400 else raw.decode('utf-8', errors='replace')[:500])
        # 原样透传上游响应（含错误状态码），前端拿到的与直连上游一致
        return StreamingResponse(iter([raw]), media_type='application/json',
                                 status_code=resp.status_code)

    async def gen():
        """SSE 透传：只提取 usage/首字延迟用于记账，字节不改动。"""
        usage: dict = {}
        pending = ''
        error_text: str | None = None
        first_token_ms: int | None = None
        try:
            async for chunk in resp.aiter_bytes():
                if resp.status_code >= 400:
                    pending += chunk.decode('utf-8', errors='ignore')
                    if not error_text and pending.strip():
                        error_text = pending[:500]
                    yield chunk
                    continue
                pending += chunk.decode('utf-8', errors='ignore')
                pending, saw_content = gateway._scan_sse(pending, usage)
                if saw_content and first_token_ms is None:
                    first_token_ms = int((time.time() - started) * 1000)
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()
            await _finish(resp.status_code, usage, error_text, first_token_ms)

    return StreamingResponse(gen(), status_code=resp.status_code,
                             media_type=resp.headers.get('content-type', 'text/event-stream'))


@router.get('/models')
async def playground_models(
    upstream_id: int | None = None,
    user: dict = Depends(security.current_user),
) -> dict:
    """测试台的模型下拉：与「模型页」同源（同一条缓存与拉取路径），
    界面上看到的清单与实际可调用的保持一致。"""
    return await models_router.fetch_models(upstream_id)
