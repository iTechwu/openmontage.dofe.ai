#!/usr/bin/env bash
# OpenMontage "caller-is-the-runtime" worker bootstrap — runtime-agnostic.
#
# OpenMontage is a pure MCP server and does not execute stages itself. The
# runtime that calls it (DSH / Codex / Claude Code / …) runs this bootstrap to
# (1) provision the OpenMontage *client* and (2) run `openmontage worker run`
# against the shared projects dir. Each stage is then dispatched to YOUR agent
# executor (see docs/RUNTIME_INTEGRATION.md for the exact contract).
#
# Modes:
#   --provision   Idempotently install the OpenMontage client runtime (package
#                 deps + ffmpeg + remotion node_modules) into a persistent dir.
#   --run         Source OpenMontage env and exec `openmontage worker run`.
#   (default)     provision, then run.
#
# This script does NOT provision or choose your agent executor. Provision your
# runtime's executor first, then set these BEFORE calling --run (they win over
# any value in .env):
#   OPENMONTAGE_AGENT_EXECUTOR_JSON   JSON argv array (your runtime's agent CLI)
#   OPENMONTAGE_AGENT_MODEL_ID        exact catalog-verified model id
#
# The OpenMontage package itself runs from this checkout because
# `python3 -m openmontage` prepends the cwd to sys.path; the persistent
# site-packages only supplies third-party deps.
set -euo pipefail

RUNTIME_DIR="${OPENMONTAGE_RUNTIME_DIR:-/var/lib/openmontage-runtime}"
SITE_PACKAGES="$RUNTIME_DIR/site-packages"
REPO="${OPENMONTAGE_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export PYTHONPATH="${SITE_PACKAGES}${PYTHONPATH:+:$PYTHONPATH}"
export PATH="$RUNTIME_DIR/bin:$PATH"

provision() {
  # 1. ffmpeg (system, idempotent; required for the remotion render stage).
  #    Prefer a fast China mirror over the slow deb.debian.org default.
  if ! command -v ffmpeg >/dev/null 2>&1; then
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*; do
      [ -f "$f" ] && sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' "$f" 2>/dev/null || true
    done
    apt-get update -qq >/dev/null 2>&1 || true
    apt-get install -y -qq ffmpeg >/dev/null 2>&1 || true
  fi

  # 2. OpenMontage + third-party deps into the persistent volume (idempotent).
  #    Reuse the wheel the OpenMontage image exported, or install from the repo.
  mkdir -p "$SITE_PACKAGES"
  if ! python3 -c "import yaml, pydantic" >/dev/null 2>&1; then
    WHEEL="$(find /exchange/openmontage-wheel "$REPO" -maxdepth 2 -name 'openmontage-*.whl' 2>/dev/null | head -1 || true)"
    if [[ -n "$WHEEL" ]]; then
      python3 -m pip install --target "$SITE_PACKAGES" --break-system-packages "$WHEEL" >/dev/null 2>&1 || true
    else
      python3 -m pip install --target "$SITE_PACKAGES" --break-system-packages "$REPO" >/dev/null 2>&1 || true
    fi
    python3 -m pip install --target "$SITE_PACKAGES" --break-system-packages \
      youtube-transcript-api scenedetect opencv-python-headless faster-whisper >/dev/null 2>&1 || true
  fi

  # 3. remotion-composer node_modules (915M). Prefer the host repo copy; a
  #    deployment with no docker socket keeps the copy on the host repo.
  if [[ ! -x "$REPO/remotion-composer/node_modules/.bin/remotion" ]]; then
    if docker exec openmontage-openmontage-mcp-1 test -x /app/remotion-composer/node_modules/.bin/remotion 2>/dev/null; then
      (cd "$REPO/remotion-composer" && docker exec openmontage-openmontage-mcp-1 tar -C /app/remotion-composer -cf - node_modules | tar -xf -) 2>/dev/null || true
    fi
  fi
}

run_worker() {
  # Capture caller-provided overrides BEFORE sourcing .env so the runtime's
  # explicit executor/model always win over the producer's .env defaults.
  local _executor="${OPENMONTAGE_AGENT_EXECUTOR_JSON-}"
  local _model="${OPENMONTAGE_AGENT_MODEL_ID-}"
  local _timeout="${OPENMONTAGE_AGENT_TIMEOUT_SECONDS-}"

  set -a
  # shellcheck disable=SC1091
  . "$REPO/.env" 2>/dev/null || true
  set +a

  [[ -n "$_executor" ]] && export OPENMONTAGE_AGENT_EXECUTOR_JSON="$_executor"
  [[ -n "$_model" ]] && export OPENMONTAGE_AGENT_MODEL_ID="$_model"
  [[ -n "$_timeout" ]] && export OPENMONTAGE_AGENT_TIMEOUT_SECONDS="$_timeout"

  export OPENMONTAGE_PROJECTS_DIR="${OPENMONTAGE_PROJECTS_DIR:-/data/projects}"
  export OPENMONTAGE_AGENT_TIMEOUT_SECONDS="${OPENMONTAGE_AGENT_TIMEOUT_SECONDS:-3600}"
  export OPENMONTAGE_PYTHON="${OPENMONTAGE_PYTHON:-python3}"

  cd "$REPO"
  exec python3 -m openmontage worker run --interval 2 --json
}

case "${1:-}" in
  --provision) provision ;;
  --run) run_worker ;;
  *) provision; run_worker ;;
esac
