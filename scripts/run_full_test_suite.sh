#!/usr/bin/env bash
# Full A-Z test verification for sprint-crew-v2 (Tier 1+2 always; Tier 3 optional).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source .venv/bin/activate

SKIP_GPU=0
WITH_EMAIL=0
WITH_AGENT_LIVE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-gpu)
      SKIP_GPU=1
      shift
      ;;
    --with-email)
      WITH_EMAIL=1
      shift
      ;;
    --with-agent-live)
      WITH_AGENT_LIVE=1
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [--skip-gpu] [--with-email] [--with-agent-live]"
      echo "  Default: Tier 1 (unit) + Tier 2 (sandbox) + GX10 GPU (preflight + greeter ship_live)"
      echo "  --skip-gpu: stop after integration_live (no vLLM)"
      echo "  --with-email: also run email ship_live in GX10 phase (~+15–20 min)"
      echo "  --with-agent-live: GX10 includes per-agent agent_live tests"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

LOG="${ROOT}/.test-suite-run.log"
: >"$LOG"

log() {
  echo "[$(date -Iseconds)] $*" | tee -a "$LOG"
}

fail() {
  log "FAIL: $*"
  exit 1
}

run_pytest() {
  local label="$1"
  shift
  log "=== $label ==="
  if "$@" 2>&1 | tee -a "$LOG"; then
    log "PASS $label"
  else
    fail "$label"
  fi
}

log "=== PHASE 0: cleanup ==="
"$ROOT/scripts/lane-ctl.sh" stop all || true
docker compose -f "$ROOT/infra/docker-compose.yml" --profile all down --remove-orphans 2>/dev/null || true
sleep 2

log "=== PHASE 1: unit tests ==="
run_pytest "unit" pytest tests/unit -q

log "=== PHASE 2: sandbox prerequisites ==="
python "$ROOT/scripts/check_sandbox_prerequisites.py" --skip-jira-smoke \
  || fail "sandbox prerequisites"

log "=== PHASE 3: credential smoke ==="
python scripts/verify_integrations.py 2>&1 | tee -a "$LOG" || fail "verify_integrations"

log "=== PHASE 4: integration_live (no GPU) ==="
run_pytest "integration_live" env INTEGRATION_LIVE=1 pytest tests/integration_live \
  -m "integration_live and not vllm_live" -q

if [[ "$SKIP_GPU" -eq 1 ]]; then
  log "=== SKIPPED GPU phases (--skip-gpu) ==="
  log "=== TIER 1+2 COMPLETE ==="
  echo "Full log: $LOG"
  exit 0
fi

log "=== PHASE 5: GX10 GPU suite (delegated) ==="
chmod +x "$ROOT/scripts/run_gx10_test_suite.sh"
GX10_ARGS=()
if [[ "$WITH_EMAIL" -eq 1 ]]; then
  GX10_ARGS+=(--with-email)
fi
if [[ "$WITH_AGENT_LIVE" -eq 1 ]]; then
  GX10_ARGS+=(--with-agent-live)
fi
"$ROOT/scripts/run_gx10_test_suite.sh" "${GX10_ARGS[@]}" 2>&1 | tee -a "$LOG"
if [[ "${PIPESTATUS[0]}" -ne 0 ]]; then
  fail "GX10 GPU suite"
fi

log "=== ALL PHASES COMPLETE ==="
echo "Full log: $LOG"
