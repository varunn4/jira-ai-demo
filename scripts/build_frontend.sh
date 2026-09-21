#!/usr/bin/env bash
# Build the React admin SPA into frontend/dist so FastAPI can serve it at /.
#
# Usage:  ./scripts/build_frontend.sh
#
# Requires Node 18+ and npm. If you use nvm, this script will try to load it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="$(cd "$SCRIPT_DIR/../frontend" && pwd)"

# Best-effort: load nvm so a system Node 12 doesn't shadow a newer install.
if ! command -v node >/dev/null 2>&1 || [ "$(node -v | cut -d. -f1 | tr -d v)" -lt 18 ]; then
  export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
  # shellcheck disable=SC1090
  [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh" && nvm use 18 >/dev/null 2>&1 || true
fi

echo "Using node $(node -v) / npm $(npm -v)"
cd "$FRONTEND_DIR"
npm install --no-audit --no-fund
npm run build
echo "Built SPA -> $FRONTEND_DIR/dist"
