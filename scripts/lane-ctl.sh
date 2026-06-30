#!/usr/bin/env bash
# Manage vLLM inference lanes on GX10.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "$ROOT/infra/docker-compose.yml" --env-file "$ROOT/.env")

usage() {
  cat <<'EOF'
Usage: lane-ctl.sh start|stop|status|health coder|planner|judge|all

Profiles map to docker-compose:
  coder   -> vllm-coder   (:8001)
  planner -> vllm-planner (:8002)
  judge   -> vllm-judge  (:8003)
  all     -> all three lanes
EOF
}

lane_url() {
  case "$1" in
    coder) echo "http://127.0.0.1:8001" ;;
    planner) echo "http://127.0.0.1:8002" ;;
    judge) echo "http://127.0.0.1:8003" ;;
    *) return 1 ;;
  esac
}

service_for() {
  case "$1" in
    coder) echo "vllm-coder" ;;
    planner) echo "vllm-planner" ;;
    judge) echo "vllm-judge" ;;
    *) return 1 ;;
  esac
}

cmd="${1:-}"
target="${2:-}"

if [[ -z "$cmd" || -z "$target" ]]; then
  usage
  exit 1
fi

case "$cmd" in
  start)
    case "$target" in
      coder)
        "${COMPOSE[@]}" --profile coder up -d vllm-coder
        ;;
      planner)
        "${COMPOSE[@]}" --profile all up -d vllm-planner
        ;;
      judge)
        "${COMPOSE[@]}" --profile judge up -d vllm-judge
        ;;
      all)
        "${COMPOSE[@]}" --profile all up -d
        ;;
      *)
        usage
        exit 1
        ;;
    esac
    ;;
  stop)
    case "$target" in
      coder|planner|judge)
        docker stop "infra-$(service_for "$target")-1" 2>/dev/null || true
        ;;
      all)
        "${COMPOSE[@]}" --profile all down || true
        ;;
      *)
        usage
        exit 1
        ;;
    esac
    ;;
  status)
    docker ps --filter name=infra-vllm --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    ;;
  health)
    fail=0
    check_one() {
      local name="$1"
      local url
      url="$(lane_url "$name")"
      if curl -sf "$url/health" >/dev/null; then
        echo "OK  $name  $url"
      else
        echo "DOWN $name $url"
        fail=1
      fi
    }
    case "$target" in
      coder|planner|judge) check_one "$target" ;;
      all)
        check_one coder
        check_one planner
        check_one judge
        ;;
      *)
        usage
        exit 1
        ;;
    esac
    exit "$fail"
    ;;
  *)
    usage
    exit 1
    ;;
esac
