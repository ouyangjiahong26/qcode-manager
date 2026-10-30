'use client';

/**
 * 添加 PAT 弹窗：label + PAT 粘贴 + 分组选择。
 *
 * 后端建账即同步测活，响应携带首次测活结果——成功后**在弹窗里展示**该结果
 * （而不是只弹个 toast 就关窗）：测活失败意味着这张 PAT 录进去也不会被调度，
 * 用户需要当场看到原因再决定改粘贴重试还是关窗。
 */
import {useEffect, useState} from 'react';
import {CheckCircle2, CircleX, Loader2} from 'lucide-react';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {accountsApi, errText} from '@/lib/api/accounts';
import type {CreateAccountResult} from '@/lib/types/accounts';
import type {Upstream} from '@/lib/api/upstreams';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Textarea} from '@/components/ui/textarea';
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

/** 默认分组在 Select 里的值（后端约定：upstream_id 0 = 默认分组） */
const DEFAULT_GROUP = '__default__';

export function AddPatDialog({
  open,
  onOpenChange,
  upstreams,
  defaultUpstreamId,
  onSuccess,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  /** 全部分组（含默认行） */
  upstreams: Upstream[];
  /** 打开时预选的分组（跟随当前 Tab） */
  defaultUpstreamId?: number | null;
  onSuccess?: () => void;
}) {
  const t = useT();
  const [label, setLabel] = useState('');
  const [pat, setPat] = useState('');
  const [group, setGroup] = useState('');
  const [busy, setBusy] = useState(false);
  /** 建账即测的结果；null = 还没提交（或已重置） */
  const [result, setResult] = useState<CreateAccountResult | null>(null);

  // 每次打开都从干净状态开始：上一次粘贴的 PAT 留在输入框里是泄露面。
  useEffect(() => {
    if (open) {
      setLabel('');
      setPat('');
      setGroup(defaultUpstreamId != null ? String(defaultUpstreamId) : DEFAULT_GROUP);
      setResult(null);
      setBusy(false);
    }
  }, [open, defaultUpstreamId]);

  async function submit() {
    if (busy) return;
    if (!label.trim() || !pat.trim()) {
      notify.err(t('accounts.addMissing'));
      return;
    }
    setBusy(true);
    try {
      const upstreamId = group === DEFAULT_GROUP ? 0 : Number(group);
      const created = await accountsApi.create(label.trim(), pat.trim(), upstreamId);
      setResult(created);
      window.dispatchEvent(new Event('qcoder-manager:accounts-changed'));
      notify.ok(
        t('accounts.addDone', {name: created.label}),
        created.test.ok ? created.test.msg : t('accounts.addTestFailed', {msg: created.test.msg}),
      );
      onSuccess?.();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent className="max-w-[420px]">
        <AlertDialogHeader>
          <AlertDialogTitle>{t('accounts.addTitle')}</AlertDialogTitle>
          <AlertDialogDescription>{t('accounts.addDesc')}</AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogBody>
          <div className="space-y-3 px-6 pb-2">
            {result ? (
              /* 建账即测结果：通过 / 失败 + 原因。失败时保留输入便于改粘贴重试。 */
              <div
                className={
                  'flex items-start gap-2 rounded-2xl bg-muted px-3 py-2.5 text-xs ' +
                  (result.test.ok ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400')
                }
              >
                {result.test.ok
                  ? <CheckCircle2 className="mt-px h-4 w-4 shrink-0" />
                  : <CircleX className="mt-px h-4 w-4 shrink-0" />}
                <div className="min-w-0">
                  <p className="font-medium">
                    {result.test.ok ? t('accounts.testPass') : t('accounts.testFail')}
                  </p>
                  <p className="mt-0.5 break-words text-muted-foreground">{result.test.msg}</p>
                </div>
              </div>
            ) : (
              <>
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">{t('accounts.addLabelName')}</Label>
                  <Input
                    value={label}
                    onChange={(e) => setLabel(e.target.value)}
                    placeholder={t('accounts.addLabelPlaceholder')}
                    maxLength={100}
                    autoFocus
                  />
                </div>
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">{t('accounts.addLabelPat')}</Label>
                  <Textarea
                    value={pat}
                    onChange={(e) => setPat(e.target.value)}
                    placeholder={t('accounts.addPatPlaceholder')}
                    rows={3}
                    spellCheck={false}
                    autoCapitalize="off"
                    autoComplete="off"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label className="text-[11px] text-muted-foreground">{t('accounts.addLabelGroup')}</Label>
                  <Select value={group} onValueChange={setGroup}>
                    <SelectTrigger className="h-9 w-full text-xs" aria-label={t('accounts.addLabelGroup')}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value={DEFAULT_GROUP}>{t('accounts.groupDefault')}</SelectItem>
                      {upstreams
                        .filter((u) => u.id != null)
                        .map((u) => (
                          <SelectItem key={u.id} value={String(u.id)}>{u.name}</SelectItem>
                        ))}
                    </SelectContent>
                  </Select>
                </div>
              </>
            )}
          </div>
        </AlertDialogBody>
        <AlertDialogFooter>
          {result ? (
            <>
              <Button variant="outline" onClick={() => onOpenChange(false)}>
                {t('accounts.addClose')}
              </Button>
              <Button onClick={() => {
                setLabel('');
                setPat('');
                setResult(null);
              }}>
                {t('accounts.addAnother')}
              </Button>
            </>
          ) : (
            <>
              <Button variant="outline" disabled={busy} onClick={() => onOpenChange(false)}>
                {t('common.cancel')}
              </Button>
              <Button disabled={busy} onClick={submit}>
                {busy && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
                {t('accounts.addSubmit')}
              </Button>
            </>
          )}
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
