'use client';

/**
 * 系统更新面板：当前版本、检测更新、一键更新（GitHub / 离线包）、
 * GitHub Token（掩码）、镜像加速与自动更新开关。
 *
 * qcoder 只更新面板自身（没有随包上游），一键更新按来源分两路：
 * release（GitHub 最新发布）与 uploaded（已上传的离线升级包）。更新在后台
 * 执行并重启服务——运行中 1.5 秒轮询进度，空闲 15 秒低频轮询。
 */
import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  DownloadCloud,
  Globe,
  Loader2,
  RefreshCw,
  ShieldCheck,
  Terminal,
  Trash2,
  Upload,
  X,
  XCircle,
} from 'lucide-react';
import {errText} from '@/lib/api/http';
import {settingsApi} from '@/lib/api/settings';
import {systemApi} from '@/lib/api/system';
import type {
  AutoUpdateConfig, AutoUpdateInfo, GithubMirrorInfo, GithubTokenInfo,
  UpdateCheck, UpdatePackageMeta, UpdateStatus,
} from '@/lib/types/settings';
import {notify} from '@/lib/toast';
import {fmtAgo} from '@/lib/format';
import {useAuth} from '@/lib/auth-context';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {LoadError} from '@/components/common/states/LoadError';
import {RichText} from '@/lib/i18n/rich-text';
import {useT} from '@/lib/i18n/provider';

