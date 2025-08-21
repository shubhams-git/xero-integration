"""
Streamlined Xero data export utilities focused on normalized JSON output.
Produces a single combined normalized JSON file for all periods.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

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

    def extract_and_normalize_pl_data(self, pl_response: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract and normalize P&L data from Xero response to match your desired format.
        
        Args:
            pl_response: Raw P&L response from Xero MCP
            
        Returns:
            List of normalized financial line items matching your JSON structure
        """
        normalized_data = []
        
        # Extract content from response
        content_blocks = pl_response.get("result", {}).get("content", [])
        
        for block in content_blocks:
            if not isinstance(block, dict) or "text" not in block:
                continue
                
            text = block["text"]
            
            # Try to parse JSON array from text
            if isinstance(text, str) and "[" in text and "]" in text:
                start_idx = text.find("[")
                end_idx = text.rfind("]")
                
                if start_idx != -1 and end_idx != -1:
                    try:
                        json_data = json.loads(text[start_idx:end_idx + 1])
                        normalized_data.extend(self._normalize_pl_sections(json_data))
                    except json.JSONDecodeError:
                        logger.warning("Could not parse JSON from P&L response")
                        
        return normalized_data

    def _normalize_pl_sections(self, sections: List[Dict]) -> List[Dict[str, Any]]:
        """
        Normalize P&L sections into the exact format matching your JSON structure.
        
        Args:
            sections: List of P&L sections from Xero
            
        Returns:
            List of normalized line items in your required format
        """
        normalized_items = []
        
        for section in sections:
            section_title = section.get("title", "").strip()
            section_type = section.get("rowType", "")
            
            # Handle different section types
            if section_type == "Section" and "rows" in section:
                for row in section["rows"]:
                    item = self._normalize_row(row, section_title)
                    if item:
                        normalized_items.append(item)
            
            # Handle section headers and summary rows that aren't in "rows"
            elif section_type in ["Header", "SummaryRow", "Row"]:
                item = self._normalize_section_direct(section)
                if item:
                    normalized_items.append(item)
                        
        return normalized_items

    def _normalize_row(self, row: Dict, section_title: str) -> Optional[Dict[str, Any]]:
        """
        Normalize a single P&L row into your required format.
        
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

    def _normalize_section_direct(self, section: Dict) -> Optional[Dict[str, Any]]:
        """
        Handle sections that are direct data items (like GROSS PROFIT, NET PROFIT).
        
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
        normalized_data = self.extract_and_normalize_pl_data(pl_response)
        
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

    def export_combined_periods_json(
        self,
        pl_responses: List[tuple[Dict[str, Any], str]],
        combined_filename: Optional[str] = None
    ) -> Path:
        """
        Export multiple P&L periods to a single combined normalized JSON file.
        
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
        
        # Process each period's data
        for pl_response, date_range in pl_responses:
            try:
                # Extract normalized data for this period
                normalized_data = self.extract_and_normalize_pl_data(pl_response)
                
                if normalized_data:
                    # Add period information to each record
                    for item in normalized_data:
                        item["period"] = date_range
                    
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
                "period_summaries": period_summaries
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
        
        return filepath


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


def analyze_pl_structure(pl_response: Dict[str, Any]) -> Dict[str, Any]:
    """
    Analyze P&L response structure for debugging and validation.
    
    Args:
        pl_response: Raw P&L response from Xero
        
    Returns:
        Dict with analysis results
    """
    exporter = XeroNormalizedExporter()
    normalized_data = exporter.extract_and_normalize_pl_data(pl_response)
    
    sections = set(item["section"] for item in normalized_data if item["section"])
    section_breakdown = {}
    
    for item in normalized_data:
        section = item["section"] or "(Direct Items)"
        if section not in section_breakdown:
            section_breakdown[section] = 0
        section_breakdown[section] += 1
    
    return {
        "total_line_items": len(normalized_data),
        "sections": list(sections),
        "section_breakdown": section_breakdown,
        "sample_data": normalized_data[:5],  # First 5 items for inspection
        "summary_items": [
            item for item in normalized_data 
            if item["is_summary"]
        ]
    }