"""RFP agent: builds a compliance matrix from one RFP, reading it only via the MCP server.

Usage: venv/bin/python rfp_agent.py elysian
"""

import asyncio
import json
import re
import sys
from collections import Counter
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font

# ---------------------------------------------------------------- configuration
MODEL = "claude-sonnet-5-5"
EFFORT = "high"  # low | medium | high | xhigh | max
MAX_TOOL_CALLS = 60
MAX_TOKENS = 128000
MAX_COVERAGE_REMINDERS = 3  # times the agent is sent back to read unread pages

# USD per 1M tokens for MODEL (update together with MODEL).
PRICE_INPUT = 2.00
PRICE_OUTPUT = 10.00
PRICE_CACHE_WRITE = 2.50  # 5-minute cache write = 1.25x input
PRICE_CACHE_READ = 0.20

ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "rfp_server.py"
RULEBOOK = ROOT / "labels" / "RULEBOOK.md"  # the ONLY file in labels/ this code reads
OUTPUT_DIR = ROOT / "outputs"

TYPES = [
    "Technical",
    "Submission",
    "Evaluation",
    "Legal/Contractual",
    "Insurance",
    "Staffing",
    "Info",
]
COLUMNS = ["requirement", "section", "page", "type", "quote", "quote_ok", "type_ok"]

INSTRUCTIONS = f"""

---

# Your task

You are building a compliance matrix for one RFP. Apply the rulebook above exactly.
You can read the RFP only through the tools list_rfps, search_rfp, read_pages and
read_page.

How to work:
1. Use list_rfps to get the page count.
2. Search with several terms: "shall", "must", "required", "will", "submit", "evaluat".
   Note that search_rfp matches whole words, so "evaluat" may find nothing; also try
   "evaluation" and "evaluated".
3. Read EVERY page, not only pages with search hits. Obligations often appear without
   signal words (lists, tables, headings). Read the document with read_pages in
   consecutive chunks of up to 10 pages (1-10, 11-20, ...) until the last page. Use
   read_page only to re-read a single page.
4. Label requirements, submission instructions, evaluation criteria and Info items,
   one obligation per row, as the rulebook says (including the Submission exception).
5. Before giving your final answer, review your rows against the list-intro rule:
   if a row's quote is a sentence that introduces a list (for example, it ends with
   a colon or refers to "the following"), and the list items are labelled as their
   own rows, delete the intro row. The obligation is carried by the items.
   Keep such a row only if the sentence itself states a separate obligation beyond
   introducing the list, such as a Submission row's deadline and delivery method.

You have a budget of {MAX_TOOL_CALLS} tool calls in total. Plan so that reading every
page fits in the budget.

For each item return an object with exactly these keys:
- "requirement": short description in your own words
- "section": the PDF heading the item falls under, as written
- "page": PDF page number (integer, as labelled by read_pages / read_page)
- "type": one of {", ".join(TYPES)}
- "quote": exact text copied from that page's text in read_pages / read_page output, never retyped or
  paraphrased; it must appear verbatim on the cited page

Final answer: output ONLY a JSON array of these objects. No prose, no code fences.
"""


class AgentError(Exception):
    """A clean, user-facing failure."""


# ---------------------------------------------------------------- helpers
def build_system_prompt() -> str:
    """RULEBOOK.md word for word, followed by the task instructions."""
    return RULEBOOK.read_text(encoding="utf-8") + INSTRUCTIONS


def result_text(result) -> str:
    """Flatten an MCP CallToolResult into text for Claude."""
    if result.structured_content is not None:
        return json.dumps(result.structured_content, ensure_ascii=False)
    return "\n".join(c.text for c in result.content if getattr(c, "type", "") == "text")


