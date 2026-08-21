#!/usr/bin/env bash
set -euo pipefail

ENV_NAME="${AEF_GRITS_ENV_NAME:-aef_grits_download}"
PROJECT_ID="${1:-${AEF_GRITS_PROJECT:-}}"

case "$(uname -s)" in
  Linux|Darwin) ;;
  *) echo "This helper supports Linux and macOS only." >&2; exit 2 ;;
esac

if command -v mamba >/dev/null 2>&1; then
  SOLVER=mamba
elif command -v conda >/dev/null 2>&1; then
  SOLVER=conda
else
  echo "Conda/Mamba is required. Install Miniforge, reopen the shell, and run this script again." >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if "$SOLVER" env list | awk '{print $1}' | grep -Fxq "$ENV_NAME"; then
  "$SOLVER" env update -n "$ENV_NAME" -f environment-download.yml --prune
else
  "$SOLVER" env create -n "$ENV_NAME" -f environment-download.yml
fi

echo
echo "Environment installed. Activate it with:"
echo "  conda activate $ENV_NAME"
echo
if [[ -n "$PROJECT_ID" ]]; then
  echo "After activation, explicitly authenticate and validate with:"
  echo "  aef-grits-auth login --source earthengine --auth-mode localhost --project $PROJECT_ID"
  echo "  aef-grits-auth verify --source auto --project $PROJECT_ID"
  echo "  aef-grits-doctor --auth-source auto --project $PROJECT_ID --output ./outputs"
else
  echo "Then run: aef-grits-auth login --source earthengine --auth-mode localhost --project YOUR_GEE_PROJECT"
fi
