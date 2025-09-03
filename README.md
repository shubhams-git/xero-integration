# Xero Assistant (MCP Client)

A conversational financial copilot for Xero built on the Model Context Protocol (MCP) and Google's Gemini. It connects to the official Xero MCP server (runs via npx), calls Xero tools, and returns decision-ready answers with clean UX: compact tables, sensible defaults, and clear next steps.

This client focuses on doing the task (auto-routing to the right tool, paginating, aggregating where possible), handling common Xero quirks (e.g., Profit & Loss monthly periods 1–11), and gracefully recovering from errors.

## Highlights

- **MCP-native Xero tools**: Discover and call list-profit-and-loss, list-report-balance-sheet, list-invoices, list-bank-transactions, etc.
- **Smart system instruction**: Resolves vague date phrases ("this year", "last month"), picks optimal tools, and minimizes back-and-forth.
- **Resilient P&L monthly**: If Xero rejects periods=12, the assistant automatically falls back to a 2-call or per-month strategy.
- **Good UX out of the box**:
  - Nice terminal UI (rich) with panels and tables
  - `/tools`, `/call`, `/export json|csv`, `/thinking` commands
  - Compact rendering of tool payloads; CSV/JSON export
- **Safety & redaction**: Strips sensitive headers/tokens from surfaced errors.
- **Configurable**: Toggle visible "thinking", iteration limits, model choice, and paging defaults with env vars.

## What's in here

```
mcp_client.py        # The chat client (CLI) that connects to Xero MCP and Gemini
README.md            # You are here
```

The client runs the Xero MCP server for you via `npx -y @xeroapi/xero-mcp-server@latest`.

## Architecture (how it works)

1. You ask (e.g., "How much profit did we make each month in 2025?")
2. The system instruction turns that into a small plan (pick tool + parameters).
3. The client calls MCP tools, renders compact results, and iterates if needed.
4. The assistant returns a final, decision-ready answer (with key metrics, a short table, assumptions/filters, and next steps).

## Prerequisites

- **Python 3.11+** (3.12 recommended)
- **Node.js 18+** (includes npx)
- **Xero developer credentials**
  - `XERO_CLIENT_ID`
  - `XERO_CLIENT_SECRET`
- **Google AI access** for Gemini (the `google-genai` Python SDK)

## Installation

```bash
git clone <your-repo-url>
cd xero-assistant
python -m venv .venv
. .venv/Scripts/activate   # Windows
# PowerShell: .\.venv\Scripts\Activate.ps1  |  macOS/Linux: source .venv/bin/activate
pip install -U google-genai mcp rich prompt_toolkit
node --version
npx --version
```

If you prefer, add a `requirements.txt` and pin versions you've tested with your environment.

## Configuration

Set environment variables (PowerShell examples shown; adapt for bash/zsh):

```powershell
# Required Xero app credentials
$env:XERO_CLIENT_ID     = "<your-xero-client-id>"
$env:XERO_CLIENT_SECRET = "<your-xero-client-secret>"

# Optional model + behavior
$env:GEMINI_MODEL            = "gemini-2.5-flash"  # default
$env:SHOW_THINKING           = "false"             # show/hide assistant's "thinking" summaries
$env:THINKING_BUDGET         = "-1"                # -1 dynamic, 0 off, or a fixed token budget
$env:MAX_TOOL_ITERATIONS     = "12"                # increase if you expect multi-page jobs
$env:MAX_TURNS               = "40"

# Optional: override MCP command/args (defaults run the Xero MCP server via npx)
# $env:MCP_COMMAND   = "npx"
# $env:MCP_ARGS_JSON = '["-y","@xeroapi/xero-mcp-server@latest"]'
```

The MCP server handles Xero auth using your app credentials. You may be prompted to sign in on first use.

## Run

```bash
python mcp_client.py
```

You'll see a banner, discovered tools, and a `you ▸` prompt.

## Using the CLI

### Type a natural request

- "List the org details"
- "How much profit did we make each month this year?"
- "Show cash received and paid last month and the top 5 vendors."
- "Which customers owe us the most in the last 90 days?"
- "Balance sheet as of 2025-06-30."

The assistant will:
- Resolve dates (e.g., "this year" → YYYY-01-01 to YYYY-12-31)
- Pick the right tool
- Recover from common API quirks (e.g., P&L monthly periods 1–11)
- Return a compact, decision-ready summary

### Built-in commands

