"""账号池管理端点：PAT 增删改查、测活、导出与保活时刻表。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request

from .. import accountsvc, security
from ..iputil import client_ip
from ..keepalive import get_config as get_keepalive, save_config as save_keepalive

router = APIRouter(prefix='/api', tags=['accounts'])


def _summary(accounts: list[dict]) -> dict:
    """列表附带的状态汇总：页面顶部卡片直接可用，免再发一次请求。"""
    counts = {'active': 0, 'invalid': 0, 'unknown': 0}
    for a in accounts:
        status = a.get('status') or 'unknown'
        counts[status] = counts.get(status, 0) + 1
    counts['total'] = len(accounts)
    counts['enabled'] = sum(1 for a in accounts if a.get('enabled'))
    return counts


@router.get('/accounts')
def list_accounts(
    upstream_id: int | None = Query(None),
    user: dict = Depends(security.current_user),
) -> dict:
    """PAT 列表（掩码形态，绝不回明文/密文）。upstream_id 过滤分组。"""
    if upstream_id == 0:  # 前端「默认分组」约定 0 = 不绑定
        upstream_id = None
    accounts = accountsvc.list_accounts(upstream_id)
    return {'accounts': accounts, 'summary': _summary(accounts)}


@router.post('/accounts')
def add_account(
    request: Request,
    payload: dict = Body(...),
    user: dict = Depends(security.require_admin),
) -> dict:
    """添加 PAT 并即刻同步测活（响应携带首次测活结果）。

    同步 handler：create_account 内的阻塞式测活由框架线程池承接，
    不占事件循环。
    """
    label = str(payload.get('label') or '').strip()
    pat = str(payload.get('pat') or '').strip()
    if not label:
        raise HTTPException(400, '账号名称不能为空')
    if not pat:
        raise HTTPException(400, 'PAT 不能为空')
    upstream_id = payload.get('upstream_id')
    note = str(payload.get('note') or '')
    try:
        account = accountsvc.create_account(label, pat, upstream_id, note)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    security.audit(user, 'accounts.create', account['label'],
                   f'添加 PAT（{account["test"]["msg"]}）', ip=client_ip(request))
    return account


@router.patch('/accounts/{account_id}')
def patch_account(
    account_id: int,
    payload: dict = Body(...),
    user: dict = Depends(security.require_admin),
) -> dict:
    """改 label/note/enabled/weight/upstream_id（PATCH 语义，未知字段忽略）。"""
    try:
        account = accountsvc.update_account(account_id, payload or {})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if account is None:
        raise HTTPException(404, '账号不存在')
    return account


@router.delete('/accounts/{account_id}')
def remove_account(
    account_id: int,
    request: Request,
    user: dict = Depends(security.require_admin),
) -> dict:
    account = accountsvc.get_account(account_id)
    if account is None or not accountsvc.delete_account(account_id):
        raise HTTPException(404, '账号不存在')
    security.audit(user, 'accounts.delete', account['label'],
                   f'删除 PAT {account["pat_suffix"]}', ip=client_ip(request))
    return {'ok': True}


@router.post('/accounts/test-all')
async def test_all_accounts(
    payload: dict | None = Body(None),
    user: dict = Depends(security.require_admin),
) -> dict:
    """批量测活（enabled 账号；body 可带 upstream_id 只测某个分组）。"""
    upstream_id = (payload or {}).get('upstream_id')
    if upstream_id == 0:
        upstream_id = None
    return await accountsvc.test_all(upstream_id)


@router.post('/accounts/{account_id}/test')
async def test_one_account(
    account_id: int,
    user: dict = Depends(security.require_admin),
) -> dict:
    """单个账号测活：真实鉴权打上游 /v1/models 并回写状态。"""
    result = await accountsvc.test_account(account_id)
    if result.get('missing'):
        raise HTTPException(404, '账号不存在')
    return result


@router.get('/accounts/export')
def export_accounts(
    request: Request,
    user: dict = Depends(security.require_session_admin),
) -> dict:
    """全量导出（含 PAT 明文）：仅限会话管理员，导出动作落审计。"""
    items = accountsvc.reveal_accounts()
    ip = client_ip(request)
    security.audit(user, 'accounts.export', '',
                   f'导出 {len(items)} 个 PAT（含明文）；来源 {ip}', ip=ip)
    # 明文凭据绝不允许任何中间层/浏览器缓存
    from fastapi.responses import JSONResponse
    return JSONResponse(
        {'accounts': items, 'exported_at': int(time.time())},
        headers={'Cache-Control': 'no-store, no-cache, must-revalidate, private',
                 'Pragma': 'no-cache'})


@router.get('/settings/keepalive')
def get_keepalive_config(
    user: dict = Depends(security.current_user),
) -> dict:
    return get_keepalive()


@router.post('/settings/keepalive')
def save_keepalive_config(
    request: Request,
    payload: dict = Body(...),
    user: dict = Depends(security.require_admin),
) -> dict:
    """保存保活时刻表；非法时刻/超时范围回 400。"""
    try:
        cfg = save_keepalive(payload or {})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except (TypeError, KeyError) as exc:
        raise HTTPException(400, f'配置格式错误：{exc}') from exc
    security.audit(user, 'settings.keepalive', '',
                   f'保活配置：{"开" if cfg["enabled"] else "关"} @ {",".join(cfg["times"])}',
                   ip=client_ip(request))
    return cfg
