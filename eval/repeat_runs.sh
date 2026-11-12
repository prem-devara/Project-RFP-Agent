#!/bin/bash
# Run the agent N times on one RFP, score each successful run, and keep each run's outputs
# in outputs/v2_runs/<rfp>/run<n>/. The scorer runs only if the agent exited 0 AND wrote a
# new matrix. Runs that already have a score file are skipped, so a failed batch can be
# resumed by re-running the same command.
#
# Usage: bash eval/repeat_runs.sh <rfp> <last run number> [first run number, default 1]
#   e.g. bash eval/repeat_runs.sh nyoig 3      -> runs 1, 2, 3
#        bash eval/repeat_runs.sh nyoig 3 2    -> runs 2, 3
set -u
cd "$(dirname "$0")/.." || exit 1
if [ $# -lt 2 ]; then
  echo "Usage: bash eval/repeat_runs.sh <rfp> <last run number> [first run number]" >&2
  exit 2
fi
rfp="$1"; runs="$2"; first="${3:-1}"
base="outputs/v2_runs/$rfp"
mkdir -p "$base"

mtime() {  # file modification time in seconds; 0 if missing (macOS and Linux)
  stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0
}

for n in $(seq "$first" "$runs"); do
  dir="$base/run$n"
  if ls "$dir"/*_score.xlsx >/dev/null 2>&1 && [ ! -e "$dir/FAILED" ]; then
    echo "$rfp run$n: already scored, skipping"
    continue
  fi
  mkdir -p "$dir"
  rm -f "$dir/FAILED"
  before=$(mtime "outputs/${rfp}_matrix.json")
  sleep 1  # mtime has 1-second resolution

  venv/bin/python rfp_agent.py "$rfp" > "$dir/agent_log.txt" 2>&1
  rc=$?
  after=$(mtime "outputs/${rfp}_matrix.json")
  if [ "$rc" -ne 0 ] || [ "$after" = "$before" ]; then
    echo "$rfp run$n: AGENT FAILED (exit $rc); scorer skipped" | tee "$dir/FAILED"
    continue
  fi
  cp "outputs/${rfp}_matrix.json" "outputs/${rfp}_matrix.xlsx" "$dir/"

  venv/bin/python score.py "$rfp" > "$dir/score_log.txt" 2>&1
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "$rfp run$n: SCORER FAILED (exit $rc)" | tee "$dir/FAILED"
    continue
  fi
  cp "outputs/${rfp}_matches.json" "outputs/${rfp}_score.xlsx" "$dir/"
  echo "$rfp run$n: OK"
done
