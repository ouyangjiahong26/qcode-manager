'use client';

/**
 * 仪表盘：账号池健康 + 今日/近期用量快照。
 *
 * 数据流：useAsyncAll 一次并发取 4 份（账号列表含汇总、总览、近 14 天按天、
 * 按模型），30s 心跳刷新（refresh 模式不清屏、骨架不闪）。
 * 图表序列的口径收在 lib/usage-chart（统计页复用同一套）。
 */
import {useEffect} from 'react';
import {
  Activity,
  BarChart3,
  CircleCheck,
  CircleX,
  Coins,
  KeyRound,
  TriangleAlert,
} from 'lucide-react';
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {PageSectionTabs} from '@/components/common/layout/PageSectionTabs';
import {StatCard} from '@/components/common/layout/StatCard';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {useT} from '@/lib/i18n/provider';
import {useAsyncAll} from '@/lib/use-async-data';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {accountsApi} from '@/lib/api/accounts';
import {statsApi} from '@/lib/api/stats';
import type {ModelUsage, StatsSummary} from '@/lib/types/stats';
import {fmtCompact, fmtNumber} from '@/lib/format';
import {chartSpecFor, dailySeries, topModels} from '@/lib/usage-chart';

const EMPTY_MODELS: ModelUsage[] = [];

