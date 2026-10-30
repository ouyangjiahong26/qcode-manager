/** 日志域接口。 */
import {get, post} from './http';
import type {LogListResult, LogQuery} from '@/lib/types/logs';

export const logsApi = {
  list: (q: LogQuery) => get<LogListResult>('/api/logs', {...q}),

  /** 管理员清空全部请求日志（require_session_admin：只收会话、不收 API Token） */
  clear: () => post<{ok: boolean}>('/api/logs/clear'),
};
