# 更新日志

本文件供面板「设置 → 更新日志」与一键更新流程解析，格式：版本号二级标题 + 分组列表。

## 0.1.0

首个版本。

### 新增

- PAT 账号池：粘贴纳管、即刻测活、定时保活、轮询调度、分组（多上游）、加密存储与掩码展示
- 反代网关：OpenAI 兼容 `/v1/chat/completions`（流式/非流式）、`/v1/models`、`/v1/usage`，模型别名映射，每密钥限流
- 协议兼容层：Anthropic Messages（`/v1/messages`，Claude Code 可直连）与 OpenAI Responses
- 密钥分发：有效期、Token 配额、IP 数上限、IP 与模型白名单、绑定上游分组
- 用量体系：调用日志（延迟、首字、流式、账号归属）与按天/小时 × 密钥 × 模型聚合统计
- 安全面：入站 IP 白/黑名单（CIDR）、审计日志、管理面 API Token（readonly/admin 作用域）、登录防爆破
- 运维：一键更新（发布包 sha256 校验、离线包）、PostgreSQL 异地备份、五语种界面
- 部署：Docker Compose（面板 + qoder2api）、systemd、一键安装脚本、双子域反代指南
