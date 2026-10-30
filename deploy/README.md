# 部署指南（deploy/README.md）

QCode Manager 面板（FastAPI，端口 7865）+ 上游 qoder2api（端口 8963，独立进程）。
三种形态任选：**docker-compose**（推荐）、**宿主机 systemd**（`install.sh`）、混合。

## 一、与 workbuddy-manager 共存

两者互不依赖，仅端口与目录不同，可直接同机部署：

| 服务 | 端口 | 监听 | 说明 |
|---|---|---|---|
| workbuddy-manager | 7864 | 仅本机（127.0.0.1） | 已有服务，不受影响 |
| qcode-manager | 7865 | 仅本机（127.0.0.1） | 本面板 |
| qoder2api | 8963 | 仅本机 | 上游，独立进程 |

注意：三个端口都**不要**直接对公网开放，公网一律经 nginx 反代（HTTPS）。

## 二、Docker Compose 部署（推荐）

```bash
cp .env.example .env          # 至少确认 QC_ADMIN_PASSWORD
git clone https://github.com/fengyinxia/qoder2api upstream/qoder2api
chown -R 10001:10001 ./data
docker compose up -d --build
```

上游 qoder2api 的两种接入方式（二选一）：

1. **本地构建**（默认）：`git clone` 到 `./upstream/qoder2api`，compose 用
   `build: context: ./upstream/qoder2api` 现场构建；
2. **自备镜像**：把 `docker-compose.yml` 里 `qoder2api` 服务的 `build:` 段删掉，
   `image:` 换成你自己的镜像名。

面板通过 `QC2API_BASE=http://qoder2api:8963`（compose 内网服务名）访问上游，
上游容器不映射 `ports:`，外网摸不到。数据全部落在 `./data`（备份它即可）。

首次启动若未设 `QC_ADMIN_PASSWORD`，随机密码在日志里：
`docker compose logs qcode-manager | grep -A2 密码`。

## 三、宿主机 systemd 部署

```bash
sudo bash deploy/install.sh
```

脚本会：检测 python≥3.11 / node≥20（仅构建期）→ 构建前端静态导出 → 建 venv
装依赖 → 创建服务账号并注册 `qcode-manager.service`（EnvironmentFile=.env、
WorkingDirectory=/opt/qcode-manager、Restart=always）→ 启动并健康检查。
防火墙（ufw 等）**无需**放行 7865：公网只开 80/443 由 nginx 转发。

**升级**：拉取新版本代码后重跑 `install.sh`（`data/` 数据保留不动）。

上游 qoder2api 不由本脚本管理，单独部署：参照其仓库 README 以 systemd 或
独立 compose 起在 8963（仅本机监听），面板 `.env` 里保持
`QC2API_BASE=http://127.0.0.1:8963`。

## 四、nginx 反代（pm. / api. 双子域 + TLS）

- `pm.<域名>`：管理面板，转发全部路径到 `127.0.0.1:7865`
- `api.<域名>`：API 中转，**只**转发 `/v1/*` 与 `/v2/*` 到 7865（面板与网关同进程）

证书路径换成你自己的（certbot 签发后通常在 `/etc/letsencrypt/live/<域名>/`）。

```nginx
# ── pm.<域名> 管理面板（全部路径）────────────────────────
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name pm.example.com;

    ssl_certificate     /etc/letsencrypt/live/pm.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/pm.example.com/privkey.pem;

    client_max_body_size 64M;

    # 真实 IP 透传：登录锁定、IP 白名单、请求日志都依赖它
    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # SSE（流式补全）：关闭缓冲，token 一到就推
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 300s;

    location / {
        proxy_pass http://127.0.0.1:7865;
    }
}

# ── api.<域名> API 中转（仅 /v1 与 /v2）───────────────────
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    http2 on;
    server_name api.example.com;

    ssl_certificate     /etc/letsencrypt/live/api.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.example.com/privkey.pem;

    client_max_body_size 64M;

    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 300s;

    # 只放行网关路径：管理 API（/api/*）绝不从公网域名暴露
    location /v1/ { proxy_pass http://127.0.0.1:7865; }
    location /v2/ { proxy_pass http://127.0.0.1:7865; }
    location /    { return 404; }
}

# HTTP → HTTPS 跳转
server {
    listen 80;
    listen [::]:80;
    server_name pm.example.com api.example.com;
    return 301 https://$host$request_uri;
}
```

客户端调用示例：`BASE_URL=https://api.example.com/v1` + 面板签发的 `qk_` 密钥。

### QC_TRUSTED_PROXY_CIDRS=127.0.0.1 的含义

面板靠 `X-Forwarded-For` / `X-Real-IP` 取客户端真实 IP，但这两个头**谁都能伪造**。
`QC_TRUSTED_PROXY_CIDRS` 声明"只采信来自这些网段的连接所携带的转发头"。
nginx 与面板同机部署，连接源永远是 127.0.0.1，所以只放行回环——公网直连
7865 的请求即使伪造了 XFF 也会被忽略。若 nginx 在别的机器上，把它相对面板
的来源 IP/网段加进该列表；前置 CDN 时同时调大 `QC_TRUSTED_PROXY_HOPS`。

## 五、备份与恢复

- **最小备份**：`data/` 目录——SQLite（manager.db）、users.json、secret.key
  （PAT 加密密钥，**丢了所有 PAT 无法解密**）、初始密码文件。
- **pgsync**：面板设置 → 备份页可把 SQLite 同步到 PostgreSQL（`/api/pgsync`），
  适合多实例或需要异地容灾的场景；PostgreSQL 连接串在备份页配置。
- 恢复：停服务（`systemctl stop qcode-manager` 或 `docker compose down`）→
  覆盖 `data/` → 启动。定期用 `sqlite3 data/manager.db ".backup ..."` 或直接
  tar 整个 `data/`（需先停写或接受最后几秒丢失）。

## 六、常用运维

```bash
# systemd 形态
systemctl status qcode-manager
journalctl -u qcode-manager -f

# compose 形态
docker compose logs -f qcode-manager
docker compose restart qcode-manager

# 升级
# systemd：git pull && sudo bash deploy/install.sh
# compose：git pull && docker compose up -d --build
```
