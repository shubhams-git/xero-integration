import subprocess
import json
import sys
import os

class MCPClient:
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
    
    def close(self):
        """Close the server process"""
        if self.process:
            self.process.terminate()
            self.process.wait()

def main():
    # Xero MCP server configuration
    client = MCPClient(
        server_command="npx.cmd",
        server_args=["-y", "@xeroapi/xero-mcp-server@latest"],
        env_vars={
            "XERO_CLIENT_BEARER_TOKEN": "eyJhbGciOiJSUzI1NiIsImtpZCI6IjFDQUY4RTY2NzcyRDZEQzAyOEQ2NzI2RkQwMjYxNTgxNTcwRUZDMTkiLCJ0eXAiOiJKV1QiLCJ4NXQiOiJISy1PWm5jdGJjQW8xbkp2MENZVmdWY09fQmsifQ.eyJuYmYiOjE3NTU1OTkxNjIsImV4cCI6MTc1NTYwMDk2MiwiaXNzIjoiaHR0cHM6Ly9pZGVudGl0eS54ZXJvLmNvbSIsImF1ZCI6Imh0dHBzOi8vaWRlbnRpdHkueGVyby5jb20vcmVzb3VyY2VzIiwiY2xpZW50X2lkIjoiQjU4QUU5Mjk3QkQ5NEVDRTk4NkU5ODVENThBOUMxOTgiLCJ4ZXJvX3VzZXJpZCI6IjkwMjllNTZjLTI0OWQtNGNjYy05NDFmLTBjOGIzOWU4MTc0OSIsImF1dGhlbnRpY2F0aW9uX2V2ZW50X2lkIjoiNzNhMmE5YmItYWM3MS00MzZmLWExYTctODE3ZjJjOWEyYzkzIiwianRpIjoiRDg1RTAwRkExRjExN0JBNDQ1RThGMEE4MDNCODA2MTIiLCJzY29wZSI6WyJhY2NvdW50aW5nLmNvbnRhY3RzIiwiYWNjb3VudGluZy5yZXBvcnRzLnJlYWQiLCJhY2NvdW50aW5nLnNldHRpbmdzIiwiYWNjb3VudGluZy50cmFuc2FjdGlvbnMiXX0.Xf38WgjDM7nnDwaWujnto4z9WzNw7Xn-C_S81pIpU9SNDUqMs_IMlrJ4wj3MawCE7sSc_VD22aqayf-HJPWMrLG29zAa5X5IJuBb6E8kFIJmAjVQ-0n6CtEkUqn_yDODTr1oWbhJCjS4Tfmey8VGInXcN0IWXjsMsRkQaQc0fEG6cQRZGR4mxzhOp7-vzDVkQ2WQFq2gjnSKAIxye_5fzCSaraxzCGhmA80JRWRwXhKqMro72KDAxiO-EKWG64lMb0BMFZ1uBA0LEC3tigR13TaFCkQD_EsLraFS1aqKTC42rwNWT33ZhUgZJXFwfjFxG1cxPS9xv76oEUou9hbH7g"
        }
    )
    
    try:
        print("Starting Xero MCP server...")
        client.start_server()
        
        print("Initializing connection...")
        # Initialize the MCP connection
        init_response = client.send_request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {
                "name": "python-mcp-client",
                "version": "1.0.0"
            }
        })
        print(f"Initialize response: {init_response}")
        
        print("\nGetting balance sheet for 19/08/2025...")
        # Get balance sheet and save raw response
        balance_sheet_response = client.send_request("tools/call", {
            "name": "list-report-balance-sheet",
            "arguments": {"date": "2025-08-19"}
        })
        
        # Save raw balance sheet response
        with open("balance_sheet_raw.json", "w") as f:
            json.dump(balance_sheet_response, f, indent=2)
        print("Raw balance sheet saved to: balance_sheet_raw.json")
        
        print("\nGetting profit and loss for 19/08/2025...")
        # Get profit and loss and save raw response
        profit_loss_response = client.send_request("tools/call", {
            "name": "list-profit-and-loss",
            "arguments": {"toDate": "2025-08-19"}
        })
        
        # Save raw profit and loss response
        with open("profit_loss_raw.json", "w") as f:
            json.dump(profit_loss_response, f, indent=2)
        print("Raw profit and loss saved to: profit_loss_raw.json")
            
    except Exception as e:
        print(f"Error: {e}")
        
    finally:
        client.close()

if __name__ == "__main__":
    main()