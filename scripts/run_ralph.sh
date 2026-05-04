#!/usr/bin/env bash
set -e
DONE=projects/er_review/.ralph_done
LOG=projects/er_review/.ralph_log
PROMPT=projects/er_review/.ralph_prompt.md
MAX=30
i=0
echo "=== ralph driver started at $(date) ===" >> "$LOG"
while [ ! -f "$DONE" ] && [ "$i" -lt "$MAX" ]; do
  echo "=== tick $i at $(date) ===" >> "$LOG"
  cat "$PROMPT" | claude -p --dangerously-skip-permissions >> "$LOG" 2>&1 || {
    echo "tick $i failed; retrying" >> "$LOG"
  }
  i=$((i+1))
done
if [ -f "$DONE" ]; then
  echo "=== ralph driver done at $(date) ===" >> "$LOG"
else
  echo "=== ralph driver hit MAX=$MAX without DONE ===" >> "$LOG"
fi
