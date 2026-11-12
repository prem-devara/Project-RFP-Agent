# Reproducing the v2 evaluation batch

Three runs of the v2 agent on each test RFP (Ann Arbor, Kingsport, NYOIG), each run scored
against the answer key, then summarised as mean and range. Elysian was the development RFP
and is not part of the batch.

## Code versions

| Files | Commit | Role |
|---|---|---|
| `rfp_agent.py`, `rfp_server.py`, `labels/RULEBOOK.md` | `dd7d42c` (agent v2) | Frozen: the system under test |
| `score.py` | `d2ed1a3` | Scorer (judge `claude-sonnet-5-5`, `SCORED_PAGES`) |
| `eval/repeat_runs.sh`, `eval/aggregate_runs.py` | this folder | Batch runner and summary |

Check that the frozen files match before running. This should print nothing:

```bash
git diff dd7d42c -- rfp_agent.py rfp_server.py labels/RULEBOOK.md
```

## Setup

```bash
python3 -m venv venv && venv/bin/pip install -r requirements.txt
echo "ANTHROPIC_API_KEY=..." > .env   # see .env.example
```

The answer keys `labels/{annarbor,kingsport,nyoig}_labels.xlsx` must be present. Only
`score.py` reads them. The agent and `aggregate_runs.py` never do.

## Commands

From the repository root:

```bash
bash eval/repeat_runs.sh annarbor 3
bash eval/repeat_runs.sh kingsport 3
bash eval/repeat_runs.sh nyoig 3
venv/bin/python eval/aggregate_runs.py
```

The three RFPs write different files, so they can run in parallel (append `&` to each
line, then run `wait`). Runs that already have a score file are skipped, so after a
failure (for example, API credits running out) you can re-run the same command to finish
the batch.

## Outputs (gitignored)

```
outputs/v2_runs/<rfp>/run<n>/
    agent_log.txt          agent console output: tool calls, coverage reminders, tokens, cost
    <rfp>_matrix.json      agent matrix (.xlsx copy alongside)
    score_log.txt          scorer summary, missed and extra lists
    <rfp>_matches.json     raw judge output
    <rfp>_score.xlsx       summary / matches / missed / extra sheets
    FAILED                 only present if the agent or scorer failed for that run
```

`outputs/<rfp>_*` (without `v2_runs/`) always holds the most recent run.

`aggregate_runs.py` prints the following for each RFP:
- recall and precision as mean and [min–max], overall and per type;
- key rows missed in every run vs. only some runs;
- cost.

## Cost and time (November 2026, `claude-sonnet-5-5`)

About $6.75 for the full batch: Ann Arbor ~$0.36 per run, Kingsport ~$0.46, NYOIG
~$1.20–1.80. NYOIG runs take several minutes each.

## Notes

- Results vary between runs: recall within about ±1 point on Ann Arbor and Kingsport, and
  up to ±5 points on NYOIG, where only 9 pages are scored. Compare ranges, not single runs.
- Each agent run is scored by one judge run, so judge variance is included in the range.
- Agent items on pages outside `SCORED_PAGES` (in `score.py`) are dropped before matching.
  The agent still reads and labels every page.