```
/tools                         # list available MCP tools
/call <name> <json-args>       # call a tool directly (advanced)
  e.g. /call list-invoices {"pageSize":20}
/export json <path>            # save last tool payload as JSON
/export csv <path>             # best-effort CSV if payload has a list of dicts
/thinking                      # toggle visible thinking
/help                          # this menu
/quit                          # exit
```

## What the assistant knows to do

### Smart defaults

- **Dates**: "this month/year", "last month", "LTM", quarters (Q1–Q4) → resolved automatically
- **Paging**: page size 20; newest first
- **Money**: currency codes (AUD/NZD/etc), 2-decimals, thousands separators

### Routing examples

- "org details" → `list-organisation-details`
- "profit & loss / monthly P&L" → `list-profit-and-loss` (with monthly timeframe)
  - If API returns 400: periods must be 1–11, it automatically:
    - Runs Jan–Nov with periods=11 + a separate December call, or
    - Falls back to per-month calls (≤12 total), then combines client-side
- "balance sheet as of " → `list-report-balance-sheet` with that date
- "top debtors last 90 days" → `list-invoices` + grouping by contact (paid vs outstanding if available)
- "cash received/paid last month" → `list-bank-transactions` + inflow/outflow/net + top counterparties

### Answer format (always)

1. **Direct answer** – short headline
2. **Key metrics** – bullets with numbers
3. **Details** – compact table (e.g., Month, Revenue, COGS, GP, Opex, Net)
4. **Assumptions & Filters** – exact dates/statuses/defaults used
5. **Next steps** – one or two precise follow-ups

## Exporting data

After any tool call (automatic or `/call`), you can export the last payload:

```
/export json out/invoices.json
/export csv  out/invoices.csv
```

CSV export is best-effort: it scans the payload for a tabular `list[dict]` (e.g., items, transactions, Contacts, etc.).

## Tuning behavior

- **Visible thinking**: `$env:SHOW_THINKING="true"` shows concise "Thinking Process" summaries to help you debug plans/routing.
- **Iteration limit**: If your tasks require multiple pages/months, bump `$env:MAX_TOOL_ITERATIONS` (e.g., 12–20).
- **Model choice**: Set `$env:GEMINI_MODEL` to a compatible Gemini model.

## Troubleshooting

### Xero P&L monthly error 400 ValidationException: The supplied period parameter must be an integer between 1 and 11.
→ The assistant automatically switches to two-call (Jan–Nov + Dec) or per-month calls and combines results. If you still see issues, increase `$env:MAX_TOOL_ITERATIONS`.

### Google GenAI mime type error 400 INVALID_ARGUMENT ... response_mime_type
→ This client sends `text/plain`. Ensure you're on a recent `google-genai` version and re-run.

### "npx not found" or MCP server can't start
→ Install Node.js 18+ (`node -v`, `npx -v`). The client uses: `npx -y @xeroapi/xero-mcp-server@latest`

### Auth / scope errors
→ Sign in again when prompted and ensure your Xero app has at least:
- `accounting.contacts`
- `accounting.reports.read`
- `accounting.settings`
- `accounting.transactions`

(Payroll features require the payroll scopes)

### Hit the tool-iteration limit
→ Raise `$env:MAX_TOOL_ITERATIONS` (e.g., to 12 or 20) for big, multi-page jobs.

## Security notes

- Never commit secrets/tokens. Use environment variables.
- The client redacts sensitive headers/tokens from error payloads before showing them.
- Rotate Xero credentials per your policy.

## Extending & customizing

- **System instruction**: The core behavior is defined in a triple-quoted SYSTEM string in `mcp_client.py`. Tweak routing rules, defaults, or add recipes for new workloads (e.g., tracking-category analysis).
- **Commands**: Add more CLI helpers (e.g., `/save xlsx`, `/plot`) near the command handlers.
- **Servers**: Point to other MCP servers by setting `MCP_COMMAND`/`MCP_ARGS_JSON`.

## Sample prompts to try

- "List the org details."
- "How much profit did we make each month in 2025?"
- "Show cash received and paid last month and the top 5 vendors."
- "Which customers owe us the most in the last 90 days? Summarize by contact."
- "Balance sheet as of 2025-06-30."
- "Export the last invoice list as CSV."

## Contributing

1. Fork the repo
2. Create a feature branch
3. Make changes + add tests where possible
4. Verify prompts and common flows
5. Open a PR

## License

This project is provided as-is for development and educational use. You're responsible for complying with Xero's API terms and your organization's data/security policies.