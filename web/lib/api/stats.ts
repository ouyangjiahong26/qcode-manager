/** 用量统计域 API：对齐 server/routers/stats.py。days 语义为「近 N 天且含今天」。 */
import {get} from './http';
import type {DailyUsagePoint, HourlyUsagePoint, ModelUsage, StatsSummary} from '@/lib/types/stats';

export const statsApi = {
  summary: () => get<StatsSummary>('/api/stats/summary'),
  daily: (days = 30) => get<DailyUsagePoint[]>('/api/stats/daily', {days}),
  hourly: (day?: string) => get<HourlyUsagePoint[]>('/api/stats/hourly', day ? {day} : undefined),
  byModel: (days = 30) => get<ModelUsage[]>('/api/stats/by-model', {days}),
};
