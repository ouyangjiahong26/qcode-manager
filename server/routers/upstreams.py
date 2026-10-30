"""多上游（PAT 池分组）管理接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import db, security, upstreamsvc
from ..iputil import client_ip

router = APIRouter(prefix='/api/upstreams', tags=['upstreams'])


class UpstreamIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=1, max_length=500)
    api_key: str = Field(default='', max_length=500)
    note: str = Field(default='', max_length=200)
    enabled: bool = True


class UpstreamPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    api_key: str | None = Field(default=None, max_length=500)
    note: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None


def _mask(value: str) -> str:
    """api_key 脱敏：只回末 4 位；空值与过短值不泄露任何内容。"""
    v = str(value or '')
    if len(v) <= 4:
        return '*' * len(v) if v else ''
    return f'****{v[-4:]}'


def _serialize(items: list[dict]) -> list[dict]:
    """对外形状：附上密钥 / 账号引用计数，并把 `api_key` 换成脱敏值。

    引用计数是删除守卫的前置提示：界面在删除按钮旁边直接显示「还有 n 把密钥 /
    m 个账号绑在这里」，管理员不必等到 409 才知道。默认上游（id=None）的计数
    是「未显式绑定」的那部分——它们实际也在用默认池。
    """
    keys = upstreamsvc.bound_key_count()
    accounts = upstreamsvc.bound_account_count()
    total_keys = len(db.query('SELECT id FROM api_keys'))
    total_accounts = len(db.query('SELECT id FROM accounts'))
    bound_keys = sum(keys.values())
    bound_accounts = sum(accounts.values())
    out: list[dict] = []
    for item in items:
        uid = item['id']
        if uid is None:
            kc, ac = total_keys - bound_keys, total_accounts - bound_accounts
        else:
            kc, ac = keys.get(int(uid), 0), accounts.get(int(uid), 0)
        out.append({
            **item,
            'api_key': _mask(item.get('api_key')),
            'api_key_set': bool(item.get('api_key')),
            'key_count': kc,
            'account_count': ac,
        })
    return out


@router.get('')
def list_upstreams(user: dict = Depends(security.current_user)) -> dict:
    return {'items': _serialize(upstreamsvc.list_upstreams())}


@router.post('')
def create_upstream(body: UpstreamIn, request: Request,
                    user: dict = Depends(security.require_session_admin)) -> dict:
    """新建上游。要求**会话管理员**：它带着上游的附加 api_key，属于配置级凭据，
    只读会话里 CSRF 拿不到（写操作统一要求重新登录过的会话）。"""
    try:
        created = upstreamsvc.create_upstream(
            body.name, body.base_url, body.api_key, body.note, body.enabled)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    security.audit(user, 'create_upstream', str(created['id']),
                   f'{created["name"]} → {created["base_url"]}；来源 '
                   f'{_ip(request)}', ip=_ip(request))
    return _serialize([created])[0]


@router.patch('/{upstream_id}')
def update_upstream(upstream_id: int, body: UpstreamPatch, request: Request,
                    user: dict = Depends(security.require_session_admin)) -> dict:
    patch = body.model_dump(exclude_unset=True)
    if not patch:
        raise HTTPException(status_code=400, detail='没有要修改的字段')
    try:
        updated = upstreamsvc.update_upstream(upstream_id, patch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail='上游不存在')
    security.audit(user, 'update_upstream', str(upstream_id),
                   f'改了 {", ".join(sorted(patch))}；来源 {_ip(request)}', ip=_ip(request))
    return _serialize([updated])[0]


@router.delete('/{upstream_id}')
def delete_upstream(upstream_id: int, request: Request,
                    user: dict = Depends(security.require_session_admin)) -> dict:
    upstream = upstreamsvc.get_upstream(upstream_id)
    if upstream is None:
        raise HTTPException(status_code=404, detail='上游不存在')
    deleted, used = upstreamsvc.delete_upstream(upstream_id)
    if not deleted:
        # 409：删除请求本身合法，但当前状态不允许——先解除密钥 / 账号的绑定。
        raise HTTPException(
            status_code=409,
            detail=f'仍有 {used} 个密钥 / 账号绑定在该上游上，请先改绑或删除它们')
    security.audit(user, 'delete_upstream', str(upstream_id),
                   f'{upstream["name"]}；来源 {client_ip(request)}',
                   ip=client_ip(request))
    return {'ok': True}


@router.post('/{upstream_id}/probe')
async def probe_upstream(upstream_id: int,
                         user: dict = Depends(security.require_admin)) -> dict:
    """探测某个上游是否可达（GET /v1/models）。管理员只读会话即可：探测不带
    任何修改，且结果里没有凭据。"""
    upstream = upstreamsvc.get_upstream(upstream_id)
    if upstream is None:
        raise HTTPException(status_code=404, detail='上游不存在')
    ok, message = await upstreamsvc.probe(upstream)
    return {'ok': ok, 'message': message}

