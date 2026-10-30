# CONTEXT.md

qcode-manager 是面向 [fengyinxia/qoder2api](https://github.com/fengyinxia/qoder2api) 的 Web 管理控制台与 OpenAI 兼容网关，架构仿照 workbuddy-manager（位于 `~/codes/workbuddy-manager`，称源仓库）。

## 系统边界

```
下游客户端（OpenAI SDK / Claude Code）
        │  Authorization: Bearer qk_xxx（网关密钥）
        ▼
┌────────────────────────────────────┐
│ qcode-manager            :7865     │
│ 网关 /v1/*（鉴权→IP管控→模型映射   │
│ →PAT 轮询→流式转发→日志用量落库）   │
│ 管理面 /api/*（登录/账号/密钥/日志）│
│ Next.js 静态导出托管               │
└──────────────┬─────────────────────┘
               │ Authorization: Bearer pt-xxx（PAT，由面板注入）
               ▼
┌────────────────────────────────────┐
│ qoder2api（Python，不改一行）:8963  │
│ OpenAI↔QoderWork 协议桥，无状态     │
└──────────────┬─────────────────────┘
               ▼
        Qoder 网关（qoder.com.cn）
```

## 核心术语

| 术语 | 含义 |
|---|---|
| PAT | Personal Access Token，`pt-` 开头的 Qoder 个人访问令牌，从 qoder.cn/account/integrations 创建。面板侧的「账号」就是一条 PAT |
| 网关密钥 | `qk_` 开头，面板签发给下游的 OpenAI 兼容密钥，库中只存 SHA-256 |
| API Token | `qct_` 开头，管理面 `/api/*` 的脚本令牌，scope 分 readonly/admin |
| 账号 | accounts 表中的一条 PAT 记录（label、加密 PAT、状态、用量） |
| 保活 | 定时带 PAT 调上游 `/v1/models` 测活并标记状态，是唯一保留的定时任务类型 |
| 上游 | 一个 qoder2api 实例（base_url + 可选 api_key），多上游即多个账号池分组 |

## 与源仓库的关键差异

账号池所有权反转：workbuddy2api 自管账号（auths/*.json、/status、签到），面板只做可视化；qoder2api 无状态，PAT 由请求携带，账号池（存储、测活、轮询调度）完全由本面板实现。详见 `docs/adr/0002-qoder-differences.md`。

## 约定

- 环境变量前缀 `QC_`（对应源仓库 `WB_`），密钥前缀 `qk_`（对应 `wbk_`），管理令牌前缀 `qct_`（对应 `wbt_`）
- 后端 `server/`：FastAPI 单进程，SQLite（裸 sqlite3 无 ORM），Python 3.11+
- 前端 `web/`：Next.js 15 App Router + React 19 + shadcn/ui + Tailwind v4，静态导出由后端托管
- 代码注释与文档一律中文
