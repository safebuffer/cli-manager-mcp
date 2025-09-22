#!/usr/bin/env python3
"""
Background Command Execution MCP Server

This MCP server provides tools for executing shell commands in the background
and retrieving their results asynchronously. It's designed for AI agents that
need to run long-running or blocking commands without blocking the main thread.

Features:
- Execute shell commands in background threads
- Retrieve command results asynchronously
- Thread-safe process management
- Automatic cleanup of completed processes
- Comprehensive error handling and logging

SECURITY WARNING:
This server executes shell commands directly and should ONLY used with trusted AI agents or Run in sandboxed/isolated environments

"""

import asyncio
import json
import logging
import sys
import subprocess
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from dataclasses import dataclass, asdict

from mcp.server import FastMCP

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class CommandResult:
    """
    Data class representing the result of a background command execution.
    
    This class encapsulates all the information about a completed command execution,
    including its output, error messages, return code, and completion timestamp.
    
    Attributes:
        output (str): The standard output from the command execution.
        error (str): The standard error output from the command execution.
        returncode (int): The exit code returned by the command (0 typically means success).
        finished_at (Optional[datetime]): Timestamp when the command completed execution.
    
    Security Note:
        The output and error fields may contain sensitive information from the executed
        command. Ensure proper handling and sanitization when logging or displaying results.
    """
    output: str
    error: str
    returncode: int
    finished_at: Optional[datetime] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert the CommandResult to a dictionary for JSON serialization.
        
        This method serializes the CommandResult instance to a dictionary format
        suitable for JSON encoding. The datetime field is converted to ISO format.
        
        Returns:
            Dict[str, Any]: Dictionary representation of the CommandResult with
                           datetime converted to ISO format string.
        
        Example:
            >>> result = CommandResult("Hello", "", 0, datetime.now())
            >>> result_dict = result.to_dict()
            >>> print(result_dict['finished_at'])  # ISO format datetime string
        """
        result = asdict(self)
        if self.finished_at:
            result['finished_at'] = self.finished_at.isoformat()
        return result


class BackgroundProcessRef:
    """
    Reference to a background process with thread-safe access to results.
    
    This class manages the lifecycle of a background process, including
    thread management, result collection, and cleanup. It provides a
    thread-safe interface for monitoring and controlling background processes.
    
    The class uses threading locks to ensure thread-safe access to process
    state and results, preventing race conditions in multi-threaded environments.
    
    """
    
    def __init__(self, process: subprocess.Popen, thread: threading.Thread, cmd: str):
        """
        Initialize a background process reference.
        
        Creates a new reference to track a background process with its associated
        thread and command. The reference provides thread-safe access to process
        state and results.
        
        Args:
            process (subprocess.Popen): The subprocess.Popen instance representing
                                      the running command.
            thread (threading.Thread): The thread that monitors the process execution.
            cmd (str): The command string that was executed (for logging/debugging).
        
        """
        self.process = process
        self.thread = thread
        self.cmd = cmd
        self._result: Optional[CommandResult] = None
        self._finished = threading.Event()
        self._created_at = datetime.now()
        self._lock = threading.Lock()
    
    def is_running(self) -> bool:
        """
        Check if the process is still running.
        
        This method performs a thread-safe check to determine if the background
        process is still active. It checks both the monitoring thread status
        and the actual process status.
        
        Returns:
            bool: True if the process is still running, False otherwise.
                 Returns False if either the monitoring thread has died or
                 the process has terminated.
        
        Thread Safety:
            This method is thread-safe and can be called from multiple threads
            without additional synchronization.
        """
        return self.thread.is_alive() and self.process.poll() is None
    
    def get_result(self) -> Optional[CommandResult]:
        """
        Get the command result if available.
        
        Retrieves the CommandResult object containing the output, error, and
        return code of the completed process. Returns None if the process
        is still running or if no result has been set yet.
        
        Returns:
            Optional[CommandResult]: The command result if the process has finished
                                   and results are available, None otherwise.
                
        Thread Safety:
            This method is thread-safe and uses internal locking to prevent
            race conditions when accessing the result.
        """
        with self._lock:
            return self._result
    
    def wait_for_completion(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for the process to complete.
        
        Blocks the calling thread until the background process completes or
        the specified timeout expires. This method uses a threading.Event
        to provide efficient waiting without busy polling.
        
        Args:
            timeout (Optional[float]): Maximum time to wait in seconds.
                                     If None, waits indefinitely until completion.
        
        Returns:
            bool: True if the process completed within the timeout period,
                 False if the timeout expired before completion.
        
        Thread Safety:
            This method is thread-safe and can be called from multiple threads.
            Multiple threads can wait on the same process completion.
        
        Example:
            >>> process_ref = execute_command("long_running_command")
            >>> if process_ref.wait_for_completion(timeout=30.0):
            ...     result = process_ref.get_result()
            ... else:
            ...     print("Process did not complete within 30 seconds")
        """
        return self._finished.wait(timeout)
    
    def _set_result(self, result: CommandResult) -> None:
        """
        Set the command result (internal use only).
        
        This method is used internally by the monitoring thread to store
        the command execution result and signal completion to waiting threads.
        
        Args:
            result (CommandResult): The command execution result to store.
        
        Thread Safety:
            This method is thread-safe and uses internal locking to prevent
            race conditions when setting the result and signaling completion.
        
        """
        with self._lock:
            self._result = result
            self._finished.set()
    
    def cleanup(self) -> None:
        """
        Clean up the process and thread resources.
        
        Attempts to gracefully terminate the background process and clean up
        associated resources. If the process doesn't respond to termination
        within 5 seconds, it will be forcefully killed.
        
        This method should be called when the process reference is no longer
        needed to prevent resource leaks and orphaned processes.
        
        Process Termination Strategy:
            1. First attempts graceful termination (SIGTERM)
            2. If process doesn't respond within 5 seconds, force kills (SIGKILL)
            3. Logs any errors during cleanup but doesn't raise exceptions
                
        Thread Safety:
            This method is not thread-safe and should only be called from
            a single thread or with proper synchronization.
        """
        try:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        except Exception as e:
            logger.warning(f"Error during process cleanup: {e}")


