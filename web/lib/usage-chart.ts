/**
 * 用量图表的序列构造 —— 纯函数，不依赖 React / recharts。
 *
 * 单独成模块的原因：仪表盘与统计页都要画「按天用量趋势」，两处各自拼一遍
 * 必然出现口径分叉（一处补零、一处不补；一处含今天、一处不含）。这里收口：
 *   · dailySeries —— 把后端 /stats/daily 的行整理成图表点，并按本地日历日补零：
 *     没有流量的日子在图上应该是「0 柱」，而不是让曲线直接跳过去；
 *   · chartSpecFor —— 由数据推导系列与 Y 轴上限（取整齐档位），
 *     没有这一步 recharts 会把轴取到数据极值，柱子贴边、0 线不在视觉基线。
 */
import type {DailyUsagePoint, ModelUsage} from '@/lib/types/stats';

/** 图表点：dailySeries 的输出行 */
export interface UsageChartPoint {
  /** YYYY-MM-DD（本地时区） */
  day: string;
  /** 短标签（M/D），给 X 轴用 */
  label: string;
  requests: number;
  failed: number;
  prompt_tokens: number;
  completion_tokens: number;
  /** prompt + completion 合计 */
  tokens: number;
}

/** 单个系列的描述（key 同时是数据字段名与颜色 token） */
export interface ChartSeries {
  key: 'requests' | 'failed' | 'tokens' | 'prompt_tokens' | 'completion_tokens';
  color: string;
  stackId?: string;
}

export interface UsageChartSpec {
  data: UsageChartPoint[];
  series: ChartSeries[];
  /** Y 轴整齐上限（1/2/2.5/5 × 10^n 档）；数据全 0 时给一个不夸张的默认档 */
  yTop: number;
}

/**
 * 把 /stats/daily 的行整理为图表序列：按本地日历日补零、排序、算 tokens 合计。
 *
 * 范围取「近 days 天且含今天」（与后端 _since 同口径）：前端自己补齐两端，
 * 不依赖后端是否返回无流量的日子。后端只保证「只有失败的日子」也出行，
 * 两边合并后全范围都有点。
 */
export function dailySeries(rows: readonly DailyUsagePoint[], days = 14): UsageChartPoint[] {
  const byDay = new Map<string, UsageChartPoint>();
  for (const r of rows) {
    const p = Number(r.prompt_tokens) || 0;
    const c = Number(r.completion_tokens) || 0;
    byDay.set(r.day, {
      day: r.day,
      label: shortLabel(r.day),
      requests: Number(r.requests) || 0,
      failed: Number(r.failed) || 0,
      prompt_tokens: p,
      completion_tokens: c,
      tokens: p + c,
    });
  }
  const out: UsageChartPoint[] = [];
  const n = Math.max(1, Math.min(3650, Math.floor(days)));
  const today = new Date();
  for (let i = n - 1; i >= 0; i--) {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() - i);
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    out.push(byDay.get(key) ?? {
      day: key,
      label: shortLabel(key),
      requests: 0,
      failed: 0,
      prompt_tokens: 0,
      completion_tokens: 0,
      tokens: 0,
    });
  }
  return out;
}

/** YYYY-MM-DD → 本地短标签（如 9/28；跨年时保留年份避免歧义）。两处调用同一格式，收口在此。 */
function shortLabel(day: string): string {
  const [, m, d] = day.split('-');
  const year = Number(day.slice(0, 4));
  const md = `${Number(m)}/${Number(d)}`;
  return year === new Date().getFullYear() ? md : `${year}/${md}`;
}

/**
 * 由序列数据推导图表规格。variant 决定画什么：
 *   · 'requests' —— 请求量（含失败系列）；
 *   · 'tokens'   —— token 总量（prompt 与 completion 各一柱，堆叠）。
 */
export function chartSpecFor(
  points: readonly UsageChartPoint[],
  variant: 'requests' | 'tokens',
): UsageChartSpec {
  const data = [...points];
  if (variant === 'requests') {
    const top = Math.max(0, ...data.map((p) => Math.max(p.requests, p.failed)));
    return {
      data,
      series: [
        {key: 'requests', color: 'var(--chart-1)'},
        {key: 'failed', color: 'var(--chart-5, #ef4444)'},
      ],
      yTop: niceTop(top),
    };
  }
  const top = Math.max(0, ...data.map((p) => p.tokens));
  return {
    data,
    series: [
      {key: 'prompt_tokens', color: 'var(--chart-1)', stackId: 'tok'},
      {key: 'completion_tokens', color: 'var(--chart-2)', stackId: 'tok'},
    ],
    yTop: niceTop(top),
  };
}

/**
 * 把任意峰值抬到 1/2/2.5/5 × 10^n 的整齐上限。
 * 全 0 时给 5：轴上留出刻度空间，同时不夸大「没有流量」这个事实。
 */
export function niceTop(max: number): number {
  if (!Number.isFinite(max) || max <= 0) return 5;
  const exp = Math.floor(Math.log10(max));
  const base = Math.pow(10, exp);
  for (const m of [1, 2, 2.5, 5, 10]) {
    if (max <= m * base) return m * base;
  }
  return 10 * base;
}

/** 按模型取前 N 个（后端已按 token 降序，这里只截断），供柱状图用 */
export function topModels(rows: readonly ModelUsage[], limit = 8): ModelUsage[] {
  return rows.slice(0, Math.max(1, limit));
}
