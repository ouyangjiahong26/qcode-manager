'use client';

/**
 * 账号池页：分组 Tab + PAT 表。
 *
 * 数据流：useAsyncAll 并发取「当前范围的账号列表 + 全部分组清单」，30s 心跳
 * 刷新（refresh 模式不清屏）；增删改由各弹窗完成后调 reload，并广播
 * 'qcoder-manager:accounts-changed'（仪表盘等页面据此同步自己的计数）。
 *
 * 分组过滤的口径：后端 list 端点只支持「按具名上游过滤」，「默认分组」
 * （upstream_id 为空）没有服务端过滤参数——所以「全部 / 默认」两个 Tab 都拉
 * 全量列表，默认分组在前端本地过滤，其余 Tab 走 upstream_id 参数。
 */
import {useEffect, useMemo, useState} from 'react';
import {
  Download,
  KeyRound,
  Loader2,
  MessageSquare,
  Scale,
  Trash2,
  Waypoints,
} from 'lucide-react';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {PageSectionTabs} from '@/components/common/layout/PageSectionTabs';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Switch} from '@/components/ui/switch';
import {Tabs, TabsList, TabsTrigger} from '@/components/ui/tabs';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {useT} from '@/lib/i18n/provider';
import {useAuth} from '@/lib/auth-context';
import {useAsyncAll} from '@/lib/use-async-data';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {notify} from '@/lib/toast';
import {accountsApi, errText} from '@/lib/api/accounts';
import {upstreamsApi, type Upstream} from '@/lib/api/upstreams';
import type {Account, AccountStatus} from '@/lib/types/accounts';
import {fmtAgo, fmtCompact, fmtNumber} from '@/lib/format';
import {AddPatDialog} from '@/components/common/accounts/AddPatDialog';
import {TestAllButton} from '@/components/common/accounts/TestAllButton';
import {AccountNoteDialog} from '@/components/common/accounts/AccountNoteDialog';
import {AccountWeightDialog} from '@/components/common/accounts/AccountWeightDialog';
import {MovePatDialog} from '@/components/common/accounts/MovePatDialog';

/** Tab 值：all = 全部；default = 默认分组；其余为上游 id 字符串 */
type GroupTab = 'all' | 'default' | `up:${number}`;

/** 状态 → Badge 变体与文案键（静态小表，Record 足够） */
const STATUS_META: Record<AccountStatus, {cls: string; key: string}> = {
  active: {cls: 'bg-emerald-500/12 text-emerald-600 dark:text-emerald-400', key: 'accounts.statusActive'},
  invalid: {cls: 'bg-red-500/12 text-red-600 dark:text-red-400', key: 'accounts.statusInvalid'},
  unknown: {cls: 'bg-gray-500/12 text-gray-500 dark:text-gray-400', key: 'accounts.statusUnknown'},
};

