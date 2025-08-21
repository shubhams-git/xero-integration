#!/usr/bin/env python3
"""
utils/xero_tools.py

Professional Xero API wrapper functions using MCP client.
Includes data models, error handling, and comprehensive business logic.
Enhanced with better authentication error detection and logging.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, Union

from .mcp_connection import MCPClient, MCPError, MCPAuthenticationError

logger = logging.getLogger(__name__)


# ---------- Data Models ----------

@dataclass
class DateRange:
    """Represents a date range for reporting."""
    start_date: date
    end_date: date
    
    def to_iso_dict(self) -> Dict[str, str]:
        """Convert to dictionary with ISO date strings."""
        return {
            "fromDate": self.start_date.isoformat(),
            "toDate": self.end_date.isoformat()
        }
    
    def __str__(self) -> str:
        return f"{self.start_date.isoformat()} to {self.end_date.isoformat()}"


@dataclass 
class XeroToolResponse:
    """Wrapper for Xero tool responses with metadata."""
    success: bool
    data: Any
    tool_name: str
    execution_time: Optional[float] = None
    error_message: Optional[str] = None
    is_auth_error: bool = False


class XeroToolError(MCPError):
    """Specific error for Xero tool operations."""
    pass


def is_authentication_error(error_message: str) -> bool:
    """
    Check if an error message indicates an authentication failure.
    
    Args:
        error_message: Error message to check
        
    Returns:
        bool: True if this appears to be an authentication error
    """
    if not error_message:
        return False
        
    auth_indicators = [
        "status code 401",
        "status code 403",
        "unauthorized", 
        "forbidden",
        "authentication failed",
        "invalid token",
        "token expired",
        "access denied",
        "token has expired",
        "invalid bearer token"
    ]
    
    error_lower = error_message.lower()
    return any(indicator in error_lower for indicator in auth_indicators)


# ---------- Date Utilities ----------

def get_current_financial_year(reference_date: Optional[date] = None) -> DateRange:
    """
    Get the current financial year date range (assuming April 1 - March 31).
    
    Args:
        reference_date: Reference date (defaults to today)
        
    Returns:
        DateRange: Financial year range
    """
    if reference_date is None:
        reference_date = date.today()
    
    # Financial year starts April 1
    if reference_date.month >= 4:  # April onwards = current FY
        fy_start = date(reference_date.year, 4, 1)
        fy_end = date(reference_date.year + 1, 3, 31)
    else:  # Jan-Mar = previous FY
        fy_start = date(reference_date.year - 1, 4, 1)
        fy_end = date(reference_date.year, 3, 31)
    
    return DateRange(fy_start, fy_end)


def get_last_complete_month(reference_date: Optional[date] = None) -> DateRange:
    """
    Get the last complete month's date range.
    
    Args:
        reference_date: Reference date (defaults to today)
        
    Returns:
        DateRange: Last complete month range
    """
    if reference_date is None:
        reference_date = date.today()
    
    # First day of current month
    first_current = date(reference_date.year, reference_date.month, 1)
    # Last day of previous month
    last_previous = first_current - timedelta(days=1)
    # First day of previous month
    first_previous = date(last_previous.year, last_previous.month, 1)
    
    return DateRange(first_previous, last_previous)


def get_current_month(reference_date: Optional[date] = None) -> DateRange:
    """
    Get the current month's date range.
    
    Args:
        reference_date: Reference date (defaults to today)
        
    Returns:
        DateRange: Current month range
    """
    if reference_date is None:
        reference_date = date.today()
    
    # First day of current month
    first_day = date(reference_date.year, reference_date.month, 1)
    
    # Last day of current month
    if reference_date.month == 12:
        last_day = date(reference_date.year + 1, 1, 1) - timedelta(days=1)
    else:
        last_day = date(reference_date.year, reference_date.month + 1, 1) - timedelta(days=1)
    
    return DateRange(first_day, last_day)


def get_quarter_dates(quarter: int, year: int) -> DateRange:
    """
    Get date range for a specific quarter.
    
    Args:
        quarter: Quarter number (1-4)
        year: Year
        
    Returns:
        DateRange: Quarter date range
        
    Raises:
        ValueError: If quarter is not 1-4
    """
    if quarter not in [1, 2, 3, 4]:
        raise ValueError("Quarter must be between 1 and 4")
    
    quarter_starts = {
        1: (1, 1),   # Jan 1
        2: (4, 1),   # Apr 1  
        3: (7, 1),   # Jul 1
        4: (10, 1)   # Oct 1
    }
    
    quarter_ends = {
        1: (3, 31),  # Mar 31
        2: (6, 30),  # Jun 30
        3: (9, 30),  # Sep 30
        4: (12, 31)  # Dec 31
    }
    
    start_month, start_day = quarter_starts[quarter]
    end_month, end_day = quarter_ends[quarter]
    
    start_date = date(year, start_month, start_day)
    end_date = date(year, end_month, end_day)
    
    return DateRange(start_date, end_date)


# ---------- Response Formatting ----------

def format_content_blocks(response: Dict[str, Any], pretty_json: bool = True) -> str:
    """
    Format MCP response content blocks into readable text.
    
    Args:
        response: MCP tool response
        pretty_json: Whether to pretty-print JSON arrays
        
    Returns:
        str: Formatted content
    """
    content_blocks = response.get("result", {}).get("content", [])
    
    if not content_blocks:
        return "(no content returned)"
    
    formatted_parts = []
    
    for i, block in enumerate(content_blocks, 1):
        if len(content_blocks) > 1:
            formatted_parts.append(f"--- Content Block {i} ---")
        
        if isinstance(block, dict) and "text" in block:
            text = block["text"]
            
            if pretty_json and isinstance(text, str):
                # Try to detect and format JSON arrays
                start_idx = text.find("[")
                end_idx = text.rfind("]")
                
                if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
                    try:
                        json_part = text[start_idx:end_idx + 1]
                        parsed_json = json.loads(json_part)
                        
                        # Add any text before JSON
                        if start_idx > 0:
                            formatted_parts.append(text[:start_idx].strip())
                        
                        # Add formatted JSON
                        formatted_parts.append(json.dumps(parsed_json, indent=2))
                        continue
                        
                    except json.JSONDecodeError:
                        pass  # Fall through to regular text handling
            
            # Regular text handling
            formatted_parts.append(str(text).strip())
        else:
            # Non-text block, format as JSON
            formatted_parts.append(json.dumps(block, indent=2))
    
    return "\n\n".join(formatted_parts)


def print_formatted_response(response: Dict[str, Any], title: Optional[str] = None) -> None:
    """
    Print a formatted MCP response with optional title.
    
    Args:
        response: MCP tool response
        title: Optional title to display
    """
    if title:
        print(f"\n=== {title} ===")
    
    formatted_content = format_content_blocks(response)
    print(formatted_content)


# ---------- Core Xero Tool Wrappers ----------

def get_organisation_details(client: MCPClient) -> XeroToolResponse:
    """
    Get organisation details from Xero.
    
    Args:
        client: MCP client instance
        
    Returns:
        XeroToolResponse: Organisation details
    """
    start_time = datetime.now()
    
    try:
        # Try primary method first
        logger.debug("🏢 Fetching organisation details")
        response = client.call_tool("list-organisation-details", {})
        
        execution_time = (datetime.now() - start_time).total_seconds()
        
        return XeroToolResponse(
            success=True,
            data=response,
            tool_name="list-organisation-details",
            execution_time=execution_time
        )
        
    except MCPAuthenticationError as e:
        execution_time = (datetime.now() - start_time).total_seconds()
        error_msg = str(e)
        
        return XeroToolResponse(
            success=False,
            data=None,
            tool_name="list-organisation-details",
            execution_time=execution_time,
            error_message=error_msg,
            is_auth_error=True
        )
        
    except MCPError as e:
        logger.warning(f"Primary method failed, trying fallback: {e}")
        
        try:
            # Fallback method
            response = client.call_tool("get-organisation", {})
            execution_time = (datetime.now() - start_time).total_seconds()
            
            return XeroToolResponse(
                success=True,
                data=response,
                tool_name="get-organisation",
                execution_time=execution_time
            )
            
        except MCPAuthenticationError as fallback_auth_error:
            execution_time = (datetime.now() - start_time).total_seconds()
            error_msg = str(fallback_auth_error)
            
            return XeroToolResponse(
                success=False,
                data=None,
                tool_name="organisation-details",
                execution_time=execution_time,
                error_message=error_msg,
                is_auth_error=True
            )
            
        except MCPError as fallback_error:
            execution_time = (datetime.now() - start_time).total_seconds()
            error_msg = f"Both methods failed. Primary: {e}, Fallback: {fallback_error}"
            
            # Check if either error looks like an auth error
            is_auth = (is_authentication_error(str(e)) or 
                      is_authentication_error(str(fallback_error)))
            
            return XeroToolResponse(
                success=False,
                data=None,
                tool_name="organisation-details",
                execution_time=execution_time,
                error_message=error_msg,
                is_auth_error=is_auth
            )


def get_profit_and_loss(
    client: MCPClient,
    date_range: DateRange,
    standard_layout: bool = True
) -> XeroToolResponse:
    """
    Get Profit & Loss report for specified date range.
    
    Args:
        client: MCP client instance
        date_range: Date range for the report
        standard_layout: Whether to use standard layout
        
    Returns:
        XeroToolResponse: P&L report data
    """
    start_time = datetime.now()
    
    try:
        logger.debug(f"📊 Fetching P&L report for {date_range}")
        
        arguments = date_range.to_iso_dict()
        arguments["standardLayout"] = standard_layout
        
        response = client.call_tool("list-profit-and-loss", arguments)
        execution_time = (datetime.now() - start_time).total_seconds()
        
        return XeroToolResponse(
            success=True,
            data=response,
            tool_name="list-profit-and-loss",
            execution_time=execution_time
        )
        
    except MCPAuthenticationError as e:
        execution_time = (datetime.now() - start_time).total_seconds()
        
        return XeroToolResponse(
            success=False,
            data=None,
            tool_name="list-profit-and-loss", 
            execution_time=execution_time,
            error_message=str(e),
            is_auth_error=True
        )
        
    except MCPError as e:
        execution_time = (datetime.now() - start_time).total_seconds()
        error_msg = str(e)
        is_auth = is_authentication_error(error_msg)
        
        return XeroToolResponse(
            success=False,
            data=None,
            tool_name="list-profit-and-loss", 
            execution_time=execution_time,
            error_message=error_msg,
            is_auth_error=is_auth
        )


def get_balance_sheet(
    client: MCPClient,
    as_at_date: date,
    standard_layout: bool = True
) -> XeroToolResponse:
    """
    Get Balance Sheet report as at specified date.
    
    Args:
        client: MCP client instance
        as_at_date: Date for balance sheet
        standard_layout: Whether to use standard layout
        
    Returns:
        XeroToolResponse: Balance sheet data
    """
    start_time = datetime.now()
    
    try:
        logger.debug(f"📈 Fetching Balance Sheet as at {as_at_date.isoformat()}")
        
        response = client.call_tool("list-report-balance-sheet", {
            "date": as_at_date.isoformat(),
            "standardLayout": standard_layout
        })
        
        execution_time = (datetime.now() - start_time).total_seconds()
        
        return XeroToolResponse(
            success=True,
            data=response,
            tool_name="list-report-balance-sheet",
            execution_time=execution_time
        )
        
    except MCPAuthenticationError as e:
        execution_time = (datetime.now() - start_time).total_seconds()
        
        return XeroToolResponse(
            success=False,
            data=None,
            tool_name="list-report-balance-sheet",
            execution_time=execution_time,
            error_message=str(e),
            is_auth_error=True
        )
        
    except MCPError as e:
        execution_time = (datetime.now() - start_time).total_seconds()
        error_msg = str(e)
        is_auth = is_authentication_error(error_msg)
        
        return XeroToolResponse(
            success=False,
            data=None,
            tool_name="list-report-balance-sheet",
            execution_time=execution_time,
            error_message=error_msg,
            is_auth_error=is_auth
        )


def get_contacts(
    client: MCPClient,
    where: Optional[str] = None,
    order: Optional[str] = None,
    page: int = 1
) -> XeroToolResponse:
    """
    Get contacts from Xero.
    
    Args:
        client: MCP client instance
        where: Optional filter condition
        order: Optional sort order
        page: Page number for pagination
        
    Returns:
        XeroToolResponse: Contacts data
    """
    start_time = datetime.now()
    
    try:
        logger.debug("👥 Fetching contacts")
        
        arguments = {"page": page}
        if where:
            arguments["where"] = where
        if order:
            arguments["order"] = order
        
        response = client.call_tool("list-contacts", arguments)
        execution_time = (datetime.now() - start_time).total_seconds()
        
        return XeroToolResponse(
            success=True,
            data=response,
            tool_name="list-contacts",
            execution_time=execution_time
        )
        
    except MCPAuthenticationError as e:
        execution_time = (datetime.now() - start_time).total_seconds()
        
        return XeroToolResponse(
            success=False,
            data=None,
            tool_name="list-contacts",
            execution_time=execution_time,
            error_message=str(e),
            is_auth_error=True
        )
        
    except MCPError as e:
        execution_time = (datetime.now() - start_time).total_seconds()
        error_msg = str(e)
        is_auth = is_authentication_error(error_msg)
        
        return XeroToolResponse(
            success=False,
            data=None,
            tool_name="list-contacts",
            execution_time=execution_time,
            error_message=error_msg,
            is_auth_error=is_auth
        )


def get_invoices(
    client: MCPClient,
    where: Optional[str] = None,
    order: Optional[str] = None,
    statuses: Optional[str] = None,
    page: int = 1
) -> XeroToolResponse:
    """
    Get invoices from Xero.
    
    Args:
        client: MCP client instance
        where: Optional filter condition
        order: Optional sort order  
        statuses: Optional status filter
        page: Page number for pagination
        
    Returns:
        XeroToolResponse: Invoices data
    """
    start_time = datetime.now()
    
    try:
        logger.debug("🧾 Fetching invoices")
        
        arguments = {"page": page}
        if where:
            arguments["where"] = where
        if order:
            arguments["order"] = order
        if statuses:
            arguments["Statuses"] = statuses
            
        response = client.call_tool("list-invoices", arguments)
        execution_time = (datetime.now() - start_time).total_seconds()
        
        return XeroToolResponse(
            success=True,
            data=response,
            tool_name="list-invoices", 
            execution_time=execution_time
        )
        
    except MCPAuthenticationError as e:
        execution_time = (datetime.now() - start_time).total_seconds()
        
        return XeroToolResponse(
            success=False,
            data=None,
            tool_name="list-invoices",
            execution_time=execution_time,
            error_message=str(e),
            is_auth_error=True
        )
        
    except MCPError as e:
        execution_time = (datetime.now() - start_time).total_seconds()
        error_msg = str(e)
        is_auth = is_authentication_error(error_msg)
        
        return XeroToolResponse(
            success=False,
            data=None,
            tool_name="list-invoices",
            execution_time=execution_time,
            error_message=error_msg,
            is_auth_error=is_auth
        )


# ---------- Convenience Functions ----------

def get_monthly_pl_summary(client: MCPClient, months_back: int = 3) -> List[XeroToolResponse]:
    """
    Get P&L summaries for the last N complete months.
    
    Args:
        client: MCP client instance
        months_back: Number of months to fetch (default 3)
        
    Returns:
        List[XeroToolResponse]: P&L responses for each month
    """
    results = []
    current_date = date.today()
    
    for i in range(months_back):
        # Calculate the month to fetch
        target_month = current_date.month - i - 1
        target_year = current_date.year
        
        if target_month <= 0:
            target_month += 12
            target_year -= 1
        
        # Get the month's date range
        first_day = date(target_year, target_month, 1)
        if target_month == 12:
            last_day = date(target_year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = date(target_year, target_month + 1, 1) - timedelta(days=1)
        
        date_range = DateRange(first_day, last_day)
        
        logger.debug(f"Fetching P&L for {date_range}")
        pl_response = get_profit_and_loss(client, date_range)
        results.append(pl_response)
        
        # If we hit an auth error, stop trying
        if pl_response.is_auth_error:
            logger.warning("Authentication error detected, stopping monthly fetch")
            break
    
    return results


def health_check(client: MCPClient) -> Dict[str, Any]:
    """
    Perform a basic health check of the Xero connection.
    
    Args:
        client: MCP client instance
        
    Returns:
        Dict: Health check results
    """
    logger.info("🔍 Performing Xero connection health check")
    
    health_results = {
        "overall_status": "unknown",
        "tests": {},
        "summary": {},
        "authentication_status": "unknown"
    }
    
    # Test 1: Organisation details
    org_response = get_organisation_details(client)
    health_results["tests"]["organisation"] = {
        "success": org_response.success,
        "execution_time": org_response.execution_time,
        "error": org_response.error_message,
        "is_auth_error": org_response.is_auth_error
    }
    
    # If org check failed with auth error, mark it and skip other tests
    if org_response.is_auth_error:
        health_results["authentication_status"] = "failed"
        health_results["overall_status"] = "authentication_failed"
    else:
        # Test 2: Last month P&L (only if auth is working)
        last_month = get_last_complete_month()
        pl_response = get_profit_and_loss(client, last_month)
        health_results["tests"]["profit_loss"] = {
            "success": pl_response.success,
            "execution_time": pl_response.execution_time,
            "error": pl_response.error_message,
            "date_range": str(last_month),
            "is_auth_error": pl_response.is_auth_error
        }
        
        if pl_response.is_auth_error:
            health_results["authentication_status"] = "failed"
        else:
            health_results["authentication_status"] = "valid"
    
    # Calculate overall status
    auth_errors = any(test.get("is_auth_error", False) for test in health_results["tests"].values())
    if auth_errors:
        health_results["overall_status"] = "authentication_failed"
    else:
        all_successful = all(test["success"] for test in health_results["tests"].values())
        health_results["overall_status"] = "healthy" if all_successful else "degraded"
    
    # Summary statistics
    successful_tests = sum(1 for test in health_results["tests"].values() if test["success"])
    total_tests = len(health_results["tests"])
    avg_response_time = sum(
        test["execution_time"] or 0 
        for test in health_results["tests"].values()
    ) / max(total_tests, 1)
    
    health_results["summary"] = {
        "successful_tests": successful_tests,
        "total_tests": total_tests,
        "success_rate": successful_tests / max(total_tests, 1),
        "average_response_time": avg_response_time,
        "authentication_errors": sum(1 for test in health_results["tests"].values() if test.get("is_auth_error", False))
    }
    
    return health_results