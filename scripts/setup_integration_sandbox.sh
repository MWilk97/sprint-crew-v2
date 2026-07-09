#!/usr/bin/env bash
# One-shot sandbox setup: bootstrap fixture repo, verify credentials, run integration_live.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

find fixtures -type d \( -name __pycache__ -o -name .pytest_cache \) -exec rm -rf {} + 2>/dev/null || true

log() {
  echo "[setup] $*"
}

fail() {
  echo "[setup] FAIL: $*" >&2
  exit 1
}

for cmd in git curl python3; do
  command -v "$cmd" >/dev/null 2>&1 || fail "required command missing: $cmd"
done

if [[ ! -f "$ROOT/.env" ]]; then
  if [[ -f "$ROOT/.env.example" ]]; then
    log "Creating .env from .env.example — fill in Jira/GitHub tokens before continuing"
    cp "$ROOT/.env.example" "$ROOT/.env"
    fail ".env created — edit credentials (USE_MOCK_INTEGRATIONS=false, JIRA_*, GITHUB_*) and re-run"
  fi
  fail ".env missing and no .env.example found"
fi

if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  log "Creating virtualenv and installing package ..."
  python3 -m venv "$ROOT/.venv"
  "$ROOT/.venv/bin/pip" install -q -e "$ROOT[dev]"
fi

# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"

# Ensure live integrations unless user explicitly wants mocks
if grep -q '^USE_MOCK_INTEGRATIONS=true' "$ROOT/.env" 2>/dev/null; then
  log "Setting USE_MOCK_INTEGRATIONS=false in .env for sandbox setup"
  sed -i 's/^USE_MOCK_INTEGRATIONS=true/USE_MOCK_INTEGRATIONS=false/' "$ROOT/.env"
fi

log "Checking sandbox prerequisites (env only) ..."
python "$ROOT/scripts/check_sandbox_prerequisites.py" --skip-jira-smoke || fail "prerequisites check failed"

log "Bootstrapping fixture repo (fixtures/repo -> GITHUB_FIXTURE_REPO_GREETER) ..."
chmod +x "$ROOT/scripts/bootstrap_fixture_repos.sh"
"$ROOT/scripts/bootstrap_fixture_repos.sh"

log "Verifying Jira + GitHub credentials ..."
python "$ROOT/scripts/verify_integrations.py" || fail "verify_integrations failed"

log "Running integration_live tests (no GPU) ..."
INTEGRATION_LIVE=1 pytest tests/integration_live -m "integration_live and not vllm_live" -q \
  || fail "integration_live pytest failed"

log "PASS: sandbox ready — run GX10 suite with ./scripts/run_gx10_test_suite.sh when on GPU host"
