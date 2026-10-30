# QCode Manager 容器镜像（管理面板 + OpenAI 兼容网关）
#
# 多阶段构建：
#   阶段一 web-builder —— Node 20 构建 Next.js 静态导出（web/out）
#   阶段二 runtime    —— Python 3.12 运行时，仅装 server 依赖并拷贝 server/ 与前端产物
#
# 本镜像不含上游 qoder2api：上游是独立进程/镜像，由 docker-compose.yml 单独编排，
# 面板通过 QC2API_BASE 指向它（compose 网络内用服务名 http://qoder2api:8963）。

# ── 阶段一：前端静态导出 ─────────────────────────────────
FROM node:20-slim AS web-builder

WORKDIR /src

# 可选：npm 镜像源（中国大陆访问官方 registry 慢且易断；留空 = 官方源，失败自动降级 npmmirror）
#   docker compose build --build-arg NPM_REGISTRY=https://registry.npmmirror.com
ARG NPM_REGISTRY=""

COPY web/package.json web/package-lock.json* ./
RUN set -eu; \
    npm_ci() { npm ci --no-audit --no-fund ${1:+--registry="$1"}; }; \
    if [ -n "${NPM_REGISTRY}" ]; then \
        npm_ci "${NPM_REGISTRY}"; \
    elif npm_ci ""; then \
        echo "npm: 依赖安装成功（官方源）"; \
    else \
        echo "npm: 官方源失败，改用 registry.npmmirror.com 重试…" >&2; \
        rm -rf node_modules; \
        npm_ci https://registry.npmmirror.com; \
    fi

COPY web/ ./
# build:export 即 NEXT_OUTPUT_EXPORT=1 next build，产物落在 out/
RUN npm run build:export && test -f out/index.html

# ── 阶段二：运行时镜像 ───────────────────────────────────
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QC_MANAGER_HOST=0.0.0.0 \
    QC_MANAGER_PORT=7865 \
    QC_DATA_DIR=/app/data \
    QC_STATIC_DIR=/app/web/out

# git：面板「一键更新」要 git fetch；curl：健康探针
RUN set -eu; \
    apt-get -o Acquire::Retries=1 -o Acquire::http::Timeout=15 -o Acquire::https::Timeout=15 update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（层缓存：改代码不必重装依赖）
# 可选：PyPI 镜像（留空 = 官方源，失败自动降级清华镜像）
ARG PIP_INDEX_URL=""
COPY server/requirements.txt /app/server/requirements.txt
RUN set -eu; \
    pip_install() { \
        pip install --no-cache-dir --retries 5 --timeout 60 --index-url "$1" -r /app/server/requirements.txt; \
    }; \
    if [ -n "${PIP_INDEX_URL}" ]; then \
        pip_install "${PIP_INDEX_URL}"; \
    elif pip_install https://pypi.org/simple; then \
        echo "pip: 依赖安装成功（官方源）"; \
    else \
        echo "pip: 官方源失败，改用清华镜像重试…" >&2; \
        pip_install https://pypi.tuna.tsinghua.edu.cn/simple; \
    fi

# 再拷代码与前端产物
COPY server /app/server
COPY --from=web-builder /src/out /app/web/out

# 非 root 运行（uid 10001，与 data 卷的宿主属主约定一致，见 docker-compose.yml）
# PYTHONUSERBASE 钉死用户站点路径：容器内一键更新补装依赖时只能写到 /app/.local，
# 且该目录必须先存在，Python 才会把它加进 sys.path。
ENV PYTHONUSERBASE=/app/.local
RUN useradd -u 10001 -m -s /bin/bash app \
    && mkdir -p /app/.local /app/data \
    && chown -R 10001:10001 /app
USER app

# 容器内监听 7865；对外是否暴露由 compose / 运行时决定（建议只绑宿主回环，走反代）
EXPOSE 7865

HEALTHCHECK --interval=30s --timeout=5s --retries=3 --start-period=10s \
    CMD curl -fsS http://127.0.0.1:7865/healthz || exit 1

CMD ["python", "-m", "uvicorn", "server.main:app", "--host", "0.0.0.0", "--port", "7865"]
