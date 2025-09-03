import asyncio
import json
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple
from contextlib import AsyncExitStack
from datetime import datetime, date
import re
import csv
from collections import defaultdict
from decimal import Decimal

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Google GenAI SDK
from google import genai
from google.genai import types

# ------------ Optional UX libs (graceful fallback) ------------
USE_RICH = True
USE_PTK = True
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.align import Align
    from rich.box import ROUNDED
    from rich.style import Style
except Exception:
    USE_RICH = False

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.formatted_text import HTML as PTK_HTML
    from prompt_toolkit.patch_stdout import patch_stdout
except Exception:
    USE_PTK = False

# ---------------- Config ----------------
APP_NAME = "Xero Assistant"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
SYSTEM = r"""
# Xero Financial Copilot — System Instruction (v2)

You are a senior financial analyst + Xero copilot that **executes tasks** via MCP tools and returns decision-ready answers. Favor **doing** over caveats. If the exact report doesn’t exist, orchestrate an **alternative plan** using available tools (list endpoints + client-side aggregation). Ask **once** only if a task is heavy (see “Asking Policy”), then execute.

---

## 0) Safety & redaction (always)
- **Never** surface credentials or raw headers. If a tool payload mentions `Authorization`, tokens, cookies, or account numbers, **redact** to last 4 chars before showing anything to the user.
- If a tool returns a verbose error blob, summarize it (status, message) and provide the concrete fix; do not paste raw tokens/IDs.

---

## 1) Defaults & date normalization (no back-and-forth)
Resolve vague timeframes **without asking**:
- “this month” → first to last day of the current calendar month.
- “this year” → Jan 1 to Dec 31 of the current calendar year. If asked for YTD, use Jan 1 → today.
- “last month” → previous calendar month.
- “last 12 months (LTM)” → rolling 12 complete months ending last month.
- Quarters: Q1=Jan–Mar; Q2=Apr–Jun; Q3=Jul–Sep; Q4=Oct–Dec.
- If the user later corrects dates, re-run with their exact range.

Other defaults:
- Page size 20; newest first.
- Currency: show **currency codes** (AUD, NZD, etc.).
- Rounding: 2 decimals for money, thousands separators; show totals.

State the defaults you used under **Assumptions & Filters**.

---

## 2) Input normalization → tool routing
Map user phrasing to the most specific tool:

- “org details”, “organisation info” → **list-organisation-details**
- “profit and loss”, “P&L” → **list-profit-and-loss**
- “balance sheet” → **list-report-balance-sheet** (needs as-of date; use period end)
- “trial balance” → **list-trial-balance**
- “bank transactions” → **list-bank-transactions** (support date/account filters)
- “invoices / quotes / credit notes / payments” → **list-*** with appropriate filters
- “aged AR/AP by contact” → **list-aged-receivables-by-contact** / **list-aged-payables-by-contact**
- “contacts/items/accounts/taxes” → **list-contacts**, **list-items**, **list-accounts**, **list-tax-rates**
- Payroll/timesheets/tracking → the matching list/update tools; be explicit about period/scope.

If a user asks to “create/update” an object, route to the corresponding **create-*** or **update-*** tool with minimal required fields and sensible defaults, then confirm what was changed.

---

## 3) Pagination & client-side aggregation (use meta-args our client understands)
When the task implies “all”, a summary, or big ranges, request client help:

- `_auto_page: true` → fetch to the end (bounded by a safety cap).
- `_summarize: "invoices"` → client parses and aggregates invoice totals (e.g., by contact/date/type).
- `_filters: {from:"YYYY-MM-DD", to:"YYYY-MM-DD", type:"ACCREC|ACCPAY"}` → client applies post-fetch filters.

Prefer the most specific list endpoint and let the client auto-page + aggregate.

---

## 4) Report recipes (do, don’t ask)

### 4.1 Profit & Loss — monthly view for a year
Issue with Xero API: `periods` must be **1–11** in some modes. Do **not** ask the user about this. Use one of these tactics automatically:

**Preferred:** Call **list-profit-and-loss** with `fromDate`=`YYYY-01-01`, `toDate`=`YYYY-12-31`, `timeframe=MONTH` **without** `periods`, if the tool supports it.

**Fallback (when periods is enforced 1–11):**
- Run 2 calls:
  - Call A: `fromDate=YYYY-01-01`, `toDate=YYYY-11-30`, `timeframe=MONTH`, `periods=11`
  - Call B: `fromDate=YYYY-12-01`, `toDate=YYYY-12-31`, `timeframe=MONTH`, `periods=1`
- Combine the 12 months client-side and present a single table.

**If the tool exposes only a single month:** loop months Jan→Dec (≤12 calls, allowed by Asking Policy), aggregate client-side.

Always present: Revenue, COGS, Gross Profit, Opex, Net Profit per month; include totals and (if applicable) YoY or MoM deltas.

### 4.2 Balance Sheet
- Use **list-report-balance-sheet** with `asOfDate` = requested end date; if a range is given, show **end-of-period** snapshot and (optionally) compare with start.

### 4.3 Cash movement from bank transactions (proxy cash-flow)
- Use **list-bank-transactions** with date range; compute **inflow**, **outflow**, **net cash**, top counterparties, and largest 5 transactions.
- Support filters: account, type (RECEIVE/SPEND), and tracking (if exposed).

### 4.4 Invoices KPIs
- Use **list-invoices** with `_auto_page: true` and `_summarize: "invoices"`.
- Common asks:
  - “overdue now” → filter by status + dueDate < today.
  - “top debtors last 90 days” → filter date range + type=ACCREC; show top contacts by outstanding.
  - “collections this month” → filter payments or invoices with status PAID, group by day/contact.

### 4.5 Aged AR/AP by contact
- Use **list-aged-receivables-by-contact** / **list-aged-payables-by-contact**, optionally scoped to a named contact.

### 4.6 Tracking categories
- If user asks “by region/class/project”, pass tracking filters where the tool supports them; otherwise fetch and **group client-side**.

---

## 5) Asking Policy (minimize friction)
- Proceed **without asking** if the plan needs **≤12 tool calls** or **≤10 auto-pages**.
- If more than that, ask **once**: “This will run ~N calls/pages; OK to proceed?”
- On **rate-limit** or **auth** errors, retry once with smaller scope; otherwise return the 1-line diagnosis + fix.

---

## 6) Error-recovery playbooks (examples)
- **P&L 400: “periods 1–11”** → Switch to the **two-call** or **per-month loop** strategy and proceed.
- **From/To + Periods conflict** → Remove `periods` and rely on `fromDate`/`toDate` + `timeframe`.
- **Scope/auth** → Ask the user to re-auth or add scope (state the missing scope).
- **Empty data** → Say it plainly and suggest the next concrete step (expand range, different status, include other orgs).

---

## 7) Output Contract (always visible text)
Structure every answer:

1) **Direct answer** — 1 short paragraph with the headline result.
2) **Key metrics** — bullets with numbers.
3) **Details** — compact table or concise bullets (top rows/aggregates). For monthly P&L, show Month, Revenue, COGS, GP, Opex, Net.
4) **Assumptions & Filters** — exact dates, statuses, currency, defaults used, and any redactions applied.
5) **Next steps** — 1–2 precise follow-ups or actions (e.g., “drill into July variance by contact?”).

Use absolute dates (YYYY-MM-DD). Prefer aggregates to raw dumps.

---

## 8) Examples (follow these patterns)

- **“How much profit did we make each month this year?”**
  - Resolve “this year” → current calendar year (Jan 1–Dec 31).
  - Try P&L with timeframe=MONTH (no periods). If rejected, run 2-call or 12-call fallback. Present 12-row table.

- **“Show cash received and paid last month, and top 5 vendors.”**
  - list-bank-transactions with last month, compute inflow/outflow/net, group vendors; show top 5.

- **“Which customers owe us the most in the last 90 days?”**
  - list-invoices with `_auto_page: true`, `_summarize: "invoices"`, `_filters` last 90 days, type=ACCREC; show top contacts with outstanding.

- **“Balance sheet as of 2025-06-30?”**
  - list-report-balance-sheet with asOf=2025-06-30; show assets/liabilities/equity totals and key ratios.

---

## 9) Final reminders
- Think briefly, then act; chain only when needed.
- Prefer the most specific tool; let the client auto-page and summarize when available.
- If something truly cannot be done, propose the closest viable alternative and ask **once** if heavy; then execute.
- Return **only** user-visible text (plain or markdown). Never include raw headers, tokens, or giant payloads.

"""


