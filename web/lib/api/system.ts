/**
 * 系统维护域 API：一键更新、版本检测、离线升级包、更新日志与健康检查。
 *
 * 更新在后台执行并重启服务，因此 startUpdate 立即返回，进度靠轮询
 * updateStatus；离线包上传走 multipart（axios 自己处理 FormData）。
 */
import {del, get, http, post} from './http';
import type {
  Changelog, SystemVersions, UpdateCheck, UpdatePackageMeta, UpdateStatus,
} from '@/lib/types/settings';

export const systemApi = {
  updateStatus: () => get<UpdateStatus>('/api/system/update-status'),
  /** source：release = GitHub 最新发布；uploaded = 面板已上传的离线包 */
  startUpdate: (source: 'release' | 'uploaded' = 'release') =>
    post<{ok: boolean; message: string}>('/api/system/update', {source}),
  versions: () => get<SystemVersions>('/api/system/versions'),
  /** @param force 绕过服务端 6 小时缓存（仅管理员生效，会真实打 GitHub API） */
  checkUpdate: (force = false) =>
    get<UpdateCheck>('/api/system/check-update', force ? {force: 1} : undefined),
  packageMeta: () => get<UpdatePackageMeta>('/api/system/update-package'),
  /** multipart：body 是 FormData，axios 会自动设置边界，不能走 JSON 序列化的封装 */
  uploadPackage: (file: File, expectedSha256: string) => {
    const form = new FormData();
    form.append('file', file);
    form.append('expected_sha256', expectedSha256);
    return http.post<UpdatePackageMeta>('/api/system/update-package', form).then((r) => r.data);
  },
  deletePackage: () => del<{ok: boolean}>('/api/system/update-package'),
  changelog: () => get<Changelog>('/api/system/changelog'),
  /**
   * 健康检查：{ok, service, upstream}。设置页用它的 upstream 探测结果
   * 展示「默认上游是否可达」（默认上游没有 id，走不了 /api/upstreams/{id}/probe）。
   */
  healthz: () =>
    get<{ok: boolean; service: string; upstream: {ok: boolean; error: string}}>(
      '/healthz'),
};
