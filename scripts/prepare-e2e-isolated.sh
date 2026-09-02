#!/usr/bin/env bash
# StockMind V1 —— 隔离 E2E 环境准备脚本（可交付，不依赖固定工作目录）
#
# 作用：仅对"隔离部署"（docker-compose.iso.yml，独立 project / 独立卷 / 独立端口）
# 执行：构建启动 -> 重置隔离数据库种子 -> 等待 Beat 生成待审批计划 -> 恢复供应商故障模式。
# 绝不影响主项目 compose（stockmind，端口 3000/8000/8100）的业务数据。
#
# 用法：
#   bash scripts/prepare-e2e-isolated.sh [--no-build]
#     --no-build : 跳过镜像构建，仅启动已构建的隔离服务（默认会构建 frontend/api）
#
# 依赖：docker、docker compose、curl、bash 4+。全程不读取 .env，不输出任何密钥。
set -uo pipefail

# ---- 从脚本位置推导仓库根目录（不写死任何绝对路径）----
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.iso.yml"

ISOLATE_API="http://127.0.0.1:18000"     # 隔离 api
ISOLATE_MOCK="http://127.0.0.1:18100"    # 隔离 mock-supplier
ISOLATE_FRONTEND="http://127.0.0.1:13000"  # 隔离 frontend
HEALTH_URL="${ISOLATE_API}/health"
PLANS_URL="${ISOLATE_API}/api/v1/plans?status=pending_approval"

NO_BUILD="${1:-}"
[ "${NO_BUILD}" = "--no-build" ] && NO_BUILD=1 || NO_BUILD=0

say() { printf '[prepare-e2e-isolated] %s\n' "$*"; }
die() { printf '[prepare-e2e-isolated] ERROR: %s\n' "$*" >&2; exit 1; }

cd "${ROOT_DIR}" || die "无法进入仓库根目录 ${ROOT_DIR}"
[ -f "${COMPOSE_FILE}" ] || die "缺少 docker-compose.iso.yml"

# ---- 1) 构建并启动隔离服务（可选 --no-build）----
if [ "${NO_BUILD}" = "1" ]; then
  say "跳过构建，启动隔离服务（--no-build）"
  docker compose -f "${COMPOSE_FILE}" up -d || die "隔离服务启动失败"
else
  say "构建并启动隔离服务"
  docker compose -f "${COMPOSE_FILE}" build frontend api || die "隔离镜像构建失败"
  docker compose -f "${COMPOSE_FILE}" up -d || die "隔离服务启动失败"
fi

# ---- 2) 健康轮询：等待隔离 api 就绪（带超时与报错，不用固定长 sleep）----
say "等待隔离 api 健康（${HEALTH_URL}）"
api_ready=0
for i in $(seq 1 60); do
  if curl -sf "${HEALTH_URL}" >/dev/null 2>&1; then
    api_ready=1
    break
  fi
  sleep 2
done
[ "${api_ready}" = "1" ] || die "隔离 api 在 120s 内未就绪"

# ---- 3) 重置隔离数据库种子（仅隔离库；主库不受影响）----
# 重置会 DROP SCHEMA，必须先停掉持有连接的 api/worker/beat，避免死锁；
# 重置后再启动（只动隔离服务，主项目不受影响）。
say "重置隔离数据库种子（先停隔离 api/worker/beat 释放连接）"
docker compose -f "${COMPOSE_FILE}" stop api worker beat >/dev/null 2>&1 || true
# 用一次性容器执行 seed --reset（隔离库内完成，无业务连接占用）
docker compose -f "${COMPOSE_FILE}" run --rm --no-deps api \
  python -m app.seed.seed --reset >/dev/null 2>&1 \
  || { say "隔离种子重置失败（尝试再次启动后重试）"; docker compose -f "${COMPOSE_FILE}" up -d api worker beat >/dev/null 2>&1 || true; die "隔离种子重置失败"; }
say "隔离种子已重置"
# 重启隔离服务（api 启动时 alembic 幂等；不触发主项目）
docker compose -f "${COMPOSE_FILE}" up -d api worker beat >/dev/null 2>&1 || die "隔离服务重启失败"

# ---- 3.5) 等待隔离 api 重新就绪 ----
api_ready=0
for i in $(seq 1 60); do
  if curl -sf "${HEALTH_URL}" >/dev/null 2>&1; then api_ready=1; break; fi
  sleep 2
done
[ "${api_ready}" = "1" ] || die "隔离 api 重启后 120s 内未就绪"

# ---- 4) 轮询等待 Beat 生成待审批计划（带超时，不用固定 70s sleep）----
say "等待隔离 Beat 生成待审批计划（${PLANS_URL}）"
plans_ready=0
for i in $(seq 1 60); do
  code=$(curl -s -o /dev/null -w "%{http_code}" "${PLANS_URL}" -H "X-Actor-Id: carol")
  if [ "${code}" = "200" ]; then
    plans_ready=1
    break
  fi
  sleep 3
done
[ "${plans_ready}" = "1" ] || die "隔离 Beat 在 180s 内未生成待审批计划（HTTP=${code}）"
say "待审批计划已生成（HTTP 200）"

# ---- 5) 恢复隔离供应商故障模式为 normal ----
say "恢复隔离供应商故障模式为 normal"
curl -sf -X PUT "${ISOLATE_MOCK}/fault-modes/SUP-001" -H "Content-Type: application/json" -d '{"mode":"normal"}' >/dev/null 2>&1 || die "无法连接隔离 mock-supplier"
# 通过隔离 api 的管理接口切换（保证角色与审计路径一致）
for sid in SUP-001 SUP-002 SUP-003 SUP-004 SUP-005; do
  curl -sf -X PUT "${ISOLATE_API}/api/v1/admin/fault-modes/${sid}" \
    -H "Content-Type: application/json" -H "X-Actor-Id: eve" \
    -d '{"mode":"normal"}' >/dev/null 2>&1 \
    || die "切换 ${sid} 故障模式失败"
done
say "故障模式已恢复 normal"

# ---- 6) 汇总输出（供 E2E_BASE_URL 使用）----
cat <<EOF
[prepare-e2e-isolated] 隔离环境就绪：
  E2E_BASE_URL=${ISOLATE_FRONTEND}
  API=${ISOLATE_API}
  模拟供应商=${ISOLATE_MOCK}
  停止隔离环境：docker compose -f ${COMPOSE_FILE} down
EOF
