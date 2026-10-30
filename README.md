# QCode Manager

**Qoder PAT 账号池管理控制台 · OpenAI 兼容反代网关**

面向 [`qoder2api`](https://github.com/fengyinxia/qoder2api) 的 Web 管理端：PAT 纳管与测活保活、密钥分发、IP 与模型白名单、调用日志与用量统计。架构仿照 [workbuddy-manager](https://github.com/ithtelab/workbuddy-manager)，把它在 CodeBuddy 场景的运营能力（多账号池、多密钥、审计统计）带到 Qoder 生态。

![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white)
![Next.js](https://img.shields.io/badge/Next.js-15-000000?logo=nextdotjs&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-22c55e)

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

## 架构

```
下游客户端（OpenAI SDK / Claude Code）
        │  Authorization: Bearer qk_xxx（网关密钥）
        ▼
┌────────────────────────────────────┐
│ qcode-manager            :7865     │
│ 网关 /v1/*（鉴权→IP 管控→模型映射  │
│ →PAT 轮询→流式转发→日志用量落库）  │
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

## 快速开始

```bash
# 1) 上游（任选其一）
git clone https://github.com/fengyinxia/qoder2api upstream/qoder2api
# 2) 面板与上游一起起（compose 内自动构建两者）
docker compose up -d
```

或源仓库 systemd 部署：`sudo bash deploy/install.sh`。装完打开 `http://127.0.0.1:7865`，初始管理员密码在首启日志与 `data/initial-admin-password.txt`。

公网部署（反代、TLS、与 workbuddy-manager 共存、`pm.` 管理域 + `api.` 中转域的双子域配置）见 [部署指南](deploy/README.md)。

## 三步上手

1. **纳管账号**：[qoder.cn/account/integrations](https://qoder.cn/account/integrations) 创建个人访问令牌（PAT，`pt-` 开头），面板「账号」页粘贴纳管，即刻自动测活
2. **分发密钥**：「密钥」页创建 `qk_` 网关密钥，按需设置有效期、Token 配额、IP 与模型白名单、绑定上游分组
3. **下游接入**：`base_url=http://<面板>:7865/v1`，`api_key=qk_xxx`；Claude Code 可直连 `/v1/messages`（Anthropic 兼容）

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

## 安全说明

- PAT 以 Fernet 加密存储（密钥 `data/secret.key`），前端只见掩码与尾 4 位
- 网关密钥、管理面 Token 只在创建时显示一次，库里只存哈希
- 入站 IP 白 / 黑名单（CIDR）、每密钥 IP 数上限与模型白名单
- 敏感操作全部留审计日志；登录防爆破（IP + 用户名双维度锁定）
- 公网部署请走反向代理 + TLS，并透传 `X-Real-IP`（见部署指南），面板自身只绑 `127.0.0.1`

## 与 workbuddy-manager 的差异

上游形态不同带来账号层重写：CodeBuddy 的扫码 / 签到 / 积分体系在 Qoder 无对应物，本项目以「PAT 池 + 测活保活」替代；密钥分发、用量统计、安全面、多上游、备份与更新等运营能力保持同构。完整决策记录见 `docs/adr/0002-qoder-differences.md`。

## 开发

```bash
# 后端（终端 A）
uv venv && uv pip install --python .venv/Scripts/python.exe -r server/requirements.txt
python dev/mock_upstream.py                 # 模拟上游 :8963
.venv/Scripts/python.exe -m uvicorn server.main:app --port 7865 --reload

# 前端（终端 B）
cd web && npm install && npm run dev         # http://localhost:3000，/api /v1 反代到 :7865

# 测试
.venv/Scripts/python.exe -m unittest discover -s server/tests -t .
```

## 许可

[MIT](LICENSE)
