/**
 * 聊天测试台接口。
 *
 * `/chat` 是 SSE 流式，axios 拿不到 ReadableStream，页面里用原生 fetch 直接调
 * （见 app/(main)/playground/page.tsx 的注释）；这里只承载 JSON 部分。
 */
import {get} from './http';
import type {ModelCatalog} from '@/lib/types/models';

/** 测试台的聊天请求体，字段与 server/routers/playground.py 的 ChatIn 对齐 */
export interface PlaygroundChatBody {
  model: string;
  messages: {role: 'user' | 'assistant'; content: string}[];
  stream: boolean;
  /** 推理档位；空串 = 不指定 */
  reasoning_effort?: string;
}

export const playgroundApi = {
  /** 测试台的模型下拉：与模型页同源（同一条后端缓存路径） */
  models: () => get<ModelCatalog>('/api/playground/models'),
};
