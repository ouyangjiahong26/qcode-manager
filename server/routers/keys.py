"""API 密钥管理接口。"""
from __future__ import annotations

import ipaddress
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import config, db, keysvc, security, upstreamsvc
from ..iputil import client_ip

router = APIRouter(prefix='/api/keys', tags=['keys'])

# 校验失败时给用户的说明。放在写入**之前**拦，而不是存下去再说 ——
# 存坏数据的后果是「该密钥永久不可用」：`ip_matches` 对非法 CIDR 一律返回
# False（fail-closed，方向是对的），于是白名单里只要有一个写错的 CIDR，
# 这把密钥对**所有**来源 IP 都拒绝，而报错只说「不在白名单内」，
# 用户完全看不出是自己把 CIDR 写错了。宁可在这里拒掉并说清怎么写。
_CIDR_HINT = ('IP 白名单里有无法识别的条目：{bad}。\n'
              '  请写成单个 IP（1.2.3.4）或 CIDR（10.0.0.0/8）的形态。\n'
              '  留着它会让这把密钥**拒绝所有来源**（因为匹配不上任何 IP）。')


def _check_ip_allowlist(items: list[str] | None) -> None:
    """逐项校验 IP 白名单，非法即 400（附上该怎么写）。"""
    for raw in items or []:
        s = str(raw).strip()
        if not s:
            continue      # 空项由 keysvc 过滤掉，不算错
        try:
            ipaddress.ip_network(s, strict=False)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=_CIDR_HINT.format(bad=s)) from None


def _check_upstream(upstream_id: object) -> None:
    """校验密钥要绑定的上游存在（未绑定 / 默认上游 = None 或 0，直接放行）。

    为什么在这里拦而不是等转发时：绑一个不存在的 id，症状是**每一条请求都
    503**（见 upstreamsvc.resolve_for_key 的「不静默回落」），而管理员在界面
    上看到的是「密钥建好了」。建的时候就报 400，比事后对着 503 排查便宜得多。

    默认上游（id=None）不是数据库里的一行，所以显式放过——它是合法取值。
    """
    if upstream_id in (None, '', 0, '0'):
        return
    if upstreamsvc.get_upstream(upstream_id) is None:
        raise HTTPException(status_code=400,
                            detail=f'选定的上游不存在（id={upstream_id}），它可能已被删除')


def _check_name(name: str | None) -> None:
    """名称不能只有空白。

    否则列表里会出现一行「没有名字」的密钥，管理员认不出它是干什么的、
    也不知道是自己误操作建的。
    """
    if name is not None and not str(name).strip():
        raise HTTPException(status_code=400, detail='密钥名称不能只有空格')


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # 绑定的上游（多上游 / 分组隔离）：null / 0 / 省略 = 默认上游。
    # 类型是 int 而不是 str：界面上是下拉选 id，存成整数才能在外键式查询里用。
    upstream_id: int | None = None
    expires_at: int | None = None
    max_ips: int = 0
    ip_allowlist: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    # token 额度：0 = 不限。
    quota: int = 0


class KeyPatch(BaseModel):
    name: str | None = None
    # 显式传 null 是**允许**的：把密钥改回默认上游。
    upstream_id: int | None = None
    enabled: bool | None = None
    expires_at: int | None = None
    max_ips: int | None = None
    ip_allowlist: list[str] | None = None
    models: list[str] | None = None
    quota: int | None = None


class WhitelistCheckIn(BaseModel):
    models: list[str] = Field(default_factory=list)


# 上游模型清单的轻量缓存：check-models 挂在输入框失焦上，校验刻意**只读
# 缓存、不发网络**——它不该让一次上游慢响应把交互拖住；而且校验失败是可
# 接受的（下次再看），打上游失败反而更糟。缓存由本接口自己维护（5 分钟）。
_KNOWN_TTL = 300
_known_cache: dict[str, list] = {'at': 0.0, 'ids': None}


async def _known_model_ids() -> set[str] | None:
    """当前上游的模型 id 集合；拿不到返回 None（调用侧按「无法校验」处理）。"""
    now = time.time()
    if _known_cache['ids'] is not None and now - _known_cache['at'] < _KNOWN_TTL:
        return _known_cache['ids'] and set(_known_cache['ids'])
    ids: set[str] | None = None
    try:
        # 惰性导入：账号池与上游客户端由并行片交付，本模块自身不依赖其常驻
        from .. import accountsvc
        from ..services import qoder2api
        picked = accountsvc.pick_pat()
        pat = picked[1] if picked else ''
        result = await qoder2api.get_models(config.QC2API_BASE, pat or None,
                                            api_key=config.QC2API_KEY)
        data = result.get('data') if isinstance(result, dict) else None
        if isinstance(data, list):
            ids = {str(m.get('id')) for m in data
                   if isinstance(m, dict) and m.get('id')}
    except Exception:  # noqa: BLE001
        ids = None
    _known_cache['at'] = now
    _known_cache['ids'] = sorted(ids) if ids else None
    return ids


