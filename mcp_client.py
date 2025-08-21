"""
MCP client focused on normalized JSON export only.
Produces a single combined xero_pl_normalized file.
"""

import logging
import os
import sys
from pathlib import Path
from typing import Optional

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
    get_profit_and_loss,
    print_formatted_response,
    DateRange
)

# Import our streamlined exporter
from utils.xero_data_export import XeroNormalizedExporter, analyze_pl_structure

# Configure logging with better formatting
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)-20s - %(levelname)-8s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Suppress debug logs from utils unless needed
logging.getLogger('utils.mcp_connection').setLevel(logging.WARNING)


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


def export_current_month_pl(client, export_dir: str = "exports") -> bool:
    """
    Export current month's P&L data in normalized JSON format.
    
    Args:
        client: MCP client instance
        export_dir: Directory for exported files
        
    Returns:
        bool: True if successful
        
    Raises:
        AuthenticationFailureError: If authentication fails
    """
    logger.info("📊 Starting current month P&L export (normalized JSON)")
    
    # Create exporter
    exporter = XeroNormalizedExporter(export_dir)
    
    # Get last complete month's P&L data
    last_month = get_last_complete_month()
    logger.info(f"📅 Target period: {last_month}")
    
    pl_response = get_profit_and_loss(client, last_month)
    check_authentication_and_halt(pl_response, "P&L data fetch")
    
    if not pl_response.success:
        logger.error(f"❌ Failed to get P&L data: {pl_response.error_message}")
        return False
    
    # Display the P&L for verification
    print_formatted_response(
        pl_response.data,
        f"P&L Data - {last_month}"
    )
    
    # Analyze the structure
    logger.info("🔍 Analyzing P&L data structure...")
    analysis = analyze_pl_structure(pl_response.data)
    
    print(f"\n=== P&L Data Analysis ===")
    print(f"Total line items: {analysis['total_line_items']}")
    print(f"Sections found: {len(analysis['sections'])}")
    print(f"Section breakdown:")
    for section, count in analysis['section_breakdown'].items():
        print(f"  - {section}: {count} items")
    
    # Export to normalized JSON format
    date_range_str = f"{last_month.start_date}_to_{last_month.end_date}"
    
    try:
        logger.info("💾 Exporting P&L data to normalized JSON...")
        
        json_path = exporter.export_pl_normalized_json(
            pl_response.data, 
            date_range=date_range_str
        )
        
        print(f"\n=== Export Results ===")
        print(f"✅ Normalized JSON: {json_path}")
        
        # Validate the exported file matches your structure
        logger.info("✅ Validating exported file structure...")
        try:
            import json
            with open(json_path, 'r', encoding='utf-8') as f:
                exported_data = json.load(f)
            
            metadata = exported_data.get('export_metadata', {})
            data = exported_data.get('data', [])
            
            print(f"\n=== Export Validation ===")
            print(f"Format: {metadata.get('format')}")
            print(f"Record count: {metadata.get('record_count')}")
            print(f"Date range: {metadata.get('date_range')}")
            print(f"Actual data records: {len(data)}")
            
            # Show sample of the data structure
            if data:
                print(f"\n=== Sample Record Structure ===")
                sample = data[0]
                for key, value in sample.items():
                    print(f"  {key}: {repr(value)} ({type(value).__name__})")
        
        except Exception as e:
            logger.warning(f"⚠️ Could not validate exported file: {e}")
            
        return True
        
    except Exception as e:
        logger.error(f"❌ Export failed: {e}")
        return False


