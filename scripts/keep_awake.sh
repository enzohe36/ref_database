#!/bin/bash
set -e

INTERVAL=60
DISTANCE=1

echo "Keeping awake. Mouse jiggles ${DISTANCE}px every ${INTERVAL}s. Ctrl+C to stop."

while true; do
  cliclick m:+${DISTANCE},+0
  sleep 0.2
  cliclick m:-${DISTANCE},+0
  sleep "$INTERVAL"
done
