"""
JSON-RPC client for Model Context Protocol (MCP) servers over stdio.
Includes proper error handling, logging, and type safety with enhanced authentication error detection.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
DEFAULT_TIMEOUT = 30.0


class MCPError(Exception):
    """Base exception for MCP client errors."""
    pass


class MCPConnectionError(MCPError):
    """Raised when connection to MCP server fails."""
    pass


class MCPTimeoutError(MCPError):
    """Raised when MCP server doesn't respond within timeout."""
    pass


class MCPServerError(MCPError):
    """Raised when MCP server returns an error response."""
    pass


class MCPAuthenticationError(MCPError):
    """Raised when authentication fails (401/403 errors)."""
    pass


def detect_authentication_error(response_text: str) -> bool:
    """
    Detect authentication/authorization errors in response text.
    
    Args:
        response_text: Response text to analyze
        
    Returns:
        bool: True if authentication error detected
    """
    auth_indicators = [
        "status code 401",
        "status code 403", 
        "unauthorized",
        "forbidden",
        "authentication failed",
        "invalid token",
        "token expired",
        "access denied"
    ]
    
    text_lower = response_text.lower()
    return any(indicator in text_lower for indicator in auth_indicators)


def get_npx_executable() -> str:
    """
    Get the appropriate npx executable for the current platform.
    
    Returns:
        str: The npx executable name/path
        
    Raises:
        MCPConnectionError: If npx is not found
    """
    npx_cmd = "npx.cmd" if platform.system().lower().startswith("win") else "npx"
    
    try:
        subprocess.run([npx_cmd, "--version"], 
                      capture_output=True, 
                      check=True, 
                      timeout=10)
        return npx_cmd
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise MCPConnectionError(
            f"npx not found or not working. Please install Node.js/npm. Error: {e}"
        ) from e


