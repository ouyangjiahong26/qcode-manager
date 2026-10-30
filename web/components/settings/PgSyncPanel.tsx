'use client';

/**
 * PostgreSQL 异地备份面板：连接配置、定时自动备份、迁移 / 恢复与进度日志。
 *
 * 轮询只拉 /status 不碰表单（空闲 15 秒、任务中 1.5 秒），否则会把正在填的
 * 字打回旧值。恢复是破坏性动作：后端不做二次确认位，前端必须弹 ConfirmDialog。
 */
import {useCallback, useEffect, useRef, useState} from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Database,
  DownloadCloud,
  Loader2,
  PlugZap,
  Save,
  UploadCloud,
  XCircle,
} from 'lucide-react';
import {errText} from '@/lib/api/http';
import {pgSyncApi} from '@/lib/api/pgsync';
import type {PgSyncConfig, PgSyncStatus} from '@/lib/types/settings';
import {notify} from '@/lib/toast';
import {fmtAgo, fmtDateTime} from '@/lib/format';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {RichText} from '@/lib/i18n/rich-text';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';

/** 表单初值：与后端默认配置一致，避免首帧渲染出 undefined 输入框 */
const BLANK: PgSyncConfig = {
  enabled: false,
  host: '',
  port: 5432,
  dbname: '',
  user: '',
  password: '',
  sslmode: 'prefer',
  interval_minutes: 0,
  keep_local_backup: true,
  last_export_at: 0,
  last_import_at: 0,
};

