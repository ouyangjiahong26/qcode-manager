'use client';

/**
 * 账号权重编辑弹窗：PATCH weight（整数 ≥1）。
 * 权重当前「存而不用」（后端调度先保持纯轮询），弹窗里如实说明，
 * 避免用户改完以为调度立刻变了。
 */
import {useEffect, useState} from 'react';
import {Loader2} from 'lucide-react';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {accountsApi, errText} from '@/lib/api/accounts';
import type {Account} from '@/lib/types/accounts';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {
  AlertDialog,
  AlertDialogBody,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';

export function AccountWeightDialog({
  account,
  open,
  onOpenChange,
  onSaved,
}: {
  account: Account | null;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onSaved: () => void;
}) {
  const t = useT();
  const [value, setValue] = useState('1');
  const [busy, setBusy] = useState(false);

  // 打开时以当前值为初值，避免拿上一个账号的权重保存写串
  useEffect(() => {
    if (open) {
      setValue(String(account?.weight && account.weight > 0 ? account.weight : 1));
      setBusy(false);
    }
  }, [open, account?.id, account?.weight]);

  async function save() {
    if (!account || busy) return;
    const weight = Number(value.trim());
    if (!Number.isInteger(weight) || weight < 1) {
      notify.err(t('accounts.weightInvalid'));
      return;
    }
    setBusy(true);
    try {
      await accountsApi.update(account.id, {weight});
      notify.ok(t('accounts.weightSaved', {weight: String(weight)}));
      onOpenChange(false);
      onSaved();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="max-w-[380px]">
        <AlertDialogHeader>
          <AlertDialogTitle>{t('accounts.weightTitle')}</AlertDialogTitle>
          <AlertDialogDescription>
            {t('accounts.weightDesc', {name: account?.label ?? ''})}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogBody>
          <div className="space-y-2 px-6 pb-2">
            <Input
              value={value}
              onChange={(e) => setValue(e.target.value)}
              inputMode="numeric"
              placeholder="1"
              autoFocus
              onKeyDown={(e) => {
                if (e.key === 'Enter') void save();
              }}
            />
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              {t('accounts.weightHint')}
            </p>
          </div>
        </AlertDialogBody>
        <AlertDialogFooter>
          <Button variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>
            {t('common.cancel')}
          </Button>
          <Button disabled={busy} onClick={save}>
            {busy && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
            {t('common.confirm')}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
