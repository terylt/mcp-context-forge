"""MCP Gateway CmdPreProcess Plugin - A linux command preprocessing plugin for OPA policies..

Copyright 2025
SPDX-License-Identifier: Apache-2.0
Authors: Teryl Taylor

"""

import importlib.metadata

# Package version
try:
    __version__ = importlib.metadata.version("cmdpreprocess")
except Exception:
    __version__ = "0.1.0"

__author__ = "Teryl Taylor"
__copyright__ = "Copyright 2025"
__license__ = "Apache 2.0"
__description__ = "A linux command preprocessing plugin for OPA policies."
__url__ = "https://ibm.github.io/mcp-context-forge/"
__download_url__ = "https://github.com/IBM/mcp-context-forge"
__packages__ = ["cmdpreprocess"]
