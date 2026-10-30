/**
 * 页面内二级导航的清单与路径解析 —— 纯函数，零运行时依赖。
 *
 * 底栏是 8 项收敛入口，「模型 / 测试台」是同一件事的两半：看模型目录与实际
 * 试跑必然要一起做，所以 /playground 不占一级入口，改成 /models 页内的二级
 * 导航（`PageSectionTabs` 渲染的就是本模块的清单）。
 *
 * `href` 是**平级**路径（`/playground` 而不是 `/models/playground`）：旧深链
 * 直接可用，将来真要嵌套只需改这里的 `href`。
 *
 * 匹配是「剥掉部署前缀后**精确匹配**」，而不是后缀匹配：`/settings/models`
 * 以 `/models` 结尾，后缀匹配会把设置页认成「模型」节，凭空长出两条 Tab。
 * 前缀由调用方传进来（必填，TypeScript 会拦住漏传；本模块不能自己 import
 * `@/lib/base-path`，那就不是零依赖了）。
 */

/** 图标标识。名字与 `PageSectionTabs` 里的映射表一一对应。 */
export type SectionNavIconKey = 'models' | 'playground';

export type SectionNavItem = {
  /** 稳定标识，与 `href` 一一对应。测试与调试用。 */
  key: string;
  /** 站内绝对路径，**不带** basePath（`Link` 自己会加）。 */
  href: string;
  /** 标签文案的 i18n 键。复用底栏的 `nav.*`。 */
  labelKey: string;
  iconKey: SectionNavIconKey;
};

/** 有二级导航的「节」。顺序即 `sectionNavFromPath` 的匹配顺序。 */
export const SECTIONS = ['models'] as const;

export type SectionKey = (typeof SECTIONS)[number];

/**
 * 每一节包含哪些页。
 *
 * 每节**第一项是该节的落点**（底栏点进去就是它）。
 */
export const SECTION_NAV: Record<SectionKey, readonly SectionNavItem[]> = {
  models: [
    {key: 'models', href: '/models', labelKey: 'nav.models', iconKey: 'models'},
    {key: 'playground', href: '/playground', labelKey: 'nav.playground', iconKey: 'playground'},
  ],
};

export function isSectionKey(value: unknown): value is SectionKey {
  return typeof value === 'string' && (SECTIONS as readonly string[]).includes(value);
}

/** 该节的所有项（顺序即渲染顺序）。 */
export function sectionNavItems(section: SectionKey): readonly SectionNavItem[] {
  return SECTION_NAV[section];
}

export type SectionNavLocation = {
  section: SectionKey;
  /** 当前路径落在哪一项上（`href` 与路径匹配的那一项）。 */
  item: SectionNavItem;
};

/**
 * 路径 → 「它属于哪一节、停在哪一项」。认不出返回 `null`。
 *
 * 认不出就返回 `null`（而不是回落到第一项）：调用方据此**什么都不渲染**。
 * 回落到第一项会让一个不属于任何节的页面凭空长出两条指向别处的 Tab，
 * 那比不渲染更糟。
 */
export function sectionNavFromPath(
  pathname: string | null | undefined,
  basePath: string,
): SectionNavLocation | null {
  // 去掉尾部斜杠：静态导出模式下 Next 会给路径补尾斜杠
  const raw = (pathname || '').replace(/\/+$/, '');
  const prefix = (basePath || '').replace(/\/+$/, '');

  // 前缀「在场才剥」：不在场时按原样比——根路径部署走的就是这一支。
  // 不需要再判断「前缀后面紧跟 `/`」：精确匹配要求 `path` 恰好等于 `href`，
  // 而 `href` 一律以 `/` 开头，前缀后面不是 `/` 的路径剥完必然不匹配。
  const path = prefix && raw.startsWith(prefix) ? raw.slice(prefix.length) : raw;

  if (!path) return null;

  for (const section of SECTIONS) {
    for (const item of SECTION_NAV[section]) {
      if (path === item.href) {
        return {section, item};
      }
    }
  }
  return null;
}
