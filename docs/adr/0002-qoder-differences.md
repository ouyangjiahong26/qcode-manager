# ADR-0002 Qoder 上游差异：账号池所有权反转与功能裁剪

日期：2026-10-30
状态：已接受

## 背景

源仓库的上游 workbuddy2api 自带账号池：面板通过共享 `auths/*.json` 文件与 `/status`、admin 接口与上游协作，账号运营（扫码、签到、积分、冷却、成长任务）是面板的重要功能面。qoder2api 的形态完全不同：无状态协议桥，PAT（Personal Access Token）由请求方每次携带，无 `/status`、无管理接口、无签到积分体系。

## 决策

### 账号池所有权反转

账号池由本面板自管：`accounts` 表存 PAT（Fernet 加密，密钥由 `data/secret.key` 派生），前端只见掩码与尾 4 位。网关转发时面板按密钥绑定的上游挑选健康 PAT 注入 `Authorization` 头；调度为轮询 + 失败跳过（`status='invalid'` 或最近测活失败的不选）。

### 测活即保活

唯一保留的定时任务是保活（keepalive）：按设置的时刻表带每个启用的 PAT 调上游 `GET /v1/models`（上游真实鉴权），回写 `status/last_check_*`。

### 裁剪清单（无对应物，不实现）

| 源功能 | 原因 |
|---|---|
| 扫码登录（tencent.py / auth/start / poll） | Qoder PAT 在网页端手动创建后粘贴纳管 |
| 签到、旅行、活跃、每日任务、任务运行器 | Qoder 无积分体系 |
| 积分查询、积分流水、积分红包（redpacket 全套 + claim 页） | 同上 |
| 令牌刷新（refreshToken 续期） | PAT 长期有效，失效重新创建 |
| 上游冷却管理（state.json、clear-cooling/force-cooling） | 上游无此机制 |
| 上游 config.json 可视化编辑、容器/服务启停重启 | qoder2api 只有环境变量，无配置文件接口 |
| 账号级代理出口（proxies 全套、lane、resolve_account_proxy） | 面板仅与上游通信；上游到 Qoder 网关的出口由上游侧配置 |
| 看门狗（上游网页改版检测） | 上游是本地协议桥 |
| realm 双区（cn/global）与 credit 计费列 | Qoder 无区域与积分计费概念 |
| 本机导入 cc-switch/ZCode | Qoder 场景无此迁移需求 |

### 保留清单

密钥分发（版本/有效期/IP 数/IP 与模型白名单/token 配额）、模型别名映射、模型中心、playground、多上游分组、入站 IP 白黑名单与审计、API Token、调用日志与用量统计（天 + 小时 × 密钥 × 模型）、Anthropic Messages 与 OpenAI Responses 兼容层、PG 异地备份、一键更新、五语种界面、docker 与 systemd 部署。

## 后果

- 数据模型简化：去掉 `checkin_logs`、`task_logs`、`red_packets*`、`account_notes` 表与 `api_keys`/`request_logs`/`usage_*` 的 realm、credit 列。
- `services/tencent.py` 不复刻；`services/wb2api.py` 重写为 `services/qoder2api.py`（两个端点）+ `accountsvc.py`（PAT 池）。
- 前端裁掉 tasks、red-packets、claim 页与 realm 切换，settings 去掉 upstream-config/proxy/watchdog 三个 Tab。
