/** 账号池域类型：字段以 server/accountsvc.py 的 _PUBLIC_COLUMNS 为准。 */

/** 账号测活状态：active = 测活通过，invalid = 失效，unknown = 尚未测过 */
export type AccountStatus = 'active' | 'invalid' | 'unknown';

export interface Account {
  id: number;
  label: string;
  /** PAT 尾 4 位（掩码形态，后端绝不回明文/密文） */
  pat_suffix: string;
  /** 所属上游分组；null = 默认分组 */
  upstream_id: number | null;
  /** SQLite 整型布尔：1 = 启用，0 = 停用 */
  enabled: number;
  status: AccountStatus;
  /** 最近测活时间（epoch 秒）；null = 从未测过 */
  last_check_at: number | null;
  last_check_ok: number | null;
  last_check_msg: string;
  requests: number;
  total_tokens: number;
  /** 调度权重（≥1，当前存而不用） */
  weight: number;
  note: string;
  created_at: number;
  updated_at: number;
}

/** 列表附带的状态汇总（GET /api/accounts 的 summary 字段） */
export interface AccountsSummary {
  total: number;
  active: number;
  invalid: number;
  unknown: number;
  enabled: number;
}

/** GET /api/accounts 响应 */
export interface AccountsListResponse {
  accounts: Account[];
  summary: AccountsSummary;
}

/** 单账号测活结果（POST /api/accounts/{id}/test） */
export interface TestResult {
  ok: boolean;
  msg: string;
}

/** 批量测活结果（POST /api/accounts/test-all） */
export interface TestAllResult {
  total: number;
  ok: number;
  failed: number;
  /** 每个账号的分项结果 */
  results?: {id: number; ok: boolean; msg: string}[];
}

/** POST /api/accounts 的响应：账号 + 建账即测的首次结果 */
export interface CreateAccountResult extends Account {
  test: TestResult;
}

/** 管理员导出的单条记录（GET /api/accounts/export，含 PAT 明文） */
export interface AccountExportItem {
  id: number;
  label: string;
  pat: string;
  upstream_id: number | null;
  enabled: number;
  status: AccountStatus;
  note: string;
  created_at: number;
}
