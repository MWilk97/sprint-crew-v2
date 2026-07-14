#!/usr/bin/env bash
# Manage vLLM inference lanes and vector stack on GX10.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE=(docker compose -f "$ROOT/infra/docker-compose.yml" --env-file "$ROOT/.env")

usage() {
  cat <<'EOF'
Usage: lane-ctl.sh start|stop|wait-stopped|status|health coder|work|vector|all

Profiles map to docker-compose:
  coder   -> vllm-coder   (:8001)
  work    -> vllm-work    (:8002) ScrumMaster + TechLead + Formatter + Reviewer
  vector  -> qdrant (:6333) + embed-sidecar (:8080)
  all     -> coder + work

stop: docker stop + wait until container is not running
wait-stopped: poll until container status != running (timeout 180s)
EOF
}

normalize_lane() {
  case "$1" in
    work|coder|qdrant|embed|vector|all) echo "$1" ;;
    *) return 1 ;;
  esac
}

container_name() {
  echo "infra-$(service_for "$1")-1"
}

wait_stopped() {
  local target="$1"
  local name
  name="$(container_name "$target")"
  local deadline=$((SECONDS + 180))
  while (( SECONDS < deadline )); do
    local status
    status="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo "missing")"
    if [[ "$status" != "running" ]]; then
      return 0
    fi
    sleep 2
  done
  echo "TIMEOUT: $name still running after 180s" >&2
  return 1
}

lane_url() {
  case "$1" in
    coder) echo "http://127.0.0.1:8001" ;;
    work) echo "http://127.0.0.1:8002" ;;
    qdrant) echo "http://127.0.0.1:6333" ;;
    embed) echo "http://127.0.0.1:8080" ;;
    *) return 1 ;;
  esac
}

service_for() {
  case "$1" in
    coder) echo "vllm-coder" ;;
    work) echo "vllm-work" ;;
    qdrant) echo "qdrant" ;;
    embed) echo "embed-sidecar" ;;
    *) return 1 ;;
  esac
}

cmd="${1:-}"
target="${2:-}"

if [[ -z "$cmd" || -z "$target" ]]; then
  usage
  exit 1
fi

if ! target="$(normalize_lane "$target")"; then
  usage
  exit 1
fi

case "$cmd" in
  start)
    case "$target" in
      coder)
        "${COMPOSE[@]}" --profile coder up -d vllm-coder
        ;;
      work)
        "${COMPOSE[@]}" --profile work up -d vllm-work
        ;;
      vector)
        "${COMPOSE[@]}" --profile vector up -d qdrant embed-sidecar
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
      coder|work|qdrant|embed)
        docker stop "$(container_name "$target")" 2>/dev/null || true
        wait_stopped "$target"
        ;;
      vector)
        docker stop "$(container_name qdrant)" "$(container_name embed)" 2>/dev/null || true
        wait_stopped qdrant || true
        wait_stopped embed || true
        ;;
      all)
        "${COMPOSE[@]}" --profile all down || true
        "${COMPOSE[@]}" --profile vector down || true
        ;;
      *)
        usage
        exit 1
        ;;
    esac
    ;;
  wait-stopped)
    case "$target" in
      coder|work|qdrant|embed) wait_stopped "$target" ;;
      vector)
        wait_stopped qdrant || true
        wait_stopped embed || true
        ;;
      all)
        for lane in coder work; do
          wait_stopped "$lane" || true
        done
        ;;
      *)
        usage
        exit 1
        ;;
    esac
    ;;
  status)
    docker ps --filter name=infra- --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    ;;
  health)
    fail=0
    check_one() {
      local name="$1"
      local url
      url="$(lane_url "$name")"
      local path="/health"
      if [[ "$name" == "qdrant" ]]; then
        path="/healthz"
      fi
      if curl -sf "$url$path" >/dev/null; then
        echo "OK  $name  $url"
      else
        echo "DOWN $name $url"
        fail=1
      fi
    }
    case "$target" in
      coder|work|qdrant|embed) check_one "$target" ;;
      vector)
        check_one qdrant
        check_one embed
        ;;
      all)
        check_one coder
        check_one work
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
