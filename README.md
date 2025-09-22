# Background Command Execution MCP Server

A Model Context Protocol (MCP) server that provides tools for executing shell commands in the background and retrieving their results asynchronously. This server is designed for AI agents that need to run long-running or blocking commands without blocking the main thread.

## Features

- **Asynchronous Execution**: Execute shell commands in background threads
- **Result Retrieval**: Get command results asynchronously using reference IDs
- **Thread Safety**: Thread-safe process management with proper locking
- **Automatic Cleanup**: Automatic cleanup of completed processes after 1 hour
- **Process Monitoring**: Real-time monitoring of process status
- **Comprehensive Logging**: Detailed logging for debugging and monitoring
- **Error Handling**: Robust error handling with detailed error messages
- **Resource Management**: Configurable limits on concurrent processes

The server will start and listen for MCP connections. It provides the following tools:

### Available Tools

#### 1. Execute Command in Background

Execute a shell command in the background and get a reference ID.

**Parameters:**
- `cmd` (string): The shell command to execute

**Returns:**
```json
{
  "success": true,
  "ref_id": "uuid-string",
  "error": null
}
```

**Example:**
```python
# Execute a long-running command
result = await execute_command_background("python long_running_script.py")
```

#### 2. Get Background Command Result

Retrieve the result of a previously started background command.

**Parameters:**
- `ref_id` (string): Reference ID returned by execute_command_background

**Returns:**
```json
{
  "running": false,
  "cmd": "python long_running_script.py",
  "output": "Command output here...",
  "error": "Any error output...",
  "returncode": 0,
  "created_at": "2024-01-01T12:00:00",
  "finished_at": "2024-01-01T12:05:00"
}
```

**Example:**
```python
# Get the result of a background command
result = await get_command_background_result("uuid-string")
```

#### 3. List Background Processes

List all active background processes with their status.

**Returns:**
```json
{
  "success": true,
  "processes": [
    {
      "ref_id": "uuid-string",
      "cmd": "python script.py",
      "running": true,
      "created_at": "2024-01-01T12:00:00"
    }
  ],
  "count": 1
}
```

**Example:**
```python
# List all active processes
processes = await list_background_processes()
```

#### 4. Clean Up Background Process

Clean up a specific background process by its reference ID.

**Parameters:**
- `ref_id` (string): Reference ID of the process to clean up

**Returns:**
```json
{
  "success": true,
  "ref_id": "uuid-string",
  "message": "Process cleaned up successfully"
}
```

**Example:**
```python
# Clean up a specific process
result = await cleanup_background_process("uuid-string")
```
