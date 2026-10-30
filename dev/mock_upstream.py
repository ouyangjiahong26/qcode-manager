"""模拟 qoder2api 上游，供本地开发与集成验证。

接口形态对齐 fengyinxia/qoder2api：
- GET  /v1/models             带 PAT 返回动态目录；不带返回兜底表
- POST /v1/chat/completions   Bearer PAT 鉴权，支持流式与非流式，回带 usage

PAT 规则：`pt-` 开头即视为有效，除非 PAT 中含 `invalid`（用于测活失败场景）。
用法：python dev/mock_upstream.py [--port 8963]
"""

from __future__ import annotations

import argparse
import time
import uuid

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

app = FastAPI(title='mock qoder2api', version='0.1.0')

# 与 qoder2api 网关动态目录同构的兜底模型表
MODELS = [
    {'id': 'Qwen3.8-Max-Preview', 'object': 'model', 'created': 0, 'owned_by': 'qoder'},
    {'id': 'Qwen3.7-Max', 'object': 'model', 'created': 0, 'owned_by': 'qoder'},
    {'id': 'Qwen3.7-Plus', 'object': 'model', 'created': 0, 'owned_by': 'qoder'},
    {'id': 'Qwen3.6-Flash', 'object': 'model', 'created': 0, 'owned_by': 'qoder'},
    {'id': 'DeepSeek-V4-Pro', 'object': 'model', 'created': 0, 'owned_by': 'qoder'},
    {'id': 'DeepSeek-V4-Flash', 'object': 'model', 'created': 0, 'owned_by': 'qoder'},
    {'id': 'GLM-5.2', 'object': 'model', 'created': 0, 'owned_by': 'qoder'},
    {'id': 'Kimi-K2.7-Code', 'object': 'model', 'created': 0, 'owned_by': 'qoder'},
    {'id': 'MiniMax-M2.7', 'object': 'model', 'created': 0, 'owned_by': 'qoder'},
]
MODEL_IDS = {m['id'] for m in MODELS}


def _check_pat(authorization: str | None) -> str:
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(status_code=401, detail='missing PAT')
    pat = authorization[len('Bearer '):].strip()
    if not pat.startswith('pt-') or 'invalid' in pat:
        raise HTTPException(status_code=401, detail='invalid PAT')
    return pat


class ChatIn(BaseModel):
    model: str
    messages: list[dict]
    stream: bool = False
    max_tokens: int | None = None


@app.get('/v1/models')
def models(authorization: str | None = Header(default=None)):
    if authorization:
        _check_pat(authorization)
        # 带 PAT 走「动态目录」：与兜底一致即可满足面板联调
        return {'object': 'list', 'data': MODELS}
    return {'object': 'list', 'data': MODELS}


@app.post('/v1/chat/completions')
def chat(body: ChatIn, authorization: str | None = Header(default=None)):
    pat = _check_pat(authorization)
    if body.model not in MODEL_IDS:
        return JSONResponse(
            status_code=400,
            content={'error': {'message': f"Unsupported model '{body.model}'",
                               'type': 'invalid_request_error', 'code': 'model_not_found'}},
        )
    ident = f'{pat[:8]}…'
    reply = f'[mock {body.model} via {ident}] 收到 {len(body.messages)} 条消息：' + (
        str(body.messages[-1].get('content'))[:200] if body.messages else '')
    usage = {'prompt_tokens': 32, 'completion_tokens': 24, 'total_tokens': 56}
    created = int(time.time())
    cid = 'chatcmpl-' + uuid.uuid4().hex[:12]

    if not body.stream:
        return {
            'id': cid, 'object': 'chat.completion', 'created': created, 'model': body.model,
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': reply}}],
            'usage': usage,
        }

    def gen():
        chunks = ['收到', '，这是', '来自模拟', '上游的', '流式回复。']
        for i, text in enumerate(chunks):
            yield ('data: ' + _chunk(cid, created, body.model,
                                     {'role': 'assistant', 'content': text}, None) + '\n\n')
            time.sleep(0.05)
        # stream_options.include_usage=true 时上游会在末尾回 usage chunk
        yield ('data: ' + _chunk(cid, created, body.model, {}, 'stop', usage) + '\n\n')
        yield 'data: [DONE]\n\n'

    return StreamingResponse(gen(), media_type='text/event-stream')


def _chunk(cid: str, created: int, model: str, delta: dict, finish: str | None,
           usage: dict | None = None) -> str:
    import json
    obj = {'id': cid, 'object': 'chat.completion.chunk', 'created': created, 'model': model,
           'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}
    if usage is not None:
        obj['usage'] = usage
    return json.dumps(obj, ensure_ascii=False)


if __name__ == '__main__':
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8963)
    args = parser.parse_args()
    print(f'mock qoder2api listening on http://{args.host}:{args.port}')
    uvicorn.run(app, host=args.host, port=args.port, log_level='warning')
