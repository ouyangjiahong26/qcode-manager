'use client';

import {usePathname} from 'next/navigation';
import {
  Database,
  DownloadCloud,
  FileText,
  Info,
  KeyRound,
  Server,
  Shuffle,
  Users,
} from 'lucide-react';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {SectionTabs, type SectionTabItem} from '@/components/common/layout/SectionTabs';
import {useT} from '@/lib/i18n/provider';
import {
  DEFAULT_SETTINGS_TAB,
  SETTINGS_TABS,
  SETTINGS_TAB_LABEL_KEYS,
  settingsTabFromPath,
  settingsTabHref,
  type SettingsTab,
} from '@/lib/settings-tabs';
import {UpstreamEndpoints} from '@/components/settings/UpstreamEndpoints';
import {ModelMapPanel} from '@/components/settings/ModelMapPanel';
import {UsersPanel} from '@/components/settings/UsersPanel';
import {TokensPanel} from '@/components/settings/TokensPanel';
import {UpdatePanel} from '@/components/settings/UpdatePanel';
import {PgSyncPanel} from '@/components/settings/PgSyncPanel';
import {ChangelogPanel} from '@/components/settings/ChangelogPanel';
import {AboutCard} from '@/components/settings/AboutCard';

/**
 * 设置页外壳：页头 + 8 个 Tab 的路由导航 + 当前 Tab 的内容面板。
 *
 * 内容承载在 layout 而不是各子路由 page：切换 Tab 只是换 URL（子路由
 * page.tsx return null），面板实例随路由挂载 / 卸载，各面板自己取数、
 * 自己管理三态——外壳不掺和数据。Tab 清单、顺序、标签键全部来自
 * lib/settings-tabs（命令面板与后端路由测试用的是同一份）。
 */

/** Tab 图表（标签键在 settings-tabs 里，这里是 JSX，进不了零依赖模块） */
const TAB_ICONS: Record<SettingsTab, React.ReactNode> = {
  upstream: <Server className="h-3.5 w-3.5" />,
  models: <Shuffle className="h-3.5 w-3.5" />,
  users: <Users className="h-3.5 w-3.5" />,
  tokens: <KeyRound className="h-3.5 w-3.5" />,
  system: <DownloadCloud className="h-3.5 w-3.5" />,
  backup: <Database className="h-3.5 w-3.5" />,
  changelog: <FileText className="h-3.5 w-3.5" />,
  about: <Info className="h-3.5 w-3.5" />,
};

/** 当前 Tab 渲染哪个面板。新增 Tab 时在这里补一行即可 */
function SettingsPanel({tab}: {tab: SettingsTab}) {
  switch (tab) {
    case 'upstream':
      return <UpstreamEndpoints />;
    case 'models':
      return <ModelMapPanel />;
    case 'users':
      return <UsersPanel />;
    case 'tokens':
      return <TokensPanel />;
    case 'system':
      return <UpdatePanel />;
    case 'backup':
      return <PgSyncPanel />;
    case 'changelog':
      return <ChangelogPanel />;
    case 'about':
      return <AboutCard />;
  }
}

export default function SettingsLayout({children}: {children: React.ReactNode}) {
  const t = useT();
  const pathname = usePathname();
  // 认不出时回落默认 Tab——那只是「换地址那一帧」的兜底（/settings 由
  // page.tsx replace 到规范路径）
  const tab: SettingsTab = settingsTabFromPath(pathname) ?? DEFAULT_SETTINGS_TAB;

  const items: SectionTabItem[] = SETTINGS_TABS.map((name) => ({
    href: settingsTabHref(name),
    label: t(SETTINGS_TAB_LABEL_KEYS[name]),
    icon: TAB_ICONS[name],
  }));

  return (
    <div className="space-y-4">
      <PageHeader title={t('settings.title')} description={t('settings.desc')} />
      {/* Tab 多，手机上会撑破容器：允许横向滚动 */}
      <div className="-mx-1 overflow-x-auto px-1 pb-1">
        <SectionTabs items={items} active={settingsTabHref(tab)} />
      </div>
      <SettingsPanel tab={tab} />
      {/* 子路由 page 只 return null（让 URL 可寻址）；照常渲染以守住 layout 约定 */}
      {children}
    </div>
  );
}
