/**
 * 设置页的二级 Tab 清单与路径解析。
 *
 * **为什么把它单独放在一个零依赖模块里**：Tab 名字、顺序、默认项、路径形状
 * 四件事被多处同时用到——外壳（渲染导航）、`/settings/<tab>` 的路由目录、
 * 命令面板（⌘K 把每个 Tab 当目的地）。任何一处写死一份副本，都会在下次加
 * Tab 时静默漂移：加了名字却没有目录 → 点进去 404；有了目录却不在清单里 →
 * 导航上看不见。所以只留一份，其余都从这里读。
 *
 * 零依赖是测试的前提：本文件被 Node 直接 import（type stripping），只要有
 * 一个 import 就会把「机器上没装前端依赖」变成测试失败。
 */

/** 与 `TabsTrigger` 的 `value` 一一对应，顺序即导航顺序。 */
export const SETTINGS_TABS = [
  'upstream',
  'models',
  'users',
  'tokens',
  'system',
  // 「数据备份」紧挨「系统更新」：两件事都属于「这台机器上的运维」，
  // 而且都在回答同一个问题——「我的东西会不会丢」。
  'backup',
  'changelog',
  'about',
] as const;

export type SettingsTab = (typeof SETTINGS_TABS)[number];

/** `/settings` 重定向到它。与 `SETTINGS_TABS[0]` 是同一个值，但单独起名——「第一个」是顺序，这个才是「默认」。 */
export const DEFAULT_SETTINGS_TAB: SettingsTab = 'upstream';

export function isSettingsTab(value: unknown): value is SettingsTab {
  return typeof value === 'string' && (SETTINGS_TABS as readonly string[]).includes(value);
}

/** Tab 的可寻址路径。`Link` 会自己处理 basePath 与 export 模式下的尾斜杠。 */
export function settingsTabHref(tab: SettingsTab): string {
  return `/settings/${tab}`;
}

/**
 * 每个 Tab 的**标签键**。
 *
 * 图标仍然留在外壳（那是 JSX，进不来这个零依赖模块）。
 */
export const SETTINGS_TAB_LABEL_KEYS: Record<SettingsTab, string> = {
  upstream: 'settings.tabUpstream',
  models: 'settings.tabModels',
  users: 'settings.tabUsers',
  tokens: 'settings.tabTokens',
  system: 'settings.tabSystem',
  backup: 'settings.tabBackup',
  changelog: 'settings.tabChangelog',
  about: 'settings.tabAbout',
};

/**
 * 从 `usePathname()` 的值里解析出当前 Tab。
 *
 * **不 import `BASE_PATH` 的原因**（子路径部署下这条路必须仍然成立）：
 * 模块要保持零依赖，所以判据改成**匹配后缀**：`/settings/models` 与
 * `/qcoder-manager/settings/models` 都能认出来，与是否带前缀无关。
 *
 * 返回 `null` 表示「不在设置页 / 没有子路径」——调用方据此回落到默认 Tab。
 */
export function settingsTabFromPath(pathname: string | null | undefined): SettingsTab | null {
  // export 模式开了 trailingSlash，客户端拿到的可能是 `/settings/users/`
  const path = (pathname || '').replace(/\/+$/, '');
  const matched = /(?:^|\/)settings\/([^/]+)$/.exec(path);
  if (!matched) return null;
  return isSettingsTab(matched[1]) ? matched[1] : null;
}