export function UpdatePanel() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [versions, setVersions] = useState<{manager: string; repo: string} | null>(null);
  const [check, setCheck] = useState<UpdateCheck | null>(null);
  const [checking, setChecking] = useState(false);
  const [busy, setBusy] = useState(false);
  /** 结果横幅是否已关闭：running 边沿复位，不依赖时间戳存在与否 */
  const [resultDismissed, setResultDismissed] = useState(false);
  const [pkg, setPkg] = useState<UpdatePackageMeta | null>(null);
  const [pkgBusy, setPkgBusy] = useState(false);
  const [pkgSha, setPkgSha] = useState('');
  /** 几份数据全没取到：版本与「有没有新版本」都不能下结论（服务可能在重启） */
  const [loadFailed, setLoadFailed] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    const [st, vs, ck, pk] = await Promise.allSettled([
      systemApi.updateStatus(),
      systemApi.versions(),
      systemApi.checkUpdate(),
      systemApi.packageMeta(),
    ]);
    if (pk.status === 'fulfilled') setPkg(pk.value);
    if (st.status === 'fulfilled') setStatus(st.value);
    if (vs.status === 'fulfilled') setVersions(vs.value);
    if (ck.status === 'fulfilled') setCheck(ck.value);
    // 全失败才报错：更新期间服务重启，单份失败是瞬时的；但全没到时页面上的
    // 「版本 — / 无更新」就是在编造结论。
    setLoadFailed([st, vs, ck, pk].every((x) => x.status === 'rejected'));
  }, []);

  /** 上传离线升级包：前端只选文件 + 填发布页摘要，核对与落盘都在服务端 */
  const uploadPackage = useCallback(
    async (file: File) => {
      setPkgBusy(true);
      try {
        const meta = await systemApi.uploadPackage(file, pkgSha.trim());
        setPkg(meta);
        notify.ok(
          t('settings.updPkgUploaded', {version: meta.version || ''}),
          t('settings.updPkgUploadedDesc', {
            mb: String(Math.round((meta.size || 0) / 1024 / 1024)),
          }),
        );
        setPkgSha('');
      } catch (e) {
        notify.err(errText(e));
      } finally {
        setPkgBusy(false);
      }
    },
    [pkgSha, t],
  );

  const startUpdate = useCallback(
    async (source: 'release' | 'uploaded') => {
      setBusy(true);
      try {
        const r = await systemApi.startUpdate(source);
        notify.ok(t('settings.updStarted'), r.message);
        await load();
      } catch (e) {
        notify.err(errText(e));
      } finally {
        setBusy(false);
      }
    },
    [load, t],
  );

  const dropPackage = useCallback(async () => {
    setPkgBusy(true);
    try {
      await systemApi.deletePackage();
      setPkg({present: false});
      notify.ok(t('settings.updPkgCleared'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setPkgBusy(false);
    }
  }, [t]);

  // ── GitHub Token / 镜像 / 自动更新 ──
  const [gh, setGh] = useState<GithubTokenInfo | null>(null);
  const [ghInput, setGhInput] = useState('');
  const [ghBusy, setGhBusy] = useState(false);
  const [mirror, setMirror] = useState<GithubMirrorInfo | null>(null);
  const [mirrorInput, setMirrorInput] = useState('');
  const [mirrorBusy, setMirrorBusy] = useState(false);
  const [auto, setAuto] = useState<AutoUpdateInfo | null>(null);
  /** 表单保存的是整份配置对象：未知字段（如 target）原样带回，不会被界面丢掉 */
  const [autoForm, setAutoForm] = useState<AutoUpdateConfig | null>(null);
  const [autoBusy, setAutoBusy] = useState(false);

  useEffect(() => {
    settingsApi.autoUpdate().then((a) => {
      setAuto(a);
      setAutoForm({...a});
    }).catch(() => setAuto(null));
    settingsApi.githubToken().then(setGh).catch(() => setGh(null));
    settingsApi.githubMirror().then((m) => {
      setMirror(m);
      setMirrorInput(m.stored);
    }).catch(() => setMirror(null));
  }, []);

  /** 强制重新检测（绕过服务端 6 小时缓存） */
  const recheck = useCallback(async () => {
    setChecking(true);
    try {
      const r = await systemApi.checkUpdate(true);
      setCheck(r);
      if (r.has_any) {
        notify.warn(t('settings.updNewVersion'), t('settings.updManagerVersion', {
          v: r.manager.latest,
        }));
      } else if (r.manager.error) {
        // 失败不能说成结论：403 / 网络失败时回退的是旧缓存
        notify.warn(t('settings.updCheckFailed'), r.manager.error.slice(0, 160));
      } else {
        notify.ok(t('settings.updUpToDate'), t('settings.updUpToDateDesc'));
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setChecking(false);
    }
  }, [t]);

  const saveGh = useCallback(
    async (token: string) => {
      setGhBusy(true);
      try {
        const r = await settingsApi.saveGithubToken(token);
        setGh({...r, source: r.configured ? 'panel' : ''});
        setGhInput('');
        notify.ok(token ? t('settings.updGhSaved') : t('settings.updGhCleared'));
        await recheck();
      } catch (e) {
        notify.err(errText(e));
      } finally {
        setGhBusy(false);
      }
    },
    [recheck, t],
  );

  const saveMirror = useCallback(
    async (value: string) => {
      setMirrorBusy(true);
      try {
        const r = await settingsApi.saveGithubMirror(value);
        setMirror(r);
        setMirrorInput(r.stored);
        notify.ok(value.trim() ? t('settings.updMirrorSaved') : t('settings.updMirrorCleared'));
        // 换了取版本的路子，缓存里的结论就不作数了
        await recheck();
      } catch (e) {
        notify.err(errText(e));
      } finally {
        setMirrorBusy(false);
      }
    },
    [recheck, t],
  );

  const saveAuto = useCallback(
    async (next: AutoUpdateConfig) => {
      setAutoBusy(true);
      try {
        const r = await settingsApi.saveAutoUpdate(next);
        setAuto(r);
        setAutoForm({...r});
        notify.ok(r.enabled ? t('settings.updAutoSavedOn') : t('settings.updAutoSavedOff'));
      } catch (e) {
        notify.err(errText(e));
      } finally {
        setAutoBusy(false);
      }
    },
    [t],
  );

  useEffect(() => {
    void load();
  }, [load]);

  // 运行中高频轮询，空闲低频（服务可能在更新中短暂不可用）
  const running = !!status?.running;
  useEffect(() => {
    const timer = window.setInterval(() => void load(), running ? 1500 : 15000);
    return () => window.clearInterval(timer);
  }, [load, running]);

  // running 一旦变为 true 就复位「已关闭」，本轮结束后结果横幅重新可见
  useEffect(() => {
    if (running) setResultDismissed(false);
  }, [running]);

  // 日志自动滚到底
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [status?.logs]);

  const logs = status?.logs ?? [];
  const logText = useMemo(
    () => (logs.length ? logs.map((l) => l.text).join('\n') : status?.log_tail || ''),
    // logs 是 `status?.logs ?? []` 的派生值：status 不变时引用稳定，空数组
    // 字面量不会在每次渲染都进依赖（eslint 的静态分析看不出这一点）
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [status?.logs, status?.log_tail],
  );

  const autoDirty = useMemo(
    () =>
      !!auto &&
      !!autoForm &&
      (auto.enabled !== autoForm.enabled ||
        auto.interval_hours !== autoForm.interval_hours ||
        auto.window_start !== autoForm.window_start ||
        auto.window_end !== autoForm.window_end),
    [auto, autoForm],
  );

  const done = !!status && !status.running && status.ok !== null && !resultDismissed;

  return (
    <div className="space-y-4">
      {loadFailed && !running && (
        <LoadError message={t('settings.updLoadFailed')} onRetry={load} />
      )}

      {/* 当前版本 */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="text-sm font-medium">{t('settings.updCurrentVersion')}</div>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              onClick={() => void recheck()}
              disabled={checking || running}
            >
              <RefreshCw className={checking ? 'animate-spin' : ''} />
              {t('settings.updCheckUpdate')}
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              onClick={() => void load()}
              disabled={running}
            >
              <RefreshCw className={running ? 'animate-spin' : ''} />
              {t('settings.btnRefresh')}
            </Button>
          </div>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div className="rounded-2xl bg-background/60 px-3.5 py-3">
            <div className="text-[11px] text-muted-foreground">{t('settings.updManager')}</div>
            <div className="mt-1 text-sm font-semibold tabular-nums">
              {versions?.manager || status?.version || '—'}
            </div>
          </div>
          <div className="rounded-2xl bg-background/60 px-3.5 py-3">
            <div className="text-[11px] text-muted-foreground">{t('settings.updLastUpdate')}</div>
            <div className="mt-1 text-sm font-semibold">
              {status?.finished_at ? fmtAgo(status.finished_at) : t('format.never')}
            </div>
          </div>
        </div>
      </div>

      {/* 新版本提醒 */}
      {check?.has_any && !running && (
        <div className="rounded-[20px] border border-blue-500/40 bg-blue-500/10 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="flex items-start gap-2.5">
              <DownloadCloud className="mt-0.5 h-4 w-4 shrink-0 text-blue-500" />
              <div className="space-y-1.5">
                <div className="text-xs font-medium">
                  {t('settings.updNewVersionAvailable')}
                </div>
                <div className="text-[11px] text-muted-foreground">
                  {t('settings.updManagerUpgrade', {from: check.manager.current})}{' '}
                  <span className="font-medium text-foreground">{check.manager.latest}</span>
                </div>
                {/* 更新前就能看到 Release 正文 */}
                {!!check.manager.release_notes && (
                  <details className="group text-[11px]">
                    <summary className="cursor-pointer list-none text-foreground/80">
                      <span className="group-open:hidden">
                        {t('settings.updReleaseNotes')}
                      </span>
                      <span className="hidden group-open:inline">
                        {t('settings.updHideReleaseNotes')}
                      </span>
                    </summary>
                    <pre className="mt-1 max-h-[220px] overflow-auto whitespace-pre-wrap break-all rounded-xl bg-background/60 p-2 font-sans leading-relaxed text-muted-foreground">
                      {check.manager.release_notes}
                    </pre>
                  </details>
                )}
                {/* 产物未就位：现在点更新只会拿到 404，先说清楚并收起按钮 */}
                {check.manager.assets_ready === false && (
                  <div className="flex items-start gap-1.5 text-[11px] text-amber-600 dark:text-amber-400">
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    <span>{t('settings.updAssetsPending')}</span>
                  </div>
                )}
                <div className="text-[11px] text-muted-foreground">
                  {t('settings.updHint')}
                </div>
              </div>
            </div>
            <Button
              size="sm"
              className="rounded-full"
              disabled={!isAdmin || busy || running || check.manager.assets_ready === false}
              onClick={() => void startUpdate('release')}
            >
              <DownloadCloud />
              {t('settings.updStartUpdate')}
            </Button>
          </div>
        </div>
      )}

      {/* 本次检测失败、值来自上次成功结果：必须说明，不能让人以为「已是最新」 */}
      {check && !!check.manager.error && !running && (
        <div className="flex items-start gap-2 rounded-[20px] border border-amber-500/30 bg-amber-500/10 px-4 py-3">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
          <div className="min-w-0 space-y-1 text-[11px] leading-4">
            <div className="font-medium">{t('settings.updCheckFailed')}</div>
            <div className="break-all text-muted-foreground">
              {check.manager.error.slice(0, 200)}
            </div>
            <div className="text-muted-foreground">
              {check.manager.stale
                ? t('settings.updStaleNote', {at: fmtAgo(check.manager.stale_at || 0)})
                : t('settings.updCheckFailedHint')}
            </div>
          </div>
        </div>
      )}

      {check && !check.has_any && !check.manager.error && !running && (
        <div className="flex items-center gap-2 rounded-[20px] border border-emerald-500/30 bg-emerald-500/10 px-4 py-3">
          <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />
          <span className="text-xs">{t('settings.updUpToDate')}</span>
          {check.checked_at > 0 && (
            <span className="text-[11px] text-muted-foreground">
              {t('settings.updCheckedAgo', {ago: fmtAgo(check.checked_at)})}
            </span>
          )}
        </div>
      )}

      {/* 更新进行中 */}
      {running && (
        <div className="flex items-start gap-2.5 rounded-[20px] border border-blue-500/30 bg-blue-500/10 p-4">
          <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-blue-500" />
          <div className="min-w-0 flex-1 space-y-1">
            <div className="text-xs font-medium">
              {t('settings.updUpdatingNow', {step: status?.step || t('settings.updStepRunning')})}
            </div>
            <div className="text-[11px] text-muted-foreground">
              {t('settings.updUpdatingHint')}
            </div>
          </div>
        </div>
      )}

      {/* 上次结果 */}
      {done && (
        <div
          className={
            'flex items-start gap-2.5 rounded-[20px] border p-4 ' +
            (status?.ok
              ? 'border-emerald-500/30 bg-emerald-500/10'
              : 'border-red-500/30 bg-red-500/10')
          }
        >
          {status?.ok ? (
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
          ) : (
            <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-500" />
          )}
          <div className="min-w-0 flex-1 space-y-1">
            <div className="text-xs font-medium">
              {status?.ok ? t('settings.updDone') : t('settings.updNotDone')}
            </div>
            <div className="text-[11px] text-muted-foreground">
              {status?.ok
                ? (status.duration ? t('settings.updDoneDuration', {n: status.duration}) : '') +
                  t('settings.updDoneRestart')
                : t('settings.updFailedDetail')}
            </div>
          </div>
          <button
            type="button"
            aria-label={t('settings.updCloseTip')}
            title={t('settings.updCloseTip')}
            className="-m-1 shrink-0 rounded-full p-1 text-muted-foreground transition-colors hover:bg-foreground/10 hover:text-foreground"
            onClick={() => setResultDismissed(true)}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* 一键更新（release） */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-1 text-sm font-medium">{t('settings.updOneClick')}</div>
        <div className="mb-3 text-[11px] leading-4 text-muted-foreground">
          {t('settings.updOneClickDesc')}
        </div>
        <ConfirmDialog
          title={t('settings.updConfirmTitle')}
          description={t('settings.updConfirmDesc')}
          confirmText={t('settings.updStartUpdate')}
          onConfirm={() => startUpdate('release')}
          trigger={
            <Button className="rounded-full" disabled={!isAdmin || busy || running}>
              <DownloadCloud className="h-3.5 w-3.5" />
              {t('settings.updStartUpdate')}
            </Button>
          }
        />
        {!isAdmin && (
          <p className="mt-2 text-[11px] text-muted-foreground">
            {t('settings.updReadonlyNote')}
          </p>
        )}
      </div>

      {/* GitHub Token：未认证 60 次/小时/IP，共享出口很容易被限流 */}
      {isAdmin && (
        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-1 text-xs font-medium">{t('settings.updGhTitle')}</div>
          <div className="mb-2 text-[11px] leading-4 text-muted-foreground">
            <RichText text={t('settings.updGhDesc')} />
          </div>
          <div className="flex flex-wrap items-end gap-2">
            <div className="min-w-[16rem] flex-1 space-y-1">
              <Input
                type="password"
                value={ghInput}
                onChange={(e) => setGhInput(e.target.value)}
                placeholder={gh?.configured ? gh.masked : t('settings.updGhPlaceholder')}
                className="h-8 bg-background font-mono text-xs"
                autoComplete="off"
              />
            </div>
            <Button
              size="sm"
              className="rounded-full"
              disabled={ghBusy || !ghInput.trim()}
              onClick={() => void saveGh(ghInput.trim())}
            >
              <ShieldCheck className="h-3.5 w-3.5" />
              {t('settings.btnSave')}
            </Button>
            {gh?.configured && (
              <Button
                size="sm"
                variant="outline"
                className="rounded-full"
                disabled={ghBusy}
                onClick={() => void saveGh('')}
              >
                <Trash2 className="h-3.5 w-3.5" />
                {t('settings.updGhClear')}
              </Button>
            )}
          </div>
          <div className="mt-2 text-[11px] text-muted-foreground">
            {gh?.source === 'env'
              ? t('settings.updGhFromEnv')
              : gh?.configured
                ? t('settings.updGhConfigured', {masked: gh.masked})
                : t('settings.updGhNotSet')}
          </div>
        </div>
      )}

      {/* GitHub 镜像：国内机器直连超时时加速 */}
      {isAdmin && (
        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-1 text-xs font-medium">{t('settings.updMirrorTitle')}</div>
          <div className="mb-2 text-[11px] leading-4 text-muted-foreground">
            <RichText text={t('settings.updMirrorDesc')} />
          </div>
          <div className="flex flex-wrap items-end gap-2">
            <div className="min-w-[16rem] flex-1 space-y-1">
              <Input
                value={mirrorInput}
                onChange={(e) => setMirrorInput(e.target.value)}
                placeholder="https://gh-proxy.com/"
                aria-label={t('settings.updMirrorTitle')}
                className="h-8 bg-background font-mono text-xs"
                autoComplete="off"
              />
            </div>
            <Button
              size="sm"
              className="rounded-full"
              disabled={mirrorBusy}
              onClick={() => void saveMirror(mirrorInput.trim())}
            >
              <Globe className="h-3.5 w-3.5" />
              {t('settings.btnSave')}
            </Button>
            {mirror?.stored && (
              <Button
                size="sm"
                variant="outline"
                className="rounded-full"
                disabled={mirrorBusy}
                onClick={() => void saveMirror('')}
              >
                <Trash2 className="h-3.5 w-3.5" />
                {t('settings.updMirrorClear')}
              </Button>
            )}
          </div>
          {!!mirror?.presets?.length && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <span className="text-[11px] text-muted-foreground">
                {t('settings.updMirrorPresets')}
              </span>
              {mirror.presets.map((p) => (
                <button
                  key={p}
                  type="button"
                  className="rounded-full bg-background px-2 py-0.5 font-mono text-[11px] text-muted-foreground hover:text-foreground"
                  onClick={() => setMirrorInput(p)}
                >
                  {p}
                </button>
              ))}
            </div>
          )}
          <div className="mt-2 text-[11px] leading-4 text-muted-foreground">
            {mirror?.source === 'env'
              ? t('settings.updMirrorFromEnv', {value: mirror.env_mirror})
              : mirror?.mirror
                ? t('settings.updMirrorActive', {value: mirror.mirror})
                : t('settings.updMirrorNone')}
          </div>
        </div>
      )}

      {/* 自动更新：定时检测，只在时间窗内自动执行一键更新（默认关闭） */}
      {isAdmin && auto && autoForm && (
        <div className="rounded-[20px] bg-muted p-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="flex items-center gap-1.5 text-xs font-medium">
                <Clock className="h-3.5 w-3.5" />
                {t('settings.updAutoTitle')}
              </div>
              <div className="mt-1 text-[11px] leading-4 text-muted-foreground">
                <RichText text={t('settings.updAutoDesc')} />
              </div>
            </div>
            <Switch
              aria-label={t('settings.updAutoTitle')}
              checked={autoForm.enabled}
              disabled={autoBusy || (!!auto.unsupported_reason && !autoForm.enabled)}
              onCheckedChange={(v) => void saveAuto({...autoForm, enabled: v})}
            />
          </div>
          {!!auto.unsupported_reason && (
            <div className="mt-2 flex items-start gap-1.5 text-[11px] leading-4 text-amber-600 dark:text-amber-400">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              {t('settings.updAutoUnsupported')}
            </div>
          )}
          <div className="mt-3 grid gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <Label className="text-[11px] text-muted-foreground">
                {t('settings.updAutoInterval')}
              </Label>
              <Input
                type="number"
                min={1}
                max={168}
                value={autoForm.interval_hours}
                onChange={(e) =>
                  setAutoForm({...autoForm, interval_hours: Number(e.target.value) || 0})
                }
                className="h-8 bg-background text-xs"
                aria-label={t('settings.updAutoInterval')}
              />
            </div>
            <div className="space-y-1">
              <Label className="text-[11px] text-muted-foreground">
                {t('settings.updAutoWindow')}
              </Label>
              <div className="flex items-center gap-1.5">
                <Input
                  type="number"
                  min={0}
                  max={23}
                  value={autoForm.window_start}
                  onChange={(e) =>
                    setAutoForm({...autoForm, window_start: Number(e.target.value)})
                  }
                  className="h-8 bg-background text-xs"
                  aria-label={t('settings.updAutoWindowStart')}
                />
                <span className="text-xs text-muted-foreground">–</span>
                <Input
                  type="number"
                  min={0}
                  max={23}
                  value={autoForm.window_end}
                  onChange={(e) =>
                    setAutoForm({...autoForm, window_end: Number(e.target.value)})
                  }
                  className="h-8 bg-background text-xs"
                  aria-label={t('settings.updAutoWindowEnd')}
                />
              </div>
            </div>
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
            <div className="text-[11px] leading-4 text-muted-foreground">
              {t('settings.updAutoWindowHint', {
                tz: `UTC${auto.server_tz_offset_minutes >= 0 ? '+' : '-'}${
                  String(Math.floor(Math.abs(auto.server_tz_offset_minutes) / 60)).padStart(2, '0')
                }:${
                  String(Math.abs(auto.server_tz_offset_minutes) % 60).padStart(2, '0')
                }`,
              })}
            </div>
            {autoDirty && (
              <Button
                size="sm"
                className="rounded-full"
                disabled={autoBusy}
                onClick={() => void saveAuto(autoForm)}
              >
                {t('settings.btnSave')}
              </Button>
            )}
          </div>
          {auto.state.last_check_at > 0 && (
            <div className="mt-2 text-[11px] leading-4 text-muted-foreground">
              {t('settings.updAutoLastRun', {
                ago: fmtAgo(auto.state.last_check_at),
                msg: auto.state.last_message || '-',
              })}
            </div>
          )}
        </div>
      )}

      {/* 离线升级包：内网 / 拿不到 GitHub 的机器 */}
      {isAdmin && (
        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-1 flex flex-wrap items-center gap-2 text-sm font-medium">
            <Upload className="h-4 w-4" />
            {t('settings.updPkgTitle')}
            {pkg?.present && pkg.version && (
              <Badge variant="secondary" className="rounded-full font-mono text-[10px]">
                {pkg.version}
              </Badge>
            )}
          </div>
          <div className="mb-3 text-[11px] leading-4 text-muted-foreground">
            <RichText text={t('settings.updPkgDesc')} />
          </div>

          {pkg?.present ? (
            <div className="space-y-2">
              <div className="rounded-2xl bg-background/60 px-3.5 py-2.5 text-[11px] leading-5">
                <div className="font-mono">{pkg.name || t('settings.updPkgUnnamed')}</div>
                <div className="break-all text-muted-foreground">
                  sha256 {pkg.sha256 || '—'}
                  {pkg.size ? ` · ${(pkg.size / 1024 / 1024).toFixed(1)} MB` : ''}
                  {pkg.expected_sha256
                    ? ` · ${t('settings.updPkgCompared')}`
                    : ` · ${t('settings.updPkgNotCompared')}`}
                </div>
                {!pkg.expected_sha256 && (
                  <div className="mt-1 text-amber-600 dark:text-amber-500">
                    {t('settings.updPkgCompareHint')}
                  </div>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  className="rounded-full"
                  disabled={busy || running}
                  onClick={() => void startUpdate('uploaded')}
                >
                  <DownloadCloud className="mr-1.5 h-3.5 w-3.5" />
                  {t('settings.updPkgUse')}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="rounded-full"
                  disabled={pkgBusy || running}
                  onClick={() => void dropPackage()}
                >
                  {t('settings.updPkgClear')}
                </Button>
              </div>
            </div>
          ) : (
            <div className="space-y-2">
              <div className="flex flex-wrap items-end gap-2">
                <label className="flex flex-col gap-1">
                  <Label className="text-[11px] text-muted-foreground">
                    {t('settings.updPkgFile')}
                  </Label>
                  <input
                    type="file"
                    accept=".tar.gz,application/gzip"
                    disabled={pkgBusy || running}
                    className="text-[11px] file:mr-2 file:rounded-full file:border-0 file:bg-background file:px-3 file:py-1.5 file:text-[11px] file:text-foreground"
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      if (file) void uploadPackage(file);
                      e.target.value = '';
                    }}
                  />
                </label>
                <label className="flex min-w-[16rem] flex-1 flex-col gap-1">
                  <Label className="text-[11px] text-muted-foreground">
                    {t('settings.updPkgShaLabel')}
                  </Label>
                  <Input
                    className="h-8 bg-background font-mono text-[11px]"
                    value={pkgSha}
                    placeholder={t('settings.updPkgShaPlaceholder')}
                    onChange={(e) => setPkgSha(e.target.value)}
                  />
                </label>
              </div>
              {pkgBusy && (
                <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  {t('settings.updPkgUploading')}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* 日志：没有可看的内容时整块不渲染 */}
      {(logText || running) && (
        <div className="rounded-[20px] bg-muted p-4">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="flex items-center gap-2 text-sm font-medium">
              <Terminal className="h-4 w-4" />
              {t('settings.updLogTitle')}
            </div>
            {logs.length > 0 && (
              <Badge variant="secondary" className="rounded-full text-[10px]">
                {t('settings.updLogLines', {n: logs.length})}
              </Badge>
            )}
          </div>
          {logText ? (
            <div
              ref={logRef}
              className="scroll-slim max-h-[320px] overflow-auto rounded-2xl bg-background/60 p-3"
            >
              <pre className="whitespace-pre-wrap break-all font-mono text-[11px] leading-5 text-muted-foreground">
                {logText}
              </pre>
            </div>
          ) : (
            <div className="rounded-2xl bg-background/60 px-3 py-8 text-center text-xs text-muted-foreground">
              {t('settings.updLogEmpty')}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
