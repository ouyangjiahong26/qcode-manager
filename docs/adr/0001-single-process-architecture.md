# ADR-0001 单进程面板架构：FastAPI + Next.js 静态导出 + SQLite

日期：2026-10-30
状态：已接受

## 背景

qcoder-manager 仿照 workbuddy-manager（源仓库 `~/codes/workbuddy-manager`）构建。需要确定进程模型、技术栈与部署形态。

## 决策

与源仓库同构：

- 单进程单端口 `:7865`。后端 FastAPI（Python 3.11+，裸 sqlite3 无 ORM，httpx 出站）同时承载管理面 `/api/*`、网关 `/v1/*` `/v2/*`、以及 Next.js 静态导出的前端托管，同源无 CORS。
- 前端 Next.js 15 App Router + React 19 + shadcn/ui（new-york, zinc）+ Tailwind v4 + motion + recharts + sonner，`build:export` 产出 `web/out/` 交 FastAPI 托管。
- 状态全部落在 SQLite（WAL）与 `data/users.json`：无 redis、无 celery、无消息队列；限流、防爆破、会话均为进程内字典。
- 上游 qoder2api 独立进程（默认 `127.0.0.1:8963`），面板不改其一行代码，仅通过 HTTP 交互（`GET /v1/models`、`POST /v1/chat/completions`，PAT 作为 Bearer 由面板注入）。

## 后果

- 部署简单（一个进程 + 一个库文件），与源仓库运维经验直接对齐。
- 水平扩展受限（单实例）；与源仓库一致，接受。