export function PgSyncPanel() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [form, setForm] = useState<PgSyncConfig>(BLANK);
  const [status, setStatus] = useState<PgSyncStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [busy, setBusy] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);
  /**
   * 表单里有未保存的改动时，轮询回来的服务端值不能盖掉正在填的内容；
   * 由 patch() 置位、保存成功清零，load() 据此只回填只读展示字段。
   */
  const dirtyRef = useRef(false);

  const load = useCallback(async () => {
    try {
      const r = await pgSyncApi.config();
      setForm((f) =>
        dirtyRef.current
          ? {...f, last_export_at: r.config.last_export_at, last_import_at: r.config.last_import_at}
          : r.config,
      );
      setStatus(r.status);
      setLoadError(false);
    } catch {
      // 取不到配置时不能渲染默认值表单：那看起来像「从没配过备份」
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  /** 只刷进度，不覆盖表单 —— 轮询期间用户可能正在改输入框 */
  const pollStatus = useCallback(async () => {
    try {
      setStatus(await pgSyncApi.status());
    } catch {
      /* 任务进行中接口短暂不可用属正常，忽略 */
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const running = !!status?.running;
  useEffect(() => {
    const timer = window.setInterval(() => void pollStatus(), running ? 1500 : 15000);
    return () => window.clearInterval(timer);
  }, [running, pollStatus]);

  // 任务跑完的那一刻把配置也刷一遍（last_export_at 变了）
  const wasRunning = useRef(false);
  useEffect(() => {
    if (wasRunning.current && !running) void load();
    wasRunning.current = running;
  }, [running, load]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [status?.logs]);

  const patch = (next: Partial<PgSyncConfig>) => {
    dirtyRef.current = true;
    setForm((f) => ({...f, ...next}));
  };

  async function save() {
    setSaving(true);
    try {
      const r = await pgSyncApi.save({
        enabled: form.enabled,
        host: form.host.trim(),
        port: Number(form.port) || 5432,
        dbname: form.dbname.trim(),
        user: form.user.trim(),
        // 密码字段原样提交即可：后端 merge/save 对掩码值（********）与空串
        // 都按「不修改」处理，不会把真密码覆盖掉
        password: form.password,
        sslmode: form.sslmode,
        interval_minutes: Number(form.interval_minutes) || 0,
        keep_local_backup: form.keep_local_backup,
      });
      setForm(r.config);
      dirtyRef.current = false;
      notify.ok(t('settings.pgSaved'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }

  async function test() {
    setTesting(true);
    try {
      const r = await pgSyncApi.test({
        host: form.host.trim(),
        port: Number(form.port) || 5432,
        dbname: form.dbname.trim(),
        user: form.user.trim(),
        password: form.password,
        sslmode: form.sslmode,
      });
      if (r.ok) notify.ok(t('settings.pgTestOk'), r.server_version || r.message);
      else notify.err(r.message);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setTesting(false);
    }
  }

  async function run(kind: 'export' | 'import') {
    setBusy(true);
    try {
      const r = kind === 'export' ? await pgSyncApi.exportData() : await pgSyncApi.importData();
      if (r.ok) {
        notify.ok(kind === 'export' ? t('settings.pgExportStarted') : t('settings.pgImportStarted'));
        if (r.status) setStatus(r.status);
      } else {
        notify.warn(r.message);
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  if (loading) {
    return (
      <div className="space-y-3 rounded-[20px] bg-muted p-5">
        <SkeletonBar className="h-4 w-40" />
        <SkeletonBar className="h-40 w-full rounded-2xl" />
      </div>
    );
  }
  if (loadError) {
    return (
      <LoadError message={t('settings.pgLoadFailed')} onRetry={load} />
    );
  }

  const configured = !!form.host.trim() && !!form.dbname.trim();
  const disabled = !isAdmin || busy || running;
  const logs = status?.logs ?? [];

  return (
    <div className="space-y-4">
      {/* ── 说明 ── */}
      <div className="rounded-[20px] bg-muted p-5">
        <div className="mb-2 flex items-center gap-2 text-sm font-medium">
          <Database className="h-4 w-4" />
          {t('settings.pgTitle')}
        </div>
        <RichText text={t('settings.pgDesc')} className="text-xs leading-6 text-muted-foreground" />
      </div>

      {/* ── 连接配置 ── */}
      <div className="rounded-[20px] border border-border p-5">
        <div className="mb-4 text-sm font-medium">{t('settings.pgConnTitle')}</div>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <Label htmlFor="pg-host">{t('settings.pgHost')}</Label>
            <Input
              id="pg-host"
              value={form.host}
              disabled={!isAdmin}
              placeholder="127.0.0.1"
              onChange={(e) => patch({host: e.target.value})}
            />
            <p className="mt-1 text-xs text-muted-foreground">{t('settings.pgHostHint')}</p>
          </div>
          <div>
            <Label htmlFor="pg-port">{t('settings.pgPort')}</Label>
            <Input
              id="pg-port"
              type="number"
              value={String(form.port)}
              disabled={!isAdmin}
              onChange={(e) => patch({port: Number(e.target.value) || 0})}
            />
          </div>
          <div>
            <Label htmlFor="pg-db">{t('settings.pgDbname')}</Label>
            <Input
              id="pg-db"
              value={form.dbname}
              disabled={!isAdmin}
              placeholder="qcoder"
              onChange={(e) => patch({dbname: e.target.value})}
            />
            <p className="mt-1 text-xs text-muted-foreground">{t('settings.pgDbnameHint')}</p>
          </div>
          <div>
            <Label htmlFor="pg-user">{t('settings.pgUser')}</Label>
            <Input
              id="pg-user"
              value={form.user}
              disabled={!isAdmin}
              placeholder="postgres"
              onChange={(e) => patch({user: e.target.value})}
            />
          </div>
          <div>
            <Label htmlFor="pg-pass">{t('settings.pgPassword')}</Label>
            <Input
              id="pg-pass"
              type="password"
              value={form.password}
              disabled={!isAdmin}
              onChange={(e) => patch({password: e.target.value})}
            />
            <p className="mt-1 text-xs text-muted-foreground">{t('settings.pgPasswordHint')}</p>
          </div>
          <div>
            <Label htmlFor="pg-ssl">{t('settings.pgSslmode')}</Label>
            <Input
              id="pg-ssl"
              value={form.sslmode}
              disabled={!isAdmin}
              placeholder="prefer"
              onChange={(e) => patch({sslmode: e.target.value})}
            />
            <p className="mt-1 text-xs text-muted-foreground">{t('settings.pgSslmodeHint')}</p>
          </div>
        </div>

        {/* ── 自动备份 ── */}
        <div className="mt-5 space-y-3 border-t border-border pt-4">
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-sm">{t('settings.pgAutoEnable')}</div>
              <p className="text-xs text-muted-foreground">{t('settings.pgAutoEnableDesc')}</p>
            </div>
            <Switch
              aria-label={t('settings.pgAutoEnable')}
              checked={form.enabled}
              disabled={!isAdmin}
              onCheckedChange={(v) => patch({enabled: v})}
            />
          </div>
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-sm">{t('settings.pgInterval')}</div>
              <p className="text-xs text-muted-foreground">{t('settings.pgIntervalHint')}</p>
            </div>
            <Input
              type="number"
              className="w-28"
              value={String(form.interval_minutes)}
              disabled={!isAdmin}
              onChange={(e) => patch({interval_minutes: Number(e.target.value) || 0})}
            />
          </div>
          <div className="flex items-center justify-between gap-4">
            <div>
              <div className="text-sm">{t('settings.pgKeepBackup')}</div>
              <p className="text-xs text-muted-foreground">{t('settings.pgKeepBackupDesc')}</p>
            </div>
            <Switch
              aria-label={t('settings.pgKeepBackup')}
              checked={form.keep_local_backup}
              disabled={!isAdmin}
              onCheckedChange={(v) => patch({keep_local_backup: v})}
            />
          </div>
        </div>

        <div className="mt-5 flex flex-wrap gap-2">
          <Button onClick={() => void save()} disabled={!isAdmin || saving}>
            {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
            {t('settings.pgSave')}
          </Button>
          <Button variant="outline" onClick={() => void test()} disabled={testing || !isAdmin}>
            {testing ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <PlugZap className="h-4 w-4" />
            )}
            {t('settings.pgTest')}
          </Button>
        </div>
        {!isAdmin && (
          <p className="mt-3 text-xs text-muted-foreground">{t('settings.pgAdminOnly')}</p>
        )}
      </div>

      {/* ── 迁移 / 恢复 ── */}
      <div className="rounded-[20px] border border-border p-5">
        <div className="mb-4 text-sm font-medium">{t('settings.pgActionTitle')}</div>
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="rounded-2xl bg-muted p-4">
            <div className="flex items-center gap-2 text-sm font-medium">
              <UploadCloud className="h-4 w-4" />
              {t('settings.pgExportTitle')}
            </div>
            <p className="mt-2 text-xs leading-6 text-muted-foreground">
              {t('settings.pgExportDesc')}
            </p>
            <p className="mt-2 text-xs text-muted-foreground">
              {t('settings.pgLastExport')}
              {form.last_export_at ? ` ${fmtAgo(form.last_export_at)}` : ` ${t('format.never')}`}
            </p>
            <ConfirmDialog
              title={t('settings.pgExportConfirmTitle')}
              description={t('settings.pgExportConfirmDesc')}
              confirmText={t('settings.pgExportBtn')}
              trigger={
                <Button className="mt-3 w-full" disabled={disabled || !configured}>
                  <UploadCloud className="h-4 w-4" />
                  {t('settings.pgExportBtn')}
                </Button>
              }
              onConfirm={() => void run('export')}
            />
          </div>

          <div className="rounded-2xl bg-muted p-4">
            <div className="flex items-center gap-2 text-sm font-medium">
              <DownloadCloud className="h-4 w-4" />
              {t('settings.pgImportTitle')}
            </div>
            <RichText
              text={t('settings.pgImportDesc')}
              className="mt-2 block text-xs leading-6 text-muted-foreground"
            />
            <p className="mt-2 text-xs text-muted-foreground">
              {t('settings.pgLastImport')}
              {form.last_import_at ? ` ${fmtAgo(form.last_import_at)}` : ` ${t('format.never')}`}
            </p>
            <ConfirmDialog
              destructive
              title={t('settings.pgImportConfirmTitle')}
              description={t('settings.pgImportConfirmDesc')}
              confirmText={t('settings.pgImportBtn')}
              trigger={
                <Button
                  variant="destructive"
                  className="mt-3 w-full"
                  disabled={disabled || !configured}
                >
                  <DownloadCloud className="h-4 w-4" />
                  {t('settings.pgImportBtn')}
                </Button>
              }
              onConfirm={() => void run('import')}
            />
          </div>
        </div>
        {!configured && (
          <p className="mt-3 text-xs text-muted-foreground">{t('settings.pgNeedConfig')}</p>
        )}
      </div>

      {/* ── 进度与日志 ── */}
      {status && (status.running || status.ok !== null) && (
        <div className="rounded-[20px] border border-border p-5">
          <div className="mb-3 flex flex-wrap items-center gap-2">
            {status.running ? (
              <Badge variant="secondary">
                <Loader2 className="mr-1 h-3 w-3 animate-spin" />
                {t('settings.pgRunning')}
              </Badge>
            ) : status.ok ? (
              <Badge>
                <CheckCircle2 className="mr-1 h-3 w-3" />
                {t('settings.pgDone')}
              </Badge>
            ) : (
              <Badge variant="destructive">
                <XCircle className="mr-1 h-3 w-3" />
                {t('settings.pgFailed')}
              </Badge>
            )}
            <span className="text-xs text-muted-foreground">
              {status.kind === 'export'
                ? t('settings.pgKindExport')
                : t('settings.pgKindImport')}
            </span>
            {status.started_at ? (
              <span className="text-xs text-muted-foreground">
                {fmtDateTime(status.started_at)}
              </span>
            ) : null}
          </div>

          <div className="text-sm">{status.step}</div>

          {status.running && (
            <>
              <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full rounded-full bg-primary transition-all"
                  style={{width: `${Math.max(2, Math.min(100, status.percent))}%`}}
                />
              </div>
              <div className="mt-2 flex flex-wrap gap-3 text-xs text-muted-foreground">
                <span>
                  {t('settings.pgTableProgress', {
                    done: status.tables_done,
                    total: status.tables_total,
                  })}
                </span>
                <span>{t('settings.pgRowCount', {n: status.rows})}</span>
              </div>
            </>
          )}

          {logs.length > 0 && (
            <div
              ref={logRef}
              className="mt-3 max-h-56 overflow-auto rounded-2xl bg-muted p-3 font-mono text-xs leading-5"
            >
              {logs.map((line, i) => (
                <div
                  key={`${line.ts}-${i}`}
                  className={
                    line.level === 'error'
                      ? 'text-destructive'
                      : line.level === 'warn'
                        ? 'text-amber-600 dark:text-amber-400'
                        : 'text-muted-foreground'
                  }
                >
                  {line.text}
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* 恢复是破坏性动作，常驻提醒 */}
      <div className="flex items-start gap-2 rounded-[20px] bg-muted p-4 text-xs leading-6 text-muted-foreground">
        <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
        <RichText text={t('settings.pgWarn')} />
      </div>
    </div>
  );
}
