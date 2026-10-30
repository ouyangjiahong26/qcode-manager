'use client';

/**
 * 上游（PAT 池分组）的新增 / 编辑弹窗。
 *
 * 只保留 qoder2api 上游真正有的字段：名称、地址、api_key、备注、启用。账号页的
 * 「添加分组」也复用这份表单，因此放在独立文件而不是内联在设置页里。
 *
 * 编辑时**不回填明文 api_key**（接口只回脱敏值）：留空 = 不修改，与 PATCH 的
 * exclude_unset 语义对齐——绝不能把掩码当明文提交回去。
 */
import {useEffect, useState} from 'react';

import {errText} from '@/lib/api/http';
import {upstreamsApi} from '@/lib/api/settings';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {UpstreamEndpoint} from '@/lib/types/settings';
import {Button} from '@/components/ui/button';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {Textarea} from '@/components/ui/textarea';

interface FormState {
  name: string;
  base_url: string;
  api_key: string;
  note: string;
  enabled: boolean;
}

const emptyForm: FormState = {
  name: '',
  base_url: '',
  api_key: '',
  note: '',
  enabled: true,
};

export function UpstreamFormDialog({
  open,
  onOpenChange,
  editing,
  defaultUpstream,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  /** null = 新建；非空 = 编辑该上游 */
  editing: UpstreamEndpoint | null;
  /** 默认上游那一行：新建时用它的地址给表单带出建议值（同址 = 共用接入点） */
  defaultUpstream?: UpstreamEndpoint | null;
  /** 保存成功后的回调（拿到新建 / 更新后的那一行） */
  onSaved?: (item: UpstreamEndpoint) => void;
}) {
  const t = useT();
  const [form, setForm] = useState<FormState>(emptyForm);
  const [busy, setBusy] = useState(false);

  // 依赖只取字符串：父组件每次刷新列表都会换 item 引用，把地址写进依赖会
  // 在用户填写中途重置表单。
  const defaultBaseUrl = defaultUpstream?.base_url || '';

  useEffect(() => {
    if (!open) return;
    if (editing) {
      setForm({
        name: editing.name,
        base_url: editing.base_url,
        api_key: '', // 不回填明文；留空 = 不修改
        note: editing.note || '',
        enabled: editing.enabled,
      });
    } else {
      setForm({...emptyForm, base_url: defaultBaseUrl});
    }
  }, [open, editing, defaultBaseUrl]);

  async function submit() {
    if (busy) return;
    if (!form.name.trim() || !form.base_url.trim()) {
      notify.err(t('settings.upNameUrlRequired'));
      return;
    }
    setBusy(true);
    try {
      const saved = editing
        ? // 编辑时 api_key 留空就不带该字段（PATCH 语义：不改）
          await upstreamsApi.update(editing.id as number, {
            name: form.name.trim(),
            base_url: form.base_url.trim(),
            ...(form.api_key.trim() ? {api_key: form.api_key.trim()} : {}),
            note: form.note,
            enabled: form.enabled,
          })
        : await upstreamsApi.create({
            name: form.name.trim(),
            base_url: form.base_url.trim(),
            api_key: form.api_key.trim(),
            note: form.note,
            enabled: form.enabled,
          });
      notify.ok(editing ? t('settings.upUpdated') : t('settings.upCreated'));
      onOpenChange(false);
      onSaved?.(saved);
    } catch (e) {
      // 校验失败（地址格式、重名等）后端回 400 并说明原因——原样弹出
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[440px]" showCloseButton>
        <DialogHeader>
          <DialogTitle>
            {editing ? t('settings.upEditTitle') : t('settings.upAddTitle')}
          </DialogTitle>
          <DialogDescription>{t('settings.upFormHint')}</DialogDescription>
        </DialogHeader>
        <DialogBody className="max-h-[min(640px,calc(100dvh-12rem))]">
          <div className="space-y-4 px-6 pb-2">
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">
                {t('settings.upFieldName')}
              </Label>
              <Input
                value={form.name}
                onChange={(e) => setForm({...form, name: e.target.value})}
                placeholder={t('settings.upFieldNamePlaceholder')}
                maxLength={64}
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">
                {t('settings.upFieldUrl')}
              </Label>
              <Input
                value={form.base_url}
                onChange={(e) => setForm({...form, base_url: e.target.value})}
                placeholder="http://127.0.0.1:8963"
                maxLength={500}
              />
              <p className="text-[11px] leading-4 text-muted-foreground">
                {t('settings.upFieldUrlHint')}
              </p>
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">
                {t('settings.upFieldKey')}
              </Label>
              <Input
                value={form.api_key}
                onChange={(e) => setForm({...form, api_key: e.target.value})}
                placeholder={
                  editing && editing.api_key_set
                    ? t('settings.upFieldKeyKeep', {masked: editing.api_key})
                    : t('settings.upFieldKeyPlaceholder')
                }
                maxLength={500}
              />
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">
                {t('settings.upFieldNote')}
              </Label>
              <Textarea
                value={form.note}
                onChange={(e) => setForm({...form, note: e.target.value})}
                maxLength={200}
                rows={2}
              />
            </div>
            <div className="flex items-center justify-between">
              <Label className="text-[11px] text-muted-foreground">
                {t('settings.upFieldEnabled')}
              </Label>
              <Switch
                checked={form.enabled}
                onCheckedChange={(v) => setForm({...form, enabled: v})}
                aria-label={t('settings.upFieldEnabled')}
              />
            </div>
          </div>
        </DialogBody>
        <DialogFooter>
          <Button variant="outline" className="rounded-full" onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button className="rounded-full" disabled={busy} onClick={submit}>
            {t('settings.btnSave')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