# Thinking toggle actually controls generation + budget
# Default OFF for cleaner UX; toggle with /thinking
SHOW_THINKING = os.environ.get("SHOW_THINKING", "false").lower() in ("true", "1", "yes")
THINKING_BUDGET = int(os.environ.get("THINKING_BUDGET", "-1"))  # -1 dynamic, 0 off, >0 fixed tokens

# History/windowing
HISTORY_PATH = os.path.expanduser("~/.xero_mcp_history")
MAX_TURNS = int(os.environ.get("MAX_TURNS", "24"))  # rough bound on conversation length

# Tool-call loop guardrails
MAX_TOOL_ITERATIONS = int(os.environ.get("MAX_TOOL_ITERATIONS", "6"))

# Auto-paging safety
AUTO_PAGE_MAX = int(os.environ.get("AUTO_PAGE_MAX", "15"))  # cap pages when _auto_page is on

# UI + paging defaults
DEFAULT_PAGE_SIZE = 20
MIN_PAGE_SIZE = 8

console = Console() if USE_RICH else None

# ---------------------- Generic helpers ----------------------
def pretty_json(data: Any) -> str:
    try:
        return json.dumps(data, indent=2, ensure_ascii=False)
    except Exception:
        return str(data)

def truncate_str(s: str, limit: int = 2000) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n… [truncated {len(s)-limit} chars]"

