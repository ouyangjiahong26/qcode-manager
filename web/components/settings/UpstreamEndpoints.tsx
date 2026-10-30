'use client';

/**
 * 多上游（PAT 池分组）管理面板。
 *
 * 每个上游是一个独立账号池：密钥绑定某上游后，它的请求只走那个池。默认上游
 * （id=null）来自环境变量 QC2API_BASE，不是数据库里的一行，因此只展示、不可
 * 编辑删除——它的连通性用 /healthz 的探测结果呈现（带 id 的上游走各自的
 * /probe 接口）。每行附密钥 / 账号引用计数，删除前就能看到「还有谁绑在这」。
 */
import {useCallback, useEffect, useState} from 'react';
import {Loader2, Server} from 'lucide-react';

import {errText} from '@/lib/api/http';
import {upstreamsApi} from '@/lib/api/settings';
import {systemApi} from '@/lib/api/system';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {UpstreamEndpoint} from '@/lib/types/settings';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';
import {UpstreamFormDialog} from './UpstreamFormDialog';

/** 默认上游的可达性：一次 /healthz 的 upstream 探测结果 */
interface DefaultProbe {
  ok: boolean;
  error: string;
}

export function UpstreamEndpoints() {
  const t = useT();
  // 写操作后端要求会话管理员：对 viewer 收起按钮，避免点了只会得到 403
  const {isAdmin} = useAuth();
  /** null = 没取到（加载中或失败）：不能把空数组渲染成「没有上游」 */
  const [items, setItems] = useState<UpstreamEndpoint[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<UpstreamEndpoint | null>(null);
  const [busy, setBusy] = useState(false);
  const [probing, setProbing] = useState<string | number | null>(null);
  const [defaultProbe, setDefaultProbe] = useState<DefaultProbe | null>(null);

  const load = useCallback(async () => {
    setItems(null);
    try {
      setItems(await upstreamsApi.list());
      setFailed(false);
      return true;
    } catch {
      setFailed(true);
      return false;
    }
  }, []);

  // 首屏一次：settings 页无心跳（数据只在管理员动手时才变）
  useEffect(() => {
    void load();
  }, [load]);

  const loadDefaultProbe = useCallback(async () => {
    setProbing('default');
    try {
      const r = await systemApi.healthz();
      setDefaultProbe(r.upstream);
    } catch (e) {
      setDefaultProbe({ok: false, error: errText(e)});
    } finally {
      setProbing(null);
    }
  }, []);

  useEffect(() => {
    void loadDefaultProbe();
  }, [loadDefaultProbe]);

  async function remove(item: UpstreamEndpoint) {
    if (item.is_default || item.id == null) return;
    setBusy(true);
    try {
      await upstreamsApi.remove(item.id);
      notify.ok(t('settings.upDeleted'));
      await load();
    } catch (e) {
      // 仍有密钥 / 账号绑着时后端回 409 并在 detail 里说明数量——原样弹出
      notify.err(t('settings.upDeleteBlocked'), errText(e));
    } finally {
      setBusy(false);
    }
  }

  async function probe(item: UpstreamEndpoint) {
    if (item.is_default || item.id == null) return;
    setProbing(item.id);
    try {
      const r = await upstreamsApi.probe(item.id);
      if (r.ok) notify.ok(t('settings.upProbeOk'), r.message);
      else notify.err(t('settings.upProbeFailed'), r.message);
    } catch (e) {
      notify.err(t('settings.upProbeFailed'), errText(e));
    } finally {
      setProbing(null);
    }
  }

  const defaultItem = items?.find((i) => i.is_default) ?? null;

  return (
    <section className="space-y-3 rounded-[20px] bg-muted p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="space-y-1">
          <h2 className="text-sm font-semibold">{t('settings.upTitle')}</h2>
          <p className="text-[11px] leading-4 text-muted-foreground">{t('settings.upDesc')}</p>
        </div>
        {isAdmin && (
          <Button
            className="rounded-full"
            onClick={() => {
              setEditing(null);
              setFormOpen(true);
            }}
          >
            {t('settings.upAdd')}
          </Button>
        )}
      </div>

      {items === null && !failed && (
        <div className="space-y-2">
          {Array.from({length: 3}, (_, i) => (
            <SkeletonBar key={i} className="h-16 w-full rounded-xl" />
          ))}
        </div>
      )}
      {failed && <LoadError onRetry={load} />}

      {items !== null && (
        <div className="space-y-2">
          {items.map((item) => (
            <div
              key={item.is_default ? 'default' : item.id}
              className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-background px-3 py-2"
            >
              <div className="min-w-0 space-y-0.5">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-sm font-medium">{item.name}</span>
                  {item.is_default && (
                    <Badge variant="secondary" className="rounded-full text-[10px]">
                      {t('settings.upDefaultTag')}
                    </Badge>
                  )}
                  {!item.enabled && (
                    <Badge variant="destructive" className="rounded-full text-[10px]">
                      {t('settings.upDisabledTag')}
                    </Badge>
                  )}
                  <span className="text-[11px] text-muted-foreground">
                    {t('settings.upRefCounts', {
                      keys: item.key_count,
                      accounts: item.account_count,
                    })}
                  </span>
                </div>
                <div className="truncate font-mono text-[11px] text-muted-foreground">
                  {item.base_url}
                </div>
                {item.note ? (
                  <div className="truncate text-[11px] text-muted-foreground" title={item.note}>
                    {item.note}
                  </div>
                ) : null}
                {/* 默认上游没有 id，走不了 /probe；直接展示 /healthz 的探测结果 */}
                {item.is_default && defaultProbe && (
                  <div
                    className={
                      'text-[11px] ' +
                      (defaultProbe.ok
                        ? 'text-emerald-600 dark:text-emerald-400'
                        : 'text-red-600 dark:text-red-400')
                    }
                  >
                    {defaultProbe.ok
                      ? t('settings.upProbeOk')
                      : `${t('settings.upProbeFailed')}${
                          defaultProbe.error ? `：${defaultProbe.error}` : ''
                        }`}
                  </div>
                )}
              </div>
              {item.is_default ? (
                <Button
                  variant="outline"
                  className="rounded-full"
                  disabled={probing === 'default'}
                  onClick={() => void loadDefaultProbe()}
                >
                  {probing === 'default' ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : null}
                  {probing === 'default' ? t('settings.upProbing') : t('settings.upProbe')}
                </Button>
              ) : (
                isAdmin && (
                  <div className="flex items-center gap-2">
                    <Button
                      variant="outline"
                      className="rounded-full"
                      disabled={probing === item.id}
                      onClick={() => void probe(item)}
                    >
                      {probing === item.id ? (
                        <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      ) : null}
                      {probing === item.id ? t('settings.upProbing') : t('settings.upProbe')}
                    </Button>
                    <Button
                      variant="outline"
                      className="rounded-full"
                      onClick={() => {
                        setEditing(item);
                        setFormOpen(true);
                      }}
                    >
                      {t('settings.upEdit')}
                    </Button>
                    <ConfirmDialog
                      title={t('settings.upDeleteTitle')}
                      description={t('settings.upDeleteDesc', {name: item.name})}
                      confirmText={t('settings.upRemove')}
                      destructive
                      onConfirm={() => remove(item)}
                      trigger={
                        <Button
                          variant="ghost"
                          className="rounded-full text-destructive"
                          disabled={busy}
                        >
                          {t('settings.upRemove')}
                        </Button>
                      }
                    />
                  </div>
                )
              )}
            </div>
          ))}
          {!items.length && (
            <EmptyState
              icon={Server}
              title={t('settings.upEmpty')}
              description={t('settings.upEmptyDesc')}
              className="flex flex-col items-center justify-center py-12 text-center"
            />
          )}
        </div>
      )}

      <p className="text-[11px] leading-4 text-muted-foreground">{t('settings.upHint')}</p>

      <UpstreamFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        editing={editing}
        defaultUpstream={defaultItem}
        onSaved={() => void load()}
      />
    </section>
  );
}
