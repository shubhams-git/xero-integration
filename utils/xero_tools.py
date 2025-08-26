"""
utils/xero_tools.py

Professional Xero API wrapper functions using MCP client.
Includes data models, error handling, and efficient multi-period API calls.
Enhanced with better authentication error detection and periods support.
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


def get_month_with_comparisons(target_year: int, target_month: int, comparison_periods: int = 11) -> tuple[date, int]:
    """
    Get the target date and periods for P&L retrieval with monthly comparisons.
    
    Args:
        target_year: Year of the target month
        target_month: Month number (1-12) of the target month
        comparison_periods: Number of comparison periods (default 11 for 12 months total)
        
    Returns:
        tuple[date, int]: (target_month_date, periods) for use with get_profit_and_loss_periods
        
    Example:
        # Get December 2024 + 11 previous months (full year)
        target_date, periods = get_month_with_comparisons(2024, 12, 11)
        pl_response = get_profit_and_loss_periods(client, target_date, periods)
    """
    target_date = date(target_year, target_month, 15)  # Mid-month date
    return target_date, comparison_periods


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


def get_profit_and_loss_periods(
    client: MCPClient,
    target_month_date: date,
    periods: int = 11,
    timeframe: str = "MONTH",
    standard_layout: bool = True
) -> XeroToolResponse:
    """
    Get Profit & Loss report for a target month with multiple comparison periods.
    
    IMPORTANT: Xero P&L periods logic requires:
    - fromDate: First day of target month (e.g., 2024-12-01)
    - toDate: Last day of target month (e.g., 2024-12-31)  
    - periods: 11 (gets target month + 11 previous months = 12 total)
    - timeframe: "MONTH"
    
    This gives you the target month plus 11 months of comparison data.
    
    Args:
        client: MCP client instance
        target_month_date: Any date in the target month (e.g., 2024-12-15 for December 2024)
        periods: Number of comparison periods (11 gets you 12 months total)
        timeframe: Time frame for periods ("MONTH", "QUARTER", "YEAR")
        standard_layout: Whether to use standard layout
        
    Returns:
        XeroToolResponse: P&L report data with multiple periods
    """
    start_time = datetime.now()
    
    try:
        # Calculate first and last day of the target month
        first_day = date(target_month_date.year, target_month_date.month, 1)
        
        # Calculate last day of the month
        if target_month_date.month == 12:
            last_day = date(target_month_date.year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = date(target_month_date.year, target_month_date.month + 1, 1) - timedelta(days=1)
        
        logger.debug(f"📊 Fetching P&L report for {target_month_date.strftime('%B %Y')} with {periods} comparison periods")
        logger.debug(f"📅 Date range: {first_day} to {last_day} (full target month)")
        
        arguments = {
            "fromDate": first_day.isoformat(),
            "toDate": last_day.isoformat(),
            "periods": periods,
            "timeframe": timeframe,
            "standardLayout": standard_layout
        }
        
        logger.debug(f"P&L API arguments: {arguments}")
        
        response = client.call_tool("list-profit-and-loss", arguments)
        execution_time = (datetime.now() - start_time).total_seconds()
        
        logger.debug(f"✅ P&L periods call completed in {execution_time:.2f}s")
        logger.info(f"📊 Retrieved P&L data for {target_month_date.strftime('%B %Y')} + {periods} comparison months")
        
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


def get_balance_sheet_periods(
    client: MCPClient,
    date_range: DateRange,
    periods: int = 11,
    timeframe: str = "MONTH",
    standard_layout: bool = True
) -> XeroToolResponse:
    """
    Get Balance Sheet report with multiple periods (month-ends).
    This is the efficient version that gets up to 11 month-ends in a single API call.
    
    Args:
        client: MCP client instance
        date_range: Date range for the report (typically a full year)
        periods: Number of periods to retrieve (max 11, typically month-ends)
        timeframe: Time frame for periods ("MONTH", "QUARTER", "YEAR")
        standard_layout: Whether to use standard layout
        
    Returns:
        XeroToolResponse: Balance sheet data with multiple periods
    """
    start_time = datetime.now()
    
    try:
        logger.debug(f"📈 Fetching Balance Sheet with {periods} {timeframe.lower()}s for {date_range}")
        
        # For Balance Sheet periods, we use the end date and work backwards
        # The API will give us month-end snapshots
        arguments = {
            "date": date_range.end_date.isoformat(),
            "periods": periods,
            "timeframe": timeframe,
            "standardLayout": standard_layout
        }
        
        logger.debug(f"Balance Sheet API arguments: {arguments}")
        
        response = client.call_tool("list-report-balance-sheet", arguments)
        execution_time = (datetime.now() - start_time).total_seconds()
        
        logger.debug(f"✅ Balance Sheet periods call completed in {execution_time:.2f}s")
        
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

def get_efficient_yearly_pl_data(client: MCPClient, years: List[int]) -> List[XeroToolResponse]:
    """
    Get P&L data for multiple years using efficient API calls.
    Each year is fetched with 12 periods in a single API call using the correct Xero logic.
    
    Args:
        client: MCP client instance
        years: List of years to fetch (e.g., [2020, 2021, 2022, 2023, 2024])
        
    Returns:
        List[XeroToolResponse]: P&L responses for each year
    """
    results = []
    
    for year in years:
        logger.info(f"📊 Fetching P&L data for {year} (12 periods using December target)")
        
        # Use December of the target year as the target month
        # This will get December + 11 previous months = full year
        december_date = date(year, 12, 15)  # Any date in December works
        
        # Fetch 11 comparison periods (months) in one API call (Xero API limit)
        # This gets December + Nov, Oct, Sep... back to January = 12 months total
        pl_response = get_profit_and_loss_periods(client, december_date, periods=11)
        results.append(pl_response)
        
        # If we hit an auth error, stop trying
        if pl_response.is_auth_error:
            logger.warning("Authentication error detected, stopping yearly fetch")
            break
            
        if pl_response.success:
            logger.info(f"✅ {year}: Successfully fetched 12 months of P&L data (Dec + 11 previous)")
        else:
            logger.warning(f"⚠️ {year}: Failed to fetch P&L data - {pl_response.error_message}")
    
    return results


def get_efficient_yearly_bs_data(client: MCPClient, years: List[int]) -> List[XeroToolResponse]:
    """
    Get Balance Sheet data for multiple years using efficient API calls.
    Each year is fetched with 12 periods (month-ends) in a single API call.
    
    Args:
        client: MCP client instance
        years: List of years to fetch (e.g., [2020, 2021, 2022, 2023, 2024])
        
    Returns:
        List[XeroToolResponse]: Balance Sheet responses for each year
    """
    results = []
    
    for year in years:
        logger.info(f"📈 Fetching Balance Sheet data for {year} (12 month-ends)")
        
        # Create date range for the full year
        year_start = date(year, 1, 1)
        year_end = date(year, 12, 31)
        date_range = DateRange(year_start, year_end)
        
        # Fetch 11 periods (month-ends) in one API call (Xero API limit)
        bs_response = get_balance_sheet_periods(client, date_range, periods=11)
        results.append(bs_response)
        
        # If we hit an auth error, stop trying
        if bs_response.is_auth_error:
            logger.warning("Authentication error detected, stopping yearly fetch")
            break
            
        if bs_response.success:
            logger.info(f"✅ {year}: Successfully fetched 11 month-ends of Balance Sheet data")
        else:
            logger.warning(f"⚠️ {year}: Failed to fetch Balance Sheet data - {bs_response.error_message}")
    
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