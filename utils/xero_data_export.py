"""
Streamlined Xero data export utilities focused on normalized JSON output.
Produces normalized JSON files for P&L and Balance Sheet reports.
Enhanced to handle multi-period API responses efficiently.
"""

import json
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from calendar import monthrange

logger = logging.getLogger(__name__)


class XeroNormalizedExporter:
    """Streamlined Xero data exporter for normalized JSON output only."""
    
    def __init__(self, output_dir: Union[str, Path] = "exports"):
        """
        Initialize the exporter.
        
        Args:
            output_dir: Directory to save exported files
        """
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)

    def _map_period_to_date_info(self, period_identifier: str, response_period: str) -> Dict[str, Any]:
        """
        Map Xero's counter-intuitive period numbering to meaningful date information.
        
        Xero's period system works backwards from the target date:
        - period_1 = Most recent month (e.g., December if target is Dec 31)
        - period_2 = Previous month (November)
        - period_12 = 11 months back (January)
        
        Args:
            period_identifier: Xero period (e.g., "period_1", "period_12")
            response_period: Date range string (e.g., "2024-01-01_to_2024-12-31")
            
        Returns:
            Dict with month_name, month_number, year, date_as_of
        """
        try:
            # Extract period number
            period_num = int(period_identifier.replace("period_", ""))
            
            # Extract end date from response period
            if "_to_" in response_period:
                end_date_str = response_period.split("_to_")[1]
            else:
                end_date_str = response_period
                
            end_date = datetime.fromisoformat(end_date_str).date()
            
            # Calculate the actual month this period represents
            # period_1 = end_date month, period_2 = end_date - 1 month, etc.
            months_back = period_num - 1
            
            # Calculate target year and month
            target_year = end_date.year
            target_month = end_date.month - months_back
            
            # Handle year rollover
            while target_month <= 0:
                target_month += 12
                target_year -= 1
                
            # Get the last day of the target month for "as of" date
            last_day = monthrange(target_year, target_month)[1]
            date_as_of = date(target_year, target_month, last_day)
            
            # Month names
            month_names = [
                "", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"
            ]
            
            return {
                "month_name": month_names[target_month],
                "month_number": target_month,
                "year": target_year,
                "date_as_of": date_as_of.isoformat()
            }
            
        except (ValueError, IndexError, AttributeError) as e:
            logger.warning(f"Could not parse period info: {period_identifier}, {response_period} - {e}")
            return {
                "month_name": "Unknown",
                "month_number": 0,
                "year": 0,
                "date_as_of": "Unknown"
            }

    def _find_json_content_block(self, content_blocks: List[Dict]) -> Optional[List[Dict]]:
        """
        Find the content block that contains the actual JSON data array.
        
        Args:
            content_blocks: List of content blocks from Xero API response
            
        Returns:
            Optional[List[Dict]]: Parsed JSON data or None if not found
        """
        logger.debug(f"Analyzing {len(content_blocks)} content blocks to find JSON data")
        
        for block_idx, block in enumerate(content_blocks):
            if not isinstance(block, dict) or "text" not in block:
                logger.debug(f"Block {block_idx}: Skipping - not dict or no text key")
                continue
                
            text = block["text"]
            logger.debug(f"Block {block_idx}: Text type = {type(text)}, length = {len(str(text))}")
            
            # Skip obvious metadata blocks (short text without JSON structure)
            text_str = str(text).strip()
            if len(text_str) < 50:  # Too short to be data
                logger.debug(f"Block {block_idx}: Skipping - too short for data")
                continue
            
            # Skip blocks that don't start with '[' (not JSON arrays)
            if not text_str.startswith('['):
                logger.debug(f"Block {block_idx}: Skipping - doesn't start with '['")
                continue
            
            # Try to parse this block as JSON
            try:
                json_data = json.loads(text_str)
                if isinstance(json_data, list) and json_data:
                    logger.info(f"Block {block_idx}: ✅ Found JSON data with {len(json_data)} items")
                    return json_data
                else:
                    logger.debug(f"Block {block_idx}: JSON parsed but not a non-empty list")
                    
            except json.JSONDecodeError as e:
                logger.debug(f"Block {block_idx}: JSON parse failed - {e}")
                continue
        
        logger.warning("No valid JSON data block found in any content block")
        return None

    def extract_and_normalize_pl_data(self, pl_response: Dict[str, Any], response_period: str = "") -> List[Dict[str, Any]]:
        """
        Extract and normalize P&L data from Xero response to match your desired format.
        Enhanced to handle multi-period responses from efficient API calls.
        Fixed to properly find the JSON content block.
        
        Args:
            pl_response: Raw P&L response from Xero MCP (may contain multiple periods)
            response_period: Date range string for this response (e.g., "2024-01-01_to_2024-12-31")
            
        Returns:
            List of normalized financial line items matching your JSON structure
        """
        normalized_data = []
        
        # Extract content from response
        content_blocks = pl_response.get("result", {}).get("content", [])
        logger.debug(f"Processing {len(content_blocks)} content blocks for P&L data")
        
        # Find the block with actual JSON data
        json_data = self._find_json_content_block(content_blocks)
        
        if json_data:
            logger.debug(f"Found JSON data with {len(json_data)} sections")
            block_normalized = self._normalize_pl_sections(json_data, response_period)
            normalized_data.extend(block_normalized)
            logger.info(f"P&L extraction successful: {len(block_normalized)} items extracted")
        else:
            logger.warning("No JSON data found in P&L response")
        
        logger.info(f"P&L extraction complete: {len(normalized_data)} total items extracted")
        return normalized_data

    def extract_and_normalize_bs_data(self, bs_response: Dict[str, Any], response_period: str = "") -> List[Dict[str, Any]]:
        """
        Extract and normalize Balance Sheet data from Xero response.
        Enhanced to handle multi-period responses from efficient API calls.
        Fixed to properly find the JSON content block.
        
        Args:
            bs_response: Raw Balance Sheet response from Xero MCP (may contain multiple periods)
            response_period: Date range string for this response (e.g., "2024-01-01_to_2024-12-31")
            
        Returns:
            List of normalized balance sheet line items
        """
        normalized_data = []
        
        # Extract content from response
        content_blocks = bs_response.get("result", {}).get("content", [])
        logger.debug(f"Processing {len(content_blocks)} content blocks for Balance Sheet data")
        
        # Find the block with actual JSON data
        json_data = self._find_json_content_block(content_blocks)
        
        if json_data:
            logger.debug(f"Found JSON data with {len(json_data)} sections")
            block_normalized = self._normalize_bs_sections(json_data, response_period)
            normalized_data.extend(block_normalized)
            logger.info(f"Balance Sheet extraction successful: {len(block_normalized)} items extracted")
        else:
            logger.warning("No JSON data found in Balance Sheet response")
        
        logger.info(f"Balance Sheet extraction complete: {len(normalized_data)} total items extracted")
        return normalized_data

    def _normalize_pl_sections(self, sections: List[Dict], response_period: str = "") -> List[Dict[str, Any]]:
        """
        Normalize P&L sections into the exact format matching your JSON structure.
        Enhanced to handle multi-period data with proper period identification.
        
        Args:
            sections: List of P&L sections from Xero (may include multiple periods)
            response_period: Date range string for this response (e.g., "2024-01-01_to_2024-12-31")
            
        Returns:
            List of normalized line items in your required format
        """
        normalized_items = []
        
        for section in sections:
            section_title = section.get("title", "").strip()
            section_type = section.get("rowType", "")
            
            logger.debug(f"Processing section: '{section_title}' (type: {section_type})")
            
            # Handle different section types
            if section_type == "Section" and "rows" in section:
                for row in section["rows"]:
                    items = self._normalize_row_with_periods(row, section_title, is_pl=True, response_period=response_period)
                    if items:
                        normalized_items.extend(items)
            
            # Handle section headers and summary rows that aren't in "rows"
            elif section_type in ["Header", "SummaryRow", "Row"]:
                items = self._normalize_section_direct_with_periods(section, is_pl=True, response_period=response_period)
                if items:
                    normalized_items.extend(items)
        
        logger.debug(f"Normalized {len(normalized_items)} P&L items from {len(sections)} sections")
        return normalized_items

    def _normalize_bs_sections(self, sections: List[Dict], response_period: str = "") -> List[Dict[str, Any]]:
        """
        Normalize Balance Sheet sections into the required format.
        Enhanced to handle multi-period data with proper period identification.
        
        Args:
            sections: List of Balance Sheet sections from Xero (may include multiple periods)
            response_period: Date range string for this response (e.g., "2024-01-01_to_2024-12-31")
            
        Returns:
            List of normalized line items for Balance Sheet
        """
        normalized_items = []
        
        for section in sections:
            section_title = section.get("title", "").strip()
            section_type = section.get("rowType", "")
            
            logger.debug(f"Processing BS section: '{section_title}' (type: {section_type})")
            
            # Handle different section types
            if section_type == "Section" and "rows" in section:
                for row in section["rows"]:
                    items = self._normalize_row_with_periods(row, section_title, is_pl=False, response_period=response_period)
                    if items:
                        normalized_items.extend(items)
            
            # Handle section headers and summary rows that aren't in "rows"
            elif section_type in ["Header", "SummaryRow", "Row"]:
                items = self._normalize_section_direct_with_periods(section, is_pl=False, response_period=response_period)
                if items:
                    normalized_items.extend(items)
        
        logger.debug(f"Normalized {len(normalized_items)} Balance Sheet items from {len(sections)} sections")
        return normalized_items

    def _normalize_row_with_periods(
        self, 
        row: Dict, 
        section_title: str, 
        is_pl: bool = True,
        response_period: str = ""
    ) -> List[Dict[str, Any]]:
        """
        Normalize a single row that may contain multiple periods of data.
        
        Args:
            row: Row data from Xero report (may contain multiple cells for different periods)
            section_title: Parent section title
            is_pl: Whether this is P&L data (vs Balance Sheet)
            
        Returns:
            List of normalized row data (one item per period)
        """
        row_type = row.get("rowType", "")
        cells = row.get("cells", [])
        
        # Skip non-data rows or rows with insufficient data
        if row_type not in ["Row", "SummaryRow"] or len(cells) < 2:
            logger.debug(f"Skipping row: type={row_type}, cells={len(cells)}")
            return []
            
        # Extract account name from first cell
        account_name = cells[0].get("value", "").strip()
        
        # Skip empty accounts
        if not account_name:
            logger.debug("Skipping row with empty account name")
            return []
            
        logger.debug(f"Processing account: '{account_name}' with {len(cells)-1} value cells")
        
        # Extract account ID if available
        account_id = None
        if len(cells) > 0 and cells[0].get("attributes"):
            for attr in cells[0]["attributes"]:
                if attr.get("id") == "account":
                    account_id = attr.get("value")
                    break
        
        normalized_items = []
        
        # Process each value cell (skipping the first cell which is the account name)
        for i, cell in enumerate(cells[1:], 1):
            amount_str = cell.get("value", "0.00")
            
            # Parse amount - handle negative values and formatting
            try:
                # Remove common formatting characters
                clean_amount = str(amount_str).replace(",", "").replace("$", "").strip()
                amount = float(clean_amount) if clean_amount else 0.0
            except (ValueError, TypeError):
                logger.debug(f"Could not parse amount for {account_name} period {i}: {amount_str}")
                amount = 0.0
            
            # Determine period identifier (this might need adjustment based on actual Xero response format)
            period_identifier = f"period_{i}"
            
            # Check if cell has period-specific metadata
            if cell.get("attributes"):
                for attr in cell["attributes"]:
                    if attr.get("id") in ["period", "date", "month"]:
                        period_identifier = attr.get("value", period_identifier)
                        break
            
            # Get enhanced date information
            date_info = self._map_period_to_date_info(period_identifier, response_period)
            
            normalized_item = {
                "section": section_title,
                "account_name": account_name,
                "account_id": account_id,
                "amount": amount,
                "row_type": row_type,
                "is_summary": row_type == "SummaryRow",
                "period_identifier": period_identifier,
                "month_name": date_info["month_name"],
                "month_number": date_info["month_number"],
                "year": date_info["year"],
                "date_as_of": date_info["date_as_of"]
            }
            
            normalized_items.append(normalized_item)
                    
        logger.debug(f"Created {len(normalized_items)} period items for account '{account_name}'")
        return normalized_items

    def _normalize_section_direct_with_periods(
        self, 
        section: Dict, 
        is_pl: bool = True,
        response_period: str = ""
    ) -> List[Dict[str, Any]]:
        """
        Handle sections that are direct data items (like GROSS PROFIT, NET PROFIT) with multiple periods.
        
        Args:
            section: Section data from Xero
            is_pl: Whether this is P&L data (vs Balance Sheet)
            
        Returns:
            List of normalized section data (one item per period)
        """
        title = section.get("title", "").strip()
        row_type = section.get("rowType", "")
        
        # Skip Header rows - they don't contain financial data
        if row_type == "Header":
            logger.debug(f"Skipping Header row: {title}")
            return []
        
        if not title:
            logger.debug("Skipping section with empty title")
            return []
            
        logger.debug(f"Processing direct section: '{title}' (type: {row_type})")
        
        cells = section.get("cells", [])
        normalized_items = []
        
        # Skip if not enough cells
        if len(cells) < 2:
            logger.debug(f"Not enough cells in section '{title}': {len(cells)}")
            return []
        
        # Process each value cell (first cell is typically the title/name)
        for i, cell in enumerate(cells[1:], 1):
            amount_str = cell.get("value", "0.00")
            
            try:
                clean_amount = str(amount_str).replace(",", "").replace("$", "").strip()
                amount = float(clean_amount) if clean_amount else 0.0
            except (ValueError, TypeError):
                amount = 0.0
            
            # Determine period identifier
            period_identifier = f"period_{i}"
            if cell.get("attributes"):
                for attr in cell["attributes"]:
                    if attr.get("id") in ["period", "date", "month"]:
                        period_identifier = attr.get("value", period_identifier)
                        break
            
            # Determine if this is a summary item
            if is_pl:
                is_summary_item = (row_type == "SummaryRow" or 
                                 title in ["GROSS PROFIT", "NET PROFIT", "OPERATING PROFIT", "Total Income", "Total Operating Expenses"])
            else:
                bs_summary_titles = [
                    "Total Assets", "Total Current Assets", "Total Non-current Assets",
                    "Total Liabilities", "Total Current Liabilities", "Total Non-current Liabilities", 
                    "Total Equity", "Net Assets"
                ]
                is_summary_item = row_type == "SummaryRow" or title in bs_summary_titles
            
            # Get enhanced date information
            date_info = self._map_period_to_date_info(period_identifier, response_period)
            
            normalized_item = {
                "section": "",  # Direct items typically have empty section
                "account_name": title,
                "account_id": None,  # Direct items don't have account IDs
                "amount": amount,
                "row_type": row_type if row_type else "Row",
                "is_summary": is_summary_item,
                "period_identifier": period_identifier,
                "month_name": date_info["month_name"],
                "month_number": date_info["month_number"],
                "year": date_info["year"],
                "date_as_of": date_info["date_as_of"]
            }
            
            normalized_items.append(normalized_item)
        
        logger.debug(f"Created {len(normalized_items)} period items for direct section '{title}'")
        return normalized_items

    def _normalize_row(self, row: Dict, section_title: str) -> Optional[Dict[str, Any]]:
        """
        Legacy method for single-period normalization (kept for compatibility).
        
        Args:
            row: Row data from Xero P&L
            section_title: Parent section title
            
        Returns:
            Normalized row data matching your JSON structure
        """
        row_type = row.get("rowType", "")
        cells = row.get("cells", [])
        
        # Skip non-data rows
        if row_type not in ["Row", "SummaryRow"] or len(cells) < 2:
            return None
            
        # Extract account name and value
        account_name = cells[0].get("value", "").strip()
        amount_str = cells[1].get("value", "0.00")
        
        # Skip empty accounts
        if not account_name:
            return None
            
        # Parse amount - handle negative values and formatting
        try:
            amount = float(str(amount_str).replace(",", "").replace("$", ""))
        except (ValueError, TypeError):
            logger.warning(f"Could not parse amount for {account_name}: {amount_str}")
            amount = 0.0
            
        # Extract account ID if available
        account_id = None
        if len(cells) > 0 and cells[0].get("attributes"):
            for attr in cells[0]["attributes"]:
                if attr.get("id") == "account":
                    account_id = attr.get("value")
                    break
                    
        return {
            "section": section_title,
            "account_name": account_name,
            "account_id": account_id,
            "amount": amount,
            "row_type": row_type,
            "is_summary": row_type == "SummaryRow"
        }

    def _normalize_bs_row(self, row: Dict, section_title: str) -> Optional[Dict[str, Any]]:
        """
        Legacy method for single-period Balance Sheet normalization (kept for compatibility).
        
        Args:
            row: Row data from Xero Balance Sheet
            section_title: Parent section title
            
        Returns:
            Normalized row data for Balance Sheet
        """
        row_type = row.get("rowType", "")
        cells = row.get("cells", [])
        
        # Skip non-data rows
        if row_type not in ["Row", "SummaryRow"] or len(cells) < 2:
            return None
            
        # Extract account name and value
        account_name = cells[0].get("value", "").strip()
        amount_str = cells[1].get("value", "0.00")
        
        # Skip empty accounts
        if not account_name:
            return None
            
        # Parse amount - handle negative values and formatting
        try:
            amount = float(str(amount_str).replace(",", "").replace("$", ""))
        except (ValueError, TypeError):
            logger.warning(f"Could not parse amount for {account_name}: {amount_str}")
            amount = 0.0
            
        # Extract account ID if available
        account_id = None
        if len(cells) > 0 and cells[0].get("attributes"):
            for attr in cells[0]["attributes"]:
                if attr.get("id") == "account":
                    account_id = attr.get("value")
                    break
                    
        return {
            "section": section_title,
            "account_name": account_name,
            "account_id": account_id,
            "amount": amount,
            "row_type": row_type,
            "is_summary": row_type == "SummaryRow"
        }

    def _normalize_section_direct(self, section: Dict) -> Optional[Dict[str, Any]]:
        """
        Legacy method for single-period direct section handling (kept for compatibility).
        
        Args:
            section: Section data from Xero
            
        Returns:
            Normalized section data or None
        """
        title = section.get("title", "").strip()
        row_type = section.get("rowType", "")
        
        if not title:
            return None
            
        # Extract amount if available in cells
        amount = 0.0
        cells = section.get("cells", [])
        if len(cells) > 1:
            amount_str = cells[1].get("value", "0.00")
            try:
                amount = float(str(amount_str).replace(",", "").replace("$", ""))
            except (ValueError, TypeError):
                amount = 0.0
        
        return {
            "section": "",  # Direct items typically have empty section
            "account_name": title,
            "account_id": None,  # Direct items don't have account IDs
            "amount": amount,
            "row_type": row_type if row_type else "Row",
            "is_summary": row_type == "SummaryRow" or title in ["GROSS PROFIT", "NET PROFIT", "Total Income", "Total Operating Expenses"]
        }

    def _normalize_bs_section_direct(self, section: Dict) -> Optional[Dict[str, Any]]:
        """
        Legacy method for single-period Balance Sheet direct section handling (kept for compatibility).
        
        Args:
            section: Section data from Xero Balance Sheet
            
        Returns:
            Normalized section data or None
        """
        title = section.get("title", "").strip()
        row_type = section.get("rowType", "")
        
        if not title:
            return None
            
        # Extract amount if available in cells
        amount = 0.0
        cells = section.get("cells", [])
        if len(cells) > 1:
            amount_str = cells[1].get("value", "0.00")
            try:
                amount = float(str(amount_str).replace(",", "").replace("$", ""))
            except (ValueError, TypeError):
                amount = 0.0
        
        # Balance sheet specific summary detection
        bs_summary_titles = [
            "Total Assets", "Total Current Assets", "Total Non-current Assets",
            "Total Liabilities", "Total Current Liabilities", "Total Non-current Liabilities", 
            "Total Equity", "Net Assets"
        ]
        
        return {
            "section": "",  # Direct items typically have empty section
            "account_name": title,
            "account_id": None,  # Direct items don't have account IDs
            "amount": amount,
            "row_type": row_type if row_type else "Row",
            "is_summary": row_type == "SummaryRow" or title in bs_summary_titles
        }

    def export_pl_normalized_json(
        self, 
        pl_response: Dict[str, Any], 
        date_range: str,
        filename: Optional[str] = None
    ) -> Path:
        """
        Export P&L data to normalized JSON format matching your structure exactly.
        
        Args:
            pl_response: Raw P&L response from Xero
            date_range: Date range string (e.g., "2025-07-01_to_2025-07-31")
            filename: Custom filename (auto-generated if None)
            
        Returns:
            Path to saved JSON file
        """
        normalized_data = self.extract_and_normalize_pl_data(pl_response, date_range)
        
        if not normalized_data:
            raise ValueError("No P&L data found in response")
            
        # Generate filename matching your format
        if filename is None:
            filename = f"xero_pl_normalized_{date_range}.json"
            
        filepath = self.output_dir / filename
        
        # Create export structure matching your format exactly
        export_data = {
            "export_metadata": {
                "timestamp": datetime.now().isoformat(),
                "date_range": date_range,
                "format": "normalized",
                "record_count": len(normalized_data)
            },
            "data": normalized_data
        }
        
        # Write JSON with consistent formatting
        with open(filepath, 'w', encoding='utf-8') as jsonfile:
            json.dump(export_data, jsonfile, indent=2, ensure_ascii=False)
            
        logger.info(f"✅ P&L data exported to normalized JSON: {filepath}")
        logger.info(f"   Records: {len(normalized_data)}")
        
        return filepath

    def export_bs_normalized_json(
        self, 
        bs_response: Dict[str, Any], 
        as_at_date: str,
        filename: Optional[str] = None
    ) -> Path:
        """
        Export Balance Sheet data to normalized JSON format.
        
        Args:
            bs_response: Raw Balance Sheet response from Xero
            as_at_date: As at date string (e.g., "2025-07-31")
            filename: Custom filename (auto-generated if None)
            
        Returns:
            Path to saved JSON file
        """
        normalized_data = self.extract_and_normalize_bs_data(bs_response, as_at_date)
        
        if not normalized_data:
            raise ValueError("No Balance Sheet data found in response")
            
        # Generate filename
        if filename is None:
            filename = f"xero_bs_normalized_{as_at_date}.json"
            
        filepath = self.output_dir / filename
        
        # Create export structure
        export_data = {
            "export_metadata": {
                "timestamp": datetime.now().isoformat(),
                "as_at_date": as_at_date,
                "format": "normalized",
                "record_count": len(normalized_data)
            },
            "data": normalized_data
        }
        
        # Write JSON with consistent formatting
        with open(filepath, 'w', encoding='utf-8') as jsonfile:
            json.dump(export_data, jsonfile, indent=2, ensure_ascii=False)
            
        logger.info(f"✅ Balance Sheet data exported to normalized JSON: {filepath}")
        logger.info(f"   Records: {len(normalized_data)}")
        
        return filepath

    def export_combined_periods_json(
        self,
        pl_responses: List[tuple[Dict[str, Any], str]],
        combined_filename: Optional[str] = None
    ) -> Path:
        """
        Export multiple P&L periods to a single combined normalized JSON file.
        Enhanced to handle multi-period API responses efficiently.
        
        Args:
            pl_responses: List of tuples (pl_response, date_range_str)
            combined_filename: Optional custom filename for combined file
            
        Returns:
            Path to exported combined JSON file
        """
        if not pl_responses:
            raise ValueError("No P&L responses provided for export")
        
        combined_data = []
        period_summaries = []
        earliest_date = None
        latest_date = None
        
        # Process each response (which may contain multiple periods)
        for pl_response, date_range in pl_responses:
            try:
                # Extract normalized data for this response (may contain multiple periods)
                # Pass the response_period to avoid warnings during initial processing
                normalized_data = self.extract_and_normalize_pl_data(pl_response, date_range)
                
                if normalized_data:
                    # Add response-level period information (already processed during extraction)
                    for item in normalized_data:
                        item["response_period"] = date_range
                    
                    combined_data.extend(normalized_data)
                    
                    # Track period summary
                    period_summaries.append({
                        "period": date_range,
                        "record_count": len(normalized_data)
                    })
                    
                    # Track date range for filename
                    period_start = date_range.split("_to_")[0] if "_to_" in date_range else date_range
                    period_end = date_range.split("_to_")[1] if "_to_" in date_range else date_range
                    
                    if earliest_date is None or period_start < earliest_date:
                        earliest_date = period_start
                    if latest_date is None or period_end > latest_date:
                        latest_date = period_end
                        
                else:
                    logger.warning(f"No data found for period: {date_range}")
                    
            except Exception as e:
                logger.error(f"Failed to process period {date_range}: {e}")
                continue
        
        if not combined_data:
            raise ValueError("No valid P&L data found across all periods")
        
        # Generate combined filename
        if combined_filename is None:
            date_range_str = f"{earliest_date}_to_{latest_date}" if earliest_date and latest_date else "multi_period"
            combined_filename = f"xero_pl_normalized_combined_{date_range_str}.json"
        
        filepath = self.output_dir / combined_filename
        
        # Create combined export structure
        export_data = {
            "export_metadata": {
                "timestamp": datetime.now().isoformat(),
                "format": "normalized_combined",
                "total_record_count": len(combined_data),
                "periods_included": len(period_summaries),
                "date_range_combined": f"{earliest_date}_to_{latest_date}" if earliest_date and latest_date else "multi_period",
                "period_summaries": period_summaries,
                "api_efficiency": {
                    "total_records": len(combined_data),
                    "api_calls_made": len(period_summaries),
                    "records_per_call": len(combined_data) / max(len(period_summaries), 1)
                }
            },
            "data": combined_data
        }
        
        # Write combined JSON file
        with open(filepath, 'w', encoding='utf-8') as jsonfile:
            json.dump(export_data, jsonfile, indent=2, ensure_ascii=False)
        
        logger.info(f"✅ Combined P&L data exported: {filepath}")
        logger.info(f"   Total records: {len(combined_data)}")
        logger.info(f"   Periods covered: {len(period_summaries)}")
        logger.info(f"   Date range: {earliest_date} to {latest_date}")
        logger.info(f"   API efficiency: {len(combined_data)} records in {len(period_summaries)} calls")
        
        return filepath

    def export_combined_bs_periods_json(
        self,
        bs_responses: List[tuple[Dict[str, Any], str]],
        combined_filename: Optional[str] = None
    ) -> Path:
        """
        Export multiple Balance Sheet periods to a single combined normalized JSON file.
        Enhanced to handle multi-period API responses efficiently.
        
        Args:
            bs_responses: List of tuples (bs_response, as_at_date_str)
            combined_filename: Optional custom filename for combined file
            
        Returns:
            Path to exported combined JSON file
        """
        if not bs_responses:
            raise ValueError("No Balance Sheet responses provided for export")
        
        combined_data = []
        period_summaries = []
        earliest_date = None
        latest_date = None
        
        # Process each response (which may contain multiple periods)
        for bs_response, as_at_date in bs_responses:
            try:
                # Extract normalized data for this response (may contain multiple periods)
                # Pass the response_period to avoid warnings during initial processing
                normalized_data = self.extract_and_normalize_bs_data(bs_response, as_at_date)
                
                if normalized_data:
                    # Add response-level period information (already processed during extraction)
                    for item in normalized_data:
                        item["response_period"] = as_at_date
                    
                    combined_data.extend(normalized_data)
                    
                    # Track period summary
                    period_summaries.append({
                        "as_at_date": as_at_date,
                        "record_count": len(normalized_data)
                    })
                    
                    # Track date range for filename
                    if earliest_date is None or as_at_date < earliest_date:
                        earliest_date = as_at_date
                    if latest_date is None or as_at_date > latest_date:
                        latest_date = as_at_date
                        
                else:
                    logger.warning(f"No data found for date: {as_at_date}")
                    
            except Exception as e:
                logger.error(f"Failed to process date {as_at_date}: {e}")
                continue
        
        if not combined_data:
            raise ValueError("No valid Balance Sheet data found across all periods")
        
        # Generate combined filename
        if combined_filename is None:
            date_range_str = f"{earliest_date}_to_{latest_date}" if earliest_date and latest_date else "multi_period"
            combined_filename = f"xero_bs_normalized_combined_{date_range_str}.json"
        
        filepath = self.output_dir / combined_filename
        
        # Create combined export structure
        export_data = {
            "export_metadata": {
                "timestamp": datetime.now().isoformat(),
                "format": "normalized_combined",
                "total_record_count": len(combined_data),
                "periods_included": len(period_summaries),
                "date_range_combined": f"{earliest_date}_to_{latest_date}" if earliest_date and latest_date else "multi_period",
                "period_summaries": period_summaries,
                "api_efficiency": {
                    "total_records": len(combined_data),
                    "api_calls_made": len(period_summaries),
                    "records_per_call": len(combined_data) / max(len(period_summaries), 1)
                }
            },
            "data": combined_data
        }
        
        # Write combined JSON file
        with open(filepath, 'w', encoding='utf-8') as jsonfile:
            json.dump(export_data, jsonfile, indent=2, ensure_ascii=False)
        
        logger.info(f"✅ Combined Balance Sheet data exported: {filepath}")
        logger.info(f"   Total records: {len(combined_data)}")
        logger.info(f"   Periods covered: {len(period_summaries)}")
        logger.info(f"   Date range: {earliest_date} to {latest_date}")
        logger.info(f"   API efficiency: {len(combined_data)} records in {len(period_summaries)} calls")
        
        return filepath