def sizeof_json(obj: Any) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False))
    except Exception:
        return 0

# ---------------------- MCP result normalization ----------------------
def _flatten_mcp_result(result) -> dict:
    """
    Normalize MCP tool result into a simple payload:
      {"ok": bool, "parts": [{"type":"text","text":...} | {"type":"json","json":...} | ...], "message":str?}
    """
    payload = {"ok": not getattr(result, "isError", False), "parts": []}
    for c in getattr(result, "content", []) or []:
        typ = getattr(c, "type", None)
        if typ == "text":
            payload["parts"].append({"type": "text", "text": getattr(c, "text", "")})
        elif typ == "json":
            payload["parts"].append({"type": "json", "json": getattr(c, "json", None)})
        else:
            payload["parts"].append({"type": str(typ) or "unknown", "value": repr(c)})
    msg = getattr(result, "message", None)
    if msg:
        payload["message"] = msg
    return payload

# ---------------------- Thinking helpers ----------------------
def extract_thinking(response) -> Optional[str]:
    """
    Return human-readable thought summaries only.
    We show only parts where part.thought == True and part.text exists.
    """
    if not getattr(response, "candidates", None):
        return None
    pieces: List[str] = []
    for cand in response.candidates or []:
        content = getattr(cand, "content", None)
        if not content or not getattr(content, "parts", None):
            continue
        for part in content.parts or []:
            if getattr(part, "thought", False) and getattr(part, "text", None):
                pieces.append(part.text)
    txt = "\n".join(pieces).strip() if pieces else None
    return txt or None

def display_thinking(thinking_text: str):
    if not thinking_text or not thinking_text.strip():
        return
    if USE_RICH:
        thinking_panel = Panel(
            truncate_str(thinking_text.strip(), 4000),
            title="Thinking Process",
            title_align="left",
            box=ROUNDED,
            style="dim",
            border_style="dim blue",
            padding=(0, 1),
        )
        console.print(thinking_panel)
    else:
        print("\n🤔 Thinking Process:")
        print("-" * 40)
        for line in thinking_text.strip().split("\n"):
            print(f"  {line}")
        print("-" * 40)

def extract_text_response(response) -> str:
    """
    Collect user-visible text. If empty (sometimes happens when include_thoughts=True),
    fall back to the first thought text so the UI never stays blank.
    """
    if not getattr(response, "candidates", None):
        return ""
    out: List[str] = []

    cand = (response.candidates or [None])[0]
    if not cand or not getattr(cand, "content", None) or not getattr(cand.content, "parts", None):
        return ""

    # 1) Prefer non-thought text
    for part in cand.content.parts or []:
        if getattr(part, "text", None) and not getattr(part, "thought", False):
            out.append(part.text)

    if out:
        return "".join(out)

    # 2) Fallback: if nothing user-visible, pick the first thought text (when thinking is enabled)
    for part in cand.content.parts or []:
        if getattr(part, "thought", False) and getattr(part, "text", None):
            return part.text

    return ""

