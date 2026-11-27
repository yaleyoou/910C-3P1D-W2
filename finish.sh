#!/usr/bin/env bash
# P1 container. Validate against original frozen IDs then write measured table.
set -eo pipefail
cd /deploy
[[ "$ROLE" == p1 && "$PHASE" == blocks ]]
common=/deploy/runs/$CASE/$TRIAL
cp "$common/cal/p1/known-match.json" /run-data/known-match.json
python3 tools/check_clock.py /run-data
python3 tools/audit_requests.py /run-data > /run-data/request-audit.json
python3 -c 'import json; assert json.load(open("/run-data/request-audit.json"))["complete"],"D request audit failed"'
python3 tools/analyze_f1.py /run-data > /run-data/overlap.json
python3 tools/result.py "$common" --details /run-data/per-P.json > /run-data/result.csv
python3 tools/result.py --markdown "$common" > /run-data/result.md
cat /run-data/result.md