class BackgroundProcessManager:
    """
    Thread-safe manager for background processes.
    
    This class provides thread-safe operations for managing multiple
    background processes, including creation, monitoring, and cleanup.
    It maintains a registry of active processes and provides methods
    to execute commands, monitor their status, and clean up resources.
    
    The manager uses a background cleanup thread to automatically
    remove old completed processes to prevent memory leaks.
    
    Thread Safety:
        All public methods are thread-safe and can be called from
        multiple threads concurrently without additional synchronization.
    """
    
    def __init__(self, max_processes: int = 100):
        """
        Initialize the process manager.
        
        Creates a new BackgroundProcessManager with the specified maximum
        number of concurrent processes. Starts a background cleanup thread
        to automatically remove old completed processes.
        
        Args:
            max_processes (int): Maximum number of concurrent processes allowed.
                               Defaults to 100. This limit helps prevent resource
                               exhaustion attacks and system overload.
        
        Security Note:
            The max_processes limit is a security feature to prevent DoS attacks
            through process exhaustion. Choose an appropriate limit based on
            your system's capabilities and security requirements.
        """
        self._processes: Dict[str, BackgroundProcessRef] = {}
        self._lock = threading.RLock()
        self._max_processes = max_processes
        self._cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        self._cleanup_thread.start()
    
    def execute_command(self, cmd: Union[str, List[str]]) -> str:
        """
        Execute a command in the background.
        
        Starts a new background process to execute the specified command
        and returns a reference ID that can be used to monitor and retrieve
        the command's results.
        
        Args:
            cmd (Union[str, List[str]]): Command to execute. Can be either:
                - A string that will be executed through the shell
                - A list of arguments that will be executed directly
        
        Returns:
            str: Reference ID for the background process that can be used
                with other methods to monitor and retrieve results.
        
        Raises:
            RuntimeError: If maximum process limit is reached.
            ValueError: If command is empty or invalid.
            subprocess.SubprocessError: If process creation fails.
                    
        Example:
            >>> manager = BackgroundProcessManager()
            >>> ref_id = manager.execute_command("ls -la")
            >>> # Later...
            >>> status = manager.get_process_status(ref_id)
        """
        if not cmd:
            raise ValueError("Command cannot be empty")
        
        with self._lock:
            if len(self._processes) >= self._max_processes:
                raise RuntimeError(f"Maximum number of processes ({self._max_processes}) reached")
            
            ref_id = str(uuid.uuid4())
            
            try:
                # Determine if we should use shell
                shell = isinstance(cmd, str)
                
                # Start the process
                process = subprocess.Popen(
                    cmd,
                    shell=shell,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                
                # Create and start the monitoring thread
                thread = threading.Thread(
                    target=self._monitor_process,
                    args=(ref_id, process, cmd),
                    daemon=True
                )
                
                # Create the process reference
                process_ref = BackgroundProcessRef(process, thread, cmd)
                self._processes[ref_id] = process_ref
                
                # Start monitoring
                thread.start()
                
                logger.info(f"Started background process {ref_id}: {cmd}")
                return ref_id
                
            except Exception as e:
                logger.error(f"Failed to start background process: {e}")
                raise
    
    def get_process_status(self, ref_id: str) -> Dict[str, Any]:
        """
        Get the status of a background process.
        
        Retrieves comprehensive status information about a background process
        including whether it's running, the command being executed, and results
        if the process has completed.
        
        Args:
            ref_id (str): Reference ID of the process to query.
        
        Returns:
            Dict[str, Any]: Dictionary containing process status information:
                - running (bool): Whether the process is currently running
                - cmd (str): The command being executed
                - created_at (str): ISO timestamp when the process was created
                - output (str): Standard output (if completed)
                - error (str): Standard error output (if completed)
                - returncode (int): Process exit code (if completed)
                - finished_at (str): ISO timestamp when process completed (if completed)
                - error (str): Error message if process not found
        
        
        Thread Safety:
            This method is thread-safe and can be called concurrently.
        
        Example:
            >>> status = manager.get_process_status("process-123")
            >>> if status["running"]:
            ...     print("Process is still running")
            ... else:
            ...     print(f"Process completed with code {status['returncode']}")
        """
        with self._lock:
            process_ref = self._processes.get(ref_id)
            if not process_ref:
                return {
                    "error": "Process not found",
                    "running": False,
                    "cmd": None
                }
            
            is_running = process_ref.is_running()
            result = {
                "running": is_running,
                "cmd": process_ref.cmd,
                "created_at": process_ref._created_at.isoformat()
            }
            
            if not is_running:
                command_result = process_ref.get_result()
                if command_result:
                    result.update(command_result.to_dict())
                else:
                    result.update({
                        "output": "",
                        "error": "Process completed but result not available",
                        "returncode": -1
                    })
            
            return result
    
    def cleanup_process(self, ref_id: str) -> bool:
        """
        Clean up a specific process.
        
        Removes a process from the manager's registry and attempts to
        terminate it if it's still running. This method should be called
        when a process is no longer needed to free up resources.
        
        Args:
            ref_id (str): Reference ID of the process to clean up.
        
        Returns:
            bool: True if the process was found and cleaned up successfully,
                 False if the process was not found in the registry.
        
        
        Thread Safety:
            This method is thread-safe and can be called concurrently.
        
        Example:
            >>> success = manager.cleanup_process("process-123")
            >>> if success:
            ...     print("Process cleaned up successfully")
            ... else:
            ...     print("Process not found")
        """
        with self._lock:
            process_ref = self._processes.pop(ref_id, None)
            if process_ref:
                process_ref.cleanup()
                logger.info(f"Cleaned up process {ref_id}")
                return True
            return False
    
    def list_processes(self) -> List[Dict[str, Any]]:
        """
        List all active processes.
        
        Returns a list of all processes currently managed by this manager,
        including both running and completed processes. This method is useful
        for monitoring and debugging purposes.
        
        Returns:
            List[Dict[str, Any]]: List of dictionaries containing process information:
                - ref_id (str): Reference ID of the process
                - cmd (str): The command being executed
                - running (bool): Whether the process is currently running
                - created_at (str): ISO timestamp when the process was created
        
        
        Thread Safety:
            This method is thread-safe and can be called concurrently.
        
        Example:
            >>> processes = manager.list_processes()
            >>> for process in processes:
            ...     print(f"Process {process['ref_id']}: {process['cmd']} "
            ...           f"({'running' if process['running'] else 'completed'})")
        """
        with self._lock:
            return [
                {
                    "ref_id": ref_id,
                    "cmd": process_ref.cmd,
                    "running": process_ref.is_running(),
                    "created_at": process_ref._created_at.isoformat()
                }
                for ref_id, process_ref in self._processes.items()
            ]
    
    def _monitor_process(self, ref_id: str, process: subprocess.Popen, cmd: str) -> None:
        """
        Monitor a background process and collect its results.
        
        This method runs in a separate thread to monitor a background process
        and collect its output, error, and return code when it completes.
        The results are stored in the process reference for later retrieval.
        
        Args:
            ref_id (str): Reference ID of the process being monitored.
            process (subprocess.Popen): The subprocess to monitor.
            cmd (str): The command being executed (for logging purposes).
        
        
        Thread Safety:
            This method is designed to run in a separate thread and uses
            the process manager's lock when updating process state.
        
        Note:
            This is an internal method and should not be called directly.
            It's automatically started by execute_command().
        """
        try:
            # Wait for the process to complete
            stdout, stderr = process.communicate()
            
            # Create the result
            result = CommandResult(
                output=stdout or "",
                error=stderr or "",
                returncode=process.returncode,
                finished_at=datetime.now()
            )
            
            # Store the result
            with self._lock:
                if ref_id in self._processes:
                    self._processes[ref_id]._set_result(result)
                    logger.info(f"Process {ref_id} completed with return code {process.returncode}")
            
        except Exception as e:
            logger.error(f"Error monitoring process {ref_id}: {e}")
            # Create an error result
            error_result = CommandResult(
                output="",
                error=f"Process monitoring error: {str(e)}",
                returncode=-1,
                finished_at=datetime.now()
            )
            
            with self._lock:
                if ref_id in self._processes:
                    self._processes[ref_id]._set_result(error_result)
    
    def _cleanup_loop(self) -> None:
        """
        Background thread to clean up completed processes.
        
        This method runs continuously in a daemon thread to automatically
        clean up processes that have been completed for more than 1 hour.
        This prevents memory leaks and accumulation of old process data.
        
        The cleanup loop:
        1. Sleeps for 30 seconds between cleanup cycles
        2. Identifies processes that have completed and have results
        3. Removes processes that have been completed for more than 1 hour
        4. Logs any errors during cleanup
        
        
        Thread Safety:
            This method uses the process manager's lock when accessing
            the process registry to ensure thread safety.
        
        Note:
            This is an internal method that runs automatically in a
            daemon thread. It should not be called directly.
        """
        while True:
            try:
                time.sleep(30)  # Check every 30 seconds
                
                with self._lock:
                    completed_processes = [
                        ref_id for ref_id, process_ref in self._processes.items()
                        if not process_ref.is_running() and process_ref.get_result() is not None
                    ]
                    
                    # Clean up processes that have been completed for more than 1 hour
                    current_time = datetime.now()
                    for ref_id in completed_processes:
                        process_ref = self._processes[ref_id]
                        if (current_time - process_ref._created_at).total_seconds() > 3600:
                            self.cleanup_process(ref_id)
                            
            except Exception as e:
                logger.error(f"Error in cleanup loop: {e}")


# Global process manager instance
_process_manager = BackgroundProcessManager()

# Create FastMCP server instance
app = FastMCP("background-command-executor")


@app.tool(
    description="Execute a shell command in the background and retrieve its result later. "
                "This tool allows AI agents to run long-running or blocking commands asynchronously. "
                "Returns a reference ID for the background process, which can be used to query the result. ",
    title="Execute Command in Background",
    annotations={
        "cmd": "Shell command to execute (as a string or list of arguments). "
    }
)
async def execute_command_background(cmd: str) -> str:
    """
    Execute a shell command in the background and return a reference ID.

    This MCP tool function provides AI agents with the ability to execute
    shell commands asynchronously without blocking the main thread. The
    command is executed in a background process and a reference ID is
    returned for later result retrieval.

    Args:
        cmd (str): The shell command to execute as a string. This command
                  will be executed through the system shell.

    Returns:
        str: JSON string containing:
            - ref_id (str): Reference ID for the background process
            - success (bool): Boolean indicating if the command was started successfully
            - error (str): Error message if the command failed to start

    Example:
        >>> result = await execute_command_background("ls -la /tmp")
        >>> data = json.loads(result)
        >>> if data["success"]:
        ...     ref_id = data["ref_id"]
        ...     # Later retrieve results using get_command_background_result(ref_id)
    """
    try:
        ref_id = _process_manager.execute_command(cmd)
        return json.dumps({
            "success": True,
            "ref_id": ref_id,
            "error": None
        }, indent=2)
    except Exception as e:
        logger.error(f"Failed to execute background command: {e}")
        return json.dumps({
            "success": False,
            "ref_id": None,
            "error": f"Failed to start background command: {str(e)}"
        }, indent=2)


@app.tool(
    description="Get the result of a previously started background command using its reference ID. "
                "Returns the command, running status, and output/error if finished. ",
    title="Get Background Command Result",
    annotations={
        "ref_id": "Reference ID returned by execute_command_background. "
    }
)
async def get_command_background_result(ref_id: str) -> str:
    """
    Retrieve the result of a background command by its reference ID.

    This MCP tool function allows AI agents to retrieve the results of
    previously executed background commands. It returns comprehensive
    information about the command execution including output, errors,
    and completion status.

    Args:
        ref_id (str): The reference ID of the background process returned
                     by execute_command_background.

    Returns:
        str: JSON string containing:
            - running (bool): Whether the process is still running
            - cmd (str): The command that was executed
            - created_at (str): ISO timestamp when the process was created
            - output (str): Standard output from the command (if finished)
            - error (str): Standard error output from the command (if finished)
            - returncode (int): Process exit code (if finished)
            - finished_at (str): ISO timestamp when process completed (if finished)
            - error (str): Error message if process not found or query failed

    Example:
        >>> result = await get_command_background_result("process-123")
        >>> data = json.loads(result)
        >>> if not data["running"]:
        ...     print(f"Command completed with code {data['returncode']}")
        ...     print(f"Output: {data['output']}")
    """
    try:
        result = _process_manager.get_process_status(ref_id)
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Failed to get background command result: {e}")
        return json.dumps({
            "running": False,
            "cmd": None,
            "output": "",
            "error": f"Failed to get background command result: {str(e)}",
            "returncode": None
        }, indent=2)


@app.tool(
    description="List all active background processes with their status and creation time. ",
    title="List Background Processes",
    annotations={}
)
async def list_background_processes() -> str:
    """
    List all active background processes.

    This MCP tool function provides AI agents with the ability to list all
    currently managed background processes, including both running and
    completed processes. This is useful for monitoring and debugging purposes.

    Returns:
        str: JSON string containing:
            - success (bool): Whether the operation completed successfully
            - processes (List[Dict]): List of process information dictionaries:
                - ref_id (str): Reference ID of the process
                - cmd (str): The command being executed
                - running (bool): Whether the process is currently running
                - created_at (str): ISO timestamp when the process was created
            - count (int): Total number of processes
            - error (str): Error message if the operation failed

    Example:
        >>> result = await list_background_processes()
        >>> data = json.loads(result)
        >>> if data["success"]:
        ...     for process in data["processes"]:
        ...         print(f"Process {process['ref_id']}: {process['cmd']}")
    """
    try:
        processes = _process_manager.list_processes()
        return json.dumps({
            "success": True,
            "processes": processes,
            "count": len(processes)
        }, indent=2)
    except Exception as e:
        logger.error(f"Failed to list background processes: {e}")
        return json.dumps({
            "success": False,
            "processes": [],
            "count": 0,
            "error": f"Failed to list processes: {str(e)}"
        }, indent=2)


@app.tool(
    description="Clean up a specific background process by its reference ID. ",
    title="Clean Up Background Process",
    annotations={
        "ref_id": "Reference ID of the process to clean up. "
    }
)
async def cleanup_background_process(ref_id: str) -> str:
    """
    Clean up a specific background process.

    This MCP tool function allows AI agents to clean up specific background
    processes by their reference ID. This removes the process from the
    manager's registry and attempts to terminate it if it's still running.

    Args:
        ref_id (str): The reference ID of the process to clean up.

    Returns:
        str: JSON string containing:
            - success (bool): Whether the cleanup operation was successful
            - ref_id (str): The reference ID that was cleaned up
            - message (str): Human-readable message about the cleanup result
            - error (str): Error message if the cleanup failed

    Example:
        >>> result = await cleanup_background_process("process-123")
        >>> data = json.loads(result)
        >>> if data["success"]:
        ...     print("Process cleaned up successfully")
        ... else:
        ...     print(f"Cleanup failed: {data['error']}")
    """
    try:
        success = _process_manager.cleanup_process(ref_id)
        return json.dumps({
            "success": success,
            "ref_id": ref_id,
            "message": "Process cleaned up successfully" if success else "Process not found"
        }, indent=2)
    except Exception as e:
        logger.error(f"Failed to cleanup background process: {e}")
        return json.dumps({
            "success": False,
            "ref_id": ref_id,
            "error": f"Failed to cleanup process: {str(e)}"
        }, indent=2)


async def main():
    """
    Main function to run the background command execution MCP server.
    
    This is the primary entry point for the MCP server that provides AI agents
    with the ability to execute shell commands in the background and retrieve
    their results asynchronously. The server enables non-blocking execution
    of long-running or blocking operations.
    
    The server provides the following MCP tools:
    - execute_command_background: Execute shell commands asynchronously
    - get_command_background_result: Retrieve command execution results
    - list_background_processes: List all managed processes
    - cleanup_background_process: Clean up specific processes
    
    Error Handling:
        The server includes comprehensive error handling and logging
        for debugging and monitoring purposes.
    
    Note:
        This function runs the MCP server using the FastMCP framework
        and handles graceful shutdown on KeyboardInterrupt.
    """
    try:
        logger.info("Main Starting background command execution MCP server")
        # Use the MCP server's run method
        await app.run()
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, shutting down gracefully")
        # Clean up all processes
        for ref_id in list(_process_manager._processes.keys()):
            _process_manager.cleanup_process(ref_id)
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)