# ---------------------- UI helpers ----------------------
def banner(thinking_on: bool):
    thinking_status = "ON" if thinking_on else "OFF"
    if USE_RICH:
        title = Text(APP_NAME, style="bold cyan")
        subtitle = Text(
            f"Connected to Xero MCP · Model: {MODEL} · Thinking: {thinking_status} · {datetime.now().strftime('%Y-%m-%d %H:%M')}",
            style="dim",
        )
        console.print(Panel(Align.center(Text.assemble(title, "\n", subtitle)), box=ROUNDED))
    else:
        print(f"{APP_NAME} — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"Model: {MODEL} · Thinking: {thinking_status}")
        print("-" * 60)

def info(msg: str):
    if USE_RICH:
        console.print(f"[cyan]ℹ[/cyan] {msg}")
    else:
        print("[i]", msg)

def success(msg: str):
    if USE_RICH:
        console.print(f"[green]✔[/green] {msg}")
    else:
        print("[✓]", msg)

def warn(msg: str):
    if USE_RICH:
        console.print(f"[yellow]⚠[/yellow] {msg}")
    else:
        print("[!]", msg)

def error(msg: str):
    if USE_RICH:
        console.print(f"[red]✖[/red] {msg}")
    else:
        print("[x]", msg)

def spinner(msg: str):
    class Dummy:
        def __enter__(self):
            if not USE_RICH:
                print(msg + "...")
        def __exit__(self, exc_type, exc, tb):
            pass
    return console.status(msg) if USE_RICH else Dummy()

def render_json_table_if_applicable(payload: dict):
    """
    Generic renderer: if top-level is a list[dict], render a table of common keys; else pretty JSON.
    """
    # Try to find a list[dict] inside payload["parts"] json parts
    rows: Optional[List[Dict[str, Any]]] = None
    if not payload:
        return
    for p in payload.get("parts", []):
        if p.get("type") == "json":
            data = p.get("json")
            # Accept raw list[dict] or dict with known collection keys
            if isinstance(data, list) and (not data or isinstance(data[0], dict)):
                rows = data
                break
            if isinstance(data, dict):
                for k in (
                    "items",
                    "results",
                    "data",
                    "contacts",
                    "Contacts",
                    "transactions",
                    "Transactions",
                    "invoices_rows",
                    "totals_by_contact"
                ):
                    v = data.get(k)
                    if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                        rows = v
                        break
        if rows:
            break

    if rows is None:
        # Fallback: print JSON payload
        body = pretty_json(payload)
        if USE_RICH:
            console.print(Panel(truncate_str(body, 12000), title="Tool Payload", box=ROUNDED, style="dim"))
        else:
            print("\nTool Payload:\n" + body)
        return

    # Render table of first N rows and selected keys
    max_rows = 20
    sample = rows[:max_rows]
    # Choose up to 6 interesting keys (union of first few rows)
    keys: List[str] = []
    for r in sample[:5]:
        if isinstance(r, dict):
            for k in r.keys():
                if k not in keys:
                    keys.append(k)
                if len(keys) >= 6:
                    break
        if len(keys) >= 6:
            break
    keys = keys or ["id", "name", "date", "amount"]  # fallback

    if USE_RICH:
        table = Table(box=ROUNDED, show_lines=False, title=f"Items (showing {len(sample)} of {len(rows)})")
        table.add_column("#", justify="right", style="dim", width=4)
        for k in keys:
            table.add_column(str(k))
        for i, r in enumerate(sample, 1):
            if isinstance(r, dict):
                table.add_row(str(i), *[str(r.get(k, ""))[:80] for k in keys])
            else:
                table.add_row(str(i), *([""] * len(keys)))
        console.print(table)
    else:
        print(f"\nItems (showing {len(sample)} of {len(rows)}):")
        header = " | ".join(keys)
        print(f"# | {header}")
        for i, r in enumerate(sample, 1):
            if isinstance(r, dict):
                print(f"{i} | " + " | ".join(str(r.get(k, ""))[:80] for k in keys))
            else:
                print(f"{i} | " + " | ".join([""] * len(keys)))

# ---------------------- Export helpers (generic) ----------------------
def export_json(path: str, payload: dict):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    success(f"Saved JSON to {path}")

def export_csv_table_guess(path: str, payload: dict):
    """
    If we can find a list[dict] inside payload, dump it as CSV; else warn.
    """
    rows: Optional[List[Dict[str, Any]]] = None
    headers: List[str] = []
    for p in payload.get("parts", []):
        if p.get("type") == "json":
            data = p.get("json")
            if isinstance(data, list) and (not data or isinstance(data[0], dict)):
                rows = data
                break
            if isinstance(data, dict):
                for k in (
                    "items",
                    "results",
                    "data",
                    "contacts",
                    "Contacts",
                    "transactions",
                    "Transactions",
                    "invoices_rows",
                    "totals_by_contact"
                ):
                    v = data.get(k)
                    if isinstance(v, list) and (not v or isinstance(v[0], dict)):
                        rows = v
                        break
        if rows:
            break
    if not rows:
        warn("Could not find a tabular list in the last payload.")
        return
    # Collect headers from first few rows
    for r in rows[:10]:
        for k in r.keys():
            if k not in headers:
                headers.append(k)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow([r.get(h, "") for h in headers])
    success(f"Saved CSV to {path}")

# ---------------------- Parsing & Aggregation (client-side) ----------------------
def _count_listed_items(payload: dict) -> int:
    """Count 'Invoice ID:' lines in text blocks. Used to detect page fullness."""
    if not payload:
        return 0
    blocks = [p.get("text", "") for p in payload.get("parts", []) if p.get("type") == "text"]
    return sum(1 for line in "\n".join(blocks).splitlines() if line.strip().startswith("Invoice ID:"))

def _parse_invoices_from_text(parts: List[dict]) -> List[dict]:
    """Parse minimal invoice fields from Xero MCP's text output into structured rows."""
    invoices: List[dict] = []
    buf: List[str] = []

    def flush():
        if not buf:
            return
        block = "\n".join(buf)
        row: Dict[str, Any] = {}

        def grab(label: str, pat: str):
            m = re.search(pat, block)
            if m:
                row[label] = m.group(1).strip()

        grab("id", r"Invoice ID:\s*([^\n]+)")
        grab("type", r"Type:\s*([A-Z]+)")
        grab("contact", r"Contact:\s*([^(^\n]+)")
        grab("date_raw", r"Date:\s*([^\n]+)")
        grab("total_raw", r"Total:\s*([0-9]+(?:\.[0-9]+)?)")
        grab("currency", r"Currency:\s*([A-Z]{3})")

        if row.get("id"):
            # Normalize date from strings like: "Wed Jul 19 2017 10:00:00 GMT+1000 (...)"
            raw = row.get("date_raw", "")
            try:
                # take the first 24 chars "Wed Jul 19 2017 10:00:00"
                short = raw[:24]
                row["date"] = datetime.strptime(short, "%a %b %d %Y %H:%M:%S").date()
            except Exception:
                row["date"] = None
            try:
                row["total"] = Decimal(row.get("total_raw", "0"))
            except Exception:
                row["total"] = Decimal("0")
            invoices.append(row)
        buf.clear()

    for p in parts:
        if p.get("type") != "text":
            continue
        for line in p.get("text", "").splitlines():
            if line.startswith("Invoice ID:") and buf:
                flush()
            buf.append(line)
    flush()
    return invoices

def _apply_invoice_filters(
    invoices: List[dict],
    filters: Optional[Dict[str, str]]
) -> Tuple[List[dict], Optional[date], Optional[date], Optional[str]]:
    """Filter invoices by from/to (dates) and type (ACCREC/ACCPAY)."""
    if not filters:
        return invoices, None, None, None

    f = filters or {}
    from_d = None
    to_d = None
    t_filter = f.get("type")
    try:
        if f.get("from"):
            from_d = datetime.strptime(f["from"], "%Y-%m-%d").date()
    except Exception:
        from_d = None
    try:
        if f.get("to"):
            to_d = datetime.strptime(f["to"], "%Y-%m-%d").date()
    except Exception:
        to_d = None

    out: List[dict] = []
    for r in invoices:
        d = r.get("date")
        ty = r.get("type")
        if t_filter and ty != t_filter:
            continue
        if from_d and (not d or d < from_d):
            continue
        if to_d and (not d or d > to_d):
            continue
        out.append(r)

    return out, from_d, to_d, t_filter

def _aggregate_invoices_by_contact(invoices: List[dict]) -> List[dict]:
    """Sum totals by contact; returns list[{'contact','amount'}] sorted desc."""
    sums: Dict[str, Decimal] = defaultdict(Decimal)
    for r in invoices:
        contact = r.get("contact") or "Unknown"
        sums[contact] += r.get("total", Decimal("0"))
    rows = [{"contact": k, "amount": float(v)} for k, v in sums.items()]  # float for JSON friendliness
    rows.sort(key=lambda x: x["amount"], reverse=True)
    return rows

# ---------------------- Auto-paging wrapper ----------------------
async def _call_with_auto_paging(session, name: str, args: dict) -> dict:
    """
    Auto-pages list-* tools when `_auto_page: true` is provided.
    For invoices, supports `_summarize: "invoices"` and optional `_filters`.
    """
    do_auto = bool(args.pop("_auto_page", False))
    summarize = args.pop("_summarize", None)
    filters = args.pop("_filters", None)

    # If not auto-paging or not a list tool, do a single call.
    if not do_auto or not name.startswith("list-"):
        mcp_result = await session.call_tool(name=name, arguments=args)
        return _flatten_mcp_result(mcp_result)

    combined_parts: List[dict] = []
    page = int(args.get("page", 1))
    pages_done = 0

    while pages_done < AUTO_PAGE_MAX:
        cur_args = {**args, "page": page}
        try:
            mcp_result = await session.call_tool(name=name, arguments=cur_args)
            payload = _flatten_mcp_result(mcp_result)
        except Exception as e:
            # On error mid-way, stop and return what we have + error
            combined_parts.append({"type": "json", "json": {"warning": f"Stopped paging due to error on page {page}: {str(e)}"}})
            break

        parts = payload.get("parts", [])
        combined_parts.extend(parts)

        # Stop conditions: page returns fewer than typical page size; for invoices we detect via 'Invoice ID:' count
        item_count = _count_listed_items(payload)
        if item_count == 0 or item_count < 10:
            break

        page += 1
        pages_done += 1

    # Optional summarization for invoices
    if summarize == "invoices":
        invoices = _parse_invoices_from_text(combined_parts)
        filtered, from_d, to_d, t_filter = _apply_invoice_filters(invoices, filters)

        totals = _aggregate_invoices_by_contact(filtered)
        top = totals[0] if totals else None
        currency = "AUD"  # Xero demo data usually AUD; adjust if you parse per-invoice

        summary_json = {
            "kind": "invoices_summary",
            "currency": currency,
            "parsed_invoices": len(invoices),
            "filtered_invoices": len(filtered),
            "filters_used": {
                "from": str(from_d) if from_d else None,
                "to": str(to_d) if to_d else None,
                "type": t_filter,
            },
            # Use 'items' so /export csv works out-of-the-box
            "items": totals,  # totals_by_contact
            "top_customer": top,
        }

        # Also include a small sample of normalized rows to help export/debug
        sample_rows = [
            {
                "id": r.get("id"),
                "date": str(r.get("date")) if r.get("date") else None,
                "type": r.get("type"),
                "contact": r.get("contact"),
                "total": float(r.get("total", Decimal("0"))),
                "currency": r.get("currency") or "AUD",
            }
            for r in filtered[:500]  # cap
        ]
        summary_json["invoices_rows"] = sample_rows

        combined_parts.append({"type": "json", "json": summary_json})

    return {"ok": True, "parts": combined_parts}

# ---------------------- Turn-repair (fix INVALID_ARGUMENT) ----------------------
def _repair_orphaned_function_calls(history: List[types.Content]) -> None:
    """
    If the last turn is an assistant/model turn with function_call parts and the next
    turn is not a tool response, drop the orphaned call so the API is happy.
    """
    if not history:
        return
    last = history[-1]
    role = getattr(last, "role", None)
    parts = getattr(last, "parts", None)
    has_func_call = any(getattr(p, "function_call", None) for p in (parts or []))
    if role in ("assistant", "model") and has_func_call:
        # No tool response followed → remove the dangling call turn
        history.pop()

# ---------------------- Chat Session ----------------------
class ChatState:
    def __init__(self):
        self.history_contents: List[types.Content] = []
        self.tool_names: List[str] = []
        self.last_tool_payload: Optional[dict] = None  # generic (for /export)
        self.page_size = DEFAULT_PAGE_SIZE
        self.show_thinking = SHOW_THINKING

    def auto_page_size(self):
        try:
            _, rows = shutil.get_terminal_size((100, 30))
            usable = max(MIN_PAGE_SIZE, rows - 16)
            self.page_size = max(MIN_PAGE_SIZE, min(80, usable))
        except Exception:
            self.page_size = DEFAULT_PAGE_SIZE

    def trim_history(self):
        # Keep only the most recent MAX_TURNS contents
        if len(self.history_contents) > MAX_TURNS:
            self.history_contents = self.history_contents[-MAX_TURNS:]

# ---------------------- Core mediator ----------------------
async def chat_loop(session: ClientSession):
    client = genai.Client()
    state = ChatState()
    state.auto_page_size()

    # Discover tools
    tools_resp = await session.list_tools()
    state.tool_names = [t.name for t in tools_resp.tools]

    banner(state.show_thinking)
    info(f"Detected MCP tools: {', '.join(state.tool_names) or '(none found)'}")

    def build_config() -> types.GenerateContentConfig:
        tk_cfg = types.ThinkingConfig(
            include_thoughts=state.show_thinking,
            thinking_budget=(THINKING_BUDGET if state.show_thinking else 0),
        )
        return types.GenerateContentConfig(
            system_instruction=SYSTEM,
            temperature=0,
            tools=[session],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=tk_cfg,
            # IMPORTANT: Use an allowed MIME type. Markdown can still be sent as plain text.
            response_mime_type="text/plain",
        )

    async def run_tool_call_loop() -> str:
        """
        Send conversation to the model; if it asks for tool calls, run them with guardrails,
        return tool outputs, and iterate until a final answer is produced.
        """
        iterations = 0
        while True:
            iterations += 1
            if iterations > MAX_TOOL_ITERATIONS:
                return "I hit the tool-iteration limit; try refining your request."

            # Ensure no dangling assistant function_call before generation
            _repair_orphaned_function_calls(state.history_contents)

            with spinner("Thinking with Gemini..."):
                resp = await client.aio.models.generate_content(
                    model=MODEL,
                    contents=state.history_contents,
                    config=build_config(),
                )

            # Optional thinking panel (human-readable summaries only)
            if state.show_thinking:
                thinking_text = extract_thinking(resp)
                if thinking_text:
                    display_thinking(thinking_text)

            # Collect function calls from first candidate deterministically
            function_calls = []
            cand = (resp.candidates or [None])[0]
            if cand and getattr(resp, "function_calls", None):
                function_calls = resp.function_calls
            elif cand and cand.content and getattr(cand.content, "parts", None):
                for part in cand.content.parts or []:
                    if getattr(part, "function_call", None):
                        function_calls.append(part.function_call)

            # No tool calls → finalize
            if not function_calls:
                if cand and cand.content:
                    state.history_contents.append(cand.content)
                state.trim_history()
                return extract_text_response(resp)

            # Append assistant tool-call request to history
            if cand and cand.content:
                state.history_contents.append(cand.content)

            # Guardrails: run only known tools; ensure args is dict-like
            response_parts: List[types.Part] = []
            for fc in function_calls:
                name = getattr(fc, "name", "")
                args = dict(getattr(fc, "args", {}) or {})
                if name not in state.tool_names:
                    payload = {"ok": False, "error": f"Unknown tool '{name}'."}
                else:
                    with spinner(f"Calling MCP tool: {name}"):
                        try:
                            # Use auto-paging wrapper (supports meta-args)
                            payload = await _call_with_auto_paging(session, name, args)
                        except Exception as e:
                            payload = {"ok": False, "error": str(e)}

                # Keep last tool payload for optional export/inspection
                state.last_tool_payload = payload

                # Show a compact view to the user (generic)
                render_json_table_if_applicable(payload)

                # Return structured tool response back to the model
                response_parts.append(
                    types.Part(function_response=types.FunctionResponse(name=name, response=payload))
                )

            # One consolidated tool turn back to Gemini (MUST be immediately after the call)
            state.history_contents.append(types.Content(role="tool", parts=response_parts))
            state.trim_history()
            # Loop again (model may chain more calls or finalize)

    # ------------- Input setup -------------
    def build_session():
        completer_words = [
            "/tools", "/call", "/export", "/thinking", "/help", "/quit"
        ]
        if USE_PTK:
            completer = WordCompleter(completer_words, ignore_case=True, sentence=True, match_middle=True)
            hist = FileHistory(HISTORY_PATH)
            return PromptSession(history=hist, completer=completer)
        return None

    ptk_session = build_session()

    async def get_input() -> str:
        if USE_PTK and ptk_session:
            try:
                with patch_stdout():
                    return await ptk_session.prompt_async(
                        PTK_HTML('<b><ansicyan>you</ansicyan></b> ▸ ')
                    )
            except KeyboardInterrupt:
                return ""
        try:
            return await asyncio.to_thread(input, "\nYou: ")
        except KeyboardInterrupt:
            return ""

    def show_help():
        cmds = [
            "/tools                        list available MCP tools",
            "/call <name> <json-args>     call a tool directly, e.g. /call list-bank-transactions {\"pageSize\":10}",
            "/export json <path>          export last tool payload as JSON",
            "/export csv <path>           export last tool payload as CSV (best-effort if list[dict])",
            "/thinking                    toggle thinking on/off (affects generation + budget)",
            "/help                        show this menu",
            "/quit                        exit",
        ]
        body = "\n".join(cmds)
        if USE_RICH:
            console.print(Panel(body, title="Commands", box=ROUNDED))
        else:
            print("\nCommands:\n" + "\n".join("  " + c for c in cmds))

    def list_tools():
        if not state.tool_names:
            warn("No tools discovered.")
            return
        if USE_RICH:
            table = Table(box=ROUNDED, show_lines=False, title="Discovered MCP Tools")
            table.add_column("#", justify="right", style="dim", width=4)
            table.add_column("Name", style="bold")
            for i, n in enumerate(state.tool_names, 1):
                table.add_row(str(i), n)
            console.print(table)
        else:
            print("\nDiscovered MCP Tools:")
            for i, n in enumerate(state.tool_names, 1):
                print(f"  {i}. {n}")

    async def call_tool_direct(name: str, args_str: str):
        if name not in state.tool_names:
            error(f"Unknown tool: {name}")
            return
        try:
            args = json.loads(args_str) if args_str.strip() else {}
            if not isinstance(args, dict):
                raise ValueError("Args must be a JSON object.")
        except Exception as e:
            error(f"Invalid JSON args: {e}")
            return
        with spinner(f"Calling MCP tool: {name}"):
            try:
                payload = await _call_with_auto_paging(session, name, args)
            except Exception as e:
                error(str(e))
                return
        state.last_tool_payload = payload
        render_json_table_if_applicable(payload)

    def do_export(parts: List[str]):
        if len(parts) < 2:
            error("Usage: /export json|csv <path>")
            return
        kind = parts[0].lower()
        path = parts[1]
        if not state.last_tool_payload:
            warn("No tool payload available to export.")
            return
        if kind == "json":
            if not path.lower().endswith(".json"):
                path += ".json"
            export_json(path, state.last_tool_payload)
        elif kind == "csv":
            if not path.lower().endswith(".csv"):
                path += ".csv"
            export_csv_table_guess(path, state.last_tool_payload)
        else:
            error("Unknown export type. Use json|csv.")

    # ---------------- Main loop ----------------
    while True:
        user = (await get_input()) or ""
        user = user.strip()
        if not user:
            continue

        low = user.lower()

        # Commands
        if low in ("/quit", "/exit"):
            success("Goodbye!")
            return
        if low in ("/help", "help"):
            show_help()
            continue
        if low.startswith("/tools"):
            list_tools()
            continue
        if low.startswith("/call "):
            # /call <name> <json-args>
            try:
                _, rest = user.split(None, 1)
                name, args_str = (rest.split(None, 1) + [""])[:2]
                await call_tool_direct(name, args_str)
            except ValueError:
                error("Usage: /call <tool-name> <json-args>")
            continue
        if low.startswith("/export"):
            parts = user.split()
            do_export(parts[1:])
            continue
        if low == "/thinking":
            state.show_thinking = not state.show_thinking
            status = "enabled" if state.show_thinking else "disabled"
            success(f"Thinking {status} (budget={THINKING_BUDGET if state.show_thinking else 0})")
            banner(state.show_thinking)
            continue

        # Otherwise: forward to the model (natural language)
        state.history_contents.append(types.UserContent(parts=[types.Part(text=user)]))
        state.trim_history()

        final_text = await run_tool_call_loop()
        if final_text and final_text.strip():
            if USE_RICH:
                console.print(Panel(final_text, title="Assistant", box=ROUNDED, style=Style(color="white")))
            else:
                print(f"\nAssistant: {final_text}")
        else:
            warn("No user-visible text returned (model sent only tool calls or thoughts). Try /thinking off and ask again.")

# ---------------------- Entrypoint ----------------------
async def main():
    if not shutil.which("npx"):
        raise RuntimeError("npx is not installed. Install Node.js (includes npx).")

    # You can override the MCP server via env if desired
    mcp_command = os.environ.get("MCP_COMMAND", "npx")
    mcp_args_env = os.environ.get("MCP_ARGS_JSON", "")
    if mcp_args_env:
        try:
            args_list = json.loads(mcp_args_env)
            if not isinstance(args_list, list):
                raise ValueError
            mcp_args = args_list
        except Exception:
            raise RuntimeError("MCP_ARGS_JSON must be a JSON array of args.")
    else:
        # Default to Xero MCP server
        mcp_args = ["-y", "@xeroapi/xero-mcp-server@latest"]

    server_params = StdioServerParameters(
        command=mcp_command,
        args=mcp_args,
        env={
            "XERO_CLIENT_ID": os.environ.get("XERO_CLIENT_ID", ""),
            "XERO_CLIENT_SECRET": os.environ.get("XERO_CLIENT_SECRET", ""),
        },
    )

    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(server_params))
        async with ClientSession(read, write) as session:
            await session.initialize()
            # Header for non-rich envs
            print(f"Connected to Xero MCP at {datetime.now().isoformat(timespec='seconds')}")
            print(f"Model: {MODEL}  |  SDK: google-genai")
            await chat_loop(session)

if __name__ == "__main__":
    asyncio.run(main())
