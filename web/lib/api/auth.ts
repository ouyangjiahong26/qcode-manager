import {get, post} from './http';
import type {LoginResult, Me} from '@/lib/types/auth';


/** 会话鉴权域：登录态校验、登出、吊销全部会话。 */
export const authApi = {
  me: () => get<Me>('/api/me'),
  login: (username: string, password: string) =>
    post<LoginResult>('/api/login', {username, password}),
  logout: () => post<{ok: boolean}>('/api/logout'),
  /** 吊销当前用户的**全部**会话（含本机）——服务端递增会话版本，所有 cookie 立即失效。 */
  revokeSessions: () => post<{ok: boolean; relogin_required: boolean}>('/api/sessions/revoke'),
};
