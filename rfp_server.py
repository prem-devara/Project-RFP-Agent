"""Read-only MCP server exposing tools over the RFP PDFs in rfps/."""

import re
from pathlib import Path

# mcp>=2 renamed FastMCP to MCPServer (same high-level decorator API).
from mcp.server.mcpserver import MCPServer
# ToolError messages reach the client; other exceptions are hidden as generic errors.
from mcp.server.mcpserver.exceptions import ToolError
from pypdf import PdfReader

RFP_DIR = Path(__file__).resolve().parent / "rfps"
MAX_MATCHES = 50
CONTEXT_CHARS = 100  # on each side of a match -> ~200 chars of context
MAX_PAGES_PER_CALL = 10

mcp = MCPServer("rfp-server")

_page_cache: dict[str, list[str]] = {}


def _available() -> dict[str, Path]:
    """Map of file name -> path for every PDF directly inside rfps/."""
    return {
        p.name: p
        for p in sorted(RFP_DIR.iterdir())
        if p.is_file() and p.suffix.lower() == ".pdf"
    }


def _resolve(rfp: str) -> Path:
    """Resolve a user-supplied RFP name to an existing PDF in rfps/ (exact match only)."""
    files = _available()
    name = rfp.strip()
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    if name not in files:
        raise ToolError(
            f"Unknown RFP '{rfp}'. Available: {', '.join(files) or '(none)'}"
        )
    return files[name]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _get_pages(rfp: str) -> tuple[str, list[str]]:
    """Return (file name, list of normalized page texts), extracting once per PDF."""
    path = _resolve(rfp)
    if path.name not in _page_cache:
        reader = PdfReader(path)  # read-only; the file is never written
        _page_cache[path.name] = [
            _normalize(page.extract_text() or "") for page in reader.pages
        ]
    return path.name, _page_cache[path.name]


@mcp.tool()
def list_rfps() -> list[dict]:
    """List every RFP PDF in rfps/ with its file name and page count."""
    result = []
    for name, path in _available().items():
        if name in _page_cache:
            pages = len(_page_cache[name])
        else:
            pages = len(PdfReader(path).pages)
        result.append({"file": name, "pages": pages})
    return result


@mcp.tool()
def search_rfp(rfp: str, query: str) -> dict:
    """Case-insensitive whole-word/phrase search in one RFP.

    Returns each match with its page number (1-based, physical position) and
    about 200 characters of surrounding text. Capped at 50 matches.
    """
    words = query.split()
    if not words:
        raise ToolError("Query must not be empty.")
    # Whole-word match; words in a phrase may be separated by any whitespace.
    pattern = re.compile(
        r"(?<!\w)" + r"\s+".join(re.escape(w) for w in words) + r"(?!\w)",
        re.IGNORECASE,
    )

    name, pages = _get_pages(rfp)
    matches = []
    capped = False
    for page_no, text in enumerate(pages, start=1):
        for m in pattern.finditer(text):
            if len(matches) >= MAX_MATCHES:
                capped = True
                break
            start = max(0, m.start() - CONTEXT_CHARS)
            end = min(len(text), m.end() + CONTEXT_CHARS)
            snippet = text[start:end]
            if start > 0:
                snippet = "…" + snippet
            if end < len(text):
                snippet += "…"
            matches.append({"page": page_no, "snippet": snippet})
        if capped:
            break

    result = {
        "rfp": name,
        "query": query,
        "total_shown": len(matches),
        "capped": capped,
        "matches": matches,
    }
    if capped:
        result["note"] = (
            f"Results capped at {MAX_MATCHES} matches; more exist. "
            "Refine the query to narrow results."
        )
    return result


@mcp.tool()
def read_page(rfp: str, page: int) -> dict:
    """Return the full text of one page (1-based, as numbered in the PDF viewer)."""
    name, pages = _get_pages(rfp)
    if not isinstance(page, int) or page < 1 or page > len(pages):
        raise ToolError(
            f"Page {page} does not exist in {name} (valid pages: 1–{len(pages)})."
        )
    return {"rfp": name, "page": page, "total_pages": len(pages), "text": pages[page - 1]}


@mcp.tool()
def read_pages(rfp: str, start: int, end: int) -> dict:
    """Return the full text of pages start..end inclusive (1-based), at most 10 pages per call.

    Each page is labelled with its page number. If end is past the last page, the
    range stops at the last page.
    """
    name, pages = _get_pages(rfp)
    total = len(pages)
    if not isinstance(start, int) or not isinstance(end, int):
        raise ToolError("start and end must be integers.")
    if end < start:
        raise ToolError(f"end ({end}) must not be before start ({start}).")
    if end - start + 1 > MAX_PAGES_PER_CALL:
        raise ToolError(
            f"Requested {end - start + 1} pages ({start}–{end}); the limit is "
            f"{MAX_PAGES_PER_CALL} pages per call. Split the range into smaller chunks."
        )
    if start < 1 or start > total:
        raise ToolError(f"Page {start} does not exist in {name} (valid pages: 1–{total}).")
    last = min(end, total)
    result = {
        "rfp": name,
        "start": start,
        "end": last,
        "total_pages": total,
        "pages": [{"page": n, "text": pages[n - 1]} for n in range(start, last + 1)],
    }
    if end > total:
        result["note"] = f"{name} has {total} pages; returned pages {start}–{last}."
    return result


if __name__ == "__main__":
    mcp.run()
