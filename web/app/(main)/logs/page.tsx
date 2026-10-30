'use client';

import {useCallback, useState} from 'react';
import {ScrollText, RotateCcw, Trash2, ChevronLeft, ChevronRight} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {notify} from '@/lib/toast';
import {keysApi} from '@/lib/api/keys';
import {logsApi} from '@/lib/api/logs';
import {errText} from '@/lib/api/http';
import {useAsyncAll} from '@/lib/use-async-data';
import {useDebounced} from '@/lib/use-debounce';
import type {RequestLog} from '@/lib/types/logs';
import type {ApiKey} from '@/lib/types/keys';
import {fmtDateTime, fmtDateTimeMarked, fmtLatency, fmtNumber} from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {Button} from '@/components/ui/button';
import {CopyButton} from '@/components/ui/copy-button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Drawer,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer';
import {
  Table,
  TableCell,
  TableBody,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

const PAGE_SIZE = 20;

/**
 * 默认时间范围（天）。
 *
 * 提成常量是因为有两个地方要用同一个值：「重置筛选」要把天数改回来，还要判断
 * 「用户有没有改过筛选」（决定那个按钮能不能点）。两处各写一个 `'7'` 迟早不一致。
 */
const DEFAULT_DAYS = '7';

/**
 * 空数组的稳定引用。
 *
 * 必须是模块级常量，不能每次渲染现写一个 `[]`：它们是「还没取到」时的兜底值，
 * 每帧新建的数组会让下游任何依赖它的 `useMemo` / `useEffect` 每帧重算。
 */
const EMPTY_LOGS: RequestLog[] = [];
const EMPTY_KEYS: ApiKey[] = [];

export default function LogsPage() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [page, setPage] = useState(1);
  const [detail, setDetail] = useState<RequestLog | null>(null);

  const [keyId, setKeyId] = useState('all');
  const [status, setStatus] = useState('all');
  const [days, setDays] = useState(DEFAULT_DAYS);
  /** 文本筛选的**输入值**：绑在输入框上，逐字符更新（打字本身不能卡） */
  const [model, setModel] = useState('');
  const [ip, setIp] = useState('');

  /** 筛选一变就回第 1 页：停在第 5 页上看新筛选的结果，多半是一张空表 */
  const backToFirstPage = useCallback(() => setPage(1), []);

  /**
   * 文本筛选的**落定值**（防抖 400ms）。取数与依赖数组都用它，**不用输入值**——
   * 否则输入 `glm` 会连发 3 次请求（`g` / `gl` / `glm`）。
   *
   * 页码复位挂在 `onSettle` 上，而不是另写一个 `useEffect(() => setPage(1), [modelQ])`：
   * 落定值与页码必须落在**同一次更新**里，否则会先按「新筛选 + 旧页码」查一次、
   * 再回第 1 页查第二次。理由见 lib/use-debounce.ts 的注释。
   */
  const [modelQ, applyModel] = useDebounced(model, 400, backToFirstPage);
  const [ipQ, applyIp] = useDebounced(ip, 400, backToFirstPage);

  /**
   * 日志列表。翻页与筛选的要求正好相反，依赖因此分两组（判据与理由都在
   * lib/async-state.ts 的 `depMode` 里）：
   *
   * 全部筛选项进 `refreshDeps`（**保留现有内容静默重取**）：翻页、筛选都是高频
   * 操作，若清屏，每翻一页都会闪一次骨架，看起来像页面在抽搐。旧内容在数据
   * 到来前仍然是对的，只是「马上要换一批」。
   */
  const {
    values,
    errors: logErrors,
    isInitialLoading,
    isInitialFailed,
    isRefreshing,
    reload: reloadLogs,
  } = useAsyncAll(
    {
      logs: () =>
        logsApi.list({
          page,
          size: PAGE_SIZE,
          key_id: keyId === 'all' ? undefined : keyId,
          model: modelQ || undefined,
          status: status === 'all' ? undefined : status,
          ip: ipQ || undefined,
          days: Number(days) || undefined,
        }),
    },
    [],
    [page, days, keyId, status, modelQ, ipQ],
  );

  /**
   * 筛选下拉用的密钥列表，**单独一个 hook**。
   *
   * 它只喂那个下拉，既不随翻页也不随天数变化；若并进上面那组，每次翻页都会
   * 白打一个请求。它的失败按「部分失败」处理——下拉取不到，不该把整页日志顶掉。
   */
  const {
    values: keyValues,
    errors: keyErrors,
    reload: reloadKeys,
  } = useAsyncAll({keys: () => keysApi.list()}, []);

  const logs: RequestLog[] = values.logs?.items ?? EMPTY_LOGS;
  const total = values.logs?.total ?? 0;
  const keys: ApiKey[] = keyValues.keys ?? EMPTY_KEYS;

  const failedCount = Object.keys(logErrors).length + Object.keys(keyErrors).length;

  /** 重试：两组一起重来，避免「点了重试、密钥下拉还是空的」 */
  const retry = useCallback(() => {
    reloadLogs();
    reloadKeys();
  }, [reloadLogs, reloadKeys]);

  // 新请求会不断写入日志；心跳刷新只更新当前筛选下的列表，不会重置筛选条件
  useHeartbeat(reloadLogs, 60000);

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  /**
   * 回到第 1 页并重取。
   *
   * 给「清空日志」用：清完之后要看的是**第 1 页**，而不是刚才停留的那一页。
   * 页码本来就在第 1 页时 `setPage(1)` 是空操作、依赖没变、不会触发重取，
   * 所以得显式补一次 `reloadLogs()`；页码真的变了就交给依赖去重取，否则会连打两次。
   */
  function refetchFromFirstPage() {
    if (page === 1) reloadLogs();
    else setPage(1);
  }

  /** 有没有筛选项不是默认值（决定「重置」按钮能不能点） */
  const isFiltered =
    keyId !== 'all' || status !== 'all' || days !== DEFAULT_DAYS || model !== '' || ip !== '';

  /**
   * 一键重置全部筛选。
   *
   * 文本类必须走 `applyXxx('')`（立即落定）而不是只 `setModel('')`：后者要等 400ms
   * 防抖走完才真正生效，中间会先按「旧的文本筛选 + 新的下拉筛选」查一次，列表闪两下。
   * 这里所有 setState 都在同一个事件处理器里，React 合批成一次渲染 → 只重取一次。
   */
  function resetFilters() {
    setKeyId('all');
    setStatus('all');
    setDays(DEFAULT_DAYS);
    setModel('');
    setIp('');
    applyModel('');
    applyIp('');
    setPage(1);
  }

  const header = (
    <PageHeader
      title={t('logs.title')}
      description={t('logs.description')}
      actions={
        <>
          {isAdmin && (
            <ConfirmDialog
              title={t('logs.clearTitle')}
              description={t('logs.clearDesc')}
              confirmText={t('logs.clear')}
              destructive
              onConfirm={async () => {
                try {
                  await logsApi.clear();
                  notify.ok(t('logs.cleared'));
                } catch (e) {
                  notify.err(errText(e));
                  return;
                }
                refetchFromFirstPage();
              }}
              trigger={
                <Button variant="outline" size="sm" className="rounded-full text-red-500">
                  <Trash2 />
                  {t('logs.clear')}
                </Button>
              }
            />
          )}
        </>
      }
    />
  );

  /**
   * 首屏还没拿到日志：先给骨架，别先渲染「第 1 页 / 共 1 页」和「暂无日志」——
   * 那两句话在数据还在路上时都是错的。整页错误态同理：一份都没取到时，
   * 让用户看到「数据加载失败 + 重试」，而不是一张空表和一句「暂无日志」。
   */
  if (isInitialFailed || isInitialLoading) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        {isInitialFailed ? (
          <LoadError variant="page" onRetry={retry} />
        ) : (
          <LogsSkeleton />
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6" aria-busy={isRefreshing}>
      {header}

      {/* 部分失败：已经显示出来的日志仍然是对的，只是可能不是最新的。
          用一条常驻提示说明，**不把表格顶掉**；下一次刷新成功后自动消失。 */}
      {failedCount > 0 && <LoadError message={t('state.loadFailed')} onRetry={retry} />}

      <section className="rounded-[20px] bg-muted p-4">
        <div className="grid grid-cols-2 items-end gap-3 md:grid-cols-6">
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('logs.filterRange')}</Label>
            <Select value={days} onValueChange={(v) => { setDays(v); setPage(1); }}>
              <SelectTrigger className="bg-background" aria-label={t('logs.filterRange')}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="1">{t('logs.last24h')}</SelectItem>
                <SelectItem value="7">{t('logs.last7')}</SelectItem>
                <SelectItem value="30">{t('logs.last30')}</SelectItem>
                <SelectItem value="90">{t('logs.last90')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('logs.filterKey')}</Label>
            <Select value={keyId} onValueChange={(v) => { setKeyId(v); setPage(1); }}>
              <SelectTrigger className="bg-background" aria-label={t('logs.filterKey')}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t('logs.allKeys')}</SelectItem>
                {keys.map((k) => (
                  <SelectItem key={k.id} value={String(k.id)}>{k.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('logs.filterStatus')}</Label>
            <Select value={status} onValueChange={(v) => { setStatus(v); setPage(1); }}>
              <SelectTrigger className="bg-background" aria-label={t('logs.filterStatus')}><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t('logs.statusAll')}</SelectItem>
                <SelectItem value="ok">{t('logs.statusOk')}</SelectItem>
                <SelectItem value="error">{t('logs.statusError')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('logs.filterModel')}</Label>
            {/* 文本类防抖 400ms 后自动生效，没有「查询」按钮要按 */}
            <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder={t('logs.filterModelPlaceholder')} className="bg-background" />
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('logs.filterIp')}</Label>
            <Input value={ip} onChange={(e) => setIp(e.target.value)} placeholder={t('logs.filterIpPlaceholder')} className="bg-background" />
          </div>
          {/* 六个筛选项都是改完即生效，不需要「查询」按钮；「重置」才是有用的那个
              动作。没有筛选时置灰而不是隐藏：隐藏会让这一格塌掉、整排控件左右跳。 */}
          <Button
            variant="outline"
            className="rounded-full"
            disabled={!isFiltered}
            onClick={resetFilters}
          >
            <RotateCcw />
            {t('logs.reset')}
          </Button>
        </div>
      </section>

      <section className="overflow-hidden rounded-[20px] bg-muted">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('logs.colTime')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('logs.colKey')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">IP</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('logs.colModel')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('logs.filterStatus')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">Token</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('logs.colFirstToken')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('logs.colLatency')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('logs.colAccount')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('logs.colEffort')}</TableHead>
              <TableHead className="pr-4 text-[11px] text-muted-foreground">{t('logs.colChannel')}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {logs.map((l) => (
              <TableRow
                key={l.id}
                className="cursor-pointer border-b border-border/40"
                onClick={() => setDetail(l)}
              >
                <TableCell className="pl-4 text-xs text-muted-foreground">{fmtDateTimeMarked(l.ts)}</TableCell>
                <TableCell className="text-xs">{l.key_name || '—'}</TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">{l.ip || '—'}</TableCell>
                <TableCell className="text-xs">
                  {l.model || '—'}
                  {l.mapped_model && l.mapped_model !== l.model && (
                    <span className="ml-1 text-[11px] text-muted-foreground">→{l.mapped_model}</span>
                  )}
                </TableCell>
                <TableCell>
                  {l.status >= 200 && l.status < 300 ? (
                    <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">{l.status}</Badge>
                  ) : l.status >= 400 && l.status < 500 ? (
                    <Badge variant="secondary" className="rounded-full bg-amber-500/12 text-amber-600 dark:text-amber-400">
                      {l.status || 'ERR'}
                    </Badge>
                  ) : (
                    <Badge variant="destructive" className="rounded-full">{l.status || 'ERR'}</Badge>
                  )}
                </TableCell>
                <TableCell className="text-xs tabular-nums">
                  {l.prompt_tokens + l.completion_tokens > 0 ? (
                    fmtNumber(l.prompt_tokens + l.completion_tokens)
                  ) : (
                    <span className="text-muted-foreground/70">—</span>
                  )}
                  {/* 流式标记：一眼分清「慢在首字」还是「本来就不流式」 */}
                  {l.stream && <span className="ml-1 text-[11px] text-muted-foreground">{t('logs.streamShort')}</span>}
                </TableCell>
                {/* 首字延迟：反映「上游多久开始回话」。回答越长总耗时越大，
                    所以判断上游快慢只看这一列。非流式请求没有中间过程，显示 —。 */}
                <TableCell
                  className={
                    'text-xs tabular-nums ' +
                    (l.first_token_ms != null && l.first_token_ms >= 3000
                      ? 'font-medium text-amber-600 dark:text-amber-400'
                      : 'text-muted-foreground')
                  }
                >
                  {l.first_token_ms != null ? (
                    fmtLatency(l.first_token_ms)
                  ) : (
                    <span className="text-muted-foreground/50">—</span>
                  )}
                </TableCell>
                <TableCell className="text-xs tabular-nums text-muted-foreground">
                  {fmtLatency(l.latency_ms)}
                </TableCell>
                {/* 本次用的上游账号。账号由上游选、不在响应里回传，是按时间对账
                    回填的——比请求晚几秒，所以刚打完的请求可能还是「—」。 */}
                <TableCell className="text-xs">{l.account || '—'}</TableCell>
                {/*
                  思考强度：客户端显式传的 reasoning_effort（low/medium/high…）。
                  没传就显示「—」而不是猜默认档——同一模型不同版本的默认档可能不同，
                  写死会把「客户端没传」误读成「客户端要了某个档位」。
                */}
                <TableCell className="text-xs">
                  {l.effort ? (
                    <Badge variant="secondary" className="rounded-full font-mono text-[10px]">
                      {l.effort}
                    </Badge>
                  ) : (
                    <span className="text-muted-foreground/50">—</span>
                  )}
                </TableCell>
                {/* 客户端渠道（按 UA 归类），用来定位「哪类客户端在把账号打进 429」。
                    旧记录没有这一列，显示「—」而不是猜一个。 */}
                <TableCell className="pr-4 font-mono text-[11px] text-muted-foreground"
                           title={l.channel ? `User-Agent: ${l.ua || ''}` : undefined}>
                  {l.channel || '—'}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>

        {/* 走到这里说明首屏已经就绪——还在取数时上面已经整块返回骨架了。
            所以「没有日志」是真的没有，不需要再拿「请求在不在飞」去兜一层。 */}
        {!logs.length && (
          <EmptyState
            icon={ScrollText}
            title={t('logs.emptyTitle')}
            description={t('logs.emptyDesc')}
            className="flex flex-col items-center justify-center py-16 text-center"
          />
        )}

        <div className="flex items-center justify-between px-4 py-3">
          <div className="text-[11px] text-muted-foreground">
            {t('logs.pageInfo', {total: fmtNumber(total), page: String(page), pages: String(pages)})}
          </div>
          <div className="flex gap-1">
            <Button
              variant="outline"
              size="icon"
              className="h-7 w-7 rounded-md"
              aria-label={t('logs.previousPage')}
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </Button>
            <Button
              variant="outline"
              size="icon"
              className="h-7 w-7 rounded-md"
              aria-label={t('logs.nextPage')}
              disabled={page >= pages}
              onClick={() => setPage((p) => Math.min(pages, p + 1))}
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      </section>

      {/* 行详情：点行展开。放 Drawer 而不是弹窗——字段多、常需要对着表格对照看。 */}
      <Drawer open={!!detail} onOpenChange={(v) => !v && setDetail(null)}>
        <DrawerContent>
          <DrawerHeader>
            <DrawerTitle>{t('logs.detailTitle', {id: String(detail?.id ?? '')})}</DrawerTitle>
            <DrawerDescription>{detail ? fmtDateTime(detail.ts) : ''}</DrawerDescription>
          </DrawerHeader>
          {detail && (
            <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 pb-8 text-xs">
              {([
                ['ip', t('logs.rowIp'), detail.ip || '—'],
                ['key', t('logs.rowKey'), detail.key_name || '—'],
                // 账号来自按时间对账回填，可能比请求本身晚几秒；这里只能说
                // 「尚未采集」，不能写死原因——那是在猜。
                ['account', t('logs.rowAccount'), detail.account || t('logs.accountPending')],
                ['model', t('logs.rowModel'), detail.model || '—'],
                ['mapped', t('logs.rowMappedModel'), detail.mapped_model || '—'],
                ['status', t('logs.rowStatus'), String(detail.status)],
                [
                  'firstToken',
                  t('logs.rowFirstToken'),
                  detail.first_token_ms != null
                    ? fmtLatency(detail.first_token_ms)
                    : t('logs.notCollected'),
                ],
                ['latency', t('logs.rowLatency'), fmtLatency(detail.latency_ms)],
                ['promptTokens', t('logs.rowPromptTokens'), fmtNumber(detail.prompt_tokens)],
                ['completionTokens', t('logs.rowCompletionTokens'), fmtNumber(detail.completion_tokens)],
                ['stream', t('logs.rowStream'), detail.stream ? t('logs.yes') : t('logs.no')],
                ['ua', 'User-Agent', detail.ua || '—'],
                ['error', t('logs.rowError'), detail.error || '—'],
              ] as [string, string, string][]).map(([id, k, v]) => {
                // 这些字段内容较长且常需要贴出来（排查 / 反馈），给出复制入口
                const copyable = ['ip', 'ua', 'error'].includes(id) && v !== '—';
                return (
                  <div key={id} className="flex items-start gap-3">
                    <div className="w-32 shrink-0 text-muted-foreground">{k}</div>
                    <div className="min-w-0 flex-1 break-all font-mono">{v}</div>
                    {copyable && <CopyButton value={v} title={t('logs.copyField', {field: k})} className="-mt-1" />}
                  </div>
                );
              })}
            </div>
          )}
        </DrawerContent>
      </Drawer>
    </div>
  );
}

/**
 * 首屏骨架：筛选区 + 表格两块，结构与真实内容逐块对应，数据到位时版面不跳。
 */
function LogsSkeleton() {
  return (
    <>
      <section className="rounded-[20px] bg-muted p-4">
        <div className="grid grid-cols-2 items-end gap-3 md:grid-cols-6">
          {Array.from({length: 6}, (_, i) => (
            <div key={i} className="space-y-1.5">
              <SkeletonBar className="h-3 w-14" />
              <SkeletonBar className="h-8 w-full rounded-lg" />
            </div>
          ))}
        </div>
      </section>
      <section className="overflow-hidden rounded-[20px] bg-muted p-4">
        <div className="space-y-3">
          {Array.from({length: 10}, (_, i) => (
            <SkeletonBar key={i} className="h-8 w-full rounded-lg" />
          ))}
        </div>
      </section>
    </>
  );
}
