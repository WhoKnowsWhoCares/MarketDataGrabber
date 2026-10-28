#!/usr/bin/env bash
# Install MarketDataGrabber cron jobs (marker: # MDG:auto) into the user's crontab.
# Idempotent: removes previous MDG entries, appends fresh ones from crontab.mdg.
# Foreign crontab lines are never touched. No sudo required.
#
# Usage: install_cron.sh [--dry-run]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MDG_FILE="$SCRIPT_DIR/crontab.mdg"
DRY_RUN=0
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=1
fi

if [[ ! -f "$MDG_FILE" ]]; then
  echo "error: $MDG_FILE not found" >&2
  exit 1
fi

current="$(crontab -l 2>/dev/null || true)"

# Keep only foreign lines: drop MDG job lines and the dangling block header comment.
kept="$(printf '%s\n' "$current" | grep -v -E '# MDG:auto|^# MarketDataGrabber scheduled jobs' || true)"

new="$(printf '%s\n%s\n' "$kept" "$(cat "$MDG_FILE")")"

echo "--- crontab diff (old -> new) ---"
diff <(printf '%s\n' "$current") <(printf '%s\n' "$new") || true
echo "--- end diff ---"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "--dry-run: crontab not modified"
  exit 0
fi

printf '%s\n' "$new" | crontab -
echo "crontab installed: $(printf '%s\n' "$new" | grep -c '# MDG:auto') MDG jobs"
