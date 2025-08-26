# Xero MCP Response Structure Documentation

## Overview
This document provides a comprehensive breakdown of the Xero MCP (Model Context Protocol) response structure to help understand what data we're requesting and receiving from the Xero API.

## Understanding Xero's Period System

### The Counter-Intuitive Period Mapping
**CRITICAL**: Xero's period numbering is counter-intuitive and can lead to serious data interpretation errors.

```
Xero Period System (BACKWARDS from what you'd expect):
- period_1  = December (most recent month)
- period_2  = November  
- period_3  = October
- period_4  = September
- period_5  = August
- period_6  = July
- period_7  = June
- period_8  = May
- period_9  = April
- period_10 = March
- period_11 = February
- period_12 = January (oldest month)
```

**Example**: When requesting 2024 data with 11 periods:
- `period_1` = December 2024 (NOT January!)
- `period_11` = February 2024 (NOT November!)

This is why your example shows `period_12` as January 2024 data, not December.

## MCP Response Structure

### Top-Level Response Format
```json
{
  "result": {
    "content": [
      {
        "text": "[JSON_ARRAY_OF_FINANCIAL_DATA]"
      }
    ]
  }
}
```

### Content Blocks
The `content` array typically contains multiple blocks:
1. **Metadata blocks** - Short text with report information
2. **JSON data blocks** - The actual financial data as JSON arrays

### Financial Data Structure (P&L and Balance Sheet)

#### Section Types
- `"Section"` - Contains nested rows of accounts
- `"Header"` - Section headers (no financial data)
- `"Row"` - Individual account line items
- `"SummaryRow"` - Calculated totals and subtotals

#### Individual Row Structure
```json
{
  "title": "Account Name or Section Title",
  "rowType": "Row|SummaryRow|Header|Section",
  "cells": [
    {
      "value": "Account Name",
      "attributes": [
        {
          "id": "account",
          "value": "account-uuid-here"
        }
      ]
    },
    {
      "value": "1234.56",  // Amount for period_1 (December)
      "attributes": []
    },
    {
      "value": "2345.67",  // Amount for period_2 (November)
      "attributes": []
    }
    // ... more periods
  ]
}
```

#### Cell Structure Breakdown
- **First cell**: Always the account/section name
- **Subsequent cells**: Financial amounts for each period (period_1, period_2, etc.)
- **Attributes**: Metadata like account IDs

## Our Normalized Output Structure

### What We Transform It To
```json
{
  "section": "Bank",
  "account_name": "Business Bank Account",
  "account_id": "13918178-849a-4823-9a31-57b7eac713d7",
  "amount": -146.5,
  "row_type": "Row",
  "is_summary": false,
  "period_identifier": "period_12",
  "response_period": "2024-01-01_to_2024-12-31",
  "month_name": "January",
  "month_number": 1,
  "year": 2024,
  "date_as_of": "2024-01-31"
}
```

### Enhanced Fields We Add
- `month_name`: Human-readable month name
- `month_number`: 1-12 month number
- `year`: The actual year
- `date_as_of`: The specific date this data represents

## API Call Patterns

### Profit & Loss Requests
```json
{
  "fromDate": "2024-01-01",
  "toDate": "2024-12-31",
  "periods": 11,
  "timeframe": "MONTH",
  "standardLayout": true
}
```

### Balance Sheet Requests
```json
{
  "date": "2024-12-31",
  "periods": 11,
  "timeframe": "MONTH", 
  "standardLayout": true
}
```

## Common Pitfalls

### 1. Period Interpretation
❌ **Wrong**: Assuming `period_1` = January
✅ **Correct**: `period_1` = December (most recent)

### 2. Date Ranges
❌ **Wrong**: Using period numbers directly as months
✅ **Correct**: Mapping periods to actual calendar months

### 3. Balance Sheet vs P&L
- **P&L**: Shows activity over a period (month's transactions)
- **Balance Sheet**: Shows position at a point in time (month-end snapshot)

## Data Quality Considerations

### Amount Formatting
- Amounts come as strings: `"1,234.56"`
- Need parsing to remove commas and convert to numbers
- Negative amounts may use parentheses: `"(1,234.56)"`

### Account Identification
- Account IDs are UUIDs in attributes
- Account names are in the first cell value
- Some summary rows don't have account IDs

### Missing Data
- Empty cells may contain `""` or `"0.00"`
- Some periods may be missing if no data exists
- Handle gracefully with defaults

## Example Raw Response Analysis

### What Xero Sends Us
```json
[
  {
    "title": "Bank",
    "rowType": "Section",
    "rows": [
      {
        "rowType": "Row",
        "cells": [
          {
            "value": "Business Bank Account",
            "attributes": [{"id": "account", "value": "uuid-here"}]
          },
          {"value": "1000.00"},  // December
          {"value": "950.00"},   // November  
          {"value": "900.00"}    // October
        ]
      }
    ]
  }
]
```

### What We Transform It To
```json
[
  {
    "section": "Bank",
    "account_name": "Business Bank Account", 
    "account_id": "uuid-here",
    "amount": 1000.00,
    "month_name": "December",
    "month_number": 12,
    "year": 2024,
    "date_as_of": "2024-12-31"
  },
  {
    "section": "Bank",
    "account_name": "Business Bank Account",
    "account_id": "uuid-here", 
    "amount": 950.00,
    "month_name": "November",
    "month_number": 11,
    "year": 2024,
    "date_as_of": "2024-11-30"
  }
]
```

## Summary

The key insight is that Xero's period system works backwards from the target date. When you request 11 periods ending December 31, 2024, you get:
- Period 1 = December 2024
- Period 2 = November 2024
- ...
- Period 11 = February 2024

Our normalization process converts these confusing period numbers into meaningful month names and dates for accurate financial analysis and LLM consumption.