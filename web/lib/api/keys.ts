/** 密钥域接口。上游下拉也在这里取（keys 页专用的小函数，避免与他片的 upstreams 域文件冲突）。 */
import {get, post, patch, del} from './http';
import type {
  ApiKey,
  KeyCreated,
  KeyCreateInput,
  KeyPatchInput,
  UpstreamOption,
  WhitelistCheckResult,
} from '@/lib/types/keys';

export const keysApi = {
  list: () => get<ApiKey[]>('/api/keys'),

  /** 返回体含一次性明文 key（qk_ 前缀）——面板只存哈希，这是明文唯一存在的一刻 */
  create: (body: KeyCreateInput) => post<KeyCreated>('/api/keys', body),

  update: (id: number, patchBody: KeyPatchInput) => patch<ApiKey>(`/api/keys/${id}`, patchBody),

  remove: (id: number) => del<{ok: boolean}>(`/api/keys/${id}`),

  resetUsage: (id: number) => post<{ok: boolean}>(`/api/keys/${id}/reset-usage`),

  /**
   * 校验模型白名单里哪些名字匹配不到已知模型。
   * 后端只读 5 分钟缓存、不发网络，可以放心挂在输入框失焦上。
   */
  checkModels: (models: string[]) =>
    post<WhitelistCheckResult>('/api/keys/check-models', {models}),

  /** 密钥页「绑定上游」下拉的数据源（GET /api/upstreams，含默认上游 id=null） */
  listUpstreams: async (): Promise<UpstreamOption[]> =>
    (await get<{items: UpstreamOption[]}>('/api/upstreams')).items,

  /** 上游模型清单（勾选器用）；models 兼容字符串与 {id} 对象两种元素形状 */
  listUpstreamModels: async (): Promise<string[]> => {
    const r = await get<{models: unknown[]}>('/api/models');
    return (r.models || []).flatMap((m) => {
      // OpenAI 风格清单是 {id: ...} 对象，也有变体直接给字符串
      if (typeof m === 'string') return [m];
      if (m && typeof m === 'object' && 'id' in m && typeof m.id === 'string') return [m.id];
      return [];
    });
  },
};
