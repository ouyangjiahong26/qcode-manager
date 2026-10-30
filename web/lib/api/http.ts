import axios, {AxiosError} from 'axios';
import {BASE_PATH} from '@/lib/base-path';
import {t, tp} from '@/lib/i18n';

export const http = axios.create({
  // 子路径部署时接口也挂在前缀下（反向代理剥掉前缀再转发给本服务）。
  // 这里必须用 basePath，否则所有请求都会打到域名根路径的 /api 上。
  baseURL: BASE_PATH,
  withCredentials: true,
  timeout: 60000,
});

/**
 * 统一抽取后端错误信息。
 *
 * 后端的报错是中文（服务端不做多语言），这里过一遍短语表：命中已收录的后端文案
 * 就换成当前语言，没收录的原样展示——既不需要后端改造，也不会因为漏收录而
 * 显示成键名或空白。
 *
 * 403 **不做统一改写**。这个状态码在本项目里有多种互不相干的含义：角色不够
 * （require_admin）、接口只收会话不收 API Token（require_session_admin）。原文
 * 常是**可照做的操作说明**，一律改写成「权限不足」会把用户唯一能照着做的那句
 * 话抹掉。所以只要后端给了文案，就永远优先用它。
 *
 * 兜底只在后端**没给**文案时生效。那种情况下原本会露出 axios 自己的英文
 * message（"Request failed with status code 403"）——界面明明是多语言的，
 * 偏偏在这条路径上漏出英文，而且对用户没有任何指导意义。
 */
export function errText(e: unknown): string {
  const ax = e as AxiosError<{detail?: string; error?: string}>;
  const d = ax?.response?.data;
  const raw = (typeof d === 'string' ? d : d?.detail || d?.error) || '';
  if (raw) return tp(raw);
  if (ax?.response?.status === 403) return t('error.forbidden');
  const fallback = ax?.message || '';
  return fallback ? tp(fallback) : tp('请求失败');
}

http.interceptors.response.use(
  (r) => r,
  (error: AxiosError) => {
    if (error.response?.status === 401 && typeof window !== 'undefined') {
      // 会话失效：清掉缓存的登录态，避免仍显示管理员入口。
      // `hadSession` 决定要不要说「会话已过期」：首次访问本来就没有会话，
      // 那是「请登录」而不是「过期了」（用户会以为面板坏了）。
      const hadSession = !!window.sessionStorage.getItem('qcode-manager:me');
      window.sessionStorage.removeItem('qcode-manager:me');
      const path = window.location.pathname;
      const loginPath = `${BASE_PATH}/login`;
      // 根路径只是「跳到 /dashboard」的中转页，不在这里拦（否则首次访问者
      // 会先被重定向到登录页并显示「会话已过期」）。
      // 路径必须带 basePath：basePath 只自动作用于 next/router 的跳转，裸的
      // window.location.href 会跳到域名根（通常 404）。
      const isRoot = path === BASE_PATH || path === `${BASE_PATH}/`;
      if (!isRoot && path !== loginPath && !path.startsWith(`${loginPath}/`)) {
        // 带上 `?expired=1`：登录页据此显示「会话已过期，请重新登录」，而不是
        // 毫无解释地把人丢回登录页。服务端鉴权不受影响，这只是给跳转补一个原因。
        window.location.href = `${loginPath}${hadSession ? '?expired=1' : ''}`;
      }
    }
    return Promise.reject(error);
  },
);

export const get = async <T>(url: string, params?: Record<string, unknown>): Promise<T> =>
  (await http.get<T>(url, {params})).data;
export const post = async <T>(url: string, body?: unknown): Promise<T> =>
  (await http.post<T>(url, body)).data;
export const patch = async <T>(url: string, body?: unknown): Promise<T> =>
  (await http.patch<T>(url, body)).data;
export const put = async <T>(url: string, body?: unknown): Promise<T> =>
  (await http.put<T>(url, body)).data;
export const del = async <T>(url: string): Promise<T> => (await http.delete<T>(url)).data;
