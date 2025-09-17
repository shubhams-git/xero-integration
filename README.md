# Xero MCP Integration System

A comprehensive system for integrating with Xero accounting software using Model Context Protocol (MCP) and Gemini AI for natural language interactions.

## Architecture Overview

```
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│   Client    │────▶│  OAuth Flow  │───▶│   NeonDB     │
│  Browser    │     │ (server/app) │     │  (Postgres)  │
└─────────────┘     └──────────────┘     └──────────────┘
                            │                      │
                            ▼                      ▼
                    ┌──────────────┐     ┌──────────────┐
                    │ Xero OAuth   │     │ Token Store  │
                    │   Service    │     │   & Refresh  │
                    └──────────────┘     └──────────────┘
                                                   │
                    ┌──────────────────────────────
                    ▼                            
            ┌──────────────┐              ┌──────────────┐
            │ mcp_client.py│              │Xero MCP      │
            │   (Chat UI)  │◀───────────▶│   Server     │
            └──────────────┘              └──────────────┘
                    │                              
                    ▼                              
            ┌──────────────┐
            │  Gemini AI   │
            │  (2.5 Flash) │              
            └──────────────┘              
```

## Components

### 1. OAuth Backend (`server/`)
- **`app.py`**: FastAPI server handling Xero OAuth flow
- **`db.py`**: Database operations for storing/retrieving Xero credentials
- **`config.py`**: Environment configuration and URL normalization
- **`schema.sql`**: PostgreSQL schema for storing Xero user connections

### 2. MCP Client (`mcp_client.py`)
- Interactive chat interface using Gemini AI
- Connects to Xero MCP server with dynamic client selection
- Supports complex queries with automatic tool calling
- Rich terminal UI with table rendering and export capabilities

### 3. Token Management (`xero_token_refresh.py`)
- Automatic token refresh before expiry
- Handles Xero's token rotation requirements
- Command-line utility for manual token management

## Setup

### Prerequisites
- Python 3.8+
- Node.js and npm (for MCP server)
- PostgreSQL database (NeonDB recommended)
- Xero developer account with OAuth2 app configured

### Environment Variables

Create a `.env` file with:

```bash
# Xero OAuth Credentials
XERO_CLIENT_ID=your_xero_client_id
XERO_CLIENT_SECRET=your_xero_client_secret

# Database (NeonDB PostgreSQL)
DATABASE_URL=postgresql://user:password@host.neon.tech/dbname

# OAuth Redirect URL (for production use https://)
REDIRECT_BASE_URL=http://localhost:8000  # or https://your-domain.com

# Gemini AI
GEMINI_MODEL=gemini-2.5-flash  # or gemini-2.5-pro

# Optional: Xero Scopes (defaults included)
XERO_SCOPES=offline_access openid profile email accounting.settings accounting.reports.read accounting.transactions accounting.contacts
```

### Installation

1. **Install Python dependencies:**
```bash
pip install -r server/requirements.txt
pip install google-genai mcp psycopg[binary] python-dotenv requests
# Optional for enhanced UI
pip install rich prompt-toolkit
```

2. **Initialize database:**
```bash
python -c "from server.db import init_db; init_db()"
```

## Usage Flow

### 1. Initial OAuth Setup

Start the OAuth server:
```bash
python server/app.py
```

Visit `http://localhost:8000` and click "Connect to Xero" to authorize your organization.

### 2. Using the Chat Interface

Run the MCP client:
```bash
python mcp_client.py
```

The client will:
1. Display available Xero organizations from the database
2. Prompt you to select a client ID
3. Automatically refresh tokens if needed
4. Launch interactive chat with Gemini AI

### 3. Example Queries

The system handles natural language queries like:

- **Financial Reports:**
  - "Show me profit and loss for this month"
  - "What's my cash position?"
  - "Balance sheet as of last quarter end"

- **Accounts Receivable:**
  - "Who owes us the most money?"
  - "Show overdue invoices over 90 days"
  - "Create an invoice for Client ABC"

- **Operations:**
  - "Convert quote Q-001 to an invoice"
  - "Approve pending timesheets"
  - "Show top expenses by vendor this year"

### 4. Chat Commands

- `/tools` - List available MCP tools
- `/call <tool> <args>` - Call a tool directly
- `/export json <path>` - Export last result as JSON
- `/export csv <path>` - Export last result as CSV
- `/help` - Show command help
- `/quit` - Exit

## Token Management

### Automatic Refresh
Tokens are automatically refreshed when:
- Token is expired
- Token expires within 5 minutes
- During client selection if token is invalid

### Manual Refresh
Check and refresh specific client:
```bash
python xero_token_refresh.py <client_id>
```

Refresh all expiring tokens:
```bash
python xero_token_refresh.py
```

## Database Schema

The system uses a single PostgreSQL table:

```sql
xero_users
├── id (SERIAL PRIMARY KEY)
├── tenant_id (TEXT UNIQUE NOT NULL)
├── tenant_name (TEXT)
├── access_token (TEXT NOT NULL)
├── refresh_token (TEXT)
├── expires_at (TIMESTAMPTZ NOT NULL)
├── connected_at (TIMESTAMPTZ)
└── updated_at (TIMESTAMPTZ)
```

## Key Features

### Intelligent Query Processing
- Automatic tool selection based on query intent
- Multi-step planning for complex queries
- Client-side aggregation for large datasets
- Auto-pagination with safety limits

### Token Security
- Tokens stored encrypted in PostgreSQL
- Automatic rotation following Xero requirements
- Grace period refresh (30-60 seconds before expiry)
- Masked token display in UI

### Rich Data Visualization
- Tabular display for lists and reports
- Export to JSON/CSV formats
- Automatic summarization of large datasets
- Progress indicators for long operations

## Troubleshooting

### Common Issues

1. **"No valid Xero tokens found"**
   - Re-run OAuth flow: `python server/app.py`
   - Check DATABASE_URL is correct
   - Verify tokens aren't expired

2. **"Token refresh failed"**
   - Refresh tokens may be invalid
   - Re-authorize through OAuth flow
   - Check XERO_CLIENT_ID and XERO_CLIENT_SECRET

3. **"Database connection failed"**
   - Verify DATABASE_URL format
   - Check network connection to NeonDB
   - Ensure database is initialized

4. **Rate limiting**
   - MCP client implements automatic retry with backoff
   - Reduce query frequency if persistent

### Debug Mode

View stored connections:
```bash
curl http://localhost:8000/api/xero/users
```

Test database connection:
```bash
curl http://localhost:8000/test/db
```

## System Limits

- **Auto-pagination**: Maximum 15 pages per query
- **Conversation history**: Keeps last 24 turns
- **Token refresh**: 30-second safety buffer
- **Request timeout**: 15 seconds for token service
- **Export size**: Tables truncated at 20 rows for display

## MCP Tools Available

The system has access to 40+ Xero MCP tools including:

- Organization management
- Contacts and items
- Invoices and quotes
- Payments and credit notes
- Bank transactions
- Reports (P&L, Balance Sheet, Trial Balance)
- Timesheets and payroll
- Tracking categories

Full tool documentation: [Xero MCP Server](https://github.com/XeroAPI/xero-mcp-server)

