'use client';

import {useEffect, useRef, useState} from 'react';
import {KeyRound, Plus, Trash2, Pencil, RotateCcw} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {useAsyncAll} from '@/lib/use-async-data';
import {notify} from '@/lib/toast';
import {keysApi} from '@/lib/api/keys';
import {errText} from '@/lib/api/http';
import type {ApiKey, KeyCreated, UpstreamOption, KeyCreateInput, KeyPatchInput} from '@/lib/types/keys';
import {fmtDateTime, fmtNumber} from '@/lib/format';
import {BASE_PATH} from '@/lib/base-path';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {PageSectionTabs} from '@/components/common/layout/PageSectionTabs';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {ModelWhitelistInput} from '@/components/common/keys/ModelWhitelistInput';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {Button} from '@/components/ui/button';
import {CopyButton} from '@/components/ui/copy-button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {Label} from '@/components/ui/label';
import {Textarea} from '@/components/ui/textarea';
import {Switch} from '@/components/ui/switch';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

interface FormState {
  name: string;
  /** 新建：多少天后过期（0 = 永不过期）；编辑：由 expiryMode 决定是否使用 */
  expiresDays: string;
  /** 编辑专用：保持不变 / 从今天起 N 天 / 改为永不过期 */
  expiryMode: 'keep' | 'days' | 'never';
  maxIps: string;
  ipAllowlist: string;
  models: string;
  quota: string;
  upstream_id: number | null;
}

const emptyForm: FormState = {
  name: '',
  expiresDays: '0',
  expiryMode: 'keep',
  maxIps: '0',
  ipAllowlist: '',
  models: '',
  quota: '0',
  upstream_id: null,
};

/** 逗号/换行分隔的文本 → 去空白后的字符串数组（IP 白名单与模型白名单共用） */
function toLines(v: string): string[] {
  return v.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
}

/**
 * 取数完成前的空值。必须是模块级同一份：写成 `values.keys ?? []` 的话每次渲染都会
 * 新建数组，进下游的依赖后每帧都变。
 */
const EMPTY_KEYS: ApiKey[] = [];
const EMPTY_UPSTREAMS: UpstreamOption[] = [];

