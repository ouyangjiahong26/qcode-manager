/**
 * 管理面 API Token（qct_ 前缀）域 API。
 *
 * 与「API 密钥」（qk_，数据面网关凭据）是两套东西：这里的 token 授权的是
 * 管理面 /api/*，脚本 / CI 免登录用。四个接口后端一律要求会话管理员。
 */
import {get, post, patch, del} from './http';
import type {ApiToken, CreatedApiToken} from '@/lib/types/settings';

export const tokenApi = {
  list: () => get<ApiToken[]>('/api/tokens'),
  /** 创建：响应里的 token 是唯一一次明文，前端只显示这一次 */
  create: (body: {name: string; scope: 'readonly' | 'admin'; expires_at: number | null}) =>
    post<CreatedApiToken>('/api/tokens', body),
  update: (id: number, body: {name?: string; scope?: string; enabled?: boolean; expires_at?: number | null}) =>
    patch<ApiToken>(`/api/tokens/${id}`, body),
  remove: (id: number) => del<{ok: boolean}>(`/api/tokens/${id}`),
};
