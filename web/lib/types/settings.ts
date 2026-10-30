/**
 * 设置页各 Tab 共用的类型。
 *
 * 字段口径以 server/routers/{settings,upstreams,system,pgsync}.py 的实际返回为准：
 * 凭据类字段（上游 api_key、PG 密码、GitHub Token）后端只回掩码 / 是否已配置，
 * 前端表单一律「留空 = 不修改」。
 */

/** GET /api/users —— 管理端用户（不含任何口令信息） */
export interface UserItem {
  username: string;
  role: 'admin' | 'viewer';
}

/** POST /api/settings/model-map 的载荷与返回：别名 → 实际模型；`*` 为兜底 */
export type ModelMap = Record<string, string>;

/** GET /api/upstreams 里的一行（api_key 已脱敏，附引用计数） */
export interface UpstreamEndpoint {
  id: number | null;
  name: string;
  base_url: string;
  /** 脱敏形态（****abcd）；空串 = 未配置 */
  api_key: string;
  api_key_set: boolean;
  note: string;
  enabled: boolean;
  /** 默认上游（id=null）来自环境变量，不可编辑 / 删除 */
  is_default: boolean;
  /** 绑定到该上游的网关密钥 / PAT 账号数（删除守卫的前置提示） */
  key_count: number;
  account_count: number;
  created_at?: number | null;
  updated_at?: number | null;
}

/** 新建 / 编辑上游的表单体；编辑时 api_key 留空 = 不修改 */
export interface UpstreamInput {
  name: string;
  base_url: string;
  api_key?: string;
  note?: string;
  enabled?: boolean;
}

/** GET /api/settings/github-token：只回掩码与来源 */
export interface GithubTokenInfo {
  configured: boolean;
  masked: string;
  source: 'env' | 'panel' | '';
}

/** GET /api/settings/github-mirror */
export interface GithubMirrorInfo {
  mirror: string;
  stored: string;
  env_mirror: string;
  source: 'env' | 'panel' | '';
  presets: string[];
}

/** 自动更新配置（PUT 时整体提交，未知字段原样带回） */
export interface AutoUpdateConfig {
  enabled: boolean;
  interval_hours: number;
  window_start: number;
  window_end: number;
  target?: string;
  [key: string]: unknown;
}

/** GET /api/settings/auto-update：配置 + 最近一次巡检结果 + 部署形态说明 */
export interface AutoUpdateInfo extends AutoUpdateConfig {
  state: {
    last_check_at: number;
    last_action: string;
    last_message: string;
    pending_version: string;
  };
  /** 非空 = 当前部署形态不支持一键更新（前端据此禁用开关） */
  unsupported_reason: string;
  server_tz_offset_minutes: number;
}

/** GET /api/system/check-update（只针对面板自身，无上游对象） */
export interface UpdateCheck {
  checked_at: number;
  cached: boolean;
  has_any: boolean;
  manager: {
    current: string;
    latest: string;
    has_update: boolean;
    url: string;
    /** null = 未知（走了免 assets 的回退路径） */
    assets_ready: boolean | null;
    release_notes: string;
    published_at: string;
    error: string;
    /** true = 本次检测失败，展示的是上一次成功结果 */
    stale: boolean;
    stale_at: number;
    repo: string;
  };
}

/** GET /api/system/update-status（更新进度 + 当前版本；log_tail 仅管理员可见） */
export interface UpdateStatus {
  available: boolean;
  running: boolean;
  ok: boolean | null;
  target?: string;
  step: string;
  logs: {ts?: number; level?: string; text: string}[];
  log_tail?: string;
  version: string;
  started_at?: number | null;
  finished_at?: number | null;
  duration?: number;
}

/** GET /api/system/versions */
export interface SystemVersions {
  manager: string;
  repo: string;
}

/** GET /api/system/update-package：present=false 表示还没有上传过 */
export interface UpdatePackageMeta {
  present: boolean;
  name?: string;
  size?: number;
  sha256?: string;
  version?: string;
  expected_sha256?: string;
  uploaded_by?: string;
  uploaded_at?: number;
}

/** GET /api/system/changelog 里单个版本的一节（### 小标题下的条目） */
export interface ChangelogItem {
  text: string;
  /** 0 = 一级条目，>0 = 子条目（缩进展示） */
  level: number;
}

export interface ChangelogVersion {
  version: string;
  date: string;
  unreleased: boolean;
  sections: {title: string; items: ChangelogItem[]}[];
}

export interface Changelog {
  available: boolean;
  error?: string;
  current?: string;
  total?: number;
  truncated?: boolean;
  versions: ChangelogVersion[];
}

/** GET /api/tokens 的一行（不含明文；明文只在创建响应里出现一次） */
export interface ApiToken {
  id: number;
  name: string;
  prefix: string;
  scope: 'readonly' | 'admin';
  enabled: boolean;
  expires_at: number | null;
  created_at: number;
  created_by: string;
  last_used_at: number | null;
  last_used_ip: string;
}

/** POST /api/tokens 的返回：token 是唯一一次明文 */
export interface CreatedApiToken extends ApiToken {
  token: string;
}

/** POST /api/pgsync/test 的返回 */
export interface PgTestResult {
  ok: boolean;
  message: string;
  server_version?: string;
}

/** GET /api/pgsync 的 config（密码已掩码）与任务状态 */
export interface PgSyncConfig {
  enabled: boolean;
  host: string;
  port: number;
  dbname: string;
  user: string;
  password: string;
  sslmode: string;
  interval_minutes: number;
  keep_local_backup: boolean;
  last_export_at: number;
  last_import_at: number;
}

export interface PgSyncStatus {
  running: boolean;
  kind: 'export' | 'import' | '';
  ok: boolean | null;
  step: string;
  percent: number;
  tables_total: number;
  tables_done: number;
  rows: number;
  logs: {ts?: number; level?: string; text: string}[];
  started_at: number;
  finished_at: number;
}

/** POST /api/pgsync/export|import 的返回 */
export interface PgJobResult {
  ok: boolean;
  message: string;
  status?: PgSyncStatus;
}
