'use client';

/**
 * 模型别名映射面板：alias → 实际模型，`*` 为兜底。
 *
 * 目标模型下拉取自 /api/models（上游实时清单，后端 5 分钟缓存）。保存走
 * 乐观更新：先切新值让表格立即响应，reload 成功才撤回乐观值；刷新失败时
 * 撤早了表格会跳回旧值，用户会以为没保存上。
 */
import {useCallback, useEffect, useState} from 'react';
import {Plus, Shuffle, Trash2} from 'lucide-react';

import {errText} from '@/lib/api/http';
import {settingsApi} from '@/lib/api/settings';
import {modelsApi} from '@/lib/api/models';
import {useAsyncAll} from '@/lib/use-async-data';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {ModelMap} from '@/lib/types/settings';
import type {UpstreamModel} from '@/lib/types/models';
import {Button} from '@/components/ui/button';
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
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {LoadError} from '@/components/common/states/LoadError';
import {SkeletonBar} from '@/components/common/states/SkeletonBar';

export function ModelMapPanel() {
  const t = useT();
  const {isAdmin} = useAuth();
  const {values, errors, isInitialLoading, reload} = useAsyncAll(
    {modelMap: () => settingsApi.modelMap()},
    [],
  );
  const mapFailed = 'modelMap' in errors;

  /** 乐观值：保存成功后、刷新落地前先显示新值 */
  const [optimistic, setOptimistic] = useState<ModelMap | null>(null);
  const modelMap: ModelMap = optimistic ?? values.modelMap ?? {};

  const [models, setModels] = useState<UpstreamModel[]>([]);
  const [mapAlias, setMapAlias] = useState('');
  const [mapTarget, setMapTarget] = useState('');
  const [targetLoading, setTargetLoading] = useState(true);

  const loadModels = useCallback(async () => {
    setTargetLoading(true);
    try {
      const r = await modelsApi.catalog();
      setModels(r.models || []);
    } catch {
      // 下拉取不到清单时仍允许手填：映射本身不依赖上游模型列表
      setModels([]);
    } finally {
      setTargetLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadModels();
  }, [loadModels]);

  async function saveModelMap(next: ModelMap) {
    setOptimistic(next);
    try {
      await settingsApi.saveModelMap(next);
      notify.ok(t('settings.modelMapSaved'));
      // 刷新成功才撤乐观值（保存本身已成功，只是列表还没刷上）
      if (await reload()) setOptimistic(null);
    } catch (e) {
      setOptimistic(null);
      notify.err(errText(e));
    }
  }

  if (isInitialLoading) {
    return (
      <div className="rounded-[20px] bg-muted p-4">
        <SkeletonBar className="mb-4 h-3.5 w-28" />
        <div className="space-y-2">
          {Array.from({length: 3}, (_, i) => (
            <SkeletonBar key={i} className="h-9 w-full rounded-xl" />
          ))}
        </div>
      </div>
    );
  }
  if (mapFailed) {
    return <LoadError variant="page" onRetry={reload} />;
  }

  return (
    <div className="space-y-4">
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-1 text-sm font-medium">{t('settings.mapNewAlias')}</div>
        <div className="mb-3 text-[11px] leading-4 text-muted-foreground">
          {t('settings.mapNewAliasDesc')}
        </div>
        <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-4">
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('settings.mapAlias')}</Label>
            <Input
              value={mapAlias}
              onChange={(e) => setMapAlias(e.target.value)}
              placeholder="gpt-4o-mini"
              className="bg-background"
              disabled={!isAdmin}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('settings.mapTarget')}</Label>
            <Select value={mapTarget} onValueChange={setMapTarget} disabled={!isAdmin}>
              <SelectTrigger className="bg-background" aria-label={t('settings.mapTarget')}>
                <SelectValue placeholder={targetLoading ? t('settings.loading') : '…'} />
              </SelectTrigger>
              <SelectContent>
                {models.map((m) => (
                  <SelectItem key={m.id} value={m.id}>
                    {m.id}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="sm:col-span-2">
            <Button
              className="rounded-full"
              disabled={!isAdmin || !mapAlias.trim() || !mapTarget}
              onClick={() => {
                void saveModelMap({...modelMap, [mapAlias.trim()]: mapTarget});
                setMapAlias('');
                setMapTarget('');
              }}
            >
              <Plus />
              {t('settings.mapAdd')}
            </Button>
          </div>
        </div>
      </div>

      <div className="overflow-hidden rounded-[20px] bg-muted">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">
                {t('settings.mapAlias')}
              </TableHead>
              <TableHead className="text-[11px] text-muted-foreground">
                {t('settings.mapTarget')}
              </TableHead>
              {isAdmin && (
                <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">
                  {t('settings.colActions')}
                </TableHead>
              )}
            </TableRow>
          </TableHeader>
          <TableBody>
            {Object.entries(modelMap).map(([alias, target]) => (
              <TableRow key={alias} className="border-b border-border/40">
                <TableCell className="pl-4 font-mono text-xs">
                  {/* 兜底那条（键为 `*`）显示成「兜底模型」，别让用户去记星号 */}
                  {alias === '*' ? (
                    <span className="flex items-center gap-1.5">
                      <span className="font-sans text-[11px] font-medium">
                        {t('settings.mapFallbackRow')}
                      </span>
                      <code className="text-muted-foreground">*</code>
                    </span>
                  ) : (
                    alias
                  )}
                </TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">{target}</TableCell>
                {isAdmin && (
                  <TableCell className="pr-4 text-right">
                    <Button
                      variant="ghost"
                      size="icon"
                      className="h-7 w-7 rounded-md text-red-500 hover:text-red-600"
                      onClick={() => {
                        const next = {...modelMap};
                        delete next[alias];
                        void saveModelMap(next);
                      }}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {!Object.keys(modelMap).length && (
          <EmptyState
            icon={Shuffle}
            title={t('settings.mapEmpty')}
            description={t('settings.mapEmptyDesc')}
            className="flex flex-col items-center justify-center py-12 text-center"
          />
        )}
      </div>
    </div>
  );
}