def run_server_standalone():
    """
    Run the server as a standalone process.
    
    This function provides a simple way to run the MCP server as a standalone
    process. It handles the asyncio event loop setup and provides proper
    error handling and cleanup on shutdown.
    
    This is the recommended way to run the server when not integrating
    with other asyncio applications.
        
    Error Handling:
        - Handles KeyboardInterrupt for graceful shutdown
        - Cleans up all processes on shutdown
        - Logs errors and exits with appropriate codes
    
    Example:
        >>> if __name__ == "__main__":
        ...     run_server_standalone()
    """
    try:
        logger.info("Starting background command execution MCP server")
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, shutting down gracefully")
        # Clean up all processes
        for ref_id in list(_process_manager._processes.keys()):
            _process_manager.cleanup_process(ref_id)
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)


def run_server_simple():
    """
    Simple server runner that works in any asyncio context.
    
    This function provides a flexible way to run the MCP server that can
    work both in existing asyncio event loops and as a standalone process.
    It automatically detects if an event loop is already running and
    adapts accordingly.
    
    This is useful when integrating the server with other asyncio applications
    or when you need the server to run in the background of an existing loop.
    
    Behavior:
        - If no event loop is running: Creates a new loop and runs the server
        - If an event loop is already running: Creates a task in the existing loop
        - Handles both scenarios gracefully without conflicts
    
    Error Handling:
        - Handles KeyboardInterrupt for graceful shutdown
        - Cleans up all processes on shutdown
        - Logs warnings when running in existing loops
    
    Example:
        >>> # In an existing asyncio application
        >>> run_server_simple()  # Will run in background
    """
    try:
        logger.info("Starting background command execution MCP server")
        
        # Check if we're already in an event loop
        try:
            loop = asyncio.get_running_loop()
            # If we're already in a loop, we can't use asyncio.run()
            logger.warning("Already running in an asyncio loop, server will run in background")
            
            # Create a task in the existing loop and let it run
            task = loop.create_task(main())
            
            # Don't wait for completion, just return
            # The server will run in the background
            return task
            
        except RuntimeError:
            # No running loop, we can use asyncio.run directly
            asyncio.run(main())
            
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, shutting down gracefully")
        # Clean up all processes
        for ref_id in list(_process_manager._processes.keys()):
            _process_manager.cleanup_process(ref_id)
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)


