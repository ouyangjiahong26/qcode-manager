'use client';

import type {ReactNode} from 'react';
import {Boxes, MessageSquare} from 'lucide-react';
import {usePathname} from 'next/navigation';

import {SectionTabs, type SectionTabItem} from '@/components/common/layout/SectionTabs';
import {BASE_PATH} from '@/lib/base-path';
import {useT} from '@/lib/i18n/provider';
import {SECTION_NAV, sectionNavFromPath, type SectionNavIconKey} from '@/lib/section-nav';

/**
 * 页面内的二级导航。
 *
 * **用法**：页面自己渲染 `<PageSectionTabs />`，**不传任何参数**——「我在哪一节、
 * 当前停在哪一项」全部由 `usePathname()` 推出来。这样页面不可能把 `active`
 * 写错，清单也只有一份（`@/lib/section-nav`）。
 *
 * **为什么每条 Tab 都是 `<Link>`**：见 `SectionTabs` 的说明——它和
 * `ui/tabs.tsx` 的分工就是「地址栏动不动」。
 */
const ICONS: Record<SectionNavIconKey, ReactNode> = {
  models: <Boxes className="h-3.5 w-3.5" />,
  playground: <MessageSquare className="h-3.5 w-3.5" />,
};

export function PageSectionTabs() {
  const t = useT();
  // `usePathname()` 在子路径部署下**带着**部署前缀，所以要把它交给解析函数剥掉
  // （本组件可以 import `BASE_PATH`；`section-nav.ts` 不能，它要保持零依赖）。
  const here = sectionNavFromPath(usePathname(), BASE_PATH);

  // 认不出路径就什么都不渲染。**不要**回落到某一节：那会让一个不属于任何节的
  // 页面凭空长出两条指向别处的 Tab，比不渲染更糟。
  if (!here) return null;

  const items: SectionTabItem[] = SECTION_NAV[here.section].map((item) => ({
    href: item.href,
    label: t(item.labelKey),
    icon: ICONS[item.iconKey],
  }));

  return <SectionTabs items={items} active={here.item.href} />;
}
