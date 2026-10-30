/**
 * 上游（PAT 池分组）域 API：账号页只需要清单与探测两个方法，
 * 完整的上游管理由设置页的分组片负责。
 */
import {get, post} from './http';

export interface Upstream {
  id: number | null;
  name: string;
  base_url: string;
  /** 脱敏形态：仅回末 4 位（****abcd）；空值回空串 */
  api_key: string;
  api_key_set: boolean;
  note: string;
  enabled: boolean;
  /** 引用计数：该分组下的密钥 / 账号数（删除守卫的前置提示） */
  key_count: number;
  account_count: number;
}

export const upstreamsApi = {
  list: async (): Promise<Upstream[]> =>
    (await get<{items: Upstream[]}>('/api/upstreams')).items,
  /** 探测上游可达性（GET /v1/models） */
  probe: (id: number) => post<{ok: boolean; message: string}>(`/api/upstreams/${id}/probe`),
};