export default function KeysPage() {
  const t = useT();
  const {isAdmin} = useAuth();
  /**
   * 密钥列表 + 上游列表。**两份数据放同一个 hook，不拆。**
   *
   * 上游列表是弹窗里「绑定上游」下拉的全部内容。分开取会出现「弹窗已经打开、
   * 下拉里却只有『默认上游』」——用户会以为没有别的上游可绑，于是把密钥绑到
   * 默认池上。那是**静默的错误选择**，比「两块一起挂掉」更糟。所以宁可同生共死：
   * 一份没取到 → 整页错误态 + 重试，而不是渲染一个缺选项的表单。
   */
  const {values, errors, isInitialLoading, isInitialFailed, isRefreshing, reload} = useAsyncAll({
    keys: () => keysApi.list(),
    upstreams: () => keysApi.listUpstreams(),
  }, []);

  const keys: ApiKey[] = values.keys ?? EMPTY_KEYS;
  const upstreams: UpstreamOption[] = values.upstreams ?? EMPTY_UPSTREAMS;
  /**
   * 密钥列表**这一份**没取到。用来把「没有密钥」与「不知道有没有密钥」分开——
   * 两者都渲染成同一片空白，含义却正好相反（见下面空状态处的说明）。
   */
  const keysFailed = 'keys' in errors;
  /** 有字段没刷新成功：常驻提示，但**不**顶掉已经显示出来的内容 */
  const partialFailed = Object.keys(errors).length > 0;

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<ApiKey | null>(null);
  const [form, setForm] = useState<FormState>(emptyForm);
  const [busy, setBusy] = useState(false);
  /** 创建成功后一次性展示的明文（qk_ 前缀）；关掉弹窗就再也拿不回来 */
  const [issued, setIssued] = useState<KeyCreated | null>(null);
  /**
   * 模型白名单里**匹配不到已知模型**的名字。
   *
   * 为什么要有：白名单是自由文本，填错不会报错，只会在下游表现为「模型列表是
   * 空的」——而空列表看不出原因（少个连字符？填成了显示名？）。所以在编辑处
   * 当场点出来。判据由后端给（与调用侧同一份），这里只负责显示。
   *
   * `null` = 还没查/查不了（拿不到模型清单）。**不能**把 null 当成「都没问题」——
   * 那会把「暂时查不了」显示成「全部正确」。
   */
  const [unknownModels, setUnknownModels] = useState<string[] | null>(null);
  /**
   * 提示元素的 ref：它出现时要**滚进可视区**。模型白名单是弹窗里最后一个字段，
   * 提示挂在它下面——常见窗口高度下会落在弹窗内容区的可视区之下（被滚动容器
   * 裁掉），用户看不到任何东西。用 `block:'nearest'`：只在需要时滚最小距离。
   */
  const unknownRef = useRef<HTMLParagraphElement | null>(null);
  /**
   * 提交锁（同步生效，与 `busy` 的渲染状态无关）。
   *
   * `setBusy(true)` 要等下一次渲染才反映到按钮的 disabled 上，而快速连点/回车
   * 触发的第二次提交可能在那之前就进来了 —— 结果创建出多把同名密钥。用 ref
   * 记录「已提交」，在当前这次调用栈里立即生效，不依赖渲染。失败时在 `finally`
   * 里释放，不能因为一次报错就把表单永久锁死。
   */
  const submitting = useRef(false);

  // 提示出现时确保它在可视区内（见 unknownRef 的说明）
  useEffect(() => {
    if (unknownModels && unknownModels.length) {
      unknownRef.current?.scrollIntoView({block: 'nearest'});
    }
  }, [unknownModels]);

  // 密钥状态可能被下游调用改变（配额用尽、过期），心跳刷新保持同步。
  // 走 reload（刷新模式）：只把新数据换上去，**不**重走首屏流程——否则骨架会每
  // 60 秒闪一次。
  useHeartbeat(reload, 60000);

  /** 列表里显示上游名；找不到（刚被删）时退回 #id —— 不显示空白。 */
  function upstreamName(id: number): string {
    const hit = upstreams.find((u) => u.id === id);
    return hit ? hit.name : `#${id}`;
  }

  function openCreate() {
    setEditing(null);
    setForm({...emptyForm});
    setUnknownModels(null);
    setFormOpen(true);
  }

  function openEdit(k: ApiKey) {
    setEditing(k);
    setForm({
      name: k.name ?? '',
      // 默认不改动有效期；要续期/取消过期需显式选择
      expiryMode: 'keep',
      expiresDays: '0',
      maxIps: String(k.max_ips || 0),
      ipAllowlist: (k.ip_allowlist || []).join('\n'),
      models: (k.models || []).join(', '),
      quota: String(k.quota ?? 0),
      upstream_id: k.upstream_id ?? null,
    });
    setUnknownModels(null);
    setFormOpen(true);
  }

  async function submit() {
    // 同步锁：必须在任何 await / setState 之前判断并置位。
    if (submitting.current) return;
    if (!form.name.trim()) {
      notify.err(t('keys.nameRequired'));
      return;
    }
    submitting.current = true;
    setBusy(true);
    try {
      const days = Number(form.expiresDays) || 0;
      const payload: KeyCreateInput & KeyPatchInput = {
        name: form.name.trim(),
        max_ips: Number(form.maxIps) || 0,
        ip_allowlist: toLines(form.ipAllowlist),
        models: toLines(form.models),
        quota: Number(form.quota) || 0,
        // 绑定上游（多上游隔离）：null = 默认上游，照传——后端以它区分
        // 「显式改回默认上游」与「本次没提交该字段」（PATCH 语义）。
        upstream_id: form.upstream_id,
      };

      // 新建：填了天数才设过期（0 = 永不过期，不下发 expires_at）
      // 编辑：按显式选择处理，避免「打开就保存」把有效期重置
      if (!editing) {
        if (days > 0) payload.expires_at = Math.floor(Date.now() / 1000) + days * 86400;
      } else if (form.expiryMode === 'days') {
        if (days <= 0) {
          notify.err(t('keys.daysRequired'));
          return;
        }
        payload.expires_at = Math.floor(Date.now() / 1000) + days * 86400;
      } else if (form.expiryMode === 'never') {
        // 后端以 null 表示「无过期时间」
        payload.expires_at = null;
      }

      if (editing) {
        await keysApi.update(editing.id, payload);
        notify.ok(t('keys.keyUpdated'));
      } else {
        const created = await keysApi.create(payload);
        notify.ok(t('keys.keyCreated'));
        if (created.key) setIssued(created);
      }
      setFormOpen(false);
      // 刷新失败**不能**把这次创建判成失败：密钥已经建好了。
      // reload() 的返回值判断「建好了但列表没刷上」，那绝不能报成「创建失败」
      // ——用户会以为没建成功、再点一次，于是建出重复密钥。
      if (!(await reload())) {
        notify.warn(t('keys.createdButRefreshFailed'), t('keys.createdButRefreshFailedHint'));
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
      submitting.current = false;
    }
  }

  async function toggle(k: ApiKey) {
    try {
      await keysApi.update(k.id, {enabled: !k.enabled});
      notify.ok(k.enabled ? t('keys.disabled') : t('keys.enabled'));
      reload();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  // 下游接入地址：客户端才能拿到当前 origin，静态导出阶段为空。
  // 子路径部署时网关也挂在前缀下（反代剥掉前缀再转发），所以要带上 basePath。
  const baseUrl =
    typeof window !== 'undefined' ? `${window.location.origin}${BASE_PATH}` : '';

  const header = (
    <>
      <PageHeader
        title={t('keys.title')}
        description={t('keys.description')}
        actions={
          <>
            {isAdmin && (
              <Button size="sm" className="rounded-full" onClick={openCreate}>
                <Plus />
                {t('keys.newKey')}
              </Button>
            )}
          </>
        }
      />
      {/* 二级导航放在 `header` 里而不是两个 return 分支各写一遍：下面的首屏守卫
          是**早返回**，漏一处就会出现「取不到密钥时 Tab 不见了」。 */}
      <PageSectionTabs />
    </>
  );

  // 首屏：一次都没取到。
  //
  // 为什么必须在这里拦住、而不是让下面的空状态兜住：**取不到密钥 ≠ 没有密钥。**
  // 密钥是凭据，用户看到「暂无密钥」的反应是「我的密钥被删了 / 我没建过」，而事实
  // 是**不知道有没有**；更糟的是他会顺着那个按钮重建——于是建出重复密钥。
  if (isInitialFailed || isInitialLoading) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        {header}
        {isInitialFailed ? <LoadError variant="page" onRetry={reload} /> : <KeysSkeleton />}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6" aria-busy={isRefreshing}>
      {header}

      {/* 刷新时有请求失败：常驻提示，**不**顶掉已经显示出来的内容——那些数据仍然
          是对的，只是可能不是最新的。 */}
      {/* 上游清单没取到：新建密钥的「绑定上游」只剩默认项，静默建出来会绑错池。
          单独说清（比笼统的「部分失败」更能指导下一步）。 */}
      {'upstreams' in errors && (
        <LoadError message={t('keys.upstreamsFailed')} onRetry={reload} />
      )}
      {partialFailed && (
        <LoadError message={t('state.loadFailed')} onRetry={reload} />
      )}

      <section className="overflow-hidden rounded-[20px] bg-muted">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('keys.colName')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.colPrefix')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.colStatus')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.colExpiry')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.colQuota')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.colIps')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.colModels')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('keys.colLastUsed')}</TableHead>
              {isAdmin && <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">{t('keys.colActions')}</TableHead>}
            </TableRow>
          </TableHeader>
          <TableBody>
            {keys.map((k) => {
              const expired = !!k.expires_at && k.expires_at * 1000 < Date.now();
              // 额度超限要在界面上与网关的拒绝口径**一致**，否则会出现
              // 「列表显示正常、调用却被 429」，用户会以为是网关坏了。
              const overQuota = !!k.quota && k.used_tokens >= k.quota;
              return (
                <TableRow key={k.id} className="border-b border-border/40">
                  <TableCell className="pl-4 text-sm font-medium">
                    <div>{k.name}</div>
                    {/* 只标「绑定了上游」的行：默认上游是绝大多数，每行都标等于
                        没标，有值才说明这把钥匙走的是另一个账号池 */}
                    {k.upstream_id ? (
                      <div className="mt-0.5">
                        <Badge variant="secondary" className="rounded-full text-[10px]">
                          {t('keys.upstreamTag', {name: upstreamName(k.upstream_id)})}
                        </Badge>
                      </div>
                    ) : null}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-muted-foreground">{k.prefix}…</TableCell>
                  <TableCell>
                    {!k.enabled ? (
                      <Badge variant="secondary" className="rounded-full text-muted-foreground">{t('keys.badgeDisabled')}</Badge>
                    ) : expired ? (
                      <Badge variant="destructive" className="rounded-full">{t('keys.badgeExpired')}</Badge>
                    ) : overQuota ? (
                      <Badge variant="destructive" className="rounded-full">{t('keys.badgeOverQuota')}</Badge>
                    ) : (
                      <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">{t('keys.badgeOk')}</Badge>
                    )}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {k.expires_at ? fmtDateTime(k.expires_at) : t('keys.neverExpires')}
                  </TableCell>
                  <TableCell className="text-xs tabular-nums">
                    {(() => {
                      // 用量按额度比例着色：接近或超限时一眼能看出
                      const ratio = k.quota ? k.used_tokens / k.quota : 0;
                      const tone = !k.quota
                        ? 'text-muted-foreground'
                        : ratio >= 1
                          ? 'text-red-600 dark:text-red-400 font-medium'
                          : ratio >= 0.8
                            ? 'text-amber-600 dark:text-amber-400 font-medium'
                            : 'text-foreground';
                      return (
                        <span className={tone}>
                          {fmtNumber(k.used_tokens)}
                          {k.quota ? ` / ${fmtNumber(k.quota)}` : ''}
                        </span>
                      );
                    })()}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {k.max_ips ? t('keys.ipLimit', {n: k.max_ips}) : t('keys.ipUnlimited')}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {k.models?.length ? t('keys.modelsCount', {n: k.models.length}) : t('keys.modelsAll')}
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {k.last_used_at ? (
                      fmtDateTime(k.last_used_at)
                    ) : (
                      <span className="text-muted-foreground/70">{t('keys.neverUsed')}</span>
                    )}
                  </TableCell>
                  {isAdmin && (
                    <TableCell className="pr-4">
                      <div className="flex items-center justify-end gap-1">
                        <Switch
                          checked={k.enabled}
                          onCheckedChange={() => toggle(k)}
                          aria-label={k.enabled ? t('keys.disable') : t('keys.enable')}
                        />
                        <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md" title={t('keys.edit')} onClick={() => openEdit(k)}>
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <ConfirmDialog
                          title={t('keys.resetUsageTitle')}
                          description={t('keys.resetUsageDesc', {name: k.name ?? ''})}
                          onConfirm={async () => {
                            await keysApi.resetUsage(k.id);
                            notify.ok(t('keys.resetDone'));
                            reload();
                          }}
                          trigger={
                            <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md" title={t('keys.resetUsage')}>
                              <RotateCcw className="h-3.5 w-3.5" />
                            </Button>
                          }
                        />
                        {/* 与账号页删除弹窗同一处理：密钥名可能很长，放 18px 标题里
                            一折行就挤乱，改放说明里，标题固定一行。 */}
                        <ConfirmDialog
                          title={t('keys.deleteTitle')}
                          description={t('keys.deleteDesc', {name: k.name ?? ''})}
                          contentClassName="max-w-[min(380px,calc(100%-2rem))] sm:max-w-[380px]"
                          confirmText={t('keys.delete')}
                          destructive
                          onConfirm={async () => {
                            await keysApi.remove(k.id);
                            notify.ok(t('keys.deleted'));
                            reload();
                          }}
                          trigger={
                            <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md text-red-500 hover:text-red-600" title={t('keys.delete')}>
                              <Trash2 className="h-3.5 w-3.5" />
                            </Button>
                          }
                        />
                      </div>
                    </TableCell>
                  )}
                </TableRow>
              );
            })}
          </TableBody>
        </Table>

        {/* 「暂无密钥」是**一条都没有**的断言，取不到时不能说这句（见上面首屏守卫
            的说明）。`keysFailed` 覆盖「另一份取到了、这一份没有」的部分失败。 */}
        {!keys.length && !keysFailed && (
          <EmptyState
            icon={KeyRound}
            title={t('keys.emptyTitle')}
            description={t('keys.emptyDesc')}
            className="flex flex-col items-center justify-center py-16 text-center"
          >
            {isAdmin && (
              <Button className="mt-4 rounded-full" onClick={openCreate}>
                <Plus />
                {t('keys.newKey')}
              </Button>
            )}
          </EmptyState>
        )}
      </section>

      {/* 新建 / 编辑 */}
      <Dialog open={formOpen} onOpenChange={setFormOpen}>
        <DialogContent className="max-w-[520px]">
          <DialogHeader>
            <DialogTitle>{editing ? t('keys.editTitle') : t('keys.createTitle')}</DialogTitle>
            <DialogDescription>
              {editing ? t('keys.editDesc') : t('keys.createDesc')}
            </DialogDescription>
          </DialogHeader>
          <DialogBody className="max-h-[min(70vh,560px)]">
            <div className="space-y-4 px-6 pb-2">
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('keys.name')}</Label>
                <Input value={form.name} onChange={(e) => setForm({...form, name: e.target.value})} placeholder={t('keys.namePlaceholder')} />
              </div>
              {!editing ? (
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">{t('keys.expiresInDays')}</Label>
                  <Input
                    type="number"
                    min={0}
                    value={form.expiresDays}
                    onChange={(e) => setForm({...form, expiresDays: e.target.value})}
                  />
                  <p className="text-[11px] leading-4 text-muted-foreground">
                    {t('keys.expiresHint')}
                  </p>
                </div>
              ) : (
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">
                    {t('keys.expiry')}
                    {editing.expires_at
                      ? t('keys.expiryCurrent', {at: fmtDateTime(editing.expires_at)})
                      : t('keys.expiryNever')}
                  </Label>
                  <div className="flex items-center gap-2">
                    <Select
                      value={form.expiryMode}
                      onValueChange={(v) => setForm({...form, expiryMode: v as FormState['expiryMode']})}
                    >
                      <SelectTrigger className="flex-1" aria-label={t('keys.expiry')}>
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="keep">{t('keys.expiryKeep')}</SelectItem>
                        <SelectItem value="days">{t('keys.expiryFromNow')}</SelectItem>
                        <SelectItem value="never">{t('keys.expiryNeverOption')}</SelectItem>
                      </SelectContent>
                    </Select>
                    {form.expiryMode === 'days' && (
                      <Input
                        type="number"
                        min={1}
                        className="w-24"
                        placeholder={t('keys.daysPlaceholder')}
                        value={form.expiresDays}
                        onChange={(e) => setForm({...form, expiresDays: e.target.value})}
                      />
                    )}
                  </div>
                </div>
              )}
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">{t('keys.maxIps')}</Label>
                  <Input
                    type="number"
                    min={0}
                    value={form.maxIps}
                    onChange={(e) => setForm({...form, maxIps: e.target.value})}
                  />
                  <p className="text-[11px] leading-4 text-muted-foreground">
                    {t('keys.maxIpsHint')}
                  </p>
                </div>
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">{t('keys.quotaTokens')}</Label>
                  <Input
                    type="number"
                    min={0}
                    value={form.quota}
                    onChange={(e) => setForm({...form, quota: e.target.value})}
                  />
                  <p className="text-[11px] leading-4 text-muted-foreground">
                    {t('keys.quotaHint')}
                  </p>
                </div>
              </div>
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('keys.ipWhitelist')}</Label>
                <Textarea
                  rows={3}
                  value={form.ipAllowlist}
                  onChange={(e) => setForm({...form, ipAllowlist: e.target.value})}
                  placeholder={'10.0.0.0/8\n1.2.3.4'}
                />
                <p className="text-[11px] leading-4 text-muted-foreground">
                  {t('keys.ipWhitelistHint')}
                </p>
              </div>
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('keys.upstreamLabel')}</Label>
                {/* 多上游（分组隔离）：选了某个上游，这把密钥的请求就只走那个上游的
                    账号池。默认上游 = 不绑定，也就是单上游部署下的行为。 */}
                <Select
                  value={form.upstream_id == null ? '__default__' : String(form.upstream_id)}
                  onValueChange={(v) =>
                    setForm({...form, upstream_id: v === '__default__' ? null : Number(v)})
                  }
                >
                  <SelectTrigger aria-label={t('keys.upstreamLabel')}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="__default__">{t('keys.upstreamDefault')}</SelectItem>
                    {upstreams
                      .filter((u) => u.id != null)
                      .map((u) => (
                        <SelectItem key={u.id} value={String(u.id)}>
                          {u.enabled ? u.name : `${u.name}${t('keys.upstreamDisabledSuffix')}`}
                        </SelectItem>
                      ))}
                  </SelectContent>
                </Select>
                <p className="text-[11px] leading-4 text-muted-foreground">
                  {t('keys.upstreamHint')}
                </p>
              </div>
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('keys.modelWhitelist')}</Label>
                {/* 手输保留（清单里暂时没有的模型仍要能填），旁边给「从清单选择」：
                    记不住名字、写错了等调用失败才发现的日子结束。 */}
                <ModelWhitelistInput
                  value={form.models}
                  placeholder="glm-5.2, gpt-5.4"
                  onChange={(next) => {
                    setForm({...form, models: next});
                    // 改了内容就作废上次结论，避免显示过期的「都对」
                    setUnknownModels(null);
                  }}
                  // 还在打字（草稿未提交）时同样要作废：否则改到一半就切走视线，
                  // 看到的仍是上一版的结论
                  onEdit={() => setUnknownModels(null)}
                  // 离开输入框时查一次：不在每次按键时打接口（那是逐字请求），
                  // 但要早于提交——提交时才发现就得重填一遍
                  onBlur={async (next) => {
                    // 用组件交回来的新值校验：此时 form.models 还没更新，
                    // 读它会漏掉刚输入的模型。
                    const names = toLines(next);
                    if (!names.length) {
                      setUnknownModels([]);
                      return;
                    }
                    try {
                      const r = await keysApi.checkModels(names);
                      setUnknownModels(r.checked ? r.unknown : null);
                    } catch {
                      setUnknownModels(null);   // 查不了就不显示，不编造
                    }
                  }}
                />
                {unknownModels !== null && unknownModels.length > 0 && (
                  <p ref={unknownRef}
                     className="text-[10px] leading-4 text-amber-600 dark:text-amber-400">
                    {t('keys.modelsUnknown', {names: unknownModels.join(t('keys.listSeparator'))})}
                  </p>
                )}
                <p className="text-[11px] leading-4 text-muted-foreground">
                  {t('keys.modelWhitelistHint')}
                </p>
              </div>
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" className="rounded-full" onClick={() => setFormOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button className="rounded-full" onClick={submit} disabled={busy}>
              {editing ? t('keys.save') : t('keys.create')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 一次性展示新密钥：面板只存哈希，`issued.key` 是明文唯一存在的一刻，
          关掉这个弹窗就谁也拿不回来了。 */}
      <Dialog open={!!issued}
              onOpenChange={(v) => { if (!v) setIssued(null); }}>
        <DialogContent className="max-w-[min(560px,calc(100vw-2rem))]">
          <DialogHeader>
            <DialogTitle>{t('keys.createdTitle')}</DialogTitle>
            <DialogDescription>{t('keys.createdDesc')}</DialogDescription>
          </DialogHeader>
          <DialogBody>
            <div className="space-y-3 px-6 pb-3">
              {/* min-w-0 必不可少：flex 项默认 min-width:auto，长密钥会把
                  复制按钮挤出去（移动端就点不到了） */}
              <div className="flex items-center gap-2 rounded-2xl bg-muted p-3">
                <code className="min-w-0 flex-1 break-all font-mono text-xs">{issued?.key}</code>
                <CopyButton value={issued?.key ?? ''} size="sm" showLabel label={t('keys.copyKey')} />
              </div>
              <div className="flex items-center gap-2">
                <span className="text-[11px] text-muted-foreground">Base URL</span>
                <code className="min-w-0 flex-1 break-all font-mono text-[11px]">{baseUrl}/v1</code>
                <CopyButton value={`${baseUrl}/v1`} title={t('keys.copyBaseUrl')} />
              </div>
            </div>
          </DialogBody>
          <DialogFooter>
            <Button className="rounded-full" onClick={() => setIssued(null)}>
              {t('keys.savedIt')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/**
 * 首屏骨架。结构与真实内容**逐块对应**（表格块 + 若干行），而不是一坨居中的转圈：
 * 数据到位时版面不会整体跳一下。
 *
 * 页面每 60 秒心跳刷新一次，但只有「一份都没取到」才会走到这里
 * （见 use-async-data 的 isInitialLoading），所以不会一闪一闪。
 */
function KeysSkeleton() {
  return (
    <>
      <SkeletonBar className="h-9 w-52 rounded-full" />
      <section className="overflow-hidden rounded-[20px] bg-muted p-4">
        <SkeletonBar className="mb-4 h-4 w-28" />
        <div className="space-y-3">
          {Array.from({length: 5}, (_, i) => (
            <SkeletonBar key={i} className="h-11 w-full rounded-xl" />
          ))}
        </div>
      </section>
    </>
  );
}
