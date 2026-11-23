#!/usr/bin/env bash
# MiniHarness 一键部署（Ubuntu 22.04/24.04，root 或 sudo 执行）
# 用法:
#   export MINI_HARNESS_API_KEY="网关key（如 swas.xxx）"
#   export DEMO_PASSWORD="给面试官的口令"
#   bash setup_server.sh
# Base URL / 模型可用 MINI_HARNESS_BASE_URL / MINI_HARNESS_MODEL 覆盖，默认已指向 MaaS 网关
# 幂等：可重复执行，不会重建已存在的 venv/db/.env（除非加 --force-db）

set -euo pipefail

REPO_URL="https://github.com/Misterzb/mini-harness.git"
APP_DIR="/opt/mini-harness"
SVC_USER="miniharness"
LOG_DIR="/var/log/miniharness"
PORT_UI="8501"

echo "==> [1/8] 系统依赖"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  python3 python3-venv python3-pip git curl >/dev/null

echo "==> [2/8] 创建服务用户与日志目录"
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$SVC_USER"
mkdir -p "$LOG_DIR"
chown "$SVC_USER:$SVC_USER" "$LOG_DIR"

echo "==> [3/8] 拉取代码"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi
cd "$APP_DIR"

echo "==> [4/8] Python 虚拟环境与依赖"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "==> [5/8] 重建演示数据库（固定种子，可复现）"
if [[ " $* " == *" --force-db "* ]] || [ ! -f data/shop.db ] || [ ! -f data/ledger.db ]; then
  .venv/bin/python scripts/gen_mock_data.py
  .venv/bin/python scripts/build_db.py
else
  echo "    db 已存在，跳过（如需重建加 --force-db）"
fi

echo "==> [6/8] 写入 .env（网关 key + 口令）"
API_KEY="${MINI_HARNESS_API_KEY:-${DASHSCOPE_API_KEY:-}}"
if [ -z "$API_KEY" ]; then
  echo "    !! API key 未设置 → 走 mock 模式（演示可用，但非真实模型）"
fi
cat > "$APP_DIR/.env" <<EOF
MINI_HARNESS_API_KEY=${API_KEY}
MINI_HARNESS_BASE_URL=${MINI_HARNESS_BASE_URL:-https://maas-api.simple-server.cn/v1}
MINI_HARNESS_MODEL=${MINI_HARNESS_MODEL:-qwen4-max}
MINI_HARNESS_MODEL_FALLBACK=${MINI_HARNESS_MODEL_FALLBACK:-kimi-k2.6}
MINI_HARNESS_RUNTIME=loop
MINI_HARNESS_API_BASE=http://127.0.0.1:8000
MINI_HARNESS_DEMO_PASSWORD=${DEMO_PASSWORD:-}
EOF
chown "$SVC_USER:$SVC_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

echo "==> [7/8] 安装 systemd 服务"
cp deploy/miniharness-api.service deploy/miniharness-ui.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now miniharness-api.service
sleep 2
systemctl enable --now miniharness-ui.service

echo "==> [8/8] 放行防火墙端口（UFW，如启用）"
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "active"; then
  ufw allow "$PORT_UI/tcp" comment 'MiniHarness UI'
fi

echo ""
echo "==> 网关自检（key / IP 白名单 / 模型名 / JSON 输出）"
echo "    若报 403 IP 白名单，请把【服务器公网 IP】加入网关 key 的允许列表后重跑："
echo "    cd $APP_DIR && .venv/bin/python deploy/verify_gateway.py"
.venv/bin/python deploy/verify_gateway.py || true

echo ""
echo "==================== 部署完成 ===================="
echo "状态检查:"
systemctl --no-pager --lines=0 status miniharness-api.service | head -3
systemctl --no-pager --lines=0 status miniharness-ui.service | head -3
echo ""
echo "公网访问: http://<服务器公网IP>:$PORT_UI"
echo "口令: ${DEMO_PASSWORD:-(未设置，无口令保护)}"
echo ""
echo "提醒: 阿里云轻量服务器需在控制台【防火墙】放行 $PORT_UI 端口；"
echo "      API(8000) 不要放行公网——它只绑 127.0.0.1。"
echo "自检: tail -f $LOG_DIR/api.log $LOG_DIR/ui.log"