# ---------- Quick Export Functions ----------

def export_pl_normalized(
    pl_response: Dict[str, Any],
    date_range: str,
    output_dir: str = "exports"
) -> Path:
    """
    Quick export function for normalized P&L JSON matching your format.
    
    Args:
        pl_response: Raw P&L response from Xero
        date_range: Date range string (e.g., "2025-07-01_to_2025-07-31")
        output_dir: Directory to save files
        
    Returns:
        Path to exported normalized JSON file
    """
    exporter = XeroNormalizedExporter(output_dir)
    return exporter.export_pl_normalized_json(pl_response, date_range)


def export_bs_normalized(
    bs_response: Dict[str, Any],
    as_at_date: str,
    output_dir: str = "exports"
) -> Path:
    """
    Quick export function for normalized Balance Sheet JSON.
    
    Args:
        bs_response: Raw Balance Sheet response from Xero
        as_at_date: As at date string (e.g., "2025-07-31")
        output_dir: Directory to save files
        
    Returns:
        Path to exported normalized JSON file
    """
    exporter = XeroNormalizedExporter(output_dir)
    return exporter.export_bs_normalized_json(bs_response, as_at_date)


def export_combined_pl_periods(
    pl_responses: List[tuple[Dict[str, Any], str]],
    output_dir: str = "exports"
) -> Path:
    """
    Quick export function for combined normalized P&L JSON.
    
    Args:
        pl_responses: List of tuples (pl_response, date_range_str)
        output_dir: Directory to save files
        
    Returns:
        Path to exported combined JSON file
    """
    exporter = XeroNormalizedExporter(output_dir)
    return exporter.export_combined_periods_json(pl_responses)


