/**
 * 账号池域 API：对齐 server/routers/accounts.py。
 *
 * upstream_id 沿用后端约定：0 = 默认分组（不绑定），null = 不过滤。
 */
import {errText, get, post, patch, del, http} from './http';
import type {
  Account,
  AccountExportItem,
  AccountsListResponse,
  CreateAccountResult,
  TestAllResult,
  TestResult,
} from '@/lib/types/accounts';

// 各页面统一从域文件取错误文案，收口在同一处
export {errText};

export const accountsApi = {
  /** PAT 列表（掩码形态）+ 状态汇总；upstreamId 过滤分组 */
  list: (upstreamId?: number) =>
    get<AccountsListResponse>('/api/accounts', {upstream_id: upstreamId}),
  /** 添加 PAT（管理员）：同步测活，响应携带首次测活结果 */
  create: (label: string, pat: string, upstreamId?: number | null, note?: string) =>
    post<CreateAccountResult>('/api/accounts', {
      label,
      pat,
      upstream_id: upstreamId ?? null,
      note: note ?? '',
    }),
  /** PATCH 语义：只传要改的字段（label/note/enabled/weight/upstream_id） */
  update: (id: number, patchBody: Partial<Pick<Account, 'label' | 'note' | 'weight' | 'upstream_id'>> & {enabled?: boolean}) =>
    patch<Account>(`/api/accounts/${id}`, patchBody),
  remove: (id: number) => del<{ok: boolean}>(`/api/accounts/${id}`),
  /** 单账号测活：真实鉴权打上游 /v1/models 并回写状态 */
  test: (id: number) => post<TestResult>(`/api/accounts/${id}/test`),
  /** 批量测活（enabled 账号；可带 upstream_id 只测某个分组） */
  testAll: (upstreamId?: number) =>
    post<TestAllResult>('/api/accounts/test-all', upstreamId != null ? {upstream_id: upstreamId} : undefined),
  /**
   * 全量导出（含 PAT 明文，仅会话管理员）。直接走 axios 拿完整响应体，
   * 由调用方转 Blob 下载；后端已带 no-store 头，明文不落任何缓存。
   */
  export: async (): Promise<{accounts: AccountExportItem[]; exported_at: number}> =>
    (await http.get<{accounts: AccountExportItem[]; exported_at: number}>('/api/accounts/export')).data,
};
