"""Call the RFP tool functions directly (no AI involved) and print results."""

import json

from rfp_server import list_rfps, read_page, search_rfp


def section(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


section("list_rfps()")
print(json.dumps(list_rfps(), indent=2))

section('search_rfp("elysian", "must")')
res = search_rfp("elysian", "must")
print(f"Matches shown: {res['total_shown']}  |  capped: {res['capped']}")
if res.get("note"):
    print(f"NOTE: {res['note']}")
for i, m in enumerate(res["matches"], start=1):
    print(f"\n[{i}] page {m['page']}\n    {m['snippet']}")

section('read_page("elysian", 2) — first 500 characters')
page = read_page("elysian", 2)
print(f"(page {page['page']} of {page['total_pages']}, {len(page['text'])} chars total)\n")
print(page["text"][:500])
