#!/usr/bin/env bash
# GX10 GPU suite: preflight probes + real ship cycles (no stub-ship duplicates).
# Default ~25–40 min; --with-email adds email ship_live in the same warm coder block.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source .venv/bin/activate

# Match sprint_crew.graph.lanes._HEALTH_TIMEOUT_SECONDS
LANE_HEALTH_TIMEOUT=1200

WITH_EMAIL=0
WITH_AGENT_LIVE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-email)
      WITH_EMAIL=1
      shift
      ;;
    --with-agent-live)
      WITH_AGENT_LIVE=1
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [--with-email] [--with-agent-live]"
      echo "  Default: work lane preflight, then one coder block (preflight + greeter ship_live)"
      echo "  --with-email: also run email validators ship_live in the same coder block"
      echo "  --with-agent-live: run agent_live tests (reviewer on work lane, coder before ship_live)"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

LOG="${ROOT}/.gx10-test-run.log"
: >"$LOG"

log() {
  echo "[$(date -Iseconds)] $*" | tee -a "$LOG"
}

fail() {
  log "FAIL: $*"
  exit 1
}

lane_container_name() {
  case "$1" in
    coder) echo "infra-vllm-coder-1" ;;
    work) echo "infra-vllm-work-1" ;;
    *) fail "unknown lane: $1" ;;
  esac
}

dump_lane_logs() {
  local name="$1"
  local container
  container="$(lane_container_name "$name")"
  log "--- docker logs $container (last 80 lines) ---"
  docker logs "$container" 2>&1 | tail -80 | tee -a "$LOG" || true
  log "--- end docker logs ---"
}

lane_hard_reset() {
  log "lane_hard_reset: stop all + compose down"
  "$ROOT/scripts/lane-ctl.sh" stop all || true
  docker compose -f "$ROOT/infra/docker-compose.yml" --profile all down --remove-orphans 2>/dev/null || true
  sleep 5
}

wait_lane() {
  local name="$1"
  local url="$2"
  local deadline=$((SECONDS + LANE_HEALTH_TIMEOUT))
  log "Waiting for $name at $url/health (timeout ${LANE_HEALTH_TIMEOUT}s) ..."
  while (( SECONDS < deadline )); do
    if curl -sf "$url/health" >/dev/null 2>&1; then
      log "OK  $name healthy"
      return 0
    fi
    sleep 5
  done
  dump_lane_logs "$name"
  return 1
}

start_lane_with_retry() {
  local name="$1"
  local url="$2"
  local attempt
  for attempt in 1 2; do
    log "start_lane_with_retry: $name attempt $attempt/2"
    "$ROOT/scripts/lane-ctl.sh" start "$name"
    if wait_lane "$name" "$url"; then
      return 0
    fi
    log "WARN: $name not healthy on attempt $attempt"
    if [[ "$attempt" -eq 1 ]]; then
      lane_hard_reset
    fi
  done
  fail "$name not healthy after 2 attempts (${LANE_HEALTH_TIMEOUT}s each)"
}

run_pytest() {
  local label="$1"
  shift
  log "=== $label ==="
  "$@" 2>&1 | tee -a "$LOG"
  if [[ "${PIPESTATUS[0]}" -ne 0 ]]; then
    fail "$label"
  fi
  log "PASS $label"
}

log "=== PHASE 0: prerequisites + lane cleanup ==="
python "$ROOT/scripts/check_sandbox_prerequisites.py" --gx10 --require-fixture \
  || fail "GX10 prerequisites"
lane_hard_reset

log "=== PHASE 1: preflight probes (work lane) ==="
start_lane_with_retry work http://127.0.0.1:8002
run_pytest "preflight_work_tools" env PREFLIGHT_LIVE=1 pytest \
  tests/preflight/test_vllm_probes.py::test_probe_vllm_tools_work -q
run_pytest "preflight_json" env PREFLIGHT_LIVE=1 pytest \
  tests/preflight/test_vllm_probes.py::test_probe_json -q

if [[ "$WITH_AGENT_LIVE" -eq 1 ]]; then
  log "=== PHASE 1b: agent_live (Work lane — reviewer, tech_lead, formatter) ==="
  run_pytest "agent_live_work" env VLLM_LIVE=1 pytest tests/agent_live \
    -m "agent_live" -k "(reviewer or formatter or tester_reporter or tech_lead) and not tool_loop_complex" -q
fi

"$ROOT/scripts/lane-ctl.sh" stop work
"$ROOT/scripts/lane-ctl.sh" wait-stopped work

log "=== PHASE 2: coder block (preflight + ship_live greeter [+ email]) ==="
lane_hard_reset
start_lane_with_retry coder http://127.0.0.1:8001

if [[ "$WITH_AGENT_LIVE" -eq 1 ]]; then
  log "=== PHASE 2b: agent_live (coder lane) ==="
  run_pytest "agent_live_coder" env VLLM_LIVE=1 pytest \
    tests/agent_live/test_coder_live.py \
    -m "agent_live and vllm_live" -q
fi

run_pytest "preflight_coder_tools" env PREFLIGHT_LIVE=1 pytest \
  tests/preflight/test_vllm_probes.py::test_probe_vllm_tools -q
run_pytest "ship_live_greeter" env INTEGRATION_LIVE=1 VLLM_LIVE=1 pytest \
  tests/integration_live/test_ship_live_cycles.py::test_greeter_full_cycle_real_ship -q

if [[ "$WITH_EMAIL" -eq 1 ]]; then
  log "=== Restart coder lane for email ship_live (greeter cycle stops lanes) ==="
  start_lane_with_retry coder http://127.0.0.1:8001
  run_pytest "ship_live_email" env INTEGRATION_LIVE=1 VLLM_LIVE=1 pytest \
    tests/integration_live/test_ship_live_cycles.py::test_email_validation_full_cycle_real_ship -q
fi

log "=== final cleanup ==="
"$ROOT/scripts/lane-ctl.sh" stop all || true

log "=== GX10 SUITE COMPLETE ==="
echo "Full log: $LOG"
