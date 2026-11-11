"""Score the agent's compliance matrix against the answer key.

Usage: venv/bin/python score.py elysian

This is the ONLY code that reads labels/*_labels.xlsx. It is never imported by
rfp_agent.py, and the agent never reads its outputs. Both input files are read-only.
"""

import json
import sys
from collections import Counter
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font

# ---------------------------------------------------------------- configuration
JUDGE_MODEL = "claude-sonnet-5-5"
JUDGE_EFFORT = "high"
JUDGE_MAX_TOKENS = 32000

# USD per 1M tokens for JUDGE_MODEL (update together with JUDGE_MODEL).
PRICE_INPUT = 2.00
PRICE_OUTPUT = 10.00
PRICE_CACHE_WRITE = 2.50
PRICE_CACHE_READ = 0.20

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "outputs"
LABELS_DIR = ROOT / "labels"
KEY_SHEET = "labels"
FIELDS = ["requirement", "section", "page", "type", "quote"]
TYPE_ORDER = ["Technical", "Submission", "Evaluation", "Legal/Contractual", "Insurance", "Staffing"]
INFO = "info"

JUDGE_SYSTEM = """You compare two lists of items extracted from the same RFP: an AGENT list and an ANSWER KEY.
For each agent item, decide which answer-key row describes the same obligation.

Rules:
- Match on the obligation itself (what must be done, provided, submitted, or how proposals are scored), not merely on sharing the same source sentence. Several key rows can share one quote while each states a different obligation; match only the key row whose obligation the agent item states.
- Each agent item matches at most ONE key row. If an agent item combines two or more separate key obligations, match it only to the key row it primarily states; do not match it to the others.
- Several agent items may match the same key row.
- Exception for submission instructions: a key row of type Submission describing one submission instruction (including its deadline, delivery methods and address) is a single row. If the agent splits that instruction into several parts, every part matches that one key row.
- Differences in page, section or type do not by themselves prevent a match; judge the obligation.
- If no key row states the same obligation, return "none" as the key_id.

Return exactly one entry per agent item, in the order given, each with a one-line reason."""

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "key_id": {"type": "string"},  # one key id, or "none"
                    "reason": {"type": "string"},
                },
                "required": ["agent_id", "key_id", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["matches"],
    "additionalProperties": False,
}


class ScoreError(Exception):
    """A clean, user-facing failure."""


# ---------------------------------------------------------------- loading (read-only)
def load_agent(path: Path) -> list[dict]:
    if not path.exists():
        raise ScoreError(f"Agent matrix not found: {path}. Run rfp_agent.py first.")
    items = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for n, item in enumerate(items, start=1):
        row = {"id": f"AG{n}"}
        row.update({f: item.get(f) for f in FIELDS})
        out.append(row)
    return out


def load_key(path: Path) -> list[dict]:
    if not path.exists():
        raise ScoreError(f"Answer key not found: {path}")
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if KEY_SHEET not in wb.sheetnames:
            raise ScoreError(f"Sheet '{KEY_SHEET}' not found in {path.name} (sheets: {wb.sheetnames})")
        rows = wb[KEY_SHEET].iter_rows(values_only=True)
        header = [str(h).strip().lower() if h is not None else "" for h in next(rows)]
        missing = [c for c in ["id"] + FIELDS if c not in header]
        if missing:
            raise ScoreError(f"Answer key is missing columns: {missing} (found: {header})")
        col = {name: header.index(name) for name in ["id"] + FIELDS}
        key, seen = [], set()
        for values in rows:
            if values is None or all(v is None for v in values):
                continue
            kid = values[col["id"]]
            if kid is None or str(kid).strip() == "":
                continue
            kid = str(kid).strip()
            if kid in seen:
                raise ScoreError(f"Duplicate id in answer key: {kid}")
            seen.add(kid)
            row = {"id": kid}
            row.update({f: values[col[f]] for f in FIELDS})
            key.append(row)
    finally:
        wb.close()
    if not key:
        raise ScoreError("Answer key has no rows.")
    return key


