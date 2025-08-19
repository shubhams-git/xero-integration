# Xero MCP Client

A Python client for interacting with Xero accounting data through the Model Context Protocol (MCP). This project provides tools for authenticating with Xero's API and extracting financial reports as raw JSON data.

## Features

- **Authentication**: Generate OAuth2 bearer tokens for Xero API access
- **MCP Integration**: Connect to Xero's MCP server for structured data access
- **Financial Reports**: Extract Balance Sheet, Profit & Loss, and Trial Balance reports
- **Raw Data Export**: Save complete API responses as JSON files for analysis
- **Flexible Configuration**: Support for different report dates and output formats

## Project Structure

```
├── auth_token_generator.py    # OAuth2 token generation for Xero API
├── mcp_client.py             # Basic MCP client with sample report extraction
├── raw_data_client.py        # Advanced client for bulk financial data export
├── .kiro/settings/mcp.json   # MCP server configuration
└── README.md                 # This file
```

## Prerequisites

- Python 3.7+
- Node.js and npm (for running the Xero MCP server)
- Xero developer account with API credentials

## Installation

1. Clone this repository:
```bash
git clone <repository-url>
cd xero-mcp-client
```

2. Install Python dependencies:
```bash
pip install requests
```

3. Ensure Node.js is installed for the MCP server:
```bash
node --version
npm --version
```

## Configuration

### 1. Xero API Credentials

Update the credentials in `auth_token_generator.py`:
```python
CLIENT_ID = 'your-client-id'
CLIENT_SECRET = 'your-client-secret'
```

### 2. Generate Bearer Token

Run the token generator to get a fresh access token:
```bash
python auth_token_generator.py
```

### 3. Update MCP Configuration

Replace the bearer token in `.kiro/settings/mcp.json` with your fresh token:
```json
{
  "mcpServers": {
    "xero": {
      "env": {
        "XERO_CLIENT_BEARER_TOKEN": "your-fresh-bearer-token"
      }
    }
  }
}
```

## Usage

### Basic Report Extraction

Extract Balance Sheet and Profit & Loss reports:
```bash
python mcp_client.py
```

This will create:
- `balance_sheet_raw.json`
- `profit_loss_raw.json`

### Comprehensive Data Export

Extract all financial reports with custom naming:
```bash
python raw_data_client.py
```

This will create:
- `demo_company_balance_sheet_20250819_raw.json`
- `demo_company_profit_loss_20250819_raw.json`
- `demo_company_trial_balance_20250819_raw.json`

### Custom Date and Prefix

Modify the `main()` function in `raw_data_client.py`:
```python
results = client.get_financial_reports(
    report_date="2025-12-31",
    output_prefix="my_company"
)
```

## API Scopes

The client is configured with the following Xero API scopes:
- `accounting.contacts` - Access to customer and supplier information
- `accounting.reports.read` - Read access to financial reports
- `accounting.settings` - Access to chart of accounts and settings
- `accounting.transactions` - Access to invoices, bills, and transactions

## Output Format

All reports are saved as raw JSON responses from the Xero API, preserving the complete data structure for further processing or analysis.

Example output structure:
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "Balance Sheet data..."
      }
    ]
  }
}
```

## Security Notes

- Bearer tokens expire after 30 minutes
- Never commit tokens to version control
- Use environment variables for production deployments
- Regularly rotate API credentials

## Troubleshooting

### Token Expired
If you get authentication errors, generate a new bearer token:
```bash
python auth_token_generator.py
```

### MCP Server Issues
Ensure Node.js is available and the Xero MCP server can be downloaded:
```bash
npx -y @xeroapi/xero-mcp-server@latest
```

### Connection Errors
Check that your Xero app has the required scopes and is properly configured.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## License

This project is provided as-is for educational and development purposes. Please ensure compliance with Xero's API terms of service.