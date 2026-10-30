"""上游模型列表：拉取指定上游（或默认上游）在 /v1/models 上的实际清单。

面板的密钥编辑、测试台下拉都从这里取数。上游要求 PAT 才放行，因此拉取时
从对应分组挑一把健康账号；进程内缓存 5 分钟，`force=1` 绕过（会真实消耗
一次上游调用）。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException

from .. import security

router = APIRouter(prefix='/api', tags=['models'])

# 进程内缓存：{upstream_id|None: (取数时刻, 模型清单)}。上游清单极少变化，
# 而每次拉取都消耗一次 PAT 调用，5 分钟的窗口足够新鲜也足够省。
_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 300.0


def _cache_key(upstream_id: int | None) -> str:
    # None（默认上游）与显式 id 分开缓存；None 用空串做键即可区分。
    return '' if upstream_id is None else str(upstream_id)


async def fetch_models(upstream_id: int | None = None, *, force: bool = False) -> dict:
    """拉上游模型清单（带缓存）。测试台的模型下拉复用这条路径。

    抛 HTTPException：上游不可达 502、账号池为空 503——调用方（页面接口）
    直接把状态码与详情透给前端。
    """
    from .. import accountsvc, upstreamsvc
    from ..services import qoder2api

    key = _cache_key(upstream_id)
    hit = _CACHE.get(key)
    if not force and hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]

    upstream = (upstreamsvc.get_upstream(upstream_id) if upstream_id is not None
                else upstreamsvc.default_upstream())
    if not upstream:
        raise HTTPException(status_code=404, detail='上游不存在')
    if not upstream.get('enabled'):
        raise HTTPException(status_code=503, detail='该上游已停用')

    picked = accountsvc.pick_pat(upstream.get('id'))
    if picked is None:
        raise HTTPException(status_code=503, detail='该上游没有可用账号（PAT 池为空或全部失效）')
    account_id, pat = picked

    try:
        data = await qoder2api.get_models(upstream['base_url'], pat,
                                          api_key=upstream.get('api_key') or '')
    except Exception as exc:  # noqa: BLE001
        accountsvc.mark_result(account_id, False, f'拉取模型失败：{exc}'[:200])
        raise HTTPException(status_code=502, detail=f'上游不可用：{exc}') from exc

    accountsvc.mark_result(account_id, True, '拉取模型成功')
    models = data.get('data') if isinstance(data, dict) else None
    result = {'upstream_id': upstream.get('id'), 'models': models or []}
    _CACHE[key] = (time.time(), result)
    return result


@router.get('/models')
async def list_models(
    upstream_id: int | None = None,
    force: bool = False,
    user: dict = Depends(security.current_user),
) -> dict:
    """上游模型列表（▲ 登录即可；force 绕过缓存）。"""
    return await fetch_models(upstream_id, force=force)
