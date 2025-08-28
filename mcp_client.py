import asyncio
import json
import os
import shutil
from typing import Any, Dict, List, Tuple, Optional
from contextlib import AsyncExitStack
from datetime import datetime
import csv
import re

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Google GenAI SDK
from google import genai
from google.genai import types

# ------------ Optional UX libs (graceful fallback) ------------
USE_RICH = True
USE_PTK = True
USE_CLIP = True
try:
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich.align import Align
    from rich.box import ROUNDED
    from rich.prompt import Confirm
    from rich.style import Style
except Exception:
    USE_RICH = False

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.formatted_text import HTML as PTK_HTML
except Exception:
    USE_PTK = False

try:
    import pyperclip
except Exception:
    USE_CLIP = False

# ---------------- Config ----------------
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
SYSTEM = (
    "You are a helpful assistant with access to Xero via MCP tools. "
    "When the user asks for Xero data/actions, choose the appropriate tool. "
    "If listing contacts, call the list-contacts tool without asking for confirmation "
    "and summarize the first page. Be concise but clear."
)

APP_NAME = "Xero Assistant"
HISTORY_PATH = os.path.expanduser("~/.xero_mcp_history")
DEFAULT_PAGE_SIZE = 20  # overridden by terminal height if possible
MIN_PAGE_SIZE = 8

console = Console() if USE_RICH else None

# ---------------------- MCP result helpers ----------------------
def _flatten_mcp_result(result) -> dict:
    payload = {"ok": not getattr(result, "isError", False), "parts": []}
    for c in getattr(result, "content", []) or []:
        typ = getattr(c, "type", None)
        if typ == "text":
            payload["parts"].append({"type": "text", "text": getattr(c, "text", "")})
        elif typ == "json":
            payload["parts"].append({"type": "json", "json": getattr(c, "json", None)})
        else:
            payload["parts"].append({"type": "unknown", "value": repr(c)})
    msg = getattr(result, "message", None)
    if msg:
        payload["message"] = msg
    return payload

def _extract_contacts(payload: dict) -> List[Dict[str, Any]]:
    for part in payload.get("parts", []):
        if part.get("type") == "json":
            data = part.get("json")
            if isinstance(data, dict):
                for key in ("Contacts", "contacts", "items", "data", "results"):
                    val = data.get(key)
                    if isinstance(val, list) and (not val or isinstance(val[0], dict)):
                        return val
            if isinstance(data, list) and (not data or isinstance(data[0], dict)):
                return data
    return []

def _get_str(d: Dict[str, Any], *keys: str):
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None

def _fmt_name(c: Dict[str, Any]):
    name = _get_str(c, "Name", "name", "ContactName", "contactName")
    if name:
        return name
    fn = _get_str(c, "FirstName", "firstName")
    ln = _get_str(c, "LastName", "lastName")
    if fn or ln:
        return (" ".join(filter(None, [fn, ln]))).strip()
    return "(no name)"

def _fmt_email(c: Dict[str, Any]):
    email = _get_str(c, "EmailAddress", "emailAddress", "email", "primaryEmail")
    if email:
        return email
    if isinstance(c.get("Emails"), list) and c["Emails"]:
        e0 = c["Emails"][0]
        if isinstance(e0, dict):
            return _get_str(e0, "Address", "address", "email") or ""
    return ""

def _fmt_phone(c: Dict[str, Any]):
    # Try common shapes
    phone = _get_str(c, "PhoneNumber", "phone", "primaryPhone", "Mobile", "mobile")
    if phone:
        return phone
    phones = c.get("Phones") or c.get("phones")
    if isinstance(phones, list) and phones:
        p0 = phones[0]
        if isinstance(p0, dict):
            return _get_str(p0, "PhoneNumber", "phoneNumber", "number") or ""
    return ""

