#!/usr/bin/env bash
# Push fixtures/repo to dedicated GitHub sandbox repo (main = fixture baseline).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

LOCK_FILE="$ROOT/.bootstrap.lock"
if [[ -f "$LOCK_FILE" ]]; then
  echo "FAIL: bootstrap lock present ($LOCK_FILE) — another bootstrap or ship_live may be running" >&2
  exit 1
fi
trap 'rm -f "$LOCK_FILE"' EXIT
touch "$LOCK_FILE"

_env_val() {
  local key="$1"
  if [[ -f .env ]]; then
    grep -m1 "^${key}=" .env 2>/dev/null | cut -d= -f2- || true
  fi
}

GITHUB_TOKEN="${GITHUB_TOKEN:-$(_env_val GITHUB_TOKEN)}"
GITHUB_FIXTURE_REPO_GREETER="${GITHUB_FIXTURE_REPO_GREETER:-$(_env_val GITHUB_FIXTURE_REPO_GREETER)}"

if [[ -z "${GITHUB_TOKEN:-}" ]]; then
  echo "GITHUB_TOKEN is required" >&2
  exit 1
fi

ensure_github_repo() {
  local slug="$1"
  local name="${slug##*/}"
  local code
  code="$(curl -s -o /dev/null -w "%{http_code}" \
    -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    "https://api.github.com/repos/${slug}")"
  if [[ "$code" == "200" ]]; then
    return 0
  fi
  echo "Creating GitHub repo ${slug} ..."
  curl -sf -X POST \
    -H "Authorization: Bearer ${GITHUB_TOKEN}" \
    -H "Accept: application/vnd.github+json" \
    https://api.github.com/user/repos \
    -d "{\"name\":\"${name}\",\"private\":true,\"auto_init\":false}"
}

push_fixture() {
  local label="$1"
  local fixture_dir="$2"
  local repo_slug="$3"

  if [[ -z "$repo_slug" ]]; then
    echo "SKIP $label — repo slug not set" >&2
    return 0
  fi

  ensure_github_repo "$repo_slug"

  local tmp verify_dir remote
  tmp="$(mktemp -d)"
  rsync -a --exclude '__pycache__' --exclude '*.pyc' "$fixture_dir/" "$tmp/"
  (
    cd "$tmp"
    git init -b main
    git config user.name "sprint-crew"
    git config user.email "sprint-crew@local"
    git add -A
    git commit -m "bootstrap: $label fixture baseline"
    remote="https://x-access-token:${GITHUB_TOKEN}@github.com/${repo_slug}.git"
    git push "$remote" main --force
  )
  rm -rf "$tmp"
  echo "OK  $label -> $repo_slug (main)"

  verify_dir="$(mktemp -d)"
  git clone --depth 1 "https://x-access-token:${GITHUB_TOKEN}@github.com/${repo_slug}.git" "$verify_dir"
  rm -rf "$verify_dir"
  echo "OK  shallow clone smoke for $repo_slug"
}

push_fixture "greeter+validators" "$ROOT/fixtures/repo" "${GITHUB_FIXTURE_REPO_GREETER:-}"

echo "PASS: fixture repos bootstrapped"
