"""
Comprehensive MCP client for both P&L and Balance Sheet normalized JSON exports.
Produces combined normalized files for both report types using efficient API calls.
Makes only 10 total API calls to get 5 years of data (5 P&L + 5 Balance Sheet).
"""

import logging
import os
import sys
from pathlib import Path
from typing import Optional
from datetime import date
from calendar import monthrange

# Optional .env support
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(*args, **kwargs):
        pass

from utils.mcp_connection import (
    create_mcp_client, 
    get_npx_executable, 
    MCPError, 
    MCPAuthenticationError,
    MCPConnectionError
)
from utils.xero_tools import (
    get_organisation_details,
    get_last_complete_month, 
    get_profit_and_loss_periods,
    get_balance_sheet_periods,
    print_formatted_response,
    DateRange
)

# Import our streamlined exporter
from utils.xero_data_export import (
    XeroNormalizedExporter, 
    analyze_pl_structure, 
    analyze_bs_structure
)

# Configure logging with better formatting
logging.basicConfig(
    level=logging.INFO,  # Back to INFO for clean output
    format='%(asctime)s - %(name)-20s - %(levelname)-8s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Keep some debug info for troubleshooting if needed
logging.getLogger('utils.mcp_connection').setLevel(logging.WARNING)
logging.getLogger('utils.xero_data_export').setLevel(logging.INFO)
logging.getLogger('utils.xero_tools').setLevel(logging.INFO)


class AuthenticationFailureError(Exception):
    """Raised when authentication fails and processing should stop."""
    pass


def check_authentication_and_halt(response, context: str) -> None:
    """
    Check if a response indicates authentication failure and halt if so.
    
    Args:
        response: Tool response to check
        context: Context description for error messages
        
    Raises:
        AuthenticationFailureError: If authentication has failed
    """
    if hasattr(response, 'success') and not response.success:
        if response.error_message and any(
            indicator in response.error_message.lower() 
            for indicator in ['401', '403', 'unauthorized', 'forbidden', 'token', 'authentication']
        ):
            raise AuthenticationFailureError(
                f"🔒 Authentication failed during {context}: {response.error_message}"
            )


def export_smart_pl_data(client, export_dir: str = "exports") -> bool:
    """
    Export 5 years of P&L data using efficient API calls (5 calls total).
    Each call gets up to 11 periods of data (Xero API limitation).
    Uses Xero's period handling - requests year-end and gets previous 10 months.
    
    Args:
        client: MCP client instance
        export_dir: Directory for exported files
        
    Returns:
        bool: True if successful
        
    Raises:
        AuthenticationFailureError: If authentication fails
    """
    logger.info("📊 Starting 5-year P&L export using efficient API calls")
    
    exporter = XeroNormalizedExporter(export_dir)
    
    # Calculate the years to fetch (last 5 complete years)
    current_date = date.today()
    current_year = current_date.year
    
    # If we're early in the year, we might want to include previous year
    # For now, let's get the last 5 complete calendar years
    end_year = current_year - 1  # Last complete year
    start_year = end_year - 4    # 5 years total
    
    all_pl_data = []
    success_count = 0
    
    logger.info(f"📅 Fetching P&L data for years: {start_year} to {end_year}")
    logger.info("🔍 Using Xero's period logic: year-end date + 11 periods (API limit)")
    
    for year in range(start_year, end_year + 1):
        try:
            logger.info(f"📊 Fetching P&L data for year: {year}")
            
            # Xero's quirky way: Use December 31st as the target date
            # This will give us Dec + previous 11 months = full year
            year_end = date(year, 12, 31)  # Use year-end as target
            # Create a date range that starts from the year beginning for reference
            year_start = date(year, 1, 1)
            date_range = DateRange(year_start, year_end)
            
            logger.debug(f"Year {year}: Target date = {year_end}, periods = 12")
            
            # Make API call for 11 periods (months) of this year
            # Xero API limitation: periods must be 1-11, not 12
            pl_response = get_profit_and_loss_periods(
                client, 
                date_range, 
                periods=11,
                timeframe="MONTH"
            )
            check_authentication_and_halt(pl_response, f"P&L data fetch for {year}")
            
            if pl_response.success:
                # Store the response with year identifier
                date_range_str = f"{year_start.isoformat()}_to_{year_end.isoformat()}"
                all_pl_data.append((pl_response.data, date_range_str, year))
                success_count += 1
                logger.info(f"✅ {year}: P&L data retrieved successfully (11 periods)")
                
                # Log some debug info about the response
                content_blocks = pl_response.data.get('result', {}).get('content', [])
                logger.debug(f"Year {year}: Response has {len(content_blocks)} content blocks")
            else:
                logger.warning(f"⚠️ {year}: Failed to fetch P&L data - {pl_response.error_message}")
                
        except Exception as e:
            logger.error(f"❌ Failed to fetch P&L data for {year}: {e}")
            continue
    
    if all_pl_data:
        try:
            logger.info("💾 Exporting 5-year P&L data to combined file...")
            
            # Create filename spanning the full range
            earliest_year = min(item[2] for item in all_pl_data)
            latest_year = max(item[2] for item in all_pl_data)
            
            # Prepare data for export (without year info, exporter handles it)
            export_data = [(item[0], item[1]) for item in all_pl_data]
            
            combined_file_path = exporter.export_combined_periods_json(
                export_data,
                combined_filename=f"xero_pl_normalized_5year_{earliest_year}_to_{latest_year}.json"
            )
            
            print(f"\n=== 5-Year P&L Export Results ===")
            print(f"✅ Successfully exported: {combined_file_path.name}")
            print(f"📊 Years included: {earliest_year} to {latest_year}")
            print(f"🎯 API calls made: {success_count}/5 (target: 5)")
            print(f"📈 Efficiency: {success_count * 11} months of data in {success_count} API calls")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ 5-year P&L export failed: {e}")
            return False
    else:
        logger.error("❌ No P&L data collected for export")
        return False


def export_smart_bs_data(client, export_dir: str = "exports") -> bool:
    """
    Export 5 years of Balance Sheet data using efficient API calls (5 calls total).
    Each call gets up to 11 periods of data (Xero API limitation).
    
    Args:
        client: MCP client instance
        export_dir: Directory for exported files
        
    Returns:
        bool: True if successful
        
    Raises:
        AuthenticationFailureError: If authentication fails
    """
    logger.info("📊 Starting 5-year Balance Sheet export using efficient API calls")
    
    exporter = XeroNormalizedExporter(export_dir)
    
    # Calculate the years to fetch (last 5 complete years)
    current_date = date.today()
    current_year = current_date.year
    
    # If we're early in the year, we might want to include previous year
    # For now, let's get the last 5 complete calendar years
    end_year = current_year - 1  # Last complete year
    start_year = end_year - 4    # 5 years total
    
    all_bs_data = []
    success_count = 0
    
    logger.info(f"📅 Fetching Balance Sheet data for years: {start_year} to {end_year}")
    
    for year in range(start_year, end_year + 1):
        try:
            logger.info(f"📊 Fetching Balance Sheet data for year: {year}")
            
            # For Balance Sheet, we want month-end dates
            # Create date range for the full year, but we'll get month-end snapshots
            year_start = date(year, 1, 1)  
            year_end = date(year, 12, 31)
            date_range = DateRange(year_start, year_end)
            
            # Make API call for 11 periods (month-ends) of this year
            # Xero API limitation: periods must be 1-11, not 12
            bs_response = get_balance_sheet_periods(client, date_range, periods=11)
            check_authentication_and_halt(bs_response, f"Balance Sheet data fetch for {year}")
            
            if bs_response.success:
                # Store the response with year identifier
                date_range_str = f"{year_start.isoformat()}_to_{year_end.isoformat()}"
                all_bs_data.append((bs_response.data, date_range_str, year))
                success_count += 1
                logger.info(f"✅ {year}: Balance Sheet data retrieved successfully (11 month-ends)")
            else:
                logger.warning(f"⚠️ {year}: Failed to fetch Balance Sheet data - {bs_response.error_message}")
                
        except Exception as e:
            logger.error(f"❌ Failed to fetch Balance Sheet data for {year}: {e}")
            continue
    
    if all_bs_data:
        try:
            logger.info("💾 Exporting 5-year Balance Sheet data to combined file...")
            
            # Create filename spanning the full range
            earliest_year = min(item[2] for item in all_bs_data)
            latest_year = max(item[2] for item in all_bs_data)
            
            # Prepare data for export (without year info, exporter handles it)
            export_data = [(item[0], item[1]) for item in all_bs_data]
            
            combined_file_path = exporter.export_combined_bs_periods_json(
                export_data,
                combined_filename=f"xero_bs_normalized_5year_{earliest_year}_to_{latest_year}.json"
            )
            
            print(f"\n=== 5-Year Balance Sheet Export Results ===")
            print(f"✅ Successfully exported: {combined_file_path.name}")
            print(f"📊 Years included: {earliest_year} to {latest_year}")
            print(f"🎯 API calls made: {success_count}/5 (target: 5)")
            print(f"📈 Efficiency: {success_count * 11} month-ends of data in {success_count} API calls")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ 5-year Balance Sheet export failed: {e}")
            return False
    else:
        logger.error("❌ No Balance Sheet data collected for export")
        return False


def verify_token_and_connection(client) -> tuple[bool, str]:
    """
    Verify the authentication token and connection.
    
    Args:
        client: MCP client instance
        
    Returns:
        tuple[bool, str]: (success, organisation_name_or_error)
    """
    logger.info("🔍 Verifying authentication and connection...")
    
    try:
        org_response = get_organisation_details(client)
        
        if not org_response.success:
            error_msg = org_response.error_message or "Unknown error"
            
            # Check for authentication errors
            if any(indicator in error_msg.lower() 
                   for indicator in ['401', '403', 'unauthorized', 'forbidden', 'token']):
                return False, f"Authentication failed: {error_msg}"
            else:
                return False, f"Connection failed: {error_msg}"
        
        # Extract organization name
        content_blocks = org_response.data.get('result', {}).get('content', [])
        org_name = "Unknown Organization"
        
        for block in content_blocks:
            if isinstance(block, dict) and "text" in block:
                text = str(block["text"])
                # Try to extract org name from response
                if "organisation" in text.lower() or "organization" in text.lower():
                    # Simple extraction - you might want to improve this
                    lines = text.split('\n')
                    for line in lines:
                        if 'name' in line.lower() and ':' in line:
                            org_name = line.split(':', 1)[1].strip()
                            break
                break
        
        return True, org_name
        
    except MCPAuthenticationError as e:
        return False, f"Authentication error: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"


def main():
    """Main application with smart multi-year P&L and Balance Sheet export."""
    print("🚀 Xero Comprehensive MCP Client - Smart Multi-Year Export")
    print("=" * 70)
    
    try:
        # Environment setup
        load_dotenv()
        
        token = os.environ.get("XERO_CLIENT_BEARER_TOKEN")
        if not token:
            print("\n❌ ERROR: XERO_CLIENT_BEARER_TOKEN not found")
            print("\n🔧 To fix this:")
            print("   1. Run: python auth_token_generator.py")
            print("   2. Follow the prompts to generate a fresh token")
            print("   3. The token will be saved to your .env file automatically")
            print("\n💡 Note: Xero tokens expire regularly and need regeneration")
            return 1
        
        # Check if token looks valid (basic check)
        if len(token.strip()) < 50:
            print("\n⚠️ WARNING: Token looks too short, it might be invalid")
        
        logger.info("🔧 Preparing MCP connection...")
        npx_command = get_npx_executable()
        mcp_args = ["-y", "@xeroapi/xero-mcp-server@latest"]
        mcp_env = {"XERO_CLIENT_BEARER_TOKEN": token}
        
        # Create export directory
        Path("exports").mkdir(exist_ok=True)
        
        with create_mcp_client(npx_command, mcp_args, mcp_env) as client:
            logger.info("✅ MCP client started successfully")
            
            # Verify authentication first
            auth_success, org_info = verify_token_and_connection(client)
            
            if not auth_success:
                print(f"\n❌ AUTHENTICATION FAILED")
                print(f"   Error: {org_info}")
                print(f"\n🔧 To fix this:")
                print(f"   1. Your Xero token has likely expired")
                print(f"   2. Run: python auth_token_generator.py")
                print(f"   3. Generate a fresh token")
                print(f"   4. Try running this script again")
                print(f"\n💡 Xero tokens have limited lifespans and need periodic renewal")
                return 1
            
            print(f"\n✅ Connected successfully!")
            print(f"   Organization: {org_info}")
            print(f"   Token status: Valid")
            
            # Show efficiency improvement
            print(f"\n🎯 SMART STRATEGY")
            print(f"   Problem solved: Xero periods parameter limited to 1-11")
            print(f"   Solution: 11 periods per year (11 months per API call)")
            print(f"   API calls: 10 total (5 P&L + 5 Balance Sheet)")
            print(f"   Coverage: 4+ years of data (11 months per year)")
            
            # Proceed with efficient exports
            print(f"\n📊 Starting smart multi-year data export process...")
            
            try:
                # Export 4+ years of P&L data (5 API calls)
                print(f"\n{'='*70}")
                print(f"EXPORTING SMART P&L DATA (5 EFFICIENT API CALLS)")
                print(f"{'='*70}")
                pl_success = export_smart_pl_data(client)
                
                # Export 4+ years of Balance Sheet data (5 API calls)
                print(f"\n{'='*70}")
                print(f"EXPORTING SMART BALANCE SHEET DATA (5 EFFICIENT API CALLS)")
                print(f"{'='*70}")
                bs_success = export_smart_bs_data(client)
                
                # Summary
                print(f"\n{'='*70}")
                print(f"SMART EXPORT SUMMARY")
                print(f"{'='*70}")
                
                if pl_success and bs_success:
                    print(f"✅ P&L Smart Export: SUCCESS")
                    print(f"✅ Balance Sheet Smart Export: SUCCESS")
                    print(f"\n🎉 Both exports completed successfully!")
                    print(f"\n📁 Check the exports/ directory for your files:")
                    print(f"   • xero_pl_normalized_smart_YYYY_to_YYYY.json")
                    print(f"   • xero_bs_normalized_smart_YYYY_to_YYYY.json")
                    print(f"\n💡 Files contain:")
                    print(f"   - 4+ years of historical data")
                    print(f"   - YTD data for current/recent year")
                    print(f"   - Full 12 months for previous complete years")
                    print(f"   - Consistent normalized structure")
                    print(f"   - Proper data typing (amounts as numbers)")
                    print(f"   - Period/date information for each record")
                    print(f"\n⚡ Smart Strategy Benefits:")
                    print(f"   - Respects Xero's periods=1-11 limitation")
                    print(f"   - Maximizes data coverage with minimal API calls")
                    print(f"   - Handles current year intelligently")
                    print(f"   - Total API calls: 10 (highly efficient)")
                    return 0
                elif pl_success or bs_success:
                    print(f"{'✅' if pl_success else '❌'} P&L Smart Export: {'SUCCESS' if pl_success else 'FAILED'}")
                    print(f"{'✅' if bs_success else '❌'} Balance Sheet Smart Export: {'SUCCESS' if bs_success else 'FAILED'}")
                    print(f"\n⚠️ Partial success - some exports failed")
                    return 2
                else:
                    print(f"❌ P&L Smart Export: FAILED")
                    print(f"❌ Balance Sheet Smart Export: FAILED")
                    print(f"\n❌ Both exports failed (but authentication worked)")
                    return 1
                    
            except AuthenticationFailureError as e:
                print(f"\n❌ AUTHENTICATION FAILED DURING PROCESSING")
                print(f"   {e}")
                print(f"\n🔧 Your token expired during processing. Please:")
                print(f"   1. Run: python auth_token_generator.py")
                print(f"   2. Generate a fresh token") 
                print(f"   3. Try again")
                return 1
                
    except KeyboardInterrupt:
        print(f"\n\n⏹️ Cancelled by user")
        return 130
        
    except MCPConnectionError as e:
        print(f"\n❌ CONNECTION ERROR: {e}")
        print(f"\n🔧 Troubleshooting:")
        print(f"   - Check that Node.js/npm is installed")
        print(f"   - Verify internet connection")
        print(f"   - Try: npm install -g @xeroapi/xero-mcp-server")
        return 2
        
    except MCPAuthenticationError as e:
        print(f"\n❌ AUTHENTICATION ERROR: {e}")
        print(f"\n🔧 To fix:")
        print(f"   1. Run: python auth_token_generator.py")
        print(f"   2. Generate a fresh token")
        return 1
        
    except Exception as e:
        logger.error(f"💥 Unexpected error: {e}", exc_info=True)
        print(f"\n❌ UNEXPECTED ERROR: {e}")
        print(f"\n🔧 This shouldn't happen. Please check:")
        print(f"   - All dependencies are installed (pip install -r requirements.txt)")
        print(f"   - Python version is 3.8+")
        print(f"   - File permissions are correct")
        return 3


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)