# ---------------------- UX helpers ----------------------
def banner():
    if USE_RICH:
        title = Text(APP_NAME, style="bold cyan")
        subtitle = Text(f"Connected to Xero MCP · Model: {MODEL} · {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                        style="dim")
        console.print(Panel(Align.center(Text.assemble(title, "\n", subtitle)), box=ROUNDED))
    else:
        print(f"{APP_NAME} — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"Model: {MODEL}")
        print("-" * 60)

def tip_quick_actions():
    tips = [
        "[bold]/contacts[/bold] list Xero contacts",
        "[bold]/filter john[/bold] filter by name/email",
        "[bold]/view 7[/bold] see full details of item 7 on the page",
        "[bold]/copy 7[/bold] copy email of item 7",
        "[bold]/export csv contacts.csv[/bold]  or  [bold]/export md out.md[/bold]  or  [bold]/export json out.json[/bold]",
        "[bold]/page 2[/bold] · [bold]/more[/bold] · [bold]/page_size 30[/bold]",
        "[bold]/help[/bold] to see all commands",
    ]
    if USE_RICH:
        console.print(Panel("\n".join(tips), title="Quick actions", box=ROUNDED))
    else:
        print("Quick actions:")
        for t in tips:
            print(" -", re.sub(r"\[/?bold\]", "", t))

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
    # A no-op context manager if rich absent
    class Dummy:
        def __enter__(self): 
            if not USE_RICH: print(msg + "...")
        def __exit__(self, exc_type, exc, tb): 
            pass
    return console.status(msg) if USE_RICH else Dummy()

# ---------------------- Contacts table rendering ----------------------
def _plain_contacts_table(contacts: List[Dict[str, Any]], page: int, page_size: int, filter_text: Optional[str] = None):
    items = contacts
    if filter_text:
        pat = re.compile(re.escape(filter_text), re.IGNORECASE)
        def match(c):
            n = _fmt_name(c)
            e = _fmt_email(c)
            return (n and pat.search(n)) or (e and pat.search(e))
        items = [c for c in items if match(c)]

    total = len(items)
    if total == 0:
        print("No contacts found.")
        return [], 0, 0, 0

    start = max(0, (page - 1) * page_size)
    end = min(total, start + page_size)
    slice_ = items[start:end]

    rows: List[Tuple[str, str, str]] = [(_fmt_name(c), _fmt_email(c), _fmt_phone(c)) for c in slice_]
    name_w = max(len("Name"), *(len(r[0]) for r in rows)) if rows else len("Name")
    email_w = max(len("Email"), *(len(r[1]) for r in rows)) if rows else len("Email")
    phone_w = max(len("Phone"), *(len(r[2]) for r in rows)) if rows else len("Phone")

    print(f"\nContacts {start+1}-{end} of {total}" + (f" (filtered by '{filter_text}')" if filter_text else ""))
    print("-" * (name_w + email_w + phone_w + 9))
    print(f"| {'#':>3} | {'Name'.ljust(name_w)} | {'Email'.ljust(email_w)} | {'Phone'.ljust(phone_w)} |")
    print("-" * (name_w + email_w + phone_w + 9))
    for i, (n, e, p) in enumerate(rows, start=start+1):
        print(f"| {str(i).rjust(3)} | {n.ljust(name_w)} | {e.ljust(email_w)} | {p.ljust(phone_w)} |")
    print("-" * (name_w + email_w + phone_w + 9))
    return slice_, total, start+1, end

def _rich_contacts_table(contacts: List[Dict[str, Any]], page: int, page_size: int, filter_text: Optional[str] = None):
    items = contacts
    if filter_text:
        pat = re.compile(re.escape(filter_text), re.IGNORECASE)
        def match(c):
            n = _fmt_name(c)
            e = _fmt_email(c)
            return (n and pat.search(n)) or (e and pat.search(e))
        items = [c for c in items if match(c)]

    total = len(items)
    if total == 0:
        console.print(Panel("No contacts found.", style="dim"))
        return [], 0, 0, 0

    start = max(0, (page - 1) * page_size)
    end = min(total, start + page_size)
    slice_ = items[start:end]

    table = Table(box=ROUNDED, show_lines=False, title=f"Contacts {start+1}-{end} of {total}" + (f" • filter: '{filter_text}'" if filter_text else ""))
    table.add_column("#", justify="right", style="dim", width=4)
    table.add_column("Name", style="bold")
    table.add_column("Email")
    table.add_column("Phone", style="dim")

    for idx, c in enumerate(slice_, start=start+1):
        table.add_row(str(idx), _fmt_name(c), _fmt_email(c), _fmt_phone(c))

    console.print(table)
    return slice_, total, start+1, end

def render_contacts_table(contacts: List[Dict[str, Any]], page: int, page_size: int, filter_text: Optional[str] = None):
    if USE_RICH:
        return _rich_contacts_table(contacts, page, page_size, filter_text)
    return _plain_contacts_table(contacts, page, page_size, filter_text)

def pretty_json(data: Any) -> str:
    try:
        return json.dumps(data, indent=2, ensure_ascii=False)
    except Exception:
        return str(data)

# ---------------------- Export helpers ----------------------
def export_csv(path: str, contacts: List[Dict[str, Any]]):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Name", "Email", "Phone"])
        for c in contacts:
            w.writerow([_fmt_name(c) or "", _fmt_email(c) or "", _fmt_phone(c) or ""])
    success(f"Saved CSV to {path}")

def export_md(path: str, contacts: List[Dict[str, Any]]):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("| Name | Email | Phone |\n|---|---|---|\n")
        for c in contacts:
            f.write(f"| {(_fmt_name(c) or '').replace('|','/')} | {(_fmt_email(c) or '').replace('|','/')} | {(_fmt_phone(c) or '').replace('|','/')} |\n")
    success(f"Saved Markdown to {path}")

def export_json(path: str, payload: dict):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    success(f"Saved JSON to {path}")

# ---------------------- Chat Session ----------------------
class ChatState:
    def __init__(self):
        self.contacts_cache: List[Dict[str, Any]] = []
        self.contacts_last_payload: Optional[dict] = None
        self.page = 1
        self.page_size = DEFAULT_PAGE_SIZE
        self.filter_text: Optional[str] = None
        self.history_contents: List[types.Content] = []
        self.tool_names: List[str] = []
        self.list_contacts_tool: Optional[str] = None
        self.last_page_slice: List[Dict[str, Any]] = []  # slice shown last

    def auto_page_size(self):
        try:
            _, rows = shutil.get_terminal_size((100, 30))
            usable = max(MIN_PAGE_SIZE, rows - 16)
            self.page_size = max(MIN_PAGE_SIZE, min(80, usable))
        except Exception:
            self.page_size = DEFAULT_PAGE_SIZE

def discover_list_contacts(tools: List[Any]) -> Optional[str]:
    names = [getattr(t, "name", "") for t in tools]
    # Heuristics
    for n in names:
        ln = n.lower()
        if "contact" in ln and ("list" in ln or "get" in ln or "find" in ln):
            return n
    return None

async def chat_loop(session: ClientSession):
    client = genai.Client()
    state = ChatState()
    state.auto_page_size()

    # Discover tools up front
    tools_resp = await session.list_tools()
    state.tool_names = [t.name for t in tools_resp.tools]
    state.list_contacts_tool = discover_list_contacts(tools_resp.tools)

    banner()
    info(f"Detected MCP tools: {', '.join(state.tool_names) or '(none found)'}")
    if state.list_contacts_tool:
        success(f"Contacts tool detected: [bold]{state.list_contacts_tool}[/bold]" if USE_RICH else f"Contacts tool detected: {state.list_contacts_tool}")
    tip_quick_actions()

    # Helper to build config each turn (no thinking_config; dynamic by default)
    def build_config() -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            system_instruction=SYSTEM,
            temperature=0,  # deterministic for planning/tool selection
            tools=[session],  # expose MCP tool declarations to the model
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

    async def run_tool_call_loop() -> str:
        """
        Sends state.history_contents to the model, executes any MCP tool calls in sequence,
        appends function responses, and returns the final text once no more tool calls are requested.
        Also updates contacts cache when the tool payload contains contacts.
        """
        while True:
            with spinner("Thinking with Gemini..."):
                resp = await client.aio.models.generate_content(
                    model=MODEL,
                    contents=state.history_contents,
                    config=build_config(),
                )

            # Extract function calls
            function_calls = []
            if getattr(resp, "function_calls", None):
                function_calls = resp.function_calls
            else:
                for cand in resp.candidates or []:
                    if cand and cand.content:
                        for part in cand.content.parts or []:
                            if getattr(part, "function_call", None):
                                function_calls.append(part.function_call)

            if not function_calls:
                if resp.candidates and resp.candidates[0].content:
                    state.history_contents.append(resp.candidates[0].content)
                return resp.text or ""

            # Append the model's tool-call content to history
            if resp.candidates and resp.candidates[0].content:
                state.history_contents.append(resp.candidates[0].content)

            response_parts: List[types.Part] = []
            for fc in function_calls:
                name = fc.name
                args = dict(fc.args or {})
                with spinner(f"Calling MCP tool: {name}"):
                    try:
                        mcp_result = await session.call_tool(name=name, arguments=args)
                        payload = _flatten_mcp_result(mcp_result)
                    except Exception as e:
                        payload = {"ok": False, "error": str(e)}

                # Cache contacts and render immediately
                contacts = _extract_contacts(payload)
                if contacts:
                    state.contacts_cache = contacts
                    state.page = 1
                    slice_, total, s, e = render_contacts_table(
                        state.contacts_cache, state.page, state.page_size, state.filter_text
                    )
                    state.last_page_slice = slice_
                    info(f"Showing {s}-{e} of {total} contacts.")

                state.contacts_last_payload = payload
                response_parts.append(types.Part.from_function_response(name=name, response=payload))

            # Return tool responses to the model as a single tool turn
            state.history_contents.append(types.Content(role="tool", parts=response_parts))
            # Loop again (model may call more tools or finalize)

    # ------------- Input setup (prompt_toolkit if available) -------------
    def build_session():
        completer_words = [
            "/contacts", "/more", "/page", "/page_size", "/filter", "/clearfilter",
            "/view", "/copy", "/export", "/help", "/quit"
        ]
        if USE_PTK:
            completer = WordCompleter(completer_words, ignore_case=True, sentence=True, match_middle=True)
            hist = FileHistory(HISTORY_PATH)
            return PromptSession(history=hist, completer=completer)
        return None

    ptk_session = build_session()

    async def get_input() -> str:
        # Use prompt_toolkit's async API when available
        if USE_PTK and ptk_session:
            from prompt_toolkit.patch_stdout import patch_stdout
            try:
                # patch_stdout is a *sync* context manager
                with patch_stdout():
                    return await ptk_session.prompt_async(
                        PTK_HTML('<b><ansicyan>you</ansicyan></b> ▸ ')
                    )
            except KeyboardInterrupt:
                return ""
        # Fallback: don't block the event loop
        try:
            return await asyncio.to_thread(input, "\nYou: ")
        except KeyboardInterrupt:
            return ""

    # ------------- Command handlers -------------
    def show_help():
        cmds = [
            "/contacts                  list Xero contacts (shortcut to MCP tool)",
            "/filter <text>             filter cached contacts (name/email)",
            "/clearfilter               clear filter",
            "/page N, /more             paging controls",
            "/page_size N               set items per page",
            "/view <#>                  show full record (index from current view)",
            "/copy <#>                  copy email to clipboard",
            "/export csv|md|json <path> export cached contacts or last payload",
            "/help                      show this menu",
            "/quit                      exit",
        ]
        if USE_RICH:
            console.print(Panel("\n".join(cmds), title="Commands", box=ROUNDED))
        else:
            print("\nCommands:\n" + "\n".join("  " + c for c in cmds))

    async def do_contacts():
        if not state.list_contacts_tool:
            error("No contacts-capable MCP tool detected.")
            return
        with spinner(f"Calling {state.list_contacts_tool}..."):
            try:
                mcp_result = await session.call_tool(name=state.list_contacts_tool, arguments={})
                payload = _flatten_mcp_result(mcp_result)
            except Exception as e:
                error(str(e))
                return

        contacts = _extract_contacts(payload)
        state.contacts_last_payload = payload
        if not contacts:
            warn("No contacts found in tool response.")
            return

        state.contacts_cache = contacts
        state.page = 1
        slice_, total, s, e = render_contacts_table(contacts, state.page, state.page_size, state.filter_text)
        state.last_page_slice = slice_
        info(f"Showing {s}-{e} of {total} contacts.")

    def refresh_view():
        if not state.contacts_cache:
            warn("No contacts cached. Use /contacts or ask the assistant.")
            return
        slice_, total, s, e = render_contacts_table(
            state.contacts_cache, state.page, state.page_size, state.filter_text
        )
        state.last_page_slice = slice_
        info(f"Showing {s}-{e} of {total} contacts.")

    def view_contact(idx_str: str):
        try:
            idx = int(idx_str)
        except Exception:
            error("Usage: /view <index-number>")
            return
        # Map absolute index to current filtered view
        if not state.contacts_cache:
            warn("No contacts cached.")
            return

        # Recompute current view to fetch correct record
        items = state.contacts_cache
        if state.filter_text:
            pat = re.compile(re.escape(state.filter_text), re.IGNORECASE)
            def match(c):
                return ( _fmt_name(c) and pat.search(_fmt_name(c)) ) or ( _fmt_email(c) and pat.search(_fmt_email(c)) )
            items = [c for c in items if match(c)]

        if idx < 1 or idx > len(items):
            error("Index out of range.")
            return

        rec = items[idx - 1]
        body = pretty_json(rec)
        if USE_RICH:
            console.print(Panel(body, title=f"Contact #{idx} — {_fmt_name(rec)}", box=ROUNDED))
        else:
            print(f"\nContact #{idx} — {_fmt_name(rec)}\n{body}")

    def copy_email(idx_str: str):
        if not USE_CLIP:
            warn("pyperclip not installed. `pip install pyperclip` to enable copy.")
            return
        try:
            idx = int(idx_str)
        except Exception:
            error("Usage: /copy <index-number>")
            return
        # Recompute current view
        items = state.contacts_cache
        if state.filter_text:
            pat = re.compile(re.escape(state.filter_text), re.IGNORECASE)
            def match(c):
                return ( _fmt_name(c) and pat.search(_fmt_name(c)) ) or ( _fmt_email(c) and pat.search(_fmt_email(c)) )
            items = [c for c in items if match(c)]
        if idx < 1 or idx > len(items):
            error("Index out of range.")
            return
        email_val = _fmt_email(items[idx - 1]) or ""
        if not email_val:
            warn("No email on that record.")
            return
        pyperclip.copy(email_val)
        success(f"Copied: {email_val}")

    def do_export(args: List[str]):
        if not args:
            error("Usage: /export csv|md|json <path>")
            return
        kind = args[0].lower()
        path = args[1] if len(args) > 1 else None

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        if kind == "csv":
            if not state.contacts_cache:
                warn("No contacts cached.")
                return
            path = path or f"contacts-{ts}.csv"
            if not path.lower().endswith(".csv"):
                path += ".csv"
            export_csv(path, state.contacts_cache)
        elif kind == "md":
            if not state.contacts_cache:
                warn("No contacts cached.")
                return
            path = path or f"contacts-{ts}.md"
            if not path.lower().endswith(".md"):
                path += ".md"
            export_md(path, state.contacts_cache)
        elif kind == "json":
            if not state.contacts_last_payload:
                warn("No tool payload cached yet.")
                return
            path = path or f"payload-{ts}.json"
            if not path.lower().endswith(".json"):
                path += ".json"
            export_json(path, state.contacts_last_payload)
        else:
            error("Unknown export type. Use csv|md|json.")

    # ---------------- Main loop ----------------
    while True:
        user = (await get_input()).strip()
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
        if low == "/more":
            if not state.contacts_cache:
                warn("No contacts cached.")
            else:
                state.page += 1
                refresh_view()
            continue
        if low.startswith("/page "):
            try:
                p = int(user.split(None, 1)[1])
                if p < 1:
                    raise ValueError
                state.page = p
                refresh_view()
            except Exception:
                error("Usage: /page <positive-integer>")
            continue
        if low.startswith("/page_size "):
            try:
                n = int(user.split(None, 1)[1])
                if n < MIN_PAGE_SIZE:
                    n = MIN_PAGE_SIZE
                state.page_size = n
                refresh_view()
            except Exception:
                error(f"Usage: /page_size <integer ≥ {MIN_PAGE_SIZE}>")
            continue
        if low == "/contacts":
            await do_contacts()
            continue
        if low.startswith("/filter "):
            state.filter_text = user.split(None, 1)[1].strip()
            state.page = 1
            refresh_view()
            continue
        if low == "/clearfilter":
            state.filter_text = None
            refresh_view()
            continue
        if low.startswith("/view "):
            view_contact(user.split(None, 1)[1].strip())
            continue
        if low.startswith("/copy "):
            copy_email(user.split(None, 1)[1].strip())
            continue
        if low.startswith("/export"):
            parts = user.split()
            do_export(parts[1:])
            continue

        # Otherwise: forward to the model (natural language)
        # Add this user message to conversation history
        state.history_contents.append(types.UserContent(parts=[types.Part.from_text(text=user)]))

        # Manual tool-call loop until final answer
        final_text = await run_tool_call_loop()
        if final_text:
            if USE_RICH:
                console.print(Panel(final_text, title="Assistant", box=ROUNDED, style=Style(color="white")))
            else:
                print(f"\nAssistant: {final_text}")

# ---------------------- Entrypoint ----------------------
async def main():
    if not shutil.which("npx"):
        raise RuntimeError("npx is not installed. Install Node.js (includes npx).")

    server_params = StdioServerParameters(
        command="npx",
        args=["-y", "@xeroapi/xero-mcp-server@latest"],
        env={
            "XERO_CLIENT_ID": os.environ.get("XERO_CLIENT_ID", ""),
            "XERO_CLIENT_SECRET": os.environ.get("XERO_CLIENT_SECRET", ""),
            "XERO_CLIENT_BEARER_TOKEN": os.environ.get("XERO_CLIENT_BEARER_TOKEN", ""),
        },
    )

    async with AsyncExitStack() as stack:
        read, write = await stack.enter_async_context(stdio_client(server_params))
        async with ClientSession(read, write) as session:
            await session.initialize()
            # Header in plain text for environments without rich
            print(f"Connected to Xero MCP at {datetime.now().isoformat(timespec='seconds')}")
            print(f"Model: {MODEL}  |  SDK: google-genai")
            await chat_loop(session)

if __name__ == "__main__":
    asyncio.run(main())