def export_combined_multi_month_pl(client, months_back: int = 3, export_dir: str = "exports") -> bool:
    """
    Export multiple months of P&L data in a single combined normalized JSON file.
    
    Args:
        client: MCP client instance
        months_back: Number of previous months to export
        export_dir: Directory for exported files
        
    Returns:
        bool: True if successful
        
    Raises:
        AuthenticationFailureError: If authentication fails
    """
    logger.info(f"📊 Starting {months_back} months combined P&L export (single normalized JSON)")
    
    exporter = XeroNormalizedExporter(export_dir)
    
    # Get date ranges for the last N months
    from datetime import date
    from calendar import monthrange
    
    current_date = date.today()
    pl_data_to_export = []
    
    for i in range(months_back):
        # Calculate month to fetch
        target_month = current_date.month - i - 1
        target_year = current_date.year
        
        if target_month <= 0:
            target_month += 12
            target_year -= 1
        
        # Get the month's date range
        first_day = date(target_year, target_month, 1)
        last_day = date(target_year, target_month, monthrange(target_year, target_month)[1])
        
        date_range = DateRange(first_day, last_day)
        date_range_str = f"{first_day}_to_{last_day}"
        
        logger.info(f"📅 Fetching data for: {date_range}")
        
        pl_response = get_profit_and_loss(client, date_range)
        check_authentication_and_halt(pl_response, f"P&L data fetch for {date_range}")
        
        if pl_response.success:
            pl_data_to_export.append((pl_response.data, date_range_str))
            logger.info(f"✅ {date_range}: Data retrieved successfully")
        else:
            logger.warning(f"⚠️ {date_range}: Failed to fetch data - {pl_response.error_message}")
    
    if pl_data_to_export:
        try:
            # Export all collected data to a single combined file
            logger.info("💾 Exporting collected data to single combined file...")
            combined_file_path = exporter.export_combined_periods_json(pl_data_to_export)
            
            print(f"\n=== Combined Multi-Month Export Results ===")
            print(f"✅ Successfully exported combined file: {combined_file_path.name}")
            
            # Validate the combined file
            try:
                import json
                with open(combined_file_path, 'r', encoding='utf-8') as f:
                    combined_data = json.load(f)
                
                metadata = combined_data.get('export_metadata', {})
                data = combined_data.get('data', [])
                
                print(f"\n=== Combined File Validation ===")
                print(f"Format: {metadata.get('format')}")
                print(f"Total records: {metadata.get('total_record_count')}")
                print(f"Periods included: {metadata.get('periods_included')}")
                print(f"Date range: {metadata.get('date_range_combined')}")
                print(f"Actual data records: {len(data)}")
                
                # Show period breakdown
                if 'period_summaries' in metadata:
                    print(f"\n=== Period Breakdown ===")
                    for summary in metadata['period_summaries']:
                        print(f"  - {summary['period']}: {summary['record_count']} records")
                
                # Show sample record with period info
                if data:
                    print(f"\n=== Sample Record with Period ===")
                    sample = data[0]
                    for key, value in sample.items():
                        print(f"  {key}: {repr(value)} ({type(value).__name__})")
                
            except Exception as e:
                logger.warning(f"⚠️ Could not validate combined file: {e}")
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Combined export failed: {e}")
            return False
    else:
        logger.error("❌ No P&L data collected for export")
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
    """Main application with enhanced error handling and cleaner logging."""
    print("🚀 Xero MCP Client - Combined Normalized JSON Export")
    print("=" * 60)
    
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
            
            # Proceed with exports only after successful authentication
            print(f"\n📊 Starting data export process...")
            
            try:
                # Export combined multi-month data (this replaces the separate file approach)
                combined_success = export_combined_multi_month_pl(client, months_back=12)
                
                # Summary
                print(f"\n{'='*60}")
                print(f"EXPORT SUMMARY")
                print(f"{'='*60}")
                
                if combined_success:
                    print(f"✅ Combined Multi-Month Export: SUCCESS")
                    print(f"\n🎉 Export completed successfully!")
                    print(f"\n📁 Check the exports/ directory for your file:")
                    print(f"   Format: xero_pl_normalized_combined_YYYY-MM-DD_to_YYYY-MM-DD.json")
                    print(f"\n💡 Single file contains all periods with:")
                    print(f"   - Consistent structure matching your requirements")
                    print(f"   - Proper data typing (amounts as numbers)")
                    print(f"   - Period information for each record")
                    print(f"   - Comprehensive metadata for validation")
                    return 0
                else:
                    print(f"❌ Combined Multi-Month Export: FAILED")
                    print(f"\n⚠️ Export failed (but authentication worked)")
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