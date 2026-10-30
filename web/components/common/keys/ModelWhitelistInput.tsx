'use client';

import {useMemo, useState} from 'react';
import {Check, ChevronsUpDown, Search, X} from 'lucide-react';
import {keysApi} from '@/lib/api/keys';
import {useT} from '@/lib/i18n/provider';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Popover, PopoverContent, PopoverTrigger} from '@/components/ui/popover';
import {cn} from '@/lib/utils';

/**
 * 模型白名单输入：手输为主 + 「从清单里勾」。
 *
 * 为什么保留输入框：清单里暂时没有的模型（新模型、私有部署、还没同步到的上游）
 * 仍然要能填 —— 候选只解决「记不住名字、写错了要等调用失败才发现」，不该变成
 * 新的门槛。
 *
 * 存储形态是逗号/换行分隔的字符串（与库里的存法一致）：勾选 = 往字符串里追加
 * 一个名字，取消 = 移除，后端与既有密钥数据都不用动。
 *
 * 「匹配不到已知模型」的高亮校验由父组件在输入框失焦时触发（走
 * /api/keys/check-models）；勾选进来的名字来自清单，本身不可能写错，不必再查。
 */
export function ModelWhitelistInput({
  value,
  onChange,
  onEdit,
  onBlur,
  placeholder,
  disabled = false,
}: {
  value: string;
  onChange: (next: string) => void;
  /**
   * 正在编辑（新值还没提交到 `value`）。父组件据此作废上一次的校验结论，
   * 避免显示过期的「都对」。
   */
  onEdit?: () => void;
  /** 失焦：参数是**提交后**的白名单字符串（父组件据此校验已知/未知模型） */
  onBlur?: (next: string) => void;
  placeholder?: string;
  disabled?: boolean;
}) {
  const t = useT();
  const names = useMemo(() => splitNames(value), [value]);
  const [draft, setDraft] = useState('');
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  /** 上游模型清单；打开勾选弹层时拉一次，取不到就如实说、不挡手输 */
  const [catalog, setCatalog] = useState<string[] | null>(null);
  const [failed, setFailed] = useState(false);

  /** 勾选/取消：按当前顺序重建字符串（去重，保持用户看到的次序） */
  function emit(next: string[]) {
    onChange(Array.from(new Set(next)).join(', '));
  }

  function add(name: string) {
    const clean = name.trim().replace(/,+$/, '');
    if (!clean) return;
    emit([...names, clean]);
    setDraft('');
  }

  function remove(name: string) {
    emit(names.filter((n) => n !== name));
  }

  async function ensureCatalog() {
    if (catalog || failed) return;
    try {
      setCatalog(await keysApi.listUpstreamModels());
    } catch {
      setFailed(true);
    }
  }

  const needle = query.trim().toLowerCase();
  const filtered = (catalog ?? []).filter((m) => !needle || m.toLowerCase().includes(needle));

  return (
    <div className="space-y-1.5">
      {names.length > 0 && (
        <div className="flex flex-wrap gap-1">
          {names.map((name) => (
            <span
              key={name}
              className="inline-flex items-center gap-1 rounded-full bg-muted px-2 py-0.5 font-mono text-[10px]"
            >
              {name}
              {!disabled && (
                <button
                  type="button"
                  aria-label={t('keys.modelRemove', {name})}
                  onClick={() => remove(name)}
                  className="text-muted-foreground hover:text-foreground"
                >
                  <X className="h-3 w-3" />
                </button>
              )}
            </span>
          ))}
        </div>
      )}
      <div className="flex gap-1.5">
        <Input
          value={draft}
          disabled={disabled}
          placeholder={placeholder}
          onChange={(e) => {
            onEdit?.();            // 值还没提交，先把上一次的校验结论作废
            // 逗号是既有的分隔写法：粘贴一串名字时按它拆开
            if (e.target.value.includes(',')) {
              // 粘贴「a, b, c」时**一次算好再提交**：逐个调用 add() 会各自基于
              // 同一份旧 names 计算，最后只剩最后一个。
              const parts = e.target.value.split(',').map((s) => s.trim()).filter(Boolean);
              if (parts.length) emit([...names, ...parts]);
              setDraft('');
              return;
            }
            setDraft(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              add(draft);
            } else if (e.key === 'Backspace' && !draft && names.length) {
              remove(names[names.length - 1]);
            }
          }}
          onBlur={() => {
            // 把提交后的新值一起给出去：父组件拿它做「这些名字存在吗」的校验，
            // 而它自己的 state 此刻还没更新（读旧值会漏掉刚输入的这一个）。
            const merged = Array.from(new Set([...names, draft.trim()].filter(Boolean)));
            if (draft.trim()) onChange(merged.join(', '));
            setDraft('');
            onBlur?.(merged.join(', '));
          }}
        />
        <Popover
          open={open}
          onOpenChange={(next) => {
            setOpen(next);
            if (next) void ensureCatalog();
          }}
        >
          <PopoverTrigger asChild>
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={disabled}
              className="shrink-0 rounded-full"
            >
              <ChevronsUpDown className="h-3.5 w-3.5" />
              {t('keys.modelPick')}
            </Button>
          </PopoverTrigger>
          <PopoverContent align="end" className="w-[320px] p-2">
            <div className="mb-2 flex items-center gap-1.5 rounded-full bg-muted px-2.5 py-1">
              <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
              <input
                autoFocus
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t('keys.modelPickSearch')}
                className="w-full bg-transparent text-xs outline-none"
              />
            </div>
            <div className="max-h-[260px] overflow-y-auto">
              {failed && (
                <p className="px-2 py-6 text-center text-xs text-muted-foreground">
                  {t('keys.modelPickUnavailable')}
                </p>
              )}
              {!failed && !catalog && (
                <p className="px-2 py-6 text-center text-xs text-muted-foreground">
                  {t('keys.modelPickLoading')}
                </p>
              )}
              {!failed && catalog && filtered.length === 0 && (
                <p className="px-2 py-6 text-center text-xs text-muted-foreground">
                  {t('keys.modelPickEmpty')}
                </p>
              )}
              {filtered.map((m) => {
                const picked = names.includes(m);
                return (
                  <button
                    key={m}
                    type="button"
                    role="checkbox"
                    aria-checked={picked}
                    onClick={() => (picked ? remove(m) : add(m))}
                    className={cn(
                      'flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs',
                      'hover:bg-muted',
                      picked && 'bg-muted/70',
                    )}
                  >
                    <span
                      className={cn(
                        'flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded border',
                        picked ? 'border-primary bg-primary text-primary-foreground' : 'border-border',
                      )}
                    >
                      {picked && <Check className="h-2.5 w-2.5" />}
                    </span>
                    <span className="min-w-0 flex-1 truncate font-mono">{m}</span>
                  </button>
                );
              })}
            </div>
          </PopoverContent>
        </Popover>
      </div>
    </div>
  );
}

function splitNames(value: string): string[] {
  return Array.from(new Set(
    value.split(/[\n,]/).map((s) => s.trim()).filter(Boolean),
  ));
}
