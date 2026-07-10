#!/usr/bin/env python3
import os
import sys

script_dir = os.path.dirname(os.path.abspath(__file__))
os.chdir(script_dir)
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

from src.mcp_bridge import create_mcp_server

mcp = create_mcp_server()
mcp.run(transport="stdio")
