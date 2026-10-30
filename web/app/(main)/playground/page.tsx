'use client';

import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {
  Bot,
  Coins,
  Eraser,
  Info,
  Loader2,
  RefreshCw,
  User as UserIcon,
} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {PageSectionTabs} from '@/components/common/layout/PageSectionTabs';
import {LoadError} from '@/components/common/states/LoadError';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Skeleton} from '@/components/ui/skeleton';
import {AiChatInput, type ChatModelOption} from '@/components/ui/ai-chat-input';
import {CopyButton} from '@/components/ui/copy-button';
import {playgroundApi} from '@/lib/api/playground';
import {errText} from '@/lib/api/http';
import {useAsyncAll} from '@/lib/use-async-data';
import {withBasePath} from '@/lib/base-path';
import {useAuth} from '@/lib/auth-context';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';

/**
 * 模型列表的占位。
 *
 * 必须是**模块级常量**而不是每次渲染现写的 `[]`：下面用 `useEffect` 监听
 * `models` 来重置当前选中的模型，依赖若是每帧新建的数组，那个 effect 会
 * 每帧触发一次，选中项会被反复重置成第一个。
 */
const NO_MODELS: ChatModelOption[] = [];

interface Msg {
  role: 'user' | 'assistant';
  content: string;
  /** 本条回答的实测消耗（上游 usage 里的 token 数） */
  tokens?: number;
  /** 是否已收尾（用于显示光标 / 复制按钮） */
  done?: boolean;
  error?: boolean;
}

