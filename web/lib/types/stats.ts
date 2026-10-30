/** 用量统计域类型：字段以 server/routers/stats.py 为准。 */

/** 总览（GET /api/stats/summary） */
export interface StatsSummary {
  today_requests: number;
  today_tokens: number;
  week_requests: number;
  week_tokens: number;
  total_requests: number;
  total_tokens: number;
  active_keys: number;
  top_model: string | null;
  /** 统计写入是否正常（今天的日志有量而统计为 0 = 写入路径坏了） */
  usage_health: {ok: boolean; detail: string; logs_today: number};
  /** 失败请求数（4xx/5xx 分档；来自 request_logs，不在 usage_daily 里） */
  failures: {
    today_4xx: number;
    today_5xx: number;
    week_4xx: number;
    week_5xx: number;
  };
}

/** 按天聚合的一行（GET /api/stats/daily） */
export interface DailyUsagePoint {
  /** 本地时区日期，YYYY-MM-DD */
  day: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  /** 当天失败请求数（4xx + 5xx 合计） */
  failed: number;
}

/** 按模型聚合的一行（GET /api/stats/by-model） */
export interface ModelUsage {
  name: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  tokens_per_sec: number | null;
  throughput_samples: number;
}

/** 按小时聚合的一行（GET /api/stats/hourly，固定 24 个桶） */
export interface HourlyUsagePoint {
  day: string;
  hour: number;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  failed: number;
}