export default function DashboardPage() {
  const t = useT();
  const {values, errors, isInitialLoading, isInitialFailed, isRefreshing, reload} = useAsyncAll({
    accounts: () => accountsApi.list(),
    summary: () => statsApi.summary(),
    daily: () => statsApi.daily(14),
    byModel: () => statsApi.byModel(),
  });

  useHeartbeat(reload, 30000);

  // 账号页改了账号池时同步刷新本页的计数卡片
  useEffect(() => {
    const onChanged = () => void reload();
    window.addEventListener('qcoder-manager:accounts-changed', onChanged);
    return () => window.removeEventListener('qcoder-manager:accounts-changed', onChanged);
  }, [reload]);

  if (isInitialLoading) {
    return (
      <div className="space-y-4">
        <PageHeader title={t('dashboard.title')} description={t('dashboard.desc')} />
        <PageSectionTabs />
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
          {Array.from({length: 6}, (_, i) => (
            <SkeletonBar key={i} className="h-[96px] rounded-[20px]" />
          ))}
        </div>
        <SkeletonBar className="h-[260px] rounded-[20px]" />
      </div>
    );
  }
  if (isInitialFailed) {
    return (
      <div className="space-y-4">
        <PageHeader title={t('dashboard.title')} description={t('dashboard.desc')} />
        <PageSectionTabs />
        <LoadError variant="page" onRetry={reload} />
      </div>
    );
  }

  const summary: StatsSummary | null = values.summary ?? null;
  const pool = values.accounts?.summary ?? null;
  const daily = dailySeries(values.daily ?? [], 14);
  const trend = chartSpecFor(daily, 'requests');
  const models = topModels(values.byModel ?? EMPTY_MODELS, 8);

  // 失败率口径：失败数来自 request_logs（被拒的请求不在「请求数」里），
  // 分母因此是 成功 + 失败，而不是只拿成功数当分母（那会把失败率虚高）。
  const todayFailed = summary ? summary.failures.today_4xx + summary.failures.today_5xx : 0;
  const failureRate = summary && summary.today_requests + todayFailed > 0
    ? `${((todayFailed / (summary.today_requests + todayFailed)) * 100).toFixed(1)}%`
    : '—';

  return (
    <div className="space-y-4" aria-busy={isRefreshing}>
      <PageHeader title={t('dashboard.title')} description={t('dashboard.desc')} />
      <PageSectionTabs />

      {Object.keys(errors).length > 0 && <LoadError onRetry={reload} />}

      {/* 统计写入健康告警：统计没在累计时数字会「停着不动」，必须明说而不是装正常 */}
      {summary && !summary.usage_health.ok && (
        <div className="flex items-start gap-2 rounded-[16px] border border-amber-500/30 bg-amber-500/[0.07] px-3.5 py-2.5 text-xs">
          <TriangleAlert className="mt-px h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
          <span className="leading-5">{summary.usage_health.detail}</span>
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        <StatCard
          label={t('dashboard.cardTotal')} value={pool ? fmtNumber(pool.total) : '—'}
          icon={KeyRound} tone="neutral" delay={0}
        />
        <StatCard
          label={t('dashboard.cardActive')} value={pool ? fmtNumber(pool.active) : '—'}
          icon={CircleCheck} tone="success" delay={0.05}
          hint={pool ? t('dashboard.hintEnabled', {count: pool.enabled}) : undefined}
        />
        <StatCard
          label={t('dashboard.cardInvalid')} value={pool ? fmtNumber(pool.invalid) : '—'}
          icon={CircleX} tone={pool && pool.invalid > 0 ? 'danger' : 'neutral'} delay={0.1}
          hint={pool && pool.unknown > 0 ? t('dashboard.hintUnknown', {count: pool.unknown}) : undefined}
        />
        <StatCard
          label={t('dashboard.cardTodayReq')}
          value={summary ? fmtNumber(summary.today_requests) : '—'}
          icon={Activity} tone="info" delay={0.15}
        />
        <StatCard
          label={t('dashboard.cardTodayTokens')}
          value={summary ? fmtCompact(summary.today_tokens) : '—'}
          icon={Coins} tone="accent" delay={0.2}
        />
        <StatCard
          label={t('dashboard.cardFailRate')} value={failureRate}
          icon={TriangleAlert} tone={todayFailed > 0 ? 'warning' : 'neutral'} delay={0.25}
          hint={summary ? t('dashboard.hintFailures', {count: todayFailed}) : undefined}
        />
      </div>

      <div className="grid gap-3 lg:grid-cols-2">
        {/* 用量趋势：成功面积为请求量，失败单独一条线——被拒/全池不可用的日子
            在 usage_daily 里不存在，只有 failed 系列能让「出过事」被看见 */}
        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-[13px] font-medium">{t('dashboard.trendTitle')}</h2>
            <span className="text-[11px] text-muted-foreground">{t('dashboard.trendRange')}</span>
          </div>
          <div className="h-[220px]">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={trend.data} margin={{top: 4, right: 4, left: -18, bottom: 0}}>
                <defs>
                  <linearGradient id="qc-req" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--chart-1)" stopOpacity={0.35} />
                    <stop offset="100%" stopColor="var(--chart-1)" stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="currentColor" className="text-border" vertical={false} />
                <XAxis dataKey="label" tick={{fontSize: 10}} interval="preserveStartEnd" tickLine={false} axisLine={false} />
                <YAxis domain={[0, trend.yTop]} tick={{fontSize: 10}} tickLine={false} axisLine={false} width={48} />
                <Tooltip
                  contentStyle={{fontSize: 11, borderRadius: 12, border: '1px solid var(--border)'}}
                  formatter={(v: number | string, name) => [fmtNumber(Number(v)), t(`dashboard.${name}`)]}
                />
                <Area type="monotone" dataKey="requests" stroke="var(--chart-1)" strokeWidth={2} fill="url(#qc-req)" />
                <Area type="monotone" dataKey="failed" stroke="#ef4444" strokeWidth={1.5} fill="transparent" />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>

        {/* 按模型：近 30 天 token 总量（prompt + completion 合计的柱） */}
        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-[13px] font-medium">{t('dashboard.byModelTitle')}</h2>
            <span className="text-[11px] text-muted-foreground">{t('dashboard.byModelRange')}</span>
          </div>
          {models.length === 0 ? (
            <EmptyState icon={BarChart3} title={t('dashboard.byModelEmpty')} className="h-[220px] py-8" />
          ) : (
            <div className="h-[220px]">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={models} margin={{top: 4, right: 4, left: -8, bottom: 0}}>
                  <CartesianGrid strokeDasharray="3 3" stroke="currentColor" className="text-border" vertical={false} />
                  <XAxis dataKey="name" tick={{fontSize: 10}} tickLine={false} axisLine={false} interval={0} angle={-20} textAnchor="end" height={44} />
                  <YAxis tick={{fontSize: 10}} tickLine={false} axisLine={false} width={56} tickFormatter={(v: number) => fmtCompact(v)} />
                  <Tooltip
                    contentStyle={{fontSize: 11, borderRadius: 12, border: '1px solid var(--border)'}}
                    formatter={(v: number | string) => [fmtNumber(Number(v)), t('dashboard.tokens')]}
                  />
                  <Bar dataKey="prompt_tokens" stackId="tok" fill="var(--chart-1)" radius={[0, 0, 0, 0]} />
                  <Bar dataKey="completion_tokens" stackId="tok" fill="var(--chart-2)" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>
      </div>

      {/* 失败摘要：今天/近 7 天 × 4xx/5xx。4xx 多半是密钥被拒或限流，
          5xx 是池/上游不可用，分档摆出来才知道该去看哪一头 */}
      {summary && (
        <div className="rounded-[20px] bg-muted p-4">
          <h2 className="mb-3 text-[13px] font-medium">{t('dashboard.failTitle')}</h2>
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <FailTile label={t('dashboard.failToday4xx')} value={summary.failures.today_4xx} />
            <FailTile label={t('dashboard.failToday5xx')} value={summary.failures.today_5xx} />
            <FailTile label={t('dashboard.failWeek4xx')} value={summary.failures.week_4xx} />
            <FailTile label={t('dashboard.failWeek5xx')} value={summary.failures.week_5xx} />
          </div>
        </div>
      )}
    </div>
  );
}

/** 失败摘要的一格：非 0 才标红——全 0 时红色格阵列只会制造焦虑 */
function FailTile({label, value}: {label: string; value: number}) {
  return (
    <div className="rounded-2xl bg-background/60 px-3.5 py-3">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className={'mt-1.5 text-lg font-semibold tabular-nums ' + (value > 0 ? 'text-red-600 dark:text-red-400' : '')}>
        {fmtNumber(value)}
      </div>
    </div>
  );
}
