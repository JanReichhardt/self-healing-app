#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export AGENT_MODE="${AGENT_MODE:-deterministic}"
if [[ "$AGENT_MODE" != replay ]]; then
  command -v mvn >/dev/null || { echo "Install Maven: brew install maven"; exit 1; }
  java -version 2>&1 | head -1
  if ! java -version 2>&1 | head -1 | grep -Eq 'version "21[.]'; then
    echo 'Java 21 required. Set JAVA_HOME and put $JAVA_HOME/bin first on PATH.'; exit 1
  fi
fi
if [[ ! -x .venv/bin/python ]]; then
  echo 'Run uv sync --frozen --extra dev first (add --extra agent for OpenHands).'; exit 1
fi
echo "Dashboard: http://127.0.0.1:${PORT:-8000} · adapter: $AGENT_MODE"
exec .venv/bin/python -m uvicorn orchestrator.app:app --timeout-graceful-shutdown 3 --host 127.0.0.1 --port "${PORT:-8000}"