export default function PlaygroundPage() {
  const t = useT();
  const {isAdmin} = useAuth();
  /**
   * 模型列表。走统一的取数状态：首屏还没拿到之前显示骨架，而不是先写一句
   * 「暂无可用模型」——那句话在数据还在路上时是错的。
   */
  const {
    values,
    errors,
    isInitialLoading,
    isInitialFailed,
    isRefreshing,
    reload: reloadModels,
  } = useAsyncAll({
    models: async () => (await playgroundApi.models()).models ?? [],
  });
  const models: ChatModelOption[] = values.models ?? NO_MODELS;

  const [model, setModel] = useState('');
  const [effort, setEffort] = useState('');
  const [input, setInput] = useState('');
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [streaming, setStreaming] = useState(false);
  /** 本次会话累计 token 消耗，实时显示在右下角 */
  const [sessionTokens, setSessionTokens] = useState(0);

  const abortRef = useRef<AbortController | null>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const pinToBottom = useRef(true);

  // 模型列表变了就重置选择：列表刷新后旧模型可能不在新列表里，直接选第一个，
  // 避免发出去被上游拒。`NO_MODELS` 是模块级常量，首屏没数据时不会反复触发。
  useEffect(() => {
    setModel(models[0]?.id || '');
    setEffort('');
  }, [models]);

  // 自动滚到底部，但用户主动向上翻看时不要抢滚动位置
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el || !pinToBottom.current) return;
    el.scrollTop = el.scrollHeight;
  }, [msgs]);

  const onScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    pinToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || streaming) return;
    setInput('');
    pinToBottom.current = true;

    const nextMsgs: Msg[] = [...msgs, {role: 'user', content: text}];
    setMsgs([...nextMsgs, {role: 'assistant', content: '', done: false}]);
    setStreaming(true);

    const ac = new AbortController();
    abortRef.current = ac;
    try {
      // 流式对话必须用原生 fetch（axios 拿不到 ReadableStream），因此不走
      // axios 的 baseURL，要自己补 basePath。
      const res = await fetch(withBasePath('/api/playground/chat'), {
        method: 'POST',
        credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        signal: ac.signal,
        body: JSON.stringify({
          model,
          messages: nextMsgs.map((m) => ({role: m.role, content: m.content})),
          reasoning_effort: effort,
          stream: true,
        }),
      });
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          const j = await res.json();
          detail = j.detail || j.error?.message || detail;
        } catch {
          /* 响应不是 JSON，保留状态码 */
        }
        setMsgs((cur) => {
          const copy = [...cur];
          copy[copy.length - 1] = {role: 'assistant', content: detail, error: true, done: true};
          return copy;
        });
        return;
      }

      const reader = res.body?.getReader();
      if (!reader) throw new Error(t('playground.unreadable'));
      const decoder = new TextDecoder();
      let buf = '';
      let answer = '';
      let tokens = 0;

      // 手动解析 SSE：按行拆，只认 `data:` 行；逐段把增量追加到气泡上
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {stream: true});
        let idx: number;
        while ((idx = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 1);
          if (!line.startsWith('data:')) continue;
          const payload = line.slice(5).trim();
          if (!payload || payload === '[DONE]') continue;
          try {
            const obj = JSON.parse(payload);
            const delta = obj?.choices?.[0]?.delta?.content;
            if (typeof delta === 'string' && delta) {
              answer += delta;
              setMsgs((cur) => {
                const copy = [...cur];
                copy[copy.length - 1] = {...copy[copy.length - 1], content: answer, done: false};
                return copy;
              });
            }
            // 末帧（stream_options.include_usage）带真实消耗
            if (obj?.usage) {
              tokens = (obj.usage.prompt_tokens || 0) + (obj.usage.completion_tokens || 0);
            }
          } catch {
            /* 单个分片解析失败不影响后续 */
          }
        }
      }

      setMsgs((cur) => {
        const copy = [...cur];
        copy[copy.length - 1] = {
          role: 'assistant',
          content: answer || t('playground.emptyAnswer'),
          tokens,
          done: true,
        };
        return copy;
      });
      if (tokens) setSessionTokens((v) => v + tokens);
    } catch (e) {
      if ((e as Error).name === 'AbortError') {
        setMsgs((cur) => {
          const copy = [...cur];
          const last = copy[copy.length - 1];
          copy[copy.length - 1] = {...last, done: true, content: last.content || t('playground.stopped')};
          return copy;
        });
      } else {
        setMsgs((cur) => {
          const copy = [...cur];
          copy[copy.length - 1] = {
            role: 'assistant',
            content: errText(e),
            error: true,
            done: true,
          };
          return copy;
        });
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [t, input, msgs, model, effort, streaming]);

  const stop = () => abortRef.current?.abort();

  const clear = () => {
    if (streaming) return;
    setMsgs([]);
    setSessionTokens(0);
  };

  const currentModel = useMemo(() => models.find((m) => m.id === model), [models, model]);

  const header = (
    <>
      <PageHeader
        title={t('playground.title')}
        description={t('playground.description')}
        actions={
          <>
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              onClick={() => {
                reloadModels();
                notify.info(t('playground.modelsRefreshed'));
              }}
            >
              <RefreshCw />
              {t('playground.refreshModels')}
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              disabled={streaming || msgs.length === 0}
              onClick={clear}
            >
              <Eraser />
              {t('playground.clearChat')}
            </Button>
          </>
        }
      />
      {/* 放进 `header` 而不是两个 return 分支里各写一遍：首屏守卫是早返回，
          漏一处就会出现「模型列表没取到时 Tab 不见了」。 */}
      <PageSectionTabs />
    </>
  );

  /**
   * 首屏还没拿到模型列表：先给骨架，别让输入区写着「暂无可用模型」。
   *
   * 只对管理员生效——非管理员看到的是「需要管理员权限」的说明，模型取没取到
   * 与他的问题无关，拿整页错误态盖掉那句说明反而更糟。
   */
  if (isAdmin && (isInitialFailed || isInitialLoading)) {
    return (
      <div className="flex min-h-0 flex-1 flex-col gap-4 md:gap-6">
        {header}
        {isInitialFailed ? (
          <LoadError variant="page" onRetry={reloadModels} />
        ) : (
          <PlaygroundSkeleton />
        )}
      </div>
    );
  }

  return (
    // flex-1 + min-h-0：让页面占满可用高度，对话区随剩余空间自适应，
    // 不依赖写死的高度差（写死会在底部再加内容时被浮动底栏遮住）。
    <div className="flex min-h-0 flex-1 flex-col gap-4 md:gap-6" aria-busy={isRefreshing}>
      {header}

      {/* 模型列表刷不上的时候说一声，但不把已经能用的对话区顶掉——
          旧列表仍然是对的，只是可能不是最新的。 */}
      {Object.keys(errors).length > 0 && (
        <LoadError message={t('playground.partialFailed')} onRetry={reloadModels} />
      )}

      {!isAdmin ? (
        <div className="flex items-start gap-2.5 rounded-[20px] border border-amber-500/30 bg-amber-500/10 p-4 text-xs">
          <Info className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
          <div className="space-y-1">
            <div className="font-medium">{t('playground.adminRequired')}</div>
            <div className="text-muted-foreground">
              {t('playground.adminRequiredDesc')}
            </div>
          </div>
        </div>
      ) : (
        <>
          {/* 对话区。不加卡片底色：消息直接落在页面背景上更清爽，
              也让输入框成为视觉焦点。 */}
          <section className="flex min-h-[240px] min-w-0 flex-1 flex-col overflow-hidden">
            <div ref={scrollerRef} onScroll={onScroll} className="scroll-slim min-h-0 flex-1 overflow-y-auto px-1 py-4">
              {msgs.length === 0 ? (
                <div className="flex h-full items-center justify-center">
                  <div className="max-w-md space-y-2 text-center">
                    <Bot className="mx-auto h-7 w-7 text-muted-foreground/60" />
                    <div className="text-sm font-medium">{t('playground.startTitle')}</div>
                    <p className="text-[11px] leading-5 text-muted-foreground">
                      {t('playground.startDesc')}
                    </p>
                    {currentModel && (
                      <p className="text-[11px] text-muted-foreground">
                        {t('playground.currentModel')}{' '}
                        <span className="font-mono">{currentModel.id}</span>
                      </p>
                    )}
                  </div>
                </div>
              ) : (
                <div className="mx-auto w-full max-w-3xl space-y-4">
                  {msgs.map((m, i) => (
                    <div
                      key={i}
                      className={cn('flex gap-2.5', m.role === 'user' ? 'justify-end' : 'justify-start')}
                    >
                      {m.role === 'assistant' && (
                        <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-muted">
                          <Bot className="h-3.5 w-3.5 text-muted-foreground" />
                        </div>
                      )}
                      <div className={cn('min-w-0 max-w-[82%]', m.role === 'user' && 'flex flex-col items-end')}>
                        <div
                          className={cn(
                            'whitespace-pre-wrap break-words rounded-2xl px-3.5 py-2 text-xs leading-5',
                            m.role === 'user'
                              ? 'bg-foreground text-background'
                              : m.error
                                ? 'border border-red-500/30 bg-red-500/10 text-red-600 dark:text-red-400'
                                // 助手气泡用 muted 底：对话区没有灰色卡片，
                                // 若仍是 bg-background 就会与页面同色而「隐形」
                                : 'bg-muted',
                          )}
                        >
                          {m.content || <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                          {m.role === 'assistant' && !m.done && m.content && (
                            <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-foreground/50 align-middle" />
                          )}
                        </div>

                        {/* 回答下方：复制与实测消耗 */}
                        {m.role === 'assistant' && m.done && !m.error && (
                          <div className="mt-1.5 flex items-center gap-2.5 px-1 text-muted-foreground">
                            <CopyButton
                              value={m.content}
                              label=""
                              title={t('playground.copyAnswer')}
                              className="h-6 w-6"
                            />
                            {!!m.tokens && (
                              <span
                                className="inline-flex items-center gap-1 text-[10px] tabular-nums"
                                title={t('playground.tokensTitle')}
                              >
                                <Coins className="h-3 w-3" />
                                {t('playground.tokens', {n: m.tokens})}
                              </span>
                            )}
                          </div>
                        )}
                      </div>
                      {m.role === 'user' && (
                        <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-muted">
                          <UserIcon className="h-3.5 w-3.5 text-muted-foreground" />
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>

            {/* 输入区 + 右下角实时累计消耗 */}
            <div className="shrink-0 px-3 pb-3 pt-2.5">
              <div className="mx-auto w-full max-w-3xl">
                <AiChatInput
                  value={input}
                  onChange={setInput}
                  onSend={send}
                  onStop={stop}
                  streaming={streaming}
                  disabled={!isAdmin}
                  models={models}
                  model={model}
                  onModelChange={setModel}
                  effort={effort}
                  onEffortChange={setEffort}
                />
                <div className="mt-1.5 flex items-center justify-between gap-2 px-1">
                  <span className="truncate text-[11px] text-muted-foreground">
                    {models.length > 0
                      ? t('playground.modelsAvailable', {count: models.length, n: models.length})
                      : t('playground.noModels')}
                  </span>
                  <Badge
                    variant="secondary"
                    className="shrink-0 rounded-full text-[10px] tabular-nums"
                    title={t('playground.sessionTokensTitle')}
                  >
                    <Coins className="mr-1 h-3 w-3" />
                    {t('playground.sessionTokens', {n: sessionTokens})}
                  </Badge>
                </div>
              </div>
            </div>
          </section>

          {/* 居中窄栏的注脚：通栏卡片在宽屏下是一条横，既占地方又不好读 */}
          <p className="mx-auto max-w-lg shrink-0 text-center text-[11px] leading-5 text-muted-foreground">
            {t('playground.footnote1')}
            <span className="mx-1.5 text-muted-foreground/40">·</span>
            {t('playground.footnote2')}
          </p>
        </>
      )}
    </div>
  );
}

/**
 * 首屏骨架。形状对着真实版面来：一块撑满的对话区 + 底部一条输入框。
 * 对话区用 `flex-1` 与真实布局一致——写死高度的话数据到位时整页会跳一下。
 */
function PlaygroundSkeleton() {
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      <section className="flex min-h-[240px] min-w-0 flex-1 flex-col overflow-hidden">
        <div className="flex flex-1 items-center justify-center">
          <div className="w-full max-w-md space-y-3 px-4">
            <Skeleton className="mx-auto h-7 w-7 rounded-full" />
            <Skeleton className="mx-auto h-4 w-40" />
            <Skeleton className="mx-auto h-3 w-64" />
          </div>
        </div>
      </section>
      <div className="shrink-0 px-3 pb-3 pt-2.5">
        <div className="mx-auto w-full max-w-3xl space-y-2">
          <Skeleton className="h-11 w-full rounded-[16px]" />
          <Skeleton className="h-3 w-32" />
        </div>
      </div>
    </div>
  );
}
