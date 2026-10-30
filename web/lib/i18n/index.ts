/**
 * i18n 内核：字典解析 + 插值 + 复数 + 非 Hook 取值入口。
 *
 * 字典按**域**拆文件（`locales/<locale>/<domain>.json`），本文件负责把它们
 * 深合并成一份完整字典。域清单集中在下面的 import 与 `DOMAINS` 数组里：
 * 新增一个域 = 建五份 json + 这里加 import 与一行清单，漏改会直接在启动时
 * 报 undefined（deepMerge 收不到对象），不会静默缺翻译。
 */
import {DEFAULT_LOCALE, type Locale} from './config';

import zhCNCommon from './locales/zh-CN/common.json';
import zhCNNav from './locales/zh-CN/nav.json';
import zhCNLogin from './locales/zh-CN/login.json';
import zhCNDock from './locales/zh-CN/dock.json';
import zhCNPalette from './locales/zh-CN/palette.json';
import zhCNProfile from './locales/zh-CN/profile.json';
import zhCNTheme from './locales/zh-CN/theme.json';
import zhCNError from './locales/zh-CN/error.json';
import zhCNNotFound from './locales/zh-CN/notFound.json';
import zhCNSettings from './locales/zh-CN/settings.json';
import zhCNState from './locales/zh-CN/state.json';
import zhCNFormat from './locales/zh-CN/format.json';
import zhCNLanguage from './locales/zh-CN/language.json';
import zhCNAccounts from './locales/zh-CN/accounts.json';
import zhCNDashboard from './locales/zh-CN/dashboard.json';
import zhCNKeys from './locales/zh-CN/keys.json';
import zhCNLogs from './locales/zh-CN/logs.json';
import zhCNModels from './locales/zh-CN/models.json';
import zhCNPlayground from './locales/zh-CN/playground.json';

import zhTWCommon from './locales/zh-TW/common.json';
import zhTWNav from './locales/zh-TW/nav.json';
import zhTWLogin from './locales/zh-TW/login.json';
import zhTWDock from './locales/zh-TW/dock.json';
import zhTWPalette from './locales/zh-TW/palette.json';
import zhTWProfile from './locales/zh-TW/profile.json';
import zhTWTheme from './locales/zh-TW/theme.json';
import zhTWError from './locales/zh-TW/error.json';
import zhTWNotFound from './locales/zh-TW/notFound.json';
import zhTWSettings from './locales/zh-TW/settings.json';
import zhTWState from './locales/zh-TW/state.json';
import zhTWFormat from './locales/zh-TW/format.json';
import zhTWLanguage from './locales/zh-TW/language.json';
import zhTWAccounts from './locales/zh-TW/accounts.json';
import zhTWDashboard from './locales/zh-TW/dashboard.json';
import zhTWKeys from './locales/zh-TW/keys.json';
import zhTWLogs from './locales/zh-TW/logs.json';
import zhTWModels from './locales/zh-TW/models.json';
import zhTWPlayground from './locales/zh-TW/playground.json';

import enCommon from './locales/en/common.json';
import enNav from './locales/en/nav.json';
import enLogin from './locales/en/login.json';
import enDock from './locales/en/dock.json';
import enPalette from './locales/en/palette.json';
import enProfile from './locales/en/profile.json';
import enTheme from './locales/en/theme.json';
import enError from './locales/en/error.json';
import enNotFound from './locales/en/notFound.json';
import enSettings from './locales/en/settings.json';
import enState from './locales/en/state.json';
import enFormat from './locales/en/format.json';
import enLanguage from './locales/en/language.json';
import enAccounts from './locales/en/accounts.json';
import enDashboard from './locales/en/dashboard.json';
import enKeys from './locales/en/keys.json';
import enLogs from './locales/en/logs.json';
import enModels from './locales/en/models.json';
import enPlayground from './locales/en/playground.json';

import jaCommon from './locales/ja/common.json';
import jaNav from './locales/ja/nav.json';
import jaLogin from './locales/ja/login.json';
import jaDock from './locales/ja/dock.json';
import jaPalette from './locales/ja/palette.json';
import jaProfile from './locales/ja/profile.json';
import jaTheme from './locales/ja/theme.json';
import jaError from './locales/ja/error.json';
import jaNotFound from './locales/ja/notFound.json';
import jaSettings from './locales/ja/settings.json';
import jaState from './locales/ja/state.json';
import jaFormat from './locales/ja/format.json';
import jaLanguage from './locales/ja/language.json';
import jaAccounts from './locales/ja/accounts.json';
import jaDashboard from './locales/ja/dashboard.json';
import jaKeys from './locales/ja/keys.json';
import jaLogs from './locales/ja/logs.json';
import jaModels from './locales/ja/models.json';
import jaPlayground from './locales/ja/playground.json';

import koCommon from './locales/ko/common.json';
import koNav from './locales/ko/nav.json';
import koLogin from './locales/ko/login.json';
import koDock from './locales/ko/dock.json';
import koPalette from './locales/ko/palette.json';
import koProfile from './locales/ko/profile.json';
import koTheme from './locales/ko/theme.json';
import koError from './locales/ko/error.json';
import koNotFound from './locales/ko/notFound.json';
import koSettings from './locales/ko/settings.json';
import koState from './locales/ko/state.json';
import koFormat from './locales/ko/format.json';
import koLanguage from './locales/ko/language.json';
import koAccounts from './locales/ko/accounts.json';
import koDashboard from './locales/ko/dashboard.json';
import koKeys from './locales/ko/keys.json';
import koLogs from './locales/ko/logs.json';
import koModels from './locales/ko/models.json';
import koPlayground from './locales/ko/playground.json';

/** 域清单：新增域时在此登记（顺序即文件顺序，与上面的 import 一一对应）。 */
export const DOMAINS = [
  'common', 'nav', 'login', 'dock', 'palette', 'profile', 'theme',
  'error', 'notFound', 'settings', 'state', 'format', 'language',
  'accounts', 'dashboard', 'keys', 'logs', 'models', 'playground',
] as const;

type Domain = (typeof DOMAINS)[number];

/** 短语表（gettext 风格）：键是**中文源文**，值是译文。
 * 主要给 `errText` 用：后端错误文案是中文（服务端不做多语言），命中短语表就
 * 换成当前语言，没命中就原样展示。zh-CN 的短语表天然不需要（源语言即译文）。
 */
type PhraseTable = Record<string, string>;

import zhCNPhrases from './locales/zh-CN/phrases.json';
import zhTWPhrases from './locales/zh-TW/phrases.json';
import enPhrases from './locales/en/phrases.json';
import jaPhrases from './locales/ja/phrases.json';
import koPhrases from './locales/ko/phrases.json';

/** 复数形式：键名即 Intl.PluralRules 的分类，other 必备（兜底）。 */
export interface PluralForms {
  zero?: string;
  one?: string;
  two?: string;
  few?: string;
  many?: string;
  other: string;
}

export type MessageValue = string | PluralForms;

export interface Dict {
  [key: string]: MessageValue | Dict;
}

/** 递归合并字典节点：右侧（当前语言）优先，缺的键落到左侧（源语言）。 */
function deepMerge(base: Dict, override: Dict): Dict {
  const out: Dict = {...base};
  for (const [k, v] of Object.entries(override)) {
    const prev = out[k];
    out[k] =
      v && typeof v === 'object' && !('other' in (v as PluralForms)) && prev && typeof prev === 'object'
        ? deepMerge(prev as Dict, v as Dict)
        : v;
  }
  return out;
}

const zhCN: Record<Domain, Dict> = {
  common: zhCNCommon, nav: zhCNNav, login: zhCNLogin, dock: zhCNDock,
  palette: zhCNPalette, profile: zhCNProfile, theme: zhCNTheme, error: zhCNError,
  notFound: zhCNNotFound, settings: zhCNSettings, state: zhCNState,
  format: zhCNFormat, language: zhCNLanguage,
  accounts: zhCNAccounts, dashboard: zhCNDashboard, keys: zhCNKeys,
  logs: zhCNLogs, models: zhCNModels, playground: zhCNPlayground,
};
const zhTW: Record<Domain, Dict> = {
  common: zhTWCommon, nav: zhTWNav, login: zhTWLogin, dock: zhTWDock,
  palette: zhTWPalette, profile: zhTWProfile, theme: zhTWTheme, error: zhTWError,
  notFound: zhTWNotFound, settings: zhTWSettings, state: zhTWState,
  format: zhTWFormat, language: zhTWLanguage,
  accounts: zhTWAccounts, dashboard: zhTWDashboard, keys: zhTWKeys,
  logs: zhTWLogs, models: zhTWModels, playground: zhTWPlayground,
};
const en: Record<Domain, Dict> = {
  common: enCommon, nav: enNav, login: enLogin, dock: enDock,
  palette: enPalette, profile: enProfile, theme: enTheme, error: enError,
  notFound: enNotFound, settings: enSettings, state: enState,
  format: enFormat, language: enLanguage,
  accounts: enAccounts, dashboard: enDashboard, keys: enKeys,
  logs: enLogs, models: enModels, playground: enPlayground,
};
const ja: Record<Domain, Dict> = {
  common: jaCommon, nav: jaNav, login: jaLogin, dock: jaDock,
  palette: jaPalette, profile: jaProfile, theme: jaTheme, error: jaError,
  notFound: jaNotFound, settings: jaSettings, state: jaState,
  format: jaFormat, language: jaLanguage,
  accounts: jaAccounts, dashboard: jaDashboard, keys: jaKeys,
  logs: jaLogs, models: jaModels, playground: jaPlayground,
};
const ko: Record<Domain, Dict> = {
  common: koCommon, nav: koNav, login: koLogin, dock: koDock,
  palette: koPalette, profile: koProfile, theme: koTheme, error: koError,
  notFound: koNotFound, settings: koSettings, state: koState,
  format: koFormat, language: koLanguage,
  accounts: koAccounts, dashboard: koDashboard, keys: koKeys,
  logs: koLogs, models: koModels, playground: koPlayground,
};