def run_server_direct():
    """
    Run the server directly without asyncio.run() to avoid conflicts.
    
    This function provides a direct way to run the MCP server that avoids
    the common asyncio.run() conflicts when an event loop is already running.
    It uses run_until_complete() instead of asyncio.run() for better
    compatibility with existing asyncio applications.
    
    This is useful when you need more control over the event loop or when
    integrating with applications that already have an event loop running.
    
    Behavior:
        - If no event loop is running: Creates a new loop and runs the server
        - If an event loop is already running: Uses run_until_complete() on the existing loop
        - Waits for the server task to complete
    
    Error Handling:
        - Handles KeyboardInterrupt for graceful shutdown
        - Cleans up all processes on shutdown
        - Logs warnings when running in existing loops
        - Cancels tasks on interruption
    
    Example:
        >>> # When you need direct control over the event loop
        >>> run_server_direct()
    """
    try:
        logger.info("Starting background command execution MCP server")
        
        # Check if we're already in an event loop
        try:
            loop = asyncio.get_running_loop()
            # If we're already in a loop, we can't use asyncio.run()
            logger.warning("Already running in an asyncio loop, using existing loop")
            
            # Create a task in the existing loop
            task = loop.create_task(main())
            
            # Wait for the task to complete
            try:
                loop.run_until_complete(task)
            except KeyboardInterrupt:
                logger.info("Received KeyboardInterrupt, shutting down gracefully")
                task.cancel()
                # Clean up all processes
                for ref_id in list(_process_manager._processes.keys()):
                    _process_manager.cleanup_process(ref_id)
                    
        except RuntimeError:
            # No running loop, we can use asyncio.run directly
            asyncio.run(main())
            
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, shutting down gracefully")
        # Clean up all processes
        for ref_id in list(_process_manager._processes.keys()):
            _process_manager.cleanup_process(ref_id)
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)


