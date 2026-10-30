/**
 * 设置页域 API：模型映射、管理端用户、GitHub Token / 镜像 / 自动更新，
 * 以及多上游的完整 CRUD + 探测。
 *
 * 上游管理放在这里（而不是 lib/api/upstreams.ts）的原因：那份文件属于账号页，
 * 只需要清单与探测两个只读方法；这里的写操作（创建 / 修改 / 删除）全部要求
 * 会话管理员，语义与生命周期都跟设置页绑定，两边各自演进不会互相牵连。
 */
import {get, post, patch, put, del} from './http';
import type {
  AutoUpdateConfig, AutoUpdateInfo, GithubMirrorInfo, GithubTokenInfo,
  ModelMap, UpstreamEndpoint, UpstreamInput, UserItem,
} from '@/lib/types/settings';

export const settingsApi = {
  // ── 模型映射 ──
  modelMap: () => get<ModelMap>('/api/settings/model-map'),
  saveModelMap: (body: ModelMap) => post<ModelMap>('/api/settings/model-map', body),

  // ── 管理端用户 ──
  users: () => get<UserItem[]>('/api/users'),
  addUser: (body: {username: string; password: string; role: string}) =>
    post<UserItem>('/api/users', body),
  /** 改密码 / 改角色；两者都会吊销目标用户的既有会话 */
  updateUser: (username: string, body: {password?: string; role?: string}) =>
    patch<{
      username: string;
      role: string;
      sessions_revoked: boolean;
      /** 改的是自己的密码时为 true —— 前端据此引导重新登录 */
      relogin_required: boolean;
    }>(`/api/users/${encodeURIComponent(username)}`, body),
  removeUser: (username: string) =>
    del<{ok: boolean}>(`/api/users/${encodeURIComponent(username)}`),

  // ── GitHub Token / 镜像 / 自动更新（系统更新 Tab 的配置区）──
  githubToken: () => get<GithubTokenInfo>('/api/settings/github-token'),
  saveGithubToken: (token: string) =>
    put<GithubTokenInfo>('/api/settings/github-token', {token}),
  githubMirror: () => get<GithubMirrorInfo>('/api/settings/github-mirror'),
  saveGithubMirror: (mirror: string) =>
    put<GithubMirrorInfo>('/api/settings/github-mirror', {mirror}),
  autoUpdate: () => get<AutoUpdateInfo>('/api/settings/auto-update'),
  saveAutoUpdate: (cfg: AutoUpdateConfig) =>
    put<AutoUpdateInfo>('/api/settings/auto-update', cfg),
};

/** 多上游 CRUD + 探测（设置页「上游分组」Tab 专用） */
export const upstreamsApi = {
  list: async (): Promise<UpstreamEndpoint[]> =>
    (await get<{items: UpstreamEndpoint[]}>('/api/upstreams')).items,
  create: (body: UpstreamInput) => post<UpstreamEndpoint>('/api/upstreams', body),
  /** patch 只提交改动字段；api_key 留空 = 不修改（后端 exclude_unset 语义） */
  update: (id: number, body: Partial<UpstreamInput>) =>
    patch<UpstreamEndpoint>(`/api/upstreams/${id}`, body),
  /** 删除被引用（409）时后端在 detail 里说明绑定数，由调用方原样弹出 */
  remove: (id: number) => del<{ok: boolean}>(`/api/upstreams/${id}`),
  probe: (id: number) =>
    post<{ok: boolean; message: string}>(`/api/upstreams/${id}/probe`),
};
