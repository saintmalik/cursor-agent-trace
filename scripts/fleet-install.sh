#!/usr/bin/env bash
# Silent per-user Cursor Agent TRACE install (fleet / MDM helper).
# Usage:
#   TRACE_HOME=/opt/cursor-agent-trace ./scripts/fleet-install.sh
#   ./scripts/fleet-install.sh   # uses this repo root
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${TRACE_HOME:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
PYTHON="${PYTHON:-python3}"

if [[ ! -f "${ROOT}/lib/__main__.py" ]]; then
  echo "error: TRACE_HOME does not look like cursor-agent-trace: ${ROOT}" >&2
  exit 1
fi

cd "${ROOT}"
exec "${PYTHON}" -m lib install-hooks --user --quiet "$@"