def _alias_names() -> list[str]:
    """真实别名（**不含兜底 `*`**）：`*` 不是模型名，不该被当成合法白名单项。"""
    mapping = db.get_setting('model_map', {}) or {}
    if not isinstance(mapping, dict):
        return []
    return [str(k).strip() for k, v in mapping.items()
            if str(k).strip() and str(k).strip() != '*'
            and v is not None and str(v).strip()]


@router.post('/check-models')
async def check_models(body: WhitelistCheckIn,
                       user: dict = Depends(security.current_user)) -> dict:
    """检查模型白名单里哪些名字**匹配不到已知模型**。

    为什么要有个接口而不是前端自己比对：判据必须与调用侧**同一份**
    （`keysvc.model_allowed` 的口径）。前端再实现一遍就是第二份事实来源，
    迟早漂移——「能选、一选就失败」正是两处口径不一致的表现。

    **拿不到清单时不猜**：`checked: false`，前端如实显示「暂时无法校验」。
    """
    names = [str(x).strip() for x in body.models if str(x).strip()]
    if not names:
        return {'checked': True, 'unknown': []}

    known = await _known_model_ids()
    if known is None:
        return {'checked': False, 'unknown': [],
                'reason': '暂时读不到模型清单（去「模型」页刷新一次再回来）'}
    unknown = keysvc.unknown_whitelist_entries(names, known, _alias_names())
    return {'checked': True, 'unknown': unknown}


@router.get('')
def list_keys(user: dict = Depends(security.current_user)) -> list[dict]:
    return keysvc.list_keys()


@router.post('')
def create_key(body: KeyIn, request: Request,
               user: dict = Depends(security.require_admin)) -> dict:
    _check_name(body.name)
    _check_ip_allowlist(body.ip_allowlist)
    _check_upstream(body.upstream_id)
    created = keysvc.create_key(
        name=body.name,
        expires_at=body.expires_at,
        max_ips=body.max_ips,
        ip_allowlist=body.ip_allowlist,
        models=body.models,
        quota=body.quota,
        upstream_id=body.upstream_id,
    )
    # 密钥是拿额度用的凭证，发放必须留痕（含来源 IP）
    upstream_name = '默认上游'
    if created.get('upstream_id'):
        row = upstreamsvc.get_upstream(created['upstream_id'])
        upstream_name = row['name'] if row else f"#{created['upstream_id']}（已删除）"
    security.audit(user, 'create_key', str(created.get('name') or ''),
                   f"id={created.get('id')}；上游={upstream_name}；来源 {client_ip(request)}")
    return created


@router.patch('/{key_id}')
def update_key(key_id: int, body: KeyPatch, user: dict = Depends(security.require_admin)) -> dict:
    # 只校验本次真的提交了的字段（PATCH 是部分更新，`exclude_unset` 语义）
    patch = body.model_dump(exclude_unset=True)
    if 'name' in patch:
        _check_name(patch['name'])
    if 'ip_allowlist' in patch:
        _check_ip_allowlist(patch['ip_allowlist'])
    if 'upstream_id' in patch:
        _check_upstream(patch['upstream_id'])
    updated = keysvc.update_key(key_id, patch)
    if not updated:
        raise HTTPException(status_code=404, detail='密钥不存在')
    return updated


@router.post('/{key_id}/reset-usage')
def reset_usage(key_id: int, user: dict = Depends(security.require_admin)) -> dict:
    # 不存在的 id 应报 404，而不是静默成功：否则前端会提示「已重置」，
    # 而实际什么都没发生（密钥可能已被别人删掉，页面上却看着还在）。
    if not keysvc.reset_usage(key_id):
        raise HTTPException(status_code=404, detail='密钥不存在')
    return {'ok': True}


@router.delete('/{key_id}')
def delete_key(key_id: int, request: Request,
               user: dict = Depends(security.require_admin)) -> dict:
    if not keysvc.delete_key(key_id):
        raise HTTPException(status_code=404, detail='密钥不存在')
    security.audit(user, 'delete_key', str(key_id), f'来源 {client_ip(request)}')
    return {'ok': True}