/** 各语言的完整字典：域合并 + 源语言兜底。 */
const DICTS: Record<Locale, Dict> = {
  'zh-CN': Object.values(zhCN).reduce<Dict>((acc, d) => deepMerge(acc, d), {}),
  'zh-TW': Object.values(zhTW).reduce<Dict>((acc, d) => deepMerge(acc, d), {}),
  en: Object.values(en).reduce<Dict>((acc, d) => deepMerge(acc, d), {}),
  ja: Object.values(ja).reduce<Dict>((acc, d) => deepMerge(acc, d), {}),
  ko: Object.values(ko).reduce<Dict>((acc, d) => deepMerge(acc, d), {}),
};

const PHRASES: Record<Locale, PhraseTable> = {
  'zh-CN': zhCNPhrases,
  'zh-TW': zhTWPhrases,
  en: enPhrases,
  ja: jaPhrases,
  ko: koPhrases,
};

export type TParams = Record<string, string | number>;

let activeLocale: Locale = DEFAULT_LOCALE;

/** 供 I18nProvider 在渲染期调用，让非 Hook 的 t() 与界面语言保持一致。 */
export function setActiveLocale(locale: Locale): void {
  activeLocale = locale;
}

export function getActiveLocale(): Locale {
  return activeLocale;
}

/** 按 a.b.c 逐层取字典节点；取不到返回 undefined。 */
function lookup(dict: Dict, key: string): MessageValue | undefined {
  let node: Dict | MessageValue | undefined = dict;
  for (const part of key.split('.')) {
    if (!node || typeof node !== 'object' || Array.isArray(node)) return undefined;
    node = (node as Dict)[part];
  }
  return node as MessageValue | undefined;
}

function pluralOf(forms: PluralForms, locale: Locale, count: number | undefined): string {
  const n = count ?? 1;
  const rules = new Intl.PluralRules(locale);
  const tier = rules.select(n);
  return forms[tier as keyof PluralForms] ?? forms.other;
}

/**
 * {name} 占位替换。参数缺失时保留原样（界面上会直接看到花括号占位符，
 * 比默默渲染成空串更容易发现漏传参）。
 */
function interpolate(text: string, params?: TParams): string {
  if (!params) return text;
  return text.replace(/\{(\w+)\}/g, (m, name: string) =>
    name in params ? String(params[name]) : m,
  );
}

/**
 * 取译文。查找顺序：当前语言 → 源语言（简体中文）→ 键名本身。
 * 漏翻时界面显示的是键名（如 accounts.title），一眼能看出问题所在。
 */
export function translate(locale: Locale, key: string, params?: TParams): string {
  const hit = lookup(DICTS[locale], key) ?? lookup(DICTS[DEFAULT_LOCALE], key);
  if (hit === undefined) return key;
  const text = typeof hit === 'string' ? hit : pluralOf(hit, locale, numParam(params));
  return interpolate(text, params);
}

function numParam(params?: TParams): number | undefined {
  const n = params?.count;
  return typeof n === 'number' ? n : undefined;
}

/** 绑定某种语言的翻译函数（I18nProvider 用它构造 t）。 */
export function translator(locale: Locale) {
  return (key: string, params?: TParams) => translate(locale, key, params);
}

/** 非组件环境用的翻译函数：跟随界面当前语言。 */
export function t(key: string, params?: TParams): string {
  return translate(activeLocale, key, params);
}

/** 查短语表；当前语言没有该条时原样返回中文源文（不显示键、也不留空）。 */
export function translatePhrase(locale: Locale, source: string): string {
  const hit = PHRASES[locale]?.[source];
  return typeof hit === 'string' && hit ? hit : source;
}

/** 组件侧：把中文源文翻译成当前语言。 */
export function tp(source: string): string {
  return translatePhrase(activeLocale, source);
}

/** 当前语言的 BCP-47 标签，给 Intl 系列 API 用。 */
export function intlLocale(): string {
  return activeLocale;
}

/** 是否按“无空格分词”排版（中文/日文）：决定按字还是按词拆分动画等。 */
export function isCjkLocale(locale: Locale = activeLocale): boolean {
  return locale.startsWith('zh') || locale === 'ja';
}
