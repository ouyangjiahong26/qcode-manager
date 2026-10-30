'use client';

import {useCallback, useMemo, useRef, useState} from 'react';
import {Boxes, CalendarClock, Loader2, RefreshCw, Search, User} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {PageSectionTabs} from '@/components/common/layout/PageSectionTabs';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {StatCard} from '@/components/common/layout/StatCard';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import type {UpstreamModel} from '@/lib/types/models';
import {modelsApi} from '@/lib/api/models';
import {useAsyncAll} from '@/lib/use-async-data';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';

/**
 * 模型列表的占位。模块级常量而不是每帧新建的 `[]`：它进了下面 useMemo 的
 * 依赖数组，每帧新引用会让缓存永远失效。
 */
const EMPTY_MODELS: UpstreamModel[] = [];

/** created 是 Unix 秒：只展示日期就够，清单变化频率远低于天 */
function fmtDate(sec?: number): string {
  if (!sec) return '—';
  return new Date(sec * 1000).toLocaleDateString();
}

export default function ModelsPage() {
  const t = useT();
  const [q, setQ] = useState('');

  /**
   * 「强制刷新」= 绕过**后端**缓存再拉一次（`force=1` 会真实消耗一次上游调用）。
   *
   * 用 ref 而不是 state：它描述的是**下一次请求**要不要绕缓存，不参与渲染，
   * 也不该触发重渲染；放 state 里还容易被误放进依赖数组。
   */
  const forceNext = useRef(false);

  // 目录变化极少，页面不做心跳轮询，只保留手动强制刷新。
  const {values, isInitialLoading, isInitialFailed, isRefreshing, reload} = useAsyncAll({
    catalog: () => modelsApi.catalog(forceNext.current),
  });

  const data = values.catalog ?? null;

  /**
   * 强制刷新：绕过服务端缓存重拉一次。**返回本次是否取到**，调用方据此提示
   * 成功还是失败。只能看返回值，不能写 `.catch()`：`reload()` 内部已把异常
   * 收进 `errors` 并返回布尔值，返回的 Promise 永不 reject。
   */
  const refetch = useCallback(async (): Promise<boolean> => {
    forceNext.current = true;
    try {
      return await reload();
    } finally {
      forceNext.current = false;
    }
  }, [reload]);

  const models = data?.models ?? EMPTY_MODELS;

  // 搜索只按 id / owned_by 过滤——上游清单里用户真正会找的就是模型名
  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    if (!kw) return models;
    return models.filter(
      (m) =>
        m.id.toLowerCase().includes(kw) ||
        (m.owned_by || '').toLowerCase().includes(kw),
    );
  }, [models, q]);

  // owned_by 去重计数：多数上游只有一个组织，>1 时才有区分意义
  const owners = useMemo(
    () => new Set(models.map((m) => m.owned_by).filter(Boolean)),
    [models],
  );

  const header = (
    <>
      <PageHeader
        title={t('models.title')}
        description={t('models.description')}
        actions={
          <Button
            variant="outline"
            size="sm"
            className="rounded-full"
            disabled={isRefreshing}
            title={t('models.refetchTitle')}
            onClick={async () => {
              // 提示在拿到结果后**换掉**，而不是留在屏幕上：不看结果的话，
              // 无论成败都停在「正在拉取」，用户分不清是还在拉还是没拉到。
              const pending = notify.info(t('models.refetching'));
              const done = await refetch();
              notify.dismiss(pending);
              if (done) {
                notify.ok(t('models.refetchDone'), t('models.refetchDoneHint'));
              } else {
                notify.err(t('models.refetchFailed'), t('models.refetchFailedHint'));
              }
            }}
          >
            {isRefreshing ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            {t('models.refetch')}
          </Button>
        }
      />
      {/* 二级导航放在 `header` 里而不是两个 return 分支各写一遍：下面的首屏
          守卫是**早返回**，漏一处就会出现「取不到目录时 Tab 不见了」。 */}
      <PageSectionTabs />
    </>
  );

  /**
   * 首屏守卫：目录还没到（或没取到）时不渲染列表与统计卡——否则取数失败时
   * 空态会照常出现，等于告诉用户「上游没有模型」，而事实是没取到。
   * 用 `isInitialLoading / isInitialFailed`（按有没有数据判定），不用
   * 「请求在不在飞」：后者会让强制刷新把整页闪成骨架。
   */
  if (isInitialFailed || isInitialLoading) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        {isInitialFailed ? (
          <LoadError variant="page" onRetry={reload} />
        ) : (
          <ModelsSkeleton />
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6" aria-busy={isRefreshing}>
      {header}

      {/* 统计汇总：总数 / 组织数 / 最近上架 */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <StatCard
          label={t('models.statTotal')}
          value={models.length}
          hint={t('models.statTotalHint')}
          icon={Boxes}
        />
        <StatCard
          label={t('models.statOwners')}
          value={owners.size}
          hint={
            owners.size > 1
              ? t('models.statOwnersHintMulti')
              : t('models.statOwnersHint')
          }
          icon={User}
        />
        <StatCard
          label={t('models.statNewest')}
          value={models.length ? fmtDate(Math.max(...models.map((m) => m.created || 0))) : '—'}
          hint={t('models.statNewestHint')}
          icon={CalendarClock}
        />
      </div>

      {/* 搜索条 */}
      <div className="relative max-w-sm">
        <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder={t('models.searchPlaceholder')}
          className="rounded-xl pl-9"
          aria-label={t('models.searchPlaceholder')}
        />
      </div>

      {models.length === 0 ? (
        <EmptyState
          icon={Boxes}
          title={t('models.emptyTitle')}
          description={t('models.emptyDesc')}
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          icon={Search}
          title={t('models.noMatchTitle')}
          description={t('models.noMatchDesc', {q: q.trim()})}
        />
      ) : (
        <div className="overflow-hidden rounded-[16px] border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t('models.colId')}</TableHead>
                <TableHead>{t('models.colOwner')}</TableHead>
                <TableHead>{t('models.colObject')}</TableHead>
                <TableHead className="text-right">{t('models.colCreated')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {filtered.map((m) => (
                <TableRow key={m.id}>
                  {/* 模型 id 是调用时要原样填进请求的值，用等宽字体便于对照 */}
                  <TableCell className="font-mono text-xs">{m.id}</TableCell>
                  <TableCell>
                    {m.owned_by ? (
                      <Badge variant="secondary" className="rounded-md text-[10px]">
                        {m.owned_by}
                      </Badge>
                    ) : (
                      '—'
                    )}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {m.object || '—'}
                  </TableCell>
                  <TableCell className="text-right text-xs tabular-nums text-muted-foreground">
                    {fmtDate(m.created)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}

/** 首屏骨架：形状对着真实版面（三张卡 + 搜索条 + 表格行）来，数据到位不跳版 */
function ModelsSkeleton() {
  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {['', '', ''].map((_, i) => (
          <div key={i} className="min-h-[88px] rounded-[20px] bg-muted p-4 sm:min-h-[96px]">
            <SkeletonBar className="h-3 w-20" />
            <SkeletonBar className="mt-4 h-7 w-14" />
          </div>
        ))}
      </div>
      <SkeletonBar className="h-9 w-full max-w-sm rounded-xl" />
      <div className="overflow-hidden rounded-[16px] border">
        {[0, 1, 2, 3, 4].map((i) => (
          <div key={i} className="border-b last:border-b-0 px-4 py-3">
            <SkeletonBar className="h-4 w-full max-w-md" />
          </div>
        ))}
      </div>
    </div>
  );
}
