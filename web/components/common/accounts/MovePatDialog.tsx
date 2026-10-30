'use client';

/**
 * 「移动分组」弹窗：PATCH upstream_id（0 = 默认分组）。
 * 候选排除当前所在分组；默认行只在当前不是默认分组时可选。
 */
import {useEffect, useState} from 'react';
import {Loader2} from 'lucide-react';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {accountsApi, errText} from '@/lib/api/accounts';
import type {Account} from '@/lib/types/accounts';
import type {Upstream} from '@/lib/api/upstreams';
import {Button} from '@/components/ui/button';
import {Label} from '@/components/ui/label';
import {
  AlertDialog,
  AlertDialogBody,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

const DEFAULT_GROUP = '__default__';

export function MovePatDialog({
  account,
  upstreams,
  open,
  onOpenChange,
  onMoved,
}: {
  account: Account | null;
  /** 全部分组（默认分组由本弹窗自己出选项，清单里只放具名分组） */
  upstreams: Upstream[];
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onMoved: () => void;
}) {
  const t = useT();
  const [target, setTarget] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setTarget('');
      setBusy(false);
    }
  }, [open, account?.id]);

  async function submit() {
    if (!account || busy || !target) return;
    setBusy(true);
    try {
      await accountsApi.update(account.id, {
        upstream_id: target === DEFAULT_GROUP ? 0 : Number(target),
      });
      notify.ok(t('accounts.moved', {name: account.label}));
      onOpenChange(false);
      onMoved();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  // 候选：排除当前所在分组；默认行只在当前不是默认分组时可选
  const isDefault = account?.upstream_id == null;
  const candidates = upstreams.filter((u) => u.id != null && u.id !== account?.upstream_id);

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="max-w-[400px]">
        <AlertDialogHeader>
          <AlertDialogTitle>{t('accounts.moveTitle', {name: account?.label ?? ''})}</AlertDialogTitle>
          <AlertDialogDescription>{t('accounts.moveDesc')}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogBody>
          <div className="px-6 pb-2">
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">
                {t('accounts.moveTargetLabel')}
              </Label>
              <Select value={target} onValueChange={setTarget}>
                <SelectTrigger className="h-9 w-full text-xs" aria-label={t('accounts.moveTargetLabel')}>
                  <SelectValue placeholder={t('accounts.movePick')} />
                </SelectTrigger>
                <SelectContent>
                  {!isDefault && (
                    <SelectItem value={DEFAULT_GROUP}>{t('accounts.groupDefault')}</SelectItem>
                  )}
                  {candidates.map((u) => (
                    <SelectItem key={u.id} value={String(u.id)}>{u.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
        </AlertDialogBody>
        <AlertDialogFooter>
          <Button variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button disabled={busy || !target} onClick={submit}>
            {busy && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
            {t('accounts.moveConfirm')}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