def result_data(result):
    """Decode an MCP CallToolResult into Python data."""
    if result.structured_content is not None:
        data = result.structured_content
    else:
        data = json.loads(result_text(result))
    # Servers wrap non-object return values as {"result": ...}.
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    return data


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def parse_items(text: str) -> list[dict]:
    """Extract the JSON array from the model's final message."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start == -1 or end < start:
        raise ValueError("no JSON array found")
    items = json.loads(cleaned[start : end + 1])
    if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
        raise ValueError("JSON is not a list of objects")
    return items


def check_type(item: dict) -> None:
    """Set type_ok; normalize type to the rulebook spelling when it matches."""
    canonical = {t.lower(): t for t in TYPES}
    raw = str(item.get("type", "")).strip()
    if raw.lower() in canonical:
        item["type"] = canonical[raw.lower()]
        item["type_ok"] = True
    else:
        item["type_ok"] = False


async def check_quotes(session: ClientSession, rfp_file: str, items: list[dict]) -> None:
    """Citation check: the quote must appear on the cited page (case/whitespace-insensitive)."""
    page_cache: dict[int, str | None] = {}
    for item in items:
        try:
            page = int(item.get("page"))
        except (TypeError, ValueError):
            item["quote_ok"] = False
            continue
        if page not in page_cache:
            res = await session.call_tool("read_page", {"rfp": rfp_file, "page": page})
            page_cache[page] = None if res.is_error else result_data(res)["text"]
        page_text = page_cache[page]
        quote = str(item.get("quote", ""))
        item["quote_ok"] = bool(page_text and quote.strip() and _norm(quote) in _norm(page_text))


def write_outputs(stem: str, items: list[dict]) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(exist_ok=True)
    rows = [{col: item.get(col) for col in COLUMNS} for item in items]

    json_path = OUTPUT_DIR / f"{stem}_matrix.json"
    json_path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")

    wb = Workbook()
    ws = wb.active
    ws.title = "Compliance matrix"
    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows:
        ws.append([row[col] for col in COLUMNS])
    widths = {"requirement": 45, "section": 30, "page": 7, "type": 18,
              "quote": 80, "quote_ok": 10, "type_ok": 10}
    for idx, col in enumerate(COLUMNS, start=1):
        letter = ws.cell(row=1, column=idx).column_letter
        ws.column_dimensions[letter].width = widths[col]
        for cell in ws[letter][1:]:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    ws.freeze_panes = "A2"
    xlsx_path = OUTPUT_DIR / f"{stem}_matrix.xlsx"
    wb.save(xlsx_path)
    return json_path, xlsx_path


def save_raw(stem: str, text: str) -> Path:
    OUTPUT_DIR.mkdir(exist_ok=True)
    path = OUTPUT_DIR / f"{stem}_raw_response.txt"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------- agent loop
async def check_model(client: anthropic.AsyncAnthropic) -> None:
    try:
        await client.models.retrieve(MODEL)
    except anthropic.NotFoundError:
        raise AgentError(f"Model '{MODEL}' was not found. Change MODEL at the top of rfp_agent.py.")
    except anthropic.AuthenticationError:
        raise AgentError("API key rejected. Check ANTHROPIC_API_KEY in .env.")


def page_ranges(pages: list[int]) -> str:
    """[1, 2, 3, 7, 9, 10] -> '1–3, 7, 9–10'."""
    out, start = [], None
    for i, p in enumerate(pages):
        if start is None:
            start = p
        if i == len(pages) - 1 or pages[i + 1] != p + 1:
            out.append(str(start) if start == p else f"{start}–{p}")
            start = None
    return ", ".join(out)


async def run_loop(client, session, tools, rfp_file: str, total_pages: int,
                   usage: Counter) -> tuple[str, int]:
    """Run the tool-use loop. Returns (final text, tool calls used).

    Coverage check: before the final answer is accepted, every page must have been read
    via read_page/read_pages. Otherwise the agent is told which pages are unread and must
    read and label them; their items are merged with the earlier ones in code.
    """
    messages = [{
        "role": "user",
        "content": f"Build the compliance matrix for the RFP '{rfp_file}'.",
    }]
    system = build_system_prompt()
    calls = 0
    limit_hit = False
    pages_read: set[int] = set()
    collected: list[dict] = []
    reminders = 0

    while True:
        async with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": EFFORT},
            cache_control={"type": "ephemeral"},
        ) as stream:
            response = await stream.get_final_message()

        u = response.usage
        usage["input"] += u.input_tokens
        usage["output"] += u.output_tokens
        usage["cache_write"] += u.cache_creation_input_tokens or 0
        usage["cache_read"] += u.cache_read_input_tokens or 0

        if response.model != MODEL:
            raise AgentError(f"Response came from '{response.model}', not '{MODEL}'.")
        if response.stop_reason == "refusal":
            details = response.stop_details
            reason = f" (category: {details.category})" if details and details.category else ""
            raise AgentError(f"The model refused the request{reason}. No output was produced.")
        if response.stop_reason == "max_tokens":
            raise AgentError(f"Response hit max_tokens ({MAX_TOKENS}) and was cut off.")

        messages.append({"role": "assistant", "content": response.content})
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        text = "".join(b.text for b in response.content if b.type == "text")

        if response.stop_reason != "tool_use" or not tool_uses:
            missing = sorted(set(range(1, total_pages + 1)) - pages_read)
            if not missing and reminders == 0:
                return text, calls
            try:
                batch = parse_items(text)
            except (ValueError, json.JSONDecodeError) as exc:
                raw = save_raw(Path(rfp_file).stem, text)
                raise AgentError(f"Answer was not valid JSON ({exc}). Raw text saved to {raw}.")
            collected.extend(item for item in batch if item not in collected)
            if not missing:
                return json.dumps(collected, ensure_ascii=False), calls
            if limit_hit:
                raise AgentError(f"Tool limit of {MAX_TOOL_CALLS} reached with pages still unread: "
                                 f"{page_ranges(missing)}.")
            if reminders >= MAX_COVERAGE_REMINDERS:
                raise AgentError(f"Pages still unread after {reminders} coverage reminders: "
                                 f"{page_ranges(missing)}.")
            reminders += 1
            print(f"  Coverage check: pages not read: {page_ranges(missing)}. "
                  "Asking the agent to read and label them.")
            messages.append({"role": "user", "content": (
                f"Coverage check: you have not read pages {page_ranges(missing)} of {rfp_file}. "
                "Every page must be read. Read all of these pages now with read_pages (up to 10 "
                "pages per call) and label every item on them, following the rulebook. Then output "
                "ONLY a JSON array of the NEW items from these pages. Do not repeat items you have "
                "already returned; they are kept. If these pages contain no items, output []."
            )})
            continue

        if limit_hit:
            # Already told to finish; still calling tools. Stop cleanly.
            raise AgentError(
                f"Tool limit of {MAX_TOOL_CALLS} reached and the model kept calling tools "
                "instead of returning JSON.",
            )

        results = []
        for block in tool_uses:
            if calls >= MAX_TOOL_CALLS:
                limit_hit = True
                results.append({
                    "type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": "Tool limit reached; this call was not run.",
                })
                continue
            calls += 1
            print(f"  [{calls:>2}/{MAX_TOOL_CALLS}] {block.name}({json.dumps(block.input)})")
            try:
                res = await session.call_tool(block.name, block.input)
                if not res.is_error and block.name == "read_page":
                    pages_read.add(int(result_data(res)["page"]))
                elif not res.is_error and block.name == "read_pages":
                    pages_read.update(int(p["page"]) for p in result_data(res)["pages"])
                results.append({
                    "type": "tool_result", "tool_use_id": block.id,
                    "content": result_text(res) or "(empty)", "is_error": bool(res.is_error),
                })
            except Exception as exc:  # transport or protocol failure
                results.append({
                    "type": "tool_result", "tool_use_id": block.id, "is_error": True,
                    "content": f"Tool call failed: {exc}",
                })

        user_content = results
        if calls >= MAX_TOOL_CALLS:
            limit_hit = True
            user_content = results + [{
                "type": "text",
                "text": f"Tool limit of {MAX_TOOL_CALLS} calls reached. Do not call any more "
                        "tools. Return your final JSON array now, using what you have read.",
            }]
        messages.append({"role": "user", "content": user_content})


def print_summary(items, calls, usage, json_path, xlsx_path) -> None:
    cost = (
        usage["input"] * PRICE_INPUT
        + usage["output"] * PRICE_OUTPUT
        + usage["cache_write"] * PRICE_CACHE_WRITE
        + usage["cache_read"] * PRICE_CACHE_READ
    ) / 1_000_000
    by_type = Counter(i["type"] if i["type_ok"] else f"INVALID: {i.get('type')!r}" for i in items)

    print("\n" + "=" * 60 + "\nSUMMARY\n" + "=" * 60)
    print(f"Items: {len(items)}")
    for t in TYPES:
        if by_type.get(t):
            print(f"  {t:<18} {by_type[t]}")
    for t, n in by_type.items():
        if t not in TYPES:
            print(f"  {t:<18} {n}")
    print(f"Citation check:  {sum(i['quote_ok'] for i in items)}/{len(items)} quotes found on cited page")
    print(f"Type check:      {sum(i['type_ok'] for i in items)}/{len(items)} valid types")
    print(f"Tool calls:      {calls}/{MAX_TOOL_CALLS}")
    print(f"Tokens:          input {usage['input']:,} | output {usage['output']:,} | "
          f"cache write {usage['cache_write']:,} | cache read {usage['cache_read']:,}")
    print(f"Estimated cost:  ${cost:.4f}  ({MODEL})")
    print(f"Saved:           {json_path.relative_to(ROOT)}, {xlsx_path.relative_to(ROOT)}")

    failed = [i for i in items if not i["quote_ok"]]
    if failed:
        print("\nQuotes NOT found on cited page:")
        for i in failed:
            print(f"  p{i.get('page')}: {str(i.get('quote'))[:100]!r}")


async def main(rfp: str) -> None:
    load_dotenv(ROOT / ".env")
    client = anthropic.AsyncAnthropic()
    await check_model(client)

    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Resolve the RFP name up front (not counted against the model's budget).
            available = {r["file"]: r["pages"]
                         for r in result_data(await session.call_tool("list_rfps", {}))}
            wanted = rfp if rfp.lower().endswith(".pdf") else f"{rfp}.pdf"
            if wanted not in available:
                raise AgentError(f"Unknown RFP '{rfp}'. Available: {', '.join(available)}")
            stem = Path(wanted).stem

            tools = [
                {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                for t in (await session.list_tools()).tools
            ]

            print(f"Model {MODEL} OK. Analysing {wanted} with tools: "
                  f"{', '.join(t['name'] for t in tools)}")
            usage: Counter = Counter()
            text, calls = await run_loop(client, session, tools, wanted, available[wanted], usage)

            try:
                items = parse_items(text)
            except (ValueError, json.JSONDecodeError) as exc:
                raw = save_raw(stem, text)
                raise AgentError(f"Final answer was not valid JSON ({exc}). Raw text saved to {raw}.")

            for item in items:
                check_type(item)
            await check_quotes(session, wanted, items)

    json_path, xlsx_path = write_outputs(stem, items)
    print_summary(items, calls, usage, json_path, xlsx_path)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: venv/bin/python rfp_agent.py <rfp name, e.g. elysian>")
    def _find_agent_error(exc: BaseException) -> AgentError | None:
        """AgentErrors raised inside the MCP client's task groups arrive wrapped in ExceptionGroups."""
        if isinstance(exc, AgentError):
            return exc
        if isinstance(exc, BaseExceptionGroup):
            for inner in exc.exceptions:
                found = _find_agent_error(inner)
                if found is not None:
                    return found
        return None

    try:
        asyncio.run(main(sys.argv[1]))
    except (AgentError, BaseExceptionGroup) as exc:
        err = _find_agent_error(exc)
        if err is None:
            raise
        sys.exit(f"ERROR: {err}")
