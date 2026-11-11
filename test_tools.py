"""Call the RFP tool functions directly (no AI involved) and print results."""

import json

from rfp_server import list_rfps, read_page, read_pages, search_rfp


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

section('read_pages("elysian", 1, 3) — first 150 characters of each page')
chunk = read_pages("elysian", 1, 3)
print(f"(pages {chunk['start']}–{chunk['end']} of {chunk['total_pages']})")
for p in chunk["pages"]:
    same = p["text"] == read_page("elysian", p["page"])["text"]
    print(f"\n--- page {p['page']} ({len(p['text'])} chars, identical to read_page: {same})")
    print(p["text"][:150])

section("read_pages edge cases")
nyoig = read_pages("nyoig", 101, 110)
print(f'read_pages("nyoig", 101, 110): {len(nyoig["pages"])} pages, '
      f'labels {nyoig["pages"][0]["page"]}–{nyoig["pages"][-1]["page"]}')
tail = read_pages("elysian", 2, 10)
print(f'read_pages("elysian", 2, 10): pages {[p["page"] for p in tail["pages"]]}; note: {tail.get("note")}')
for args in [("nyoig", 1, 11), ("elysian", 4, 5), ("elysian", 3, 1)]:
    try:
        read_pages(*args)
        print(f"read_pages{args}: no error (unexpected)")
    except Exception as exc:
        print(f"read_pages{args}: ERROR {exc}")
