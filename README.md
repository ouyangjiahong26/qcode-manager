<div align="center">

# QCoder Manager

Qoder PAT 账号池管理控制台 · OpenAI 兼容反代网关

面向 [`qoder2api`](https://github.com/fengyinxia/qoder2api) 的 Web 管理端：PAT 纳管与测活保活、密钥分发、IP 与模型白名单、调用日志与用量统计。

![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)
![Next.js](https://img.shields.io/badge/Next.js-15-000000?logo=nextdotjs&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-22c55e)

[![Release](https://img.shields.io/github/v/release/ouyangjiahong26/qcoder-manager?color=22c55e&label=Release)](https://github.com/ouyangjiahong26/qcoder-manager/releases)
[![更新日志](https://img.shields.io/badge/更新日志-CHANGELOG-blue)](CHANGELOG.md)
[![反馈](https://img.shields.io/github/issues/ouyangjiahong26/qcoder-manager?color=f59e0b&label=反馈)](https://github.com/ouyangjiahong26/qcoder-manager/issues)

<p align="center">
  使用边界：本项目只用于管理你本人 Qoder 账号创建的 PAT，以及账号所有者明确授权的令牌。禁止批量注册、账号与令牌交易、代注册、接码、盗号、绕过平台风控，以及任何商业转售或盈利性用途。使用即表示你已阅读并同意<a href="#使用边界与免责声明">完整免责声明</a>。
</p>

</div>

---

## 这是什么

`qoder2api` 把 [QoderWork](https://qoder.com.cn) 的私有协议封装成 OpenAI 兼容接口，但它无状态：PAT 由客户端每次携带，没有账号池、没有管理界面。本项目补上运营层：

| 你原本要做的 | 现在在面板上 |
|---|---|
| 每个下游各自填 PAT | 面板纳管 PAT 池，网关自动轮询分发 |
| 手工排查哪个 PAT 失效 | 定时测活保活，状态、失败原因一目了然 |
| 所有下游共用一个 PAT | 多密钥分发，各自独立配额、IP 与模型白名单 |
| 无从知道谁用了多少 | 每次调用的模型、Token、延迟、来源 IP 全量留痕 |
| 无任何 IP 防护 | 入站白/黑名单（CIDR）+ 每密钥 IP 上限与白名单 |

面板不侵入上游：qoder2api 不改一行代码。账号池调度（存储、测活、轮询）由面板实现，PAT 在转发时注入请求头。

```
下游客户端（OpenAI SDK / Claude Code）
        │  Authorization: Bearer qk_xxx（网关密钥）
        ▼
┌────────────────────────────────────┐
│ qcoder-manager           :7865     │
│ 网关 /v1/*（鉴权、IP 管控、模型映射、│
│ PAT 轮询、流式转发、日志用量落库）  │
│ 管理面 /api/* · Next.js 静态托管   │
└──────────────┬─────────────────────┘
               │ Authorization: Bearer pt-xxx（PAT，面板注入）
               ▼
┌────────────────────────────────────┐
│ qoder2api（不改动）       :8963     │
└──────────────┬─────────────────────┘
               ▼
        Qoder 网关（qoder.com.cn）
```

---

## 功能亮点

### 账号管理

- PAT 纳管：[qoder.cn/account/integrations](https://qoder.cn/account/integrations) 创建的 PAT（`pt-` 开头）粘贴即纳管，入库前即刻测活
- 保活调度：定时带 PAT 调上游测活并标记状态，失效原因可查；调用间轮询自动跳过失效账号
- 分组管理：多上游（多个 qoder2api 实例）各自独立账号池，密钥绑定上游分组
- 加密存储：PAT 以 Fernet 加密落库，前端只见掩码与尾 4 位

### 反代网关（对外 `/v1`）

- OpenAI 兼容：`/v1/chat/completions` 流式与非流式、`/v1/models` 模型列表、`/v1/usage` 密钥自查
- 协议兼容层：Anthropic Messages（`/v1/messages`，Claude Code 可直连）与 OpenAI Responses
- 多密钥分发：每把 `qk_` 密钥独立设有效期、Token 配额、IP 数上限、IP 与模型白名单
- 模型别名映射：把下游在用的名字映到实际模型，迁移无感
- 全量审计：每次调用记密钥、IP、模型、状态码、首字延迟、Token 用量

### 可视化设置

- 用量统计：按天与按小时、密钥与模型维度聚合，失败与延迟可查
- 安全面：入站 IP 白/黑名单（CIDR）、审计日志、管理面 API Token（readonly 与 admin 作用域）、登录防爆破
- 运维：一键更新（发布包 sha256 校验、离线包上传）、PostgreSQL 异地备份
- 五语种界面：简体中文、繁體中文、English、日本語、한국어

---

## 快速开始

服务器部署需要 Docker。两种方式任选，公网访问一律走反向代理加 TLS（见部署指南）。

方式一，Docker Compose（面板与上游一起构建）：

```bash
# 1) 上游源码（compose 从这个路径构建 qoder2api）
git clone https://github.com/fengyinxia/qoder2api upstream/qoder2api
# 2) 面板与上游一起起（compose 内自动构建两者）
docker compose up -d
# 首启未设 QC_ADMIN_PASSWORD 时，初始密码看日志：
docker compose logs qcoder-manager | grep -A2 密码
```

方式二，宿主机 systemd：

```bash
sudo bash deploy/install.sh
```

装完打开 `http://127.0.0.1:7865`，初始管理员密码在首启日志与 `data/initial-admin-password.txt`。公网部署（反代、TLS、与 workbuddy-manager 同机共存、管理域与 API 中转域的双子域配置）见 [部署指南](deploy/README.md)。

---

## 使用指南

日常就三步：纳管账号、分发密钥、把下游指到 `/v1`。

1. 纳管账号：[qoder.cn/account/integrations](https://qoder.cn/account/integrations) 创建个人访问令牌（PAT，`pt-` 开头），面板「账号」页粘贴纳管，即刻自动测活
2. 分发密钥：「密钥」页创建 `qk_` 网关密钥，按需设置有效期、Token 配额、IP 与模型白名单、绑定上游分组
3. 下游接入：`base_url=http://<面板>:7865/v1`，`api_key=qk_xxx`；Claude Code 可直连 `/v1/messages`（Anthropic 兼容）

---

## 接口说明

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| `POST` | `/v1/chat/completions`（`/v2` 同） | 网关密钥 | OpenAI 兼容对话（流式 / 非流式） |
| `POST` | `/v1/responses` | 网关密钥 | OpenAI Responses 兼容 |
| `POST` | `/v1/messages` `/count_tokens` | 网关密钥 | Anthropic Messages 兼容（Claude Code 等） |
| `GET` | `/v1/models` `/v1/usage` | 网关密钥 | 模型列表 / 密钥自查 |
| `GET` | `/healthz` | 无 | 存活探测（含上游连通） |
| `POST` | `/api/login` `/api/logout` | 无 | 登录 / 登出 |
| `GET/POST/…` | `/api/accounts*` | 会话 | PAT 纳管、测活、导出 |
| `GET/POST/…` | `/api/keys*` | 会话 / 管理员 | 网关密钥管理 |
| `GET/POST/…` | `/api/upstreams*` `/api/tokens*` | 会话（管理员） | 上游分组 / 管理面 API Token |
| `GET` | `/api/logs` `/api/stats/*` | 会话 | 日志与用量 |
| `GET/POST/…` | `/api/security/*` `/api/settings/*` | 会话 / 管理员 | IP 规则与审计 / 设置 |
| `GET/POST/…` | `/api/system/*` `/api/pgsync*` | 会话（管理员） | 一键更新 / PG 异地备份 |

管理端接口细节启动后开 `QC_ENABLE_DOCS=1` 访问 `/docs`。

---

## 安全说明

- PAT 以 Fernet 加密存储（密钥 `data/secret.key`），前端只见掩码与尾 4 位
- 网关密钥、管理面 Token 只在创建时显示一次，库里只存哈希
- 入站 IP 白 / 黑名单（CIDR）、每密钥 IP 数上限与模型白名单
- 敏感操作全部留审计日志；登录防爆破（IP + 用户名双维度锁定）
- 公网部署请走反向代理 + TLS，并透传 `X-Real-IP`（见部署指南），面板自身只绑 `127.0.0.1`

---

## 已知限制

- 请求体与响应体内容不留存，调用日志只记元数据（模型、状态、Token、延迟、来源 IP），以保护隐私
- PAT 需在 qoder.cn 手动创建，面板不做自动注册或续期，令牌过期后需重新创建并更新
- 上游 qoder2api 独立部署与更新，面板不侵入上游代码，上游自身的升级以其仓库发布说明为准

---

## 与 workbuddy-manager 的差异

上游形态不同带来账号层重写：CodeBuddy 的扫码、签到、积分体系在 Qoder 无对应物，本项目以「PAT 池 + 测活保活」替代；密钥分发、用量统计、安全面、多上游、备份与更新等运营能力保持同构。完整决策记录见 `docs/adr/0002-qoder-differences.md`。

---

## 更多文档

- [部署指南](deploy/README.md)：Docker Compose、systemd、nginx 反代与 TLS、与 workbuddy-manager 共存、备份恢复
- [架构决策记录](docs/adr/)：0001 单进程架构、0002 与 workbuddy-manager 的差异

---

## 更新日志与反馈

- 更新日志：[CHANGELOG.md](CHANGELOG.md)，面板「设置 → 更新日志」也读这里
- 发布包：[Releases](https://github.com/ouyangjiahong26/qcoder-manager/releases)
- 反馈问题：[提交 Issue](https://github.com/ouyangjiahong26/qcoder-manager/issues/new/choose)

反馈时请附上版本号与错误日志，并先移除其中的密钥、令牌等敏感信息。

---

## 致谢

- [ithtelab/workbuddy-manager](https://github.com/ithtelab/workbuddy-manager)：架构与运营能力参照，密钥分发、用量统计、安全面、更新与备份的形态来源
- [fengyinxia/qoder2api](https://github.com/fengyinxia/qoder2api)：上游 OpenAI 兼容协议桥（MIT）

---

## 使用边界与免责声明

本项目的初衷是方便个人管理自己 Qoder 账号的 PAT，不是为批量运营或商业牟利设计的。

允许的用途：

- 管理你本人 Qoder 账号创建的 PAT，或账号所有者明确授权给你的 PAT；
- 在自己的设备或内网中学习、研究和自用。

禁止的用途：

- 批量注册、代注册、接码，以及买卖、出租或交易账号与令牌；
- 盗号、撞库、绕过实名认证、风控、区域限制或其他平台安全机制；
- 将本项目或其派生服务用于对外收费、转售、引流或其他盈利性用途；
- 任何违反 Qoder 服务条款、当地法律法规或侵犯他人权益的用途。

使用者应自行确认账号授权与使用方式合法合规。因滥用、误用或违反上述边界导致的封号、数据丢失、服务中断或法律责任，由使用者自行承担；项目作者与贡献者不对使用者的行为负责，也不提供任何形式的担保。

本项目与 Qoder 及其运营方无隶属或合作关系；相关商标归其权利人所有。项目按 [MIT](LICENSE) 许可「按现状」提供。

---

## License

[MIT](LICENSE)