# ---------------------------------------------------------------- judge
def judge(agent: list[dict], key: list[dict]) -> tuple[list[dict], dict]:
    """One judge call. Returns (matches, usage)."""
    client = anthropic.Anthropic()
    payload = {"AGENT": agent, "ANSWER_KEY": key}
    with client.messages.stream(
        model=JUDGE_MODEL,
        max_tokens=JUDGE_MAX_TOKENS,
        system=JUDGE_SYSTEM,
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=1)}],
        output_config={"effort": JUDGE_EFFORT, "format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
    ) as stream:
        response = stream.get_final_message()

    if response.model != JUDGE_MODEL:
        raise ScoreError(f"Judge response came from '{response.model}', not '{JUDGE_MODEL}'.")
    if response.stop_reason == "refusal":
        d = response.stop_details
        raise ScoreError("The judge refused the request"
                         + (f" (category: {d.category})" if d and d.category else "") + ".")
    if response.stop_reason == "max_tokens":
        raise ScoreError(f"Judge output hit max_tokens ({JUDGE_MAX_TOKENS}) and was cut off.")

    text = next(b.text for b in response.content if b.type == "text")
    u = response.usage
    usage = {
        "input": u.input_tokens,
        "output": u.output_tokens,
        "cache_write": u.cache_creation_input_tokens or 0,
        "cache_read": u.cache_read_input_tokens or 0,
    }
    return json.loads(text)["matches"], usage


def validate_matches(matches: list[dict], agent: list[dict], key: list[dict]) -> dict[str, dict]:
    """Every agent id exactly once; key_id must be "none" or an existing key id.

    Returns {agent_id: match}, with key_ids = [] or [key_id] for the scoring step.
    """
    agent_ids = [a["id"] for a in agent]
    key_ids = {k["id"] for k in key}
    by_agent: dict[str, dict] = {}
    for m in matches:
        aid = m["agent_id"]
        if aid not in agent_ids:
            raise ScoreError(f"Judge returned unknown agent id: {aid}")
        if aid in by_agent:
            raise ScoreError(f"Judge returned agent id twice: {aid}")
        kid = str(m["key_id"]).strip()
        if kid.lower() == "none":
            by_agent[aid] = {**m, "key_ids": []}
        elif kid in key_ids:
            by_agent[aid] = {**m, "key_ids": [kid]}
        else:
            raise ScoreError(f"Judge returned unknown key id for {aid}: {kid!r}")
    missing = [a for a in agent_ids if a not in by_agent]
    if missing:
        raise ScoreError(f"Judge skipped agent item(s): {missing}")
    return by_agent


# ---------------------------------------------------------------- scoring (pure Python)
def _type(value) -> str:
    return str(value or "").strip()


def _is_info(value) -> bool:
    return _type(value).lower() == INFO


