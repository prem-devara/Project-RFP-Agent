"""Aggregate repeated agent runs from scorer outputs only (never reads labels/*_labels.xlsx).

Usage: venv/bin/python eval/aggregate_runs.py [rfp ...]   (default: annarbor kingsport nyoig)

Reads outputs/v2_runs/<rfp>/run<n>/<rfp>_score.xlsx (summary + missed sheets) and
agent_log.txt. Runs with a FAILED marker or no score file are reported and skipped.
Prints recall/precision mean and [min–max] per RFP and per type, key rows missed in
every run vs. only some runs, and cost.
"""

import re
import sys
from collections import defaultdict
from pathlib import Path

from openpyxl import load_workbook

BASE = Path(__file__).resolve().parent.parent / "outputs" / "v2_runs"
RFPS = sys.argv[1:] or ["annarbor", "kingsport", "nyoig"]
TYPES = ["All types", "Technical", "Submission", "Evaluation", "Legal/Contractual", "Insurance", "Staffing"]


def load_run(d: Path):
    wb = load_workbook(next(d.glob("*_score.xlsx")), read_only=True)
    table, extra = {}, {}
    for r in wb["summary"].iter_rows(values_only=True):
        if r and r[0] in TYPES and len(r) >= 7 and isinstance(r[1], int):
            table[r[0]] = {"key": r[1], "key_hit": r[2], "agent": r[4], "agent_hit": r[5]}
        elif r and r[0] in ("Dropped (outside scored pages)", "Agent items total", "Estimated cost (USD)"):
            extra[r[0]] = r[1]
    missed = {}
    rows = wb["missed"].iter_rows(values_only=True)
    header = next(rows)
    for r in rows:
        if r and r[0]:
            rec = dict(zip(header, r))
            missed[str(rec["key id"])] = rec
    log = (d / "agent_log.txt").read_text()
    m = re.search(r"Estimated cost:\s+\$([\d.]+)", log)
    t = re.search(r"Tool calls:\s+(\d+)/", log)
    return {
        "table": table, "missed": missed,
        "items": extra.get("Agent items total"), "dropped": extra.get("Dropped (outside scored pages)"),
        "judge_cost": float(str(extra.get("Estimated cost (USD)", "$0")).strip("$")),
        "agent_cost": float(m.group(1)) if m else 0.0,
        "tool_calls": int(t.group(1)) if t else None,
        "reminders": log.count("Coverage check:"),
    }


def stat(values):
    if not values:
        return "n/a"
    mean = sum(values) / len(values)
    return f"{mean:6.1%}  [{min(values):.1%}–{max(values):.1%}]"


total_cost = 0.0
for rfp in RFPS:
    run_dirs = sorted(p for p in (BASE / rfp).glob("run*") if p.is_dir())
    ok_dirs = [d for d in run_dirs if any(d.glob("*_score.xlsx")) and not (d / "FAILED").exists()]
    failed = [d.name for d in run_dirs if d not in ok_dirs]
    runs = [load_run(d) for d in ok_dirs]
    n = len(runs)
    print(f"\n{'=' * 78}\n{rfp.upper()}  —  {n} successful run(s)" + (f"; failed: {failed}" if failed else ""))
    print("=" * 78)
    if not n:
        continue
    print("Per run:  items " + ", ".join(str(r["items"]) for r in runs)
          + " | dropped " + ", ".join(str(r["dropped"]) for r in runs)
          + " | tool calls " + ", ".join(str(r["tool_calls"]) for r in runs)
          + " | coverage reminders " + ", ".join(str(r["reminders"]) for r in runs))
    for i, r in enumerate(runs, 1):
        t = r["table"]["All types"]
        print(f"  run{i}: recall {t['key_hit']}/{t['key']}  precision {t['agent_hit']}/{t['agent']}  "
              f"cost ${r['agent_cost'] + r['judge_cost']:.2f}")
    cost = sum(r["agent_cost"] + r["judge_cost"] for r in runs)
    total_cost += cost

    print(f"\n{'Type':20}{'Recall  mean [range]':>30}{'Precision  mean [range]':>32}")
    for typ in TYPES:
        rec = [r["table"][typ]["key_hit"] / r["table"][typ]["key"]
               for r in runs if typ in r["table"] and r["table"][typ]["key"]]
        pre = [r["table"][typ]["agent_hit"] / r["table"][typ]["agent"]
               for r in runs if typ in r["table"] and r["table"][typ]["agent"]]
        if not rec and not pre:
            continue
        keys = {r["table"][typ]["key"] for r in runs if typ in r["table"]}
        label = f"{typ} ({'/'.join(map(str, sorted(keys)))} key)" if typ != "All types" else f"OVERALL ({keys.pop()} key)"
        print(f"{label:28}{stat(rec):>22}{stat(pre):>32}")

    counts = defaultdict(int)
    info = {}
    for r in runs:
        for kid, rec in r["missed"].items():
            counts[kid] += 1
            info[kid] = rec
    order = lambda k: int(re.sub(r"\D", "", k) or 0)
    always = sorted((k for k, c in counts.items() if c == n), key=order)
    sometimes = sorted((k for k, c in counts.items() if c < n), key=lambda k: (-counts[k], order(k)))
    print(f"\nMissed in every run ({len(always)}):")
    for k in always:
        rec = info[k]
        print(f"  {k:<5} p{rec['page']:<3} [{rec['type']}]  {rec['requirement']}")
    print(f"Missed in some runs ({len(sometimes)}):")
    for k in sometimes:
        rec = info[k]
        print(f"  {k:<5} p{rec['page']:<3} [{rec['type']}]  {rec['requirement']}   ({counts[k]}/{n} runs)")
    print(f"\nCost for {n} runs: ${cost:.2f}")

print(f"\nTotal cost: ${total_cost:.2f}")
