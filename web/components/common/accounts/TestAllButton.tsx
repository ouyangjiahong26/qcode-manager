'use client';

/**
 * 「测活全部」按钮：对当前范围（全部或某个分组）的启用账号批量测活。
 * 后端限并发（池内 5 路），请求可能持续数十秒——按钮转圈期间禁点，
 * 结果以 toast 汇总（N 通过 / M 失败），完成后回调刷新列表。
 */
import {useState} from 'react';
import {Activity, Loader2} from 'lucide-react';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {accountsApi, errText} from '@/lib/api/accounts';
import {Button} from '@/components/ui/button';

export function TestAllButton({
  upstreamId,
  onDone,
  label,
}: {
  /** null/undefined = 全部分组；数值 = 只测该分组 */
  upstreamId?: number | null;
  onDone?: () => void;
  /** 覆盖按钮文案（分组 Tab 下显示「测活本组」） */
  label?: string;
}) {
  const t = useT();
  const [busy, setBusy] = useState(false);

  async function run() {
    if (busy) return;
    setBusy(true);
    try {
      const r = await accountsApi.testAll(upstreamId ?? undefined);
      (r.failed > 0 ? notify.warn : notify.ok)(
        t('accounts.testAllDone'),
        t('accounts.testAllSummary', {ok: r.ok, failed: r.failed}),
      );
      onDone?.();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Button variant="outline" size="sm" disabled={busy} onClick={run}>
      {busy ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Activity className="mr-1 h-3.5 w-3.5" />}
      {label ?? t('accounts.testAll')}
    </Button>
  );
}
