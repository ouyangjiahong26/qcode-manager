#!/usr/bin/env bash
# ============================================================
# QCoder Manager 一键部署脚本（宿主机 + systemd 形态）
#
# 只负责面板本身；上游 qoder2api 请单独部署（systemd 独立进程或
# docker-compose.yml 里的第二个服务），详见 deploy/README.md。
#
# 用法：
#   sudo bash deploy/install.sh                    # 完整安装
#   sudo APP_DIR=/opt/custom bash deploy/install.sh  # 自定义安装目录
#   sudo bash deploy/install.sh                    # 升级：拉取新版本后重跑即可
#                                                  #（数据在 data/，不会动）
# ============================================================
set -euo pipefail

# ── 可配置项（均可用环境变量覆盖）─────────────────────────
APP_DIR="${APP_DIR:-/opt/qcoder-manager}"
MANAGER_PORT="${MANAGER_PORT:-7865}"
PY="${PY:-/usr/bin/python3}"
NODE_MIN_MAJOR=20
PY_MIN=(3 11)

# ── 输出 helpers ─────────────────────────────────────────
info()  { printf '\033[1;34m[·]\033[0m %s\n' "$*"; }
ok()    { printf '\033[1;32m[✓]\033[0m %s\n' "$*"; }
warn()  { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()   { printf '\033[1;31m[✗]\033[0m %s\n' "$*" >&2; exit 1; }
step()  { printf '\n\033[1;36m══ %s\033[0m\n' "$*"; }

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ── 0. 预检 ───────────────────────────────────────────────
step "0/4 环境预检"

[ "$(id -u)" -eq 0 ] || die "请用 root 运行（sudo bash deploy/install.sh）"

# APP_DIR 会被 chown -R 交给服务账号，指到 /opt、/home 这类公共目录会把
# 无关文件的所有权一起改掉，先拒绝明显过宽的路径。
case "$APP_DIR" in
    /|/opt|/home|/usr|/var|/etc|/root) die "APP_DIR 不能是 ${APP_DIR} 这种顶层目录" ;;
esac

# python ≥ 3.11
command -v "$PY" >/dev/null 2>&1 || die "未找到 python3（$PY）"
PYVER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
"$PY" -c "import sys;sys.exit(0 if sys.version_info>=(${PY_MIN[0]},${PY_MIN[1]}) else 1)" \
    || die "Python 版本过低（需 ≥${PY_MIN[0]}.${PY_MIN[1]}，当前 ${PYVER}）"
ok "Python ${PYVER}"

# node ≥ 20（仅构建期需要；若发布包里已带 web/out 可跳过）
BUILD_WEB=0
if [ -f "${SRC_DIR}/web/out/index.html" ]; then
    info "检测到已构建的前端 web/out，跳过 Node 构建"
else
    command -v node >/dev/null 2>&1 \
        || die "未找到 node（构建前端需要 Node ≥20；或使用带 web/out 的发布包）"
    NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]')"
    [ "$NODE_MAJOR" -ge "$NODE_MIN_MAJOR" ] \
        || die "Node 版本过低（需 ≥${NODE_MIN_MAJOR}，当前 $(node --version)）"
    ok "Node $(node --version)"
    BUILD_WEB=1
fi

# 端口占用检查（面板自身；上游 8963 只提示不阻断）
if command -v ss >/dev/null 2>&1; then
    if ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${MANAGER_PORT}\$"; then
        warn "端口 ${MANAGER_PORT} 已被占用——若是旧版本面板，本脚本会在最后重启它"
    fi
fi
if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]8963\$" \
        || warn "端口 8963（上游 qoder2api）未在监听——面板能启动，但上游不可用，见 deploy/README.md"
fi

# ── 1. 部署代码 ───────────────────────────────────────────
step "1/4 安装代码"

if [ "${SRC_DIR}" != "${APP_DIR}" ]; then
    info "同步代码到 ${APP_DIR}"
    mkdir -p "${APP_DIR}"
    # rsync 优先（增量、快），没有就 cp
    if command -v rsync >/dev/null 2>&1; then
        rsync -a --delete \
            --exclude '.git' --exclude 'data' --exclude '.env' \
            --exclude 'venv' --exclude 'web/node_modules' --exclude 'web/.next' \
            --exclude '.venv' --exclude '__pycache__' \
            "${SRC_DIR}/" "${APP_DIR}/"
    else
        cp -r "${SRC_DIR}/." "${APP_DIR}/"
    fi
