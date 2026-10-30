/**
 * 模型目录接口。后端带 5 分钟进程内缓存，`force=1` 绕过（会真实消耗一次
 * 上游调用，因此只有「强制刷新」按钮传它）。
 */
import {get} from './http';
import type {ModelCatalog} from '@/lib/types/models';

export const modelsApi = {
  /**
   * @param force 绕过后端缓存重拉一次
   */
  catalog: (force = false) =>
    get<ModelCatalog>('/api/models', force ? {force: 1} : undefined),
};
