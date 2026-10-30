/** 日志域类型：与 server/routers/logs.py 的返回字段一一对应。 */

export interface RequestLog {
  id: number;
  /** 秒级时间戳 */
  ts: number;
  key_id: number | null;
  /** 联表出来的密钥名；密钥已删时为 null */
  key_name: string | null;
  ip: string | null;
  model: string | null;
  /** 经映射后实际发给上游的模型名 */
  mapped_model: string | null;
  /** HTTP 状态码；0 = 请求没走到响应那一步 */
  status: number;
  prompt_tokens: number;
  completion_tokens: number;
  latency_ms: number;
  /** 首字延迟；null = 未采集（非流式请求或旧记录） */
  first_token_ms: number | null;
  ua: string | null;
  error: string | null;
  stream: boolean;
  /** 本次实际使用的上游账号标签；null = 尚未回填（比请求晚几秒） */
  account: string | null;
  /** 思考强度（reasoning_effort）；null = 没传 / 旧记录 */
  effort: string | null;
  /** 客户端渠道（UA 归类）；null = 该列上线前的旧记录 */
  channel: string | null;
}

export interface LogQuery {
  page?: number;
  size?: number;
  key_id?: string;
  model?: string;
  ip?: string;
  /** 'ok' | 'error' */
  status?: string;
  days?: number;
}

export interface LogListResult {
  total: number;
  items: RequestLog[];
}
