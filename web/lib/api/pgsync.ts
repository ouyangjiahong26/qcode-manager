/**
 * PostgreSQL 异地备份域 API：配置读写、连通性探测与迁移 / 恢复任务。
 * 读操作登录即可；写操作后端要求会话管理员。
 */
import {get, post} from './http';
import type {PgJobResult, PgSyncConfig, PgSyncStatus, PgTestResult} from '@/lib/types/settings';

export const pgSyncApi = {
  config: () => get<{config: PgSyncConfig; status: PgSyncStatus}>('/api/pgsync'),
  save: (body: Partial<PgSyncConfig>) =>
    post<{config: PgSyncConfig}>('/api/pgsync', body),
  /** 用表单当前值（未填字段回落已保存配置）探测连通性：先测试、后保存都走得通 */
  test: (body: Partial<PgSyncConfig>) => post<PgTestResult>('/api/pgsync/test', body),
  exportData: () => post<PgJobResult>('/api/pgsync/export'),
  importData: () => post<PgJobResult>('/api/pgsync/import'),
  status: () => get<PgSyncStatus>('/api/pgsync/status'),
};
