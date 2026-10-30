/** 密钥域类型：与 server/routers/keys.py、server/keysvc.py 的序列化字段一一对应。 */

export interface ApiKey {
  id: number;
  name: string | null;
  prefix: string;
  enabled: boolean;
  /** 秒级时间戳；null = 永不过期 */
  expires_at: number | null;
  /** 独立 IP 数上限；0 = 不限 */
  max_ips: number;
  ip_allowlist: string[];
  /** 模型白名单；空 = 不限 */
  models: string[];
  /** 绑定的上游；null = 默认上游（默认上游不是库里的一行，保持 null） */
  upstream_id: number | null;
  /** token 额度；0 = 不限 */
  quota: number;
  used_tokens: number;
  created_at: number | null;
  last_used_at: number | null;
}

/** 新建密钥的请求体（KeyIn） */
export interface KeyCreateInput {
  name: string;
  upstream_id?: number | null;
  expires_at?: number | null;
  max_ips?: number;
  ip_allowlist?: string[];
  models?: string[];
  quota?: number;
}

/** 编辑密钥的请求体（KeyPatch）：只提交要改的字段，显式 null 有语义（如改回默认上游） */
export interface KeyPatchInput {
  name?: string;
  upstream_id?: number | null;
  enabled?: boolean;
  expires_at?: number | null;
  max_ips?: number;
  ip_allowlist?: string[];
  models?: string[];
  quota?: number;
}

/** POST /api/keys 的响应：仅在创建那一刻携带明文（qk_ 前缀），库里只存哈希 */
export interface KeyCreated extends ApiKey {
  key?: string;
}

/** check-models 响应：unknown 是「匹配不到已知模型」的名字列表 */
export interface WhitelistCheckResult {
  checked: boolean;
  unknown: string[];
}

/** 上游下拉用的最小形状（GET /api/upstreams 序列化结果的子集） */
export interface UpstreamOption {
  /** null = 默认上游 */
  id: number | null;
  name: string;
  enabled: boolean;
}
