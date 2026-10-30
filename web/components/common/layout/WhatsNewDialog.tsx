'use client';

/**
 * 「本次更新」公告：进入 (main) 后拉一次 changelog，与 localStorage 里记录的
 * 已读版本号比对——版本变化才弹一次。拉取失败（changelog 文件缺失、服务重启中）
 * 一律静默：公告是锦上添花，不能因为它弹错误框。
 */
import {useEffect, useState} from 'react';
import {systemApi} from '@/lib/api/system';
import type {ChangelogVersion} from '@/lib/types/settings';
import {useT} from '@/lib/i18n/provider';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {ScrollArea} from '@/components/ui/scroll-area';

const STORAGE_KEY = 'qcoder-manager:whats-new';

export function WhatsNewDialog() {
  const t = useT();
  const [latest, setLatest] = useState<ChangelogVersion | null>(null);
  const [currentVersion, setCurrentVersion] = useState('');
  const [open, setOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    systemApi.changelog()
      .then((data) => {
        if (cancelled) return;
        // 服务端取不到 changelog（文件缺失 / 正在重启）：静默不弹
        if (!data.available) return;
        const version = data.current ?? data.versions[0]?.version;
        if (!version) return;
        let seen: string | null = null;
        try {
          seen = localStorage.getItem(STORAGE_KEY);
        } catch {
          // localStorage 不可用：当作没看过，弹这一次
        }
        if (seen === version) return;
        setCurrentVersion(version);
        setLatest(data.versions[0] ?? null);
        setOpen(true);
      })
      .catch(() => {
        // 拉取失败静默：版本号没记上，下次进面板还会再试
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function close() {
    setOpen(false);
    // 按「当前运行版本」记账：下次启动版本变了才再弹
    try {
      localStorage.setItem(STORAGE_KEY, currentVersion);
    } catch {
      // 隐私模式等场景写入失败：只是下次还会再弹一次，可接受
    }
  }

  if (!latest) return null;

  return (
    <Dialog open={open} onOpenChange={(v) => {if (!v) close();}}>
      <DialogContent showCloseButton className="max-w-[560px]">
        <DialogHeader>
          <DialogTitle>{t('common.whatsNewTitle')}</DialogTitle>
          <DialogDescription>
            {latest.version} · {latest.date}
          </DialogDescription>
        </DialogHeader>
        <DialogBody>
          <ScrollArea className="max-h-[min(60vh,480px)] px-5 pb-4">
            <div className="space-y-5 text-sm">
              {latest.sections.map((section, i) => (
                <section key={i} className="space-y-1.5">
                  <h3 className="font-medium">{section.title}</h3>
                  <ul className="space-y-1 text-muted-foreground">
                    {section.items.map((item, j) => (
                      <li
                        key={j}
                        className={item.level > 0 ? 'ml-4' : ''}
                        style={item.level > 1 ? {marginLeft: `${item.level}rem`} : undefined}
                      >
                        {item.text}
                      </li>
                    ))}
                  </ul>
                </section>
              ))}
            </div>
          </ScrollArea>
        </DialogBody>
      </DialogContent>
    </Dialog>
  );
}
