/**
 * 本地化格式化工具。
 *
 * 这些函数是纯函数（非组件），语言取自 lib/i18n 的模块级当前语言，
 * 由 I18nProvider 在渲染期同步——调用它们的组件会随语言切换重渲染。
 * 因此本文件里不要写死任何语言：日期、数字、时长、相对时间都跟随界面语言。
 */
import {intlLocale, t} from '@/lib/i18n';

/** 秒级时长格式化：已过期 / 分钟 / 小时 / 天 */
export function fmtRemain(seconds: number): string {
  if (seconds <= 0) return t('format.expired');
  if (seconds >= 365 * 86400 * 5) return t('format.permanent');
  if (seconds < 60) {
    // 不足一分钟别再向下取整成「0 分钟」：那会被读成「已到期」。
    return t('format.seconds', {count: seconds, n: seconds});
  }
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60);
    return t('format.minutes', {count: minutes, n: minutes});
  }
  // count 传数值（决定英文单复数），n 传展示串（与原实现一致，保留一位小数）
  if (seconds < 86400) {
    const hrs = Math.round((seconds / 3600) * 10) / 10;
    return t('format.hours', {count: hrs, n: hrs.toFixed(1)});
  }
  const ds = Math.round((seconds / 86400) * 10) / 10;
  return t('format.days', {count: ds, n: ds.toFixed(1)});
}

export function fmtDateTime(ts: number | null | undefined): string {
  if (!ts) return '—';
  const ms = ts > 1e12 ? ts : ts * 1000;
  const d = new Date(ms);
  return d.toLocaleString(intlLocale(), {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function fmtDate(ts: number | null | undefined): string {
  if (!ts) return '—';
  const ms = ts > 1e12 ? ts : ts * 1000;
  return new Date(ms).toLocaleDateString(intlLocale());
}

/**
 * 日期时间 + 相对「今天」的标记：`2026/09/20 14:03:11`。
 *
 * 为什么需要（issue #44）：任务页与日志页都有「近 24 小时」这个范围，它**必然跨天**
 * ——这时候列表里今天的 14:03 与昨天的 14:03 长得一模一样，扫一眼分不出哪条是
 * 今天的，只能挨个去数字段里的日期。用户的原话是「翻看 24 小时的时候会翻到前一天
 * 的记录，不方便观察各个账号运行状态」。
 *
 * 所以对**非今天**的行把日期顶到最前面并标注「昨天 / 更早」，今天的行保持简短。
 * 不直接写死「今天」的原因：列表里绝大多数行都是今天的，逐行标一遍只是噪音；
 * 真正需要区分的是那几条**不是今天**的。
 *
 * 跨天（今天/昨天）按**本地日历日**判，不是「距今 24 小时内」——用户看的是日历，
 * 凌晨 1 点看 23 小时前的记录会认为那是「昨天」，按日历判才与直觉一致。
 */
export function fmtDateTimeMarked(ts: number | null | undefined): string {
  if (!ts) return '—';
  const ms = ts > 1e12 ? ts : ts * 1000;
  const d = new Date(ms);
  const full = fmtDateTime(ts);
  const dayDiff = calendarDaysAgo(d);
  if (dayDiff === 0) return full;
  if (dayDiff === 1) return `${t('format.yesterday')} ${full}`;
  return full;
}

/** 目标时间距「今天」的本地日历天数（今天=0，昨天=1，未来=负数）。 */
function calendarDaysAgo(d: Date): number {
  const now = new Date();
  // 用 Date.UTC 把两个本地日期归一到 UTC 零点再相减：这样得到的是**日历天**之差，
  // 不受夏令时（某些时区一天是 23/25 小时）与具体时刻影响。
  const a = Date.UTC(d.getFullYear(), d.getMonth(), d.getDate());
  const b = Date.UTC(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((b - a) / 86400000);
}

/** 千分位数字 */
export function fmtNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return '0';
  return n.toLocaleString(intlLocale());
}

/** 大数紧凑显示：1.2k / 3.4M */
export function fmtCompact(n: number | null | undefined): string {
  if (!n) return '0';
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${(n / 1000).toFixed(n < 10_000 ? 1 : 0)}k`;
  return `${(n / 1_000_000).toFixed(1)}M`;
}

export function fmtLatency(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

/** 相对时间：3 分钟前 */
export function fmtAgo(ts: number | null | undefined): string {
  if (!ts) return t('format.never');
  const ms = ts > 1e12 ? ts : ts * 1000;
  const diff = Date.now() - ms;
  if (diff < 0) return t('format.justNow');
  const s = Math.floor(diff / 1000);
  if (s < 60) return t('format.secondsAgo', {count: s, n: s});
  if (s < 3600) {
    const minutes = Math.floor(s / 60);
    return t('format.minutesAgo', {count: minutes, n: minutes});
  }
  if (s < 86400) {
    const hrs = Math.floor(s / 3600);
    return t('format.hoursAgo', {count: hrs, n: hrs});
  }
  const ds = Math.floor(s / 86400);
  return t('format.daysAgo', {count: ds, n: ds});
}

/**
 * 复制文本到剪贴板，返回**是否真的复制成功**。
 *
 * 为什么必须检查回退路径的返回值（issue #57）：`document.execCommand('copy')`
 * 是**有布尔返回值**的 —— 被浏览器拒绝时返回 `false`（非安全上下文、权限被拒、
 * 不在用户手势之内都会这样）。原先忽略它、直接 `return true`，于是「根本没复制
 * 成功」也会提示「已复制到剪贴板」，用户对着空剪贴板反复点，只能来报「提示成功
 * 但复制不上」。
 *
 * 两条路径的判据都落在「真的写进去了」：
 *   1. 优先 Clipboard API —— 它只在**安全上下文**（HTTPS 或 localhost）可用，
 *      普通 HTTP 访问时 `navigator.clipboard` 是 undefined；
 *   2. 回退到 `execCommand('copy')`，并**检查它的布尔返回值**。
 * 都失败返回 `false`，由调用方如实提示（文案引导手动选择复制）。
 */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* 落到下面的回退路径 */
    }
  }

  const ta = document.createElement('textarea');
  ta.value = text;
  // 只读：避免移动端弹键盘；移出可视区且透明：不闪、不占位
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.top = '0';
  ta.style.left = '-9999px';
  ta.style.opacity = '0';
  document.body.appendChild(ta);

  const sel = document.getSelection();
  const saved = sel && sel.rangeCount > 0 ? sel.getRangeAt(0) : null;
  let ok = false;
  try {
    ta.select();
    ta.setSelectionRange(0, text.length);
    ok = document.execCommand('copy') === true;
  } catch {
    ok = false;
  } finally {
    document.body.removeChild(ta);
    // 恢复用户原来的选区：复制是旁路动作，不该把界面上已有的选择弄丢
    if (saved && sel) {
      sel.removeAllRanges();
      sel.addRange(saved);
    }
  }
  return ok;
}

