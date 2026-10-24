"""MCP server for DIgSILENT PowerFactory 2024.

The server (server.py) speaks MCP over stdio and delegates everything to a worker process
(worker.py) that hosts the PowerFactory engine in a single thread.
"""

__version__ = "0.1.0"
