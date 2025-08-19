import subprocess
import json
import sys
import os
from datetime import datetime

class XeroRawDataClient:
    def __init__(self, server_command, server_args, env_vars=None):
        self.server_command = server_command
        self.server_args = server_args
        self.env_vars = env_vars or {}
        self.process = None
        
    def start_server(self):
        """Start the MCP server process"""
        env = os.environ.copy()
        env.update(self.env_vars)
        
        cmd = [self.server_command] + self.server_args
        
        self.process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env
        )
        
    def send_request(self, method, params=None):
        """Send a JSON-RPC request to the MCP server"""
        if not self.process:
            raise Exception("Server not started")
            
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {}
        }
        
        request_json = json.dumps(request) + '\n'
        
        try:
            self.process.stdin.write(request_json)
            self.process.stdin.flush()
            
            response_line = self.process.stdout.readline()
            if not response_line:
                stderr_output = self.process.stderr.read()
                raise Exception(f"No response from server. Error: {stderr_output}")
                
            response = json.loads(response_line.strip())
            return response
            
        except Exception as e:
            stderr_output = self.process.stderr.read()
            raise Exception(f"Error communicating with server: {e}. Stderr: {stderr_output}")
    
    def save_raw_response(self, response, filename):
        """Save raw response to file"""
        with open(filename, 'w') as f:
            json.dump(response, f, indent=2)
        return filename
    
    def get_financial_reports(self, report_date=None, output_prefix="xero_data"):
        """Get all financial reports and save raw responses"""
        if not report_date:
            report_date = "2025-08-19"
            
        print(f"Fetching Xero financial data for {report_date}...")
        
        # Initialize connection
        print("Initializing connection...")
        init_response = self.send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "xero-raw-data-client",
                "version": "1.0.0"
            }
        })
        
        results = {
            "report_date": report_date,
            "files_saved": []
        }
        
        # Get Balance Sheet
        print(f"\nFetching Balance Sheet for {report_date}...")
        try:
            balance_sheet_response = self.send_request("tools/call", {
                "name": "list-report-balance-sheet",
                "arguments": {"date": report_date}
            })
            
            filename = f"{output_prefix}_balance_sheet_{report_date.replace('-', '')}_raw.json"
            self.save_raw_response(balance_sheet_response, filename)
            results["files_saved"].append(filename)
            print(f"✓ Balance Sheet saved: {filename}")
            
        except Exception as e:
            print(f"✗ Balance Sheet Error: {e}")
        
        # Get Profit & Loss
        print(f"\nFetching Profit & Loss for {report_date}...")
        try:
            profit_loss_response = self.send_request("tools/call", {
                "name": "list-profit-and-loss",
                "arguments": {"toDate": report_date}
            })
            
            filename = f"{output_prefix}_profit_loss_{report_date.replace('-', '')}_raw.json"
            self.save_raw_response(profit_loss_response, filename)
            results["files_saved"].append(filename)
            print(f"✓ Profit & Loss saved: {filename}")
            
        except Exception as e:
            print(f"✗ Profit & Loss Error: {e}")
        
        # Get Trial Balance
        print(f"\nFetching Trial Balance for {report_date}...")
        try:
            trial_balance_response = self.send_request("tools/call", {
                "name": "list-trial-balance",
                "arguments": {"date": report_date}
            })
            
            filename = f"{output_prefix}_trial_balance_{report_date.replace('-', '')}_raw.json"
            self.save_raw_response(trial_balance_response, filename)
            results["files_saved"].append(filename)
            print(f"✓ Trial Balance saved: {filename}")
            
        except Exception as e:
            print(f"✗ Trial Balance Error: {e}")
        
        return results
    
    def close(self):
        """Close the server process"""
        if self.process:
            self.process.terminate()
            self.process.wait()

def main():
    # Xero MCP server configuration
    client = XeroRawDataClient(
        server_command="npx.cmd",
        server_args=["-y", "@xeroapi/xero-mcp-server@latest"],
        env_vars={
            "XERO_CLIENT_BEARER_TOKEN": "eyJhbGciOiJSUzI1NiIsImtpZCI6IjFDQUY4RTY2NzcyRDZEQzAyOEQ2NzI2RkQwMjYxNTgxNTcwRUZDMTkiLCJ0eXAiOiJKV1QiLCJ4NXQiOiJISy1PWm5jdGJjQW8xbkp2MENZVmdWY09fQmsifQ.eyJuYmYiOjE3NTU1OTkxNjIsImV4cCI6MTc1NTYwMDk2MiwiaXNzIjoiaHR0cHM6Ly9pZGVudGl0eS54ZXJvLmNvbSIsImF1ZCI6Imh0dHBzOi8vaWRlbnRpdHkueGVyby5jb20vcmVzb3VyY2VzIiwiY2xpZW50X2lkIjoiQjU4QUU5Mjk3QkQ5NEVDRTk4NkU5ODVENThBOUMxOTgiLCJ4ZXJvX3VzZXJpZCI6IjkwMjllNTZjLTI0OWQtNGNjYy05NDFmLTBjOGIzOWU4MTc0OSIsImF1dGhlbnRpY2F0aW9uX2V2ZW50X2lkIjoiNzNhMmE5YmItYWM3MS00MzZmLWExYTctODE3ZjJjOWEyYzkzIiwianRpIjoiRDg1RTAwRkExRjExN0JBNDQ1RThGMEE4MDNCODA2MTIiLCJzY29wZSI6WyJhY2NvdW50aW5nLmNvbnRhY3RzIiwiYWNjb3VudGluZy5yZXBvcnRzLnJlYWQiLCJhY2NvdW50aW5nLnNldHRpbmdzIiwiYWNjb3VudGluZy50cmFuc2FjdGlvbnMiXX0.Xf38WgjDM7nnDwaWujnto4z9WzNw7Xn-C_S81pIpU9SNDUqMs_IMlrJ4wj3MawCE7sSc_VD22aqayf-HJPWMrLG29zAa5X5IJuBb6E8kFIJmAjVQ-0n6CtEkUqn_yDODTr1oWbhJCjS4Tfmey8VGInXcN0IWXjsMsRkQaQc0fEG6cQRZGR4mxzhOp7-vzDVkQ2WQFq2gjnSKAIxye_5fzCSaraxzCGhmA80JRWRwXhKqMro72KDAxiO-EKWG64lMb0BMFZ1uBA0LEC3tigR13TaFCkQD_EsLraFS1aqKTC42rwNWT33ZhUgZJXFwfjFxG1cxPS9xv76oEUou9hbH7g"
        }
    )
    
    try:
        print("Starting Xero Raw Data Client...")
        client.start_server()
        
        # Get all financial reports as raw data
        results = client.get_financial_reports(
            report_date="2025-08-19",
            output_prefix="demo_company"
        )
        
        print(f"\n{'='*50}")
        print("RAW DATA EXTRACTION COMPLETE")
        print(f"{'='*50}")
        print(f"Report Date: {results['report_date']}")
        print(f"Files Saved: {len(results['files_saved'])}")
        for file in results['files_saved']:
            print(f"  • {file}")
            
    except Exception as e:
        print(f"Error: {e}")
        
    finally:
        client.close()

if __name__ == "__main__":
    main()