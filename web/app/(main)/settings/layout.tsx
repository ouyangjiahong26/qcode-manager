'use client';

import {useEffect} from 'react';
import {usePathname, useRouter} from 'next/navigation';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {SectionTabs} from '@/components/common/layout/SectionTabs';
import {useT} from '@/lib/i18n/provider';
import {
  DEFAULT_SETTINGS_TAB,
  SETTINGS_TABS,
  SETTINGS_TAB_LABEL_KEYS,
  settingsTabFromPath,
  isSettingsTab,
  type SettingsTab,
} from '@/lib/settings-tabs';

/**
 * 设置页外壳骨架：渲染页头与 8 个 Tab 的导航；各 Tab 的实际内容面板由
 * settings 片在本布局里补齐（子路由 page.tsx 只负责 URL 可寻址，return null）。
 */
export default function SettingsLayout({children}: {children: React.ReactNode}) {
  const t = useT();
  const router = useRouter();
  const pathname = usePathname();
  const active = settingsTabFromPath(pathname) ?? DEFAULT_SETTINGS_TAB;

  // /settings（无子路径）替换到默认 Tab，让地址栏始终落在规范路径上。
  useEffect(() => {
    if (isSettingsTab(settingsTabFromPath(pathname))) return;
    router.replace(`/settings/${DEFAULT_SETTINGS_TAB}`);
  }, [pathname, router]);

  const items = SETTINGS_TABS.map((tab: SettingsTab) => ({
    href: `/settings/${tab}`,
    label: t(SETTINGS_TAB_LABEL_KEYS[tab]),
  }));

  return (
    <div className="space-y-4">
      <PageHeader title={t('settings.title')} description={t('settings.desc')} />
      <SectionTabs items={items} active={`/settings/${active}`} />
      {children}
    </div>
  );
}