fi
mkdir -p "${APP_DIR}/data"

# 前端静态导出（仅源码部署且没有现成产物时）
if [ "$BUILD_WEB" -eq 1 ]; then
    step "1b/4 构建前端（Node，仅构建期需要）"
    (cd "${APP_DIR}/web" \
        && { [ -d node_modules ] || npm ci --no-audit --no-fund; } \
        && npm run build:export) \
        || die "前端构建失败（npm ci / build:export）"
    test -f "${APP_DIR}/web/out/index.html" || die "缺少 web/out/index.html"
    ok "前端构建完成"
fi

# ── 2. Python 环境 ────────────────────────────────────────
step "2/4 Python 依赖"

if [ ! -x "${APP_DIR}/venv/bin/pip" ]; then
    "$PY" -m venv "${APP_DIR}/venv" || die "创建 venv 失败"
fi
"${APP_DIR}/venv/bin/pip" install --upgrade pip >/dev/null
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/server/requirements.txt"
ok "依赖就绪"

# ── 3. systemd 服务 ───────────────────────────────────────
step "3/4 注册 systemd 服务"

# 服务账号：uid 10002（避开常见业务 uid；容器形态对齐见 Dockerfile 的 10001，
# 两者不共享文件所以不必同 uid）
RUN_USER=qcoder
if id "$RUN_USER" >/dev/null 2>&1; then
    RUN_GROUP="$(id -gn "$RUN_USER")"
else
    RUN_GROUP=qcoder
    useradd --system --user-group --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$RUN_USER"
    ok "已创建服务账号 ${RUN_USER}"
fi

sed -e "s#/opt/qcoder-manager#${APP_DIR}#g" \
    -e "s#--port 7865#--port ${MANAGER_PORT}#g" \
    -e "s#QC_MANAGER_PORT=7865#QC_MANAGER_PORT=${MANAGER_PORT}#g" \
    "${APP_DIR}/deploy/qcoder-manager.service" > /etc/systemd/system/qcoder-manager.service

chown -R "${RUN_USER}:${RUN_GROUP}" "${APP_DIR}"
chmod 750 "${APP_DIR}/data"

systemctl daemon-reload
systemctl enable --now qcoder-manager >/dev/null 2>&1
systemctl restart qcoder-manager
ok "服务已注册并启动"

# ── 4. 验证与收尾 ─────────────────────────────────────────
step "4/4 验证"

sleep 2
if curl -sf -m 5 "http://127.0.0.1:${MANAGER_PORT}/healthz" >/dev/null 2>&1; then
    ok "健康检查通过（/healthz）"
else
    warn "健康检查暂未通过；查看日志：journalctl -u qcoder-manager -e --no-pager"
fi

INITIAL_PW_FILE="${APP_DIR}/data/initial-admin-password.txt"

cat <<EOF

  ┌──────────────────────────────────────────────────────┐
  │  QCoder Manager 已安装                                 │
  │  面板地址：http://127.0.0.1:${MANAGER_PORT}（请经反向代理对外）      │
  │  服务管理：systemctl {status|restart} qcoder-manager   │
  │  日志：journalctl -u qcoder-manager -f                 │
  └──────────────────────────────────────────────────────┘
EOF

if [ -r "$INITIAL_PW_FILE" ]; then
    printf '\n  初始管理员密码：%s\n  （来自 %s，首次登录后请改密）\n' \
        "$(cat "$INITIAL_PW_FILE")" "$INITIAL_PW_FILE"
else
    printf '\n  初始管理员密码见容器日志：journalctl -u qcoder-manager | grep -A2 密码\n'
fi

# 防火墙提示：面板只应经反代暴露，不开 7865 公网端口
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "active"; then
    info "检测到 ufw 已启用：无需放行 ${MANAGER_PORT}（公网请只放行 80/443 由 nginx 反代）"
fi

cat <<'EOF'

  下一步：
  1. 上游 qoder2api 需单独部署（systemd 或 docker-compose.yml 第二个服务）
     见 deploy/README.md
  2. nginx 反代与 TLS 配置样例见 deploy/README.md
  3. 升级：拉取新版本后重跑本脚本即可（data/ 数据保留）
EOF