def main_sync():
    """
    Synchronous version of main that handles asyncio context properly.
    
    This function provides a synchronous interface to run the MCP server
    that properly handles asyncio context detection and thread management.
    It's designed to work in environments where asyncio event loops may
    already be running or where you need synchronous control.
    
    The function uses threading to run the server in a separate thread
    with its own event loop when an existing loop is detected, preventing
    conflicts and ensuring proper isolation.
    
    Behavior:
        - If no event loop is running: Creates a new loop and runs the server
        - If an event loop is already running: Runs the server in a separate thread
        - Uses a queue to communicate results between threads
        - Waits for the server thread to complete
        
    Error Handling:
        - Handles KeyboardInterrupt for graceful shutdown
        - Cleans up all processes on shutdown
        - Uses thread-safe communication via queue
        - Exits with appropriate codes based on thread results
    
    Thread Safety:
        - Uses separate threads to avoid asyncio conflicts
        - Thread-safe communication via queue
        - Proper thread cleanup on completion
    
    Example:
        >>> # When you need synchronous control and thread isolation
        >>> main_sync()
    """
    try:
        logger.info("Starting background command execution MCP server")
        
        # Check if we're already in an event loop
        try:
            loop = asyncio.get_running_loop()
            # If we get here, we're already in a loop, so we need to run in a new thread
            logger.warning("Already running in an asyncio loop, running server in new thread")
            import threading
            import queue
            
            result_queue = queue.Queue()
            
            def run_in_thread():
                try:
                    # Create a new event loop in the thread
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    try:
                        new_loop.run_until_complete(main())
                        result_queue.put(0)
                    finally:
                        new_loop.close()
                except Exception as e:
                    result_queue.put(1)
                    logger.error(f"Server thread error: {e}")
            
            thread = threading.Thread(target=run_in_thread, daemon=True)
            thread.start()
            thread.join()
            
            exit_code = result_queue.get_nowait()
            if exit_code != 0:
                sys.exit(exit_code)
                
        except RuntimeError:
            # No running loop, we can use asyncio.run directly
            asyncio.run(main())
            
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, shutting down gracefully")
        # Clean up all processes
        for ref_id in list(_process_manager._processes.keys()):
            _process_manager.cleanup_process(ref_id)
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)


def run_server():
    """
    Run the server in a way that handles existing asyncio loops.
    
    This is the main entry point function that provides the most robust
    way to run the MCP server. It automatically handles various asyncio
    contexts and provides the best compatibility with different environments.
    
    This function is designed to work in:
    - Standalone applications
    - Applications with existing asyncio loops
    - Multi-threaded environments
    - Various deployment scenarios
    
    The function uses run_server_direct() internally, which provides
    the most comprehensive asyncio context handling.
        
    Error Handling:
        - Comprehensive error handling and logging
        - Graceful shutdown on KeyboardInterrupt
        - Process cleanup on all exit paths
        - Appropriate exit codes for different failure modes
    
    Usage:
        This is the recommended way to run the server in most cases.
        It provides the best balance of functionality and compatibility.
    
    Example:
        >>> # Recommended usage
        >>> run_server()
    """
    run_server_direct()


if __name__ == "__main__":
    try:
        # Set Windows event loop policy to prevent pipe warnings
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
        # Suppress asyncio warnings
        logging.getLogger('asyncio').setLevel(logging.WARNING)
        
        # Run the background command execution MCP server
        run_server_standalone()
            
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, shutting down gracefully")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)