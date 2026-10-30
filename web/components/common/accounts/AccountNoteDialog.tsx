'use client';

/**
 * 账号备注编辑弹窗：PATCH note 字段。
 * 每次打开以该账号当前备注为初值——否则会拿上一个账号的内容去保存，把备注写串。
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

export function AccountNoteDialog({
  account,
  open,
  onOpenChange,
  onSaved,
}: {
  /** null = 弹窗关闭态 */
  account: Account | null;
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onSaved: () => void;
}) {
  const t = useT();
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setNote(account?.note ?? '');
      setBusy(false);
    }
  }, [open, account?.id, account?.note]);

  async function save() {
    if (!account || busy) return;
    setBusy(true);
    try {
      await accountsApi.update(account.id, {note: note.trim()});
      notify.ok(t('accounts.noteSaved'));
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
          <AlertDialogTitle>{t('accounts.noteTitle')}</AlertDialogTitle>
          <AlertDialogDescription>
            {t('accounts.noteDesc', {name: account?.label ?? ''})}
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogBody>
          <div className="px-6 pb-2">
            <Input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={t('accounts.notePlaceholder')}
              maxLength={200}
              autoFocus
              onKeyDown={(e) => {
                if (e.key === 'Enter') void save();
              }}
            />
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