export default function AccountsPage() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [tab, setTab] = useState<GroupTab>('all');
  const [addOpen, setAddOpen] = useState(false);
  const [noteAccount, setNoteAccount] = useState<Account | null>(null);
  const [weightAccount, setWeightAccount] = useState<Account | null>(null);
  const [moveAccount, setMoveAccount] = useState<Account | null>(null);
  /** 正在测活的账号 id（行内按钮转圈） */
  const [testingId, setTestingId] = useState<number | null>(null);

  // 具名分组走服务端过滤；「全部 / 默认」拉全量（默认分组本地过滤，见文件头）
  const upstreamFilter = tab.startsWith('up:') ? Number(tab.slice(3)) : undefined;

  const {values, errors, isInitialLoading, isInitialFailed, isRefreshing, reload} = useAsyncAll({
    list: () => accountsApi.list(upstreamFilter),
    upstreams: () => upstreamsApi.list(),
  }, [upstreamFilter]);

  useHeartbeat(reload, 30000);

  // 其他页面（如仪表盘）改了账号池时同步刷新本页
  useEffect(() => {
    const onChanged = () => void reload();
    window.addEventListener('qcoder-manager:accounts-changed', onChanged);
    return () => window.removeEventListener('qcoder-manager:accounts-changed', onChanged);
  }, [reload]);

  const upstreams: Upstream[] = values.upstreams ?? [];
  const allAccounts: Account[] = values.list?.accounts ?? [];
  const accounts = tab === 'default'
    ? allAccounts.filter((a) => a.upstream_id == null)
    : allAccounts;

  const summary = values.list?.summary;
  const groupName = useMemo(() => {
    const byId = new Map<number, string>(upstreams.filter((u) => u.id != null).map((u) => [u.id as number, u.name]));
    return (id: number | null) => (id == null ? t('accounts.groupDefault') : byId.get(id) ?? t('accounts.groupGone'));
  }, [upstreams, t]);

  async function toggleEnabled(account: Account, enabled: boolean) {
    try {
      await accountsApi.update(account.id, {enabled});
      notify.ok(enabled ? t('accounts.enabledOn', {name: account.label}) : t('accounts.enabledOff', {name: account.label}));
      void reload();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  async function testOne(account: Account) {
    if (testingId != null) return;
    setTestingId(account.id);
    try {
      const r = await accountsApi.test(account.id);
      (r.ok ? notify.ok : notify.err)(`${account.label}：${r.ok ? t('accounts.testPass') : t('accounts.testFail')}`, r.msg);
      void reload();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setTestingId(null);
    }
  }

  async function removeAccount(account: Account) {
    await accountsApi.remove(account.id);
    notify.ok(t('accounts.removed', {name: account.label}));
    window.dispatchEvent(new Event('qcoder-manager:accounts-changed'));
    void reload();
  }

  async function exportAll() {
    try {
      const data = await accountsApi.export();
      const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `qcoder-accounts-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      notify.ok(t('accounts.exportDone', {count: String(data.accounts.length)}));
    } catch (e) {
      notify.err(errText(e));
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title={t('accounts.title')}
        description={t('accounts.desc')}
        actions={
          <>
            {isAdmin && (
              <Button variant="outline" size="sm" onClick={exportAll}>
                <Download className="mr-1 h-3.5 w-3.5" />
                {t('accounts.export')}
              </Button>
            )}
            <TestAllButton
              upstreamId={upstreamFilter ?? null}
              label={upstreamFilter != null ? t('accounts.testGroup') : undefined}
              onDone={() => void reload()}
            />
            {isAdmin && (
              <Button size="sm" onClick={() => setAddOpen(true)}>
                <KeyRound className="mr-1 h-3.5 w-3.5" />
                {t('accounts.addTitle')}
              </Button>
            )}
          </>
        }
      />
      <PageSectionTabs />

      {isInitialLoading ? (
        <div className="space-y-3 rounded-[20px] bg-muted p-4">
          <SkeletonBar className="h-8 w-64" />
          <SkeletonBar className="h-40 w-full" />
        </div>
      ) : isInitialFailed ? (
        <LoadError variant="page" onRetry={reload} />
      ) : (
        <div className="space-y-3" aria-busy={isRefreshing}>
          {Object.keys(errors).length > 0 && <LoadError onRetry={reload} />}

          <Tabs value={tab} onValueChange={(v) => setTab(v as GroupTab)}>
            <TabsList className="h-8 w-full justify-start overflow-x-auto">
              <TabsTrigger value="all" className="text-xs">
                {t('accounts.tabAll')}
                {summary && tab === 'all' ? ` (${summary.total})` : ''}
              </TabsTrigger>
              <TabsTrigger value="default" className="text-xs">
                {t('accounts.groupDefault')}
              </TabsTrigger>
              {upstreams
                .filter((u) => u.id != null)
                .map((u) => (
                  <TabsTrigger key={u.id} value={`up:${u.id}` as GroupTab} className="text-xs">
                    {u.name} ({u.account_count})
                  </TabsTrigger>
                ))}
            </TabsList>
          </Tabs>

          {accounts.length === 0 ? (
            <EmptyState
              icon={KeyRound}
              title={t('accounts.emptyTitle')}
              description={t('accounts.emptyDesc')}
            >
              {isAdmin && (
                <Button size="sm" onClick={() => setAddOpen(true)}>{t('accounts.addTitle')}</Button>
              )}
            </EmptyState>
          ) : (
            <div className="overflow-x-auto rounded-[20px] bg-muted">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t('accounts.colAccount')}</TableHead>
                    <TableHead>{t('accounts.colGroup')}</TableHead>
                    <TableHead>{t('accounts.colStatus')}</TableHead>
                    <TableHead>{t('accounts.colLastCheck')}</TableHead>
                    <TableHead className="text-right">{t('accounts.colRequests')}</TableHead>
                    <TableHead className="text-right">{t('accounts.colTokens')}</TableHead>
                    <TableHead className="text-right">{t('accounts.colWeight')}</TableHead>
                    <TableHead>{t('accounts.colEnabled')}</TableHead>
                    <TableHead className="text-right">{t('accounts.colActions')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {accounts.map((a) => (
                    <TableRow key={a.id} className={a.enabled ? '' : 'opacity-60'}>
                      <TableCell>
                        <div className="font-medium">{a.label}</div>
                        <div className="text-[11px] tabular-nums text-muted-foreground">
                          ****{a.pat_suffix}
                          {a.note ? ` · ${a.note}` : ''}
                        </div>
                      </TableCell>
                      <TableCell className="text-xs">{groupName(a.upstream_id)}</TableCell>
                      <TableCell>
                        <Badge variant="secondary" className={STATUS_META[a.status]?.cls ?? STATUS_META.unknown.cls}>
                          {t(STATUS_META[a.status]?.key ?? 'accounts.statusUnknown')}
                        </Badge>
                      </TableCell>
                      <TableCell className="max-w-[220px] text-xs">
                        <div className="tabular-nums">{fmtAgo(a.last_check_at)}</div>
                        {a.last_check_msg && (
                          <div className="truncate text-[11px] text-muted-foreground" title={a.last_check_msg}>
                            {a.last_check_msg}
                          </div>
                        )}
                      </TableCell>
                      <TableCell className="text-right text-xs tabular-nums">{fmtNumber(a.requests)}</TableCell>
                      <TableCell className="text-right text-xs tabular-nums">{fmtCompact(a.total_tokens)}</TableCell>
                      <TableCell className="text-right text-xs tabular-nums">{a.weight}</TableCell>
                      <TableCell>
                        {isAdmin ? (
                          <Switch
                            checked={!!a.enabled}
                            onCheckedChange={(v) => void toggleEnabled(a, v)}
                            aria-label={t('accounts.colEnabled')}
                          />
                        ) : (
                          <span className="text-xs">{a.enabled ? t('accounts.enabledYes') : t('accounts.enabledNo')}</span>
                        )}
                      </TableCell>
                      <TableCell className="text-right">
                        {isAdmin && (
                          <div className="flex items-center justify-end gap-1">
                            <Button
                              variant="ghost" size="sm" className="h-7 px-2 text-[11px]"
                              disabled={testingId != null}
                              onClick={() => void testOne(a)}
                            >
                              {testingId === a.id
                                ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                                : t('accounts.actionTest')}
                            </Button>
                            <Button
                              variant="ghost" size="sm" className="h-7 px-2 text-[11px]"
                              onClick={() => setNoteAccount(a)}
                              title={t('accounts.noteTitle')}
                            >
                              <MessageSquare className="h-3.5 w-3.5" />
                            </Button>
                            <Button
                              variant="ghost" size="sm" className="h-7 px-2 text-[11px]"
                              onClick={() => setWeightAccount(a)}
                              title={t('accounts.weightTitle')}
                            >
                              <Scale className="h-3.5 w-3.5" />
                            </Button>
                            <Button
                              variant="ghost" size="sm" className="h-7 px-2 text-[11px]"
                              onClick={() => setMoveAccount(a)}
                              title={t('accounts.moveTitle', {name: a.label})}
                            >
                              <Waypoints className="h-3.5 w-3.5" />
                            </Button>
                            <ConfirmDialog
                              trigger={
                                <Button
                                  variant="ghost" size="sm"
                                  className="h-7 px-2 text-[11px] text-destructive hover:text-destructive"
                                >
                                  <Trash2 className="h-3.5 w-3.5" />
                                </Button>
                              }
                              title={t('accounts.removeTitle', {name: a.label})}
                              description={t('accounts.removeDesc')}
                              confirmText={t('accounts.removeConfirm')}
                              destructive
                              onConfirm={() => removeAccount(a)}
                            />
                          </div>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </div>
      )}

      <AddPatDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        upstreams={upstreams}
        defaultUpstreamId={upstreamFilter ?? null}
        onSuccess={() => void reload()}
      />
      <AccountNoteDialog
        account={noteAccount}
        open={noteAccount != null}
        onOpenChange={(v) => { if (!v) setNoteAccount(null); }}
        onSaved={() => void reload()}
      />
      <AccountWeightDialog
        account={weightAccount}
        open={weightAccount != null}
        onOpenChange={(v) => { if (!v) setWeightAccount(null); }}
        onSaved={() => void reload()}
      />
      <MovePatDialog
        account={moveAccount}
        upstreams={upstreams}
        open={moveAccount != null}
        onOpenChange={(v) => { if (!v) setMoveAccount(null); }}
        onMoved={() => {
          window.dispatchEvent(new Event('qcoder-manager:accounts-changed'));
          void reload();
        }}
      />
    </div>
  );
}