class MCPClient:
    """
    Professional JSON-RPC client for MCP servers with stdio transport.
    
    Handles initialization, tool calls, and proper cleanup with context management.
    Enhanced with better authentication error detection and logging.
    """

    def __init__(
        self, 
        command: str, 
        args: List[str], 
        env: Optional[Dict[str, str]] = None,
        timeout: float = DEFAULT_TIMEOUT
    ):
        """
        Initialize MCP client.
        
        Args:
            command: The command to run (e.g., 'npx')
            args: Arguments for the command
            env: Additional environment variables
            timeout: Default timeout for RPC calls
        """
        self.command = command
        self.args = args
        self.env = env or {}
        self.timeout = timeout
        self.proc: Optional[subprocess.Popen] = None
        self._request_id = 0
        self._initialized = False
        self._authentication_failed = False

    def start(self) -> None:
        """
        Start the MCP server process and perform initialization handshake.
        
        Raises:
            MCPConnectionError: If server fails to start
            MCPServerError: If initialization fails
        """
        if self.proc is not None:
            logger.warning("MCP client already started")
            return

        # Prepare environment
        full_env = os.environ.copy()
        full_env.update(self.env)

        try:
            logger.info(f"🔌 Starting MCP server: {self.command} {' '.join(self.args)}")
            self.proc = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=0,  # Unbuffered for real-time communication
                env=full_env
            )
        except FileNotFoundError as e:
            raise MCPConnectionError(
                f"Failed to start MCP server '{self.command}'. "
                "Ensure the command is available on PATH."
            ) from e

        # Wait a moment for server to start
        time.sleep(0.1)
        
        # Check if process started successfully
        if self.proc.poll() is not None:
            stderr_output = self._read_stderr()
            raise MCPConnectionError(
                f"MCP server exited immediately. Stderr: {stderr_output}"
            )

        try:
            # Perform MCP initialization handshake
            logger.debug("🤝 Performing MCP initialization handshake")
            init_response = self._rpc("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {}
                },
                "clientInfo": {
                    "name": "xero-mcp-client", 
                    "version": "1.0.0"
                },
            })
            
            logger.info("✅ MCP initialization successful")
            logger.debug(f"Server capabilities: {init_response.get('result', {}).get('capabilities', {})}")
            self._initialized = True
            
        except Exception as e:
            self.close()
            raise MCPConnectionError(f"Failed to initialize MCP server: {e}") from e

    def close(self) -> None:
        """Gracefully terminate the MCP server process."""
        if self.proc is None:
            return

        logger.info("🔌 Closing MCP client connection")
        
        try:
            # Try graceful termination first
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                logger.warning("⚠️  MCP server didn't terminate gracefully, killing")
                self.proc.kill()
                self.proc.wait(timeout=2)
        except Exception as e:
            logger.error(f"❌ Error closing MCP server: {e}")
        finally:
            self.proc = None
            self._initialized = False

    def _read_stderr(self) -> str:
        """Read available stderr output."""
        if not self.proc or not self.proc.stderr:
            return ""
        try:
            # Non-blocking read of available stderr
            return self.proc.stderr.read()
        except Exception:
            return ""

    def _rpc(
        self, 
        method: str, 
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Send JSON-RPC request and wait for response.
        
        Args:
            method: RPC method name
            params: Method parameters
            timeout: Request timeout (uses instance default if None)
            
        Returns:
            Dict containing the RPC response
            
        Raises:
            MCPConnectionError: If connection is broken
            MCPTimeoutError: If request times out
            MCPServerError: If server returns an error
            MCPAuthenticationError: If authentication fails
        """
        if not self.proc or not self.proc.stdin or not self.proc.stdout:
            raise MCPConnectionError("MCP server not started or connection broken")

        if timeout is None:
            timeout = self.timeout

        self._request_id += 1
        request = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or {}
        }

        try:
            # Send request
            request_line = json.dumps(request) + "\n"
            logger.debug(f"📤 Sending RPC: {method}")
            self.proc.stdin.write(request_line)
            self.proc.stdin.flush()
        except BrokenPipeError as e:
            stderr_output = self._read_stderr()
            raise MCPConnectionError(
                f"Connection to MCP server broken. Stderr: {stderr_output}"
            ) from e

        # Wait for response
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                stderr_output = self._read_stderr()
                raise MCPConnectionError(
                    f"MCP server process died. Stderr: {stderr_output}"
                )

            try:
                line = self.proc.stdout.readline()
                if not line:
                    time.sleep(0.01)  # Small delay to prevent busy waiting
                    continue
                    
                line = line.strip()
                if not line:
                    continue

                response = json.loads(line)
                
                # Check if this is our response
                if response.get("id") == self._request_id:
                    if "error" in response:
                        error_info = response["error"]
                        error_message = error_info.get('message', 'Unknown error')
                        error_code = error_info.get('code', 'unknown')
                        
                        # Check for authentication errors
                        if detect_authentication_error(error_message):
                            self._authentication_failed = True
                            raise MCPAuthenticationError(
                                f"🔐 Authentication failed for '{method}': {error_message}"
                            )
                        
                        raise MCPServerError(
                            f"MCP server error for '{method}': {error_message} (code: {error_code})"
                        )
                    
                    logger.debug(f"📥 RPC '{method}' completed successfully")
                    return response
                    
            except json.JSONDecodeError as e:
                logger.debug(f"Ignoring malformed JSON from server: {line}")
                continue
            except (MCPAuthenticationError, MCPServerError):
                # Re-raise these specific errors
                raise
            except Exception as e:
                logger.error(f"Unexpected error reading response: {e}")
                continue

        raise MCPTimeoutError(f"Timeout waiting for response to '{method}' after {timeout}s")

    def call_tool(
        self, 
        name: str, 
        arguments: Dict[str, Any], 
        timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Call an MCP tool with the given arguments.
        
        Args:
            name: Tool name
            arguments: Tool arguments
            timeout: Call timeout
            
        Returns:
            Tool response
            
        Raises:
            MCPConnectionError: If not connected
            MCPServerError: If tool call fails
            MCPTimeoutError: If call times out
            MCPAuthenticationError: If authentication fails
        """
        if not self._initialized:
            raise MCPConnectionError("MCP client not initialized")
            
        if self._authentication_failed:
            raise MCPAuthenticationError("Previous authentication failure detected")
            
        logger.debug(f"🔧 Calling tool: {name}")
        logger.debug(f"Arguments: {arguments}")
        
        try:
            response = self._rpc("tools/call", {
                "name": name, 
                "arguments": arguments
            }, timeout)
            
            # Check response content for authentication errors
            content_blocks = response.get("result", {}).get("content", [])
            for block in content_blocks:
                if isinstance(block, dict) and "text" in block:
                    text = str(block["text"])
                    if detect_authentication_error(text):
                        self._authentication_failed = True
                        raise MCPAuthenticationError(
                            f"🔐 Authentication failed during tool call '{name}': {text}"
                        )
            
            return response
        
        except MCPAuthenticationError:
            # Mark authentication as failed for future calls
            self._authentication_failed = True
            raise

    def list_tools(self) -> Dict[str, Any]:
        """
        List available tools from the MCP server.
        
        Returns:
            Dict containing available tools
        """
        if not self._initialized:
            raise MCPConnectionError("MCP client not initialized")
            
        return self._rpc("tools/list")

    @property
    def has_authentication_failed(self) -> bool:
        """Check if authentication has failed."""
        return self._authentication_failed

    def __enter__(self):
        """Context manager entry."""
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit with cleanup."""
        self.close()


@contextmanager
def create_mcp_client(
    command: str,
    args: List[str],
    env: Optional[Dict[str, str]] = None,
    timeout: float = DEFAULT_TIMEOUT
):
    """
    Context manager for creating and managing MCP client lifecycle.
    
    Args:
        command: Command to run
        args: Command arguments  
        env: Additional environment variables
        timeout: Default timeout
        
    Yields:
        MCPClient: Initialized MCP client
    """
    client = MCPClient(command, args, env, timeout)
    try:
        client.start()
        yield client
    finally:
        client.close()