def export_combined_bs_periods(
    bs_responses: List[tuple[Dict[str, Any], str]],
    output_dir: str = "exports"
) -> Path:
    """
    Quick export function for combined normalized Balance Sheet JSON.
    
    Args:
        bs_responses: List of tuples (bs_response, as_at_date_str)
        output_dir: Directory to save files
        
    Returns:
        Path to exported combined JSON file
    """
    exporter = XeroNormalizedExporter(output_dir)
    return exporter.export_combined_bs_periods_json(bs_responses)


# ---------- Analysis Functions ----------

def analyze_pl_structure(pl_response: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analyze P&L response structure for debugging and validation.
    Enhanced to handle multi-period responses.
    
    Args:
        pl_response: Raw P&L response from Xero (may contain multiple periods)
        
    Returns:
        Dict with analysis results
    """
    exporter = XeroNormalizedExporter()
    normalized_data = exporter.extract_and_normalize_pl_data(pl_response)
    
    sections = set(item["section"] for item in normalized_data if item["section"])
    section_breakdown = {}
    period_breakdown = {}
    
    for item in normalized_data:
        section = item["section"] or "(Direct Items)"
        if section not in section_breakdown:
            section_breakdown[section] = 0
        section_breakdown[section] += 1
        
        # Track period breakdown if available
        period_id = item.get("period_identifier", "unknown")
        if period_id not in period_breakdown:
            period_breakdown[period_id] = 0
        period_breakdown[period_id] += 1
    
    return {
        "total_line_items": len(normalized_data),
        "sections": list(sections),
        "section_breakdown": section_breakdown,
        "period_breakdown": period_breakdown,
        "sample_data": normalized_data[:5],  # First 5 items for inspection
        "summary_items": [
            item for item in normalized_data 
            if item["is_summary"]
        ],
        "multi_period_detected": len(period_breakdown) > 1,
        "periods_found": list(period_breakdown.keys())
    }


def analyze_bs_structure(bs_response: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analyze Balance Sheet response structure for debugging and validation.
    Enhanced to handle multi-period responses.
    
    Args:
        bs_response: Raw Balance Sheet response from Xero (may contain multiple periods)
        
    Returns:
        Dict with analysis results
    """
    exporter = XeroNormalizedExporter()
    normalized_data = exporter.extract_and_normalize_bs_data(bs_response)
    
    sections = set(item["section"] for item in normalized_data if item["section"])
    section_breakdown = {}
    period_breakdown = {}
    
    for item in normalized_data:
        section = item["section"] or "(Direct Items)"
        if section not in section_breakdown:
            section_breakdown[section] = 0
        section_breakdown[section] += 1
        
        # Track period breakdown if available
        period_id = item.get("period_identifier", "unknown")
        if period_id not in period_breakdown:
            period_breakdown[period_id] = 0
        period_breakdown[period_id] += 1
    
    return {
        "total_line_items": len(normalized_data),
        "sections": list(sections),
        "section_breakdown": section_breakdown,
        "period_breakdown": period_breakdown,
        "sample_data": normalized_data[:5],  # First 5 items for inspection
        "summary_items": [
            item for item in normalized_data 
            if item["is_summary"]
        ],
        "multi_period_detected": len(period_breakdown) > 1,
        "periods_found": list(period_breakdown.keys())
    }