def _page(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _pct(num: int, den: int) -> str:
    return f"{num / den:.1%}" if den else "n/a"


def score(agent: list[dict], key: list[dict], by_agent: dict[str, dict]) -> dict:
    key_by_id = {k["id"]: k for k in key}
    scored_key = [k for k in key if not _is_info(k["type"])]
    info_key = [k for k in key if _is_info(k["type"])]

    matched_key_ids = {kid for m in by_agent.values() for kid in m["key_ids"]}

    def hits_scored(a):
        return any(not _is_info(key_by_id[k]["type"]) for k in by_agent[a["id"]]["key_ids"])

    def hits_info(a):
        return any(_is_info(key_by_id[k]["type"]) for k in by_agent[a["id"]]["key_ids"])

    agent_scored = [a for a in agent if not _is_info(a["type"])]
    agent_info = [a for a in agent if _is_info(a["type"])]

    # Types present in either list (excluding Info), canonical order first.
    present = {_type(k["type"]) for k in scored_key} | {_type(a["type"]) for a in agent_scored}
    lower_order = [t.lower() for t in TYPE_ORDER]
    types = [t for t in TYPE_ORDER if t.lower() in {p.lower() for p in present}]
    types += sorted(p for p in present if p.lower() not in lower_order)

    by_type = []
    for t in types:
        k_rows = [k for k in scored_key if _type(k["type"]).lower() == t.lower()]
        a_rows = [a for a in agent_scored if _type(a["type"]).lower() == t.lower()]
        k_hit = sum(k["id"] in matched_key_ids for k in k_rows)
        a_hit = sum(hits_scored(a) for a in a_rows)
        by_type.append({
            "type": t, "key_rows": len(k_rows), "key_matched": k_hit, "recall": _pct(k_hit, len(k_rows)),
            "agent_items": len(a_rows), "agent_matched": a_hit, "precision": _pct(a_hit, len(a_rows)),
        })

    recall_hit = sum(k["id"] in matched_key_ids for k in scored_key)
    precision_hit = sum(hits_scored(a) for a in agent_scored)

    pairs = []
    for a in agent:
        m = by_agent[a["id"]]
        for kid in m["key_ids"]:
            k = key_by_id[kid]
            pairs.append({
                "key_id": kid, "key_requirement": k["requirement"],
                "agent_id": a["id"], "agent_requirement": a["requirement"],
                "key_page": k["page"], "agent_page": a["page"],
                "page_agree": _page(k["page"]) is not None and _page(k["page"]) == _page(a["page"]),
                "key_type": k["type"], "agent_type": a["type"],
                "type_agree": _type(k["type"]).lower() == _type(a["type"]).lower(),
                "reason": m["reason"],
            })

    missed = [k for k in scored_key if k["id"] not in matched_key_ids]

    extra = []
    for a in agent:
        m = by_agent[a["id"]]
        if not m["key_ids"]:
            extra.append({**a, "status": "no match", "reason": m["reason"]})
        elif not _is_info(a["type"]) and not hits_scored(a):
            extra.append({**a, "status": "matched Info only", "reason": m["reason"]})

    return {
        "overall": {
            "scored_key_rows": len(scored_key), "key_matched": recall_hit,
            "recall": _pct(recall_hit, len(scored_key)),
            "agent_items_scored": len(agent_scored), "agent_matched": precision_hit,
            "precision": _pct(precision_hit, len(agent_scored)),
        },
        "by_type": by_type,
        "info": {
            "key_info_rows": len(info_key),
            "key_info_matched": sum(k["id"] in matched_key_ids for k in info_key),
            "agent_info_items": len(agent_info),
            "agent_info_matched_key_info": sum(hits_info(a) for a in agent_info),
            "agent_info_matched_scored": sum(hits_scored(a) for a in agent_info),
        },
        "agreement": {
            "pairs": len(pairs),
            "page_agree": sum(p["page_agree"] for p in pairs),
            "type_agree": sum(p["type_agree"] for p in pairs),
        },
        "pairs": pairs,
        "missed": missed,
        "extra": extra,
    }


def cost(usage: dict) -> float:
    return (usage["input"] * PRICE_INPUT + usage["output"] * PRICE_OUTPUT
            + usage["cache_write"] * PRICE_CACHE_WRITE + usage["cache_read"] * PRICE_CACHE_READ) / 1_000_000


# ---------------------------------------------------------------- output
def summary_rows(result: dict, usage: dict) -> list[list]:
    o, i, g = result["overall"], result["info"], result["agreement"]
    rows = [
        ["Overall (Info excluded)", "Key rows", "Matched", "Recall", "Agent items", "Matched", "Precision"],
        ["All types", o["scored_key_rows"], o["key_matched"], o["recall"],
         o["agent_items_scored"], o["agent_matched"], o["precision"]],
    ]
    for t in result["by_type"]:
        rows.append([t["type"], t["key_rows"], t["key_matched"], t["recall"],
                     t["agent_items"], t["agent_matched"], t["precision"]])
    rows += [
        [],
        ["Info (not scored)", "Count"],
        ["Key Info rows matched", f"{i['key_info_matched']}/{i['key_info_rows']}"],
        ["Agent Info items", i["agent_info_items"]],
        ["  matched a key Info row", i["agent_info_matched_key_info"]],
        ["  matched a scored key row", i["agent_info_matched_scored"]],
        [],
        ["Matched pairs", "Agree", "Total", "%"],
        ["Page agreement", g["page_agree"], g["pairs"], _pct(g["page_agree"], g["pairs"])],
        ["Type agreement", g["type_agree"], g["pairs"], _pct(g["type_agree"], g["pairs"])],
        [],
        ["Judge", JUDGE_MODEL],
        ["Tokens in / out", f"{usage['input']:,} / {usage['output']:,}"],
        ["Cache write / read", f"{usage['cache_write']:,} / {usage['cache_read']:,}"],
        ["Estimated cost (USD)", f"${cost(usage):.4f}"],
    ]
    return rows


def write_xlsx(path: Path, result: dict, usage: dict) -> None:
    wb = Workbook()

    def sheet(title, header, rows, widths):
        ws = wb.create_sheet(title)
        ws.append(header)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append(r)
        for idx, w in enumerate(widths, start=1):
            letter = ws.cell(row=1, column=idx).column_letter
            ws.column_dimensions[letter].width = w
            for cell in ws[letter][1:]:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.freeze_panes = "A2"

    s = summary_rows(result, usage)
    sheet("summary", s[0], s[1:], [30, 12, 10, 10, 12, 10, 10])
    sheet("matches",
          ["key id", "key requirement", "agent id", "agent requirement", "page agree", "type agree", "judge reason"],
          [[p["key_id"], p["key_requirement"], p["agent_id"], p["agent_requirement"],
            p["page_agree"], p["type_agree"], p["reason"]] for p in result["pairs"]],
          [9, 45, 9, 45, 11, 11, 60])
    sheet("missed", ["key id"] + FIELDS,
          [[k["id"]] + [k[f] for f in FIELDS] for k in result["missed"]],
          [9, 45, 30, 7, 18, 80])
    sheet("extra", ["agent id"] + FIELDS + ["status", "judge reason"],
          [[a["id"]] + [a[f] for f in FIELDS] + [a["status"], a["reason"]] for a in result["extra"]],
          [9, 45, 30, 7, 18, 80, 18, 60])
    del wb["Sheet"]
    wb.save(path)


def print_report(result: dict, usage: dict) -> None:
    print("=" * 72 + "\nSUMMARY\n" + "=" * 72)
    rows = summary_rows(result, usage)
    for r in rows:
        if not r:
            print()
        elif len(r) == 7:
            print(f"{r[0]:<26}{r[1]!s:>9}{r[2]!s:>9}{r[3]!s:>9}{r[4]!s:>13}{r[5]!s:>9}{r[6]!s:>11}")
        else:
            print("  ".join(f"{str(v):<26}" if n == 0 else str(v) for n, v in enumerate(r)))

    print("\n" + "=" * 72 + f"\nMISSED ({len(result['missed'])}) - key rows no agent item matched\n" + "=" * 72)
    for k in result["missed"]:
        print(f"{k['id']:<5} p{k['page']}  [{k['type']}]  {k['requirement']}")

    print("\n" + "=" * 72 + f"\nEXTRA ({len(result['extra'])}) - agent items that matched no scored key row\n" + "=" * 72)
    for a in result["extra"]:
        print(f"{a['id']:<5} p{a['page']}  [{a['type']}]  ({a['status']})  {a['requirement']}")
        print(f"      judge: {a['reason']}")


def main(rfp: str) -> None:
    load_dotenv(ROOT / ".env")
    stem = Path(rfp).stem
    agent = load_agent(OUTPUT_DIR / f"{stem}_matrix.json")
    key = load_key(LABELS_DIR / f"{stem}_labels.xlsx")
    print(f"Agent items: {len(agent)}  |  Answer key rows: {len(key)}  |  Judge: {JUDGE_MODEL}\n")

    matches, usage = judge(agent, key)
    (OUTPUT_DIR / f"{stem}_matches.json").write_text(
        json.dumps(matches, indent=2, ensure_ascii=False), encoding="utf-8")
    by_agent = validate_matches(matches, agent, key)

    result = score(agent, key, by_agent)
    xlsx = OUTPUT_DIR / f"{stem}_score.xlsx"
    write_xlsx(xlsx, result, usage)
    print_report(result, usage)
    print(f"\nSaved: outputs/{stem}_matches.json, {xlsx.relative_to(ROOT)}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: venv/bin/python score.py <rfp name, e.g. elysian>")
    try:
        main(sys.argv[1])
    except ScoreError as exc:
        sys.exit(f"ERROR: {exc}")
