# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/plugins/framework/external/mcp/server/runtime_mtls.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0
Authors: Teryl Taylor

Runtime MCP server for external plugins with mTLS support.

This module provides a custom wrapper around chuk_mcp_runtime that enables
mutual TLS (mTLS) authentication by directly configuring uvicorn.Config
instead of using the default main_async() entry point.
"""

# Standard
import asyncio
import contextlib
import logging
import os
import ssl
from typing import Any, AsyncIterator, Dict, Optional

# Third-Party
import uvicorn
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route

# chuk_mcp_runtime imports
from chuk_mcp_runtime.common.mcp_tool_decorator import (
    TOOLS_REGISTRY,
    initialize_tool_registry,
)
from chuk_mcp_runtime.server.config_loader import find_project_root, load_config
from chuk_mcp_runtime.server.logging_config import configure_logging, get_logger
from chuk_mcp_runtime.server.server import MCPServer, AuthMiddleware
from chuk_mcp_runtime.server.server_registry import ServerRegistry
from chuk_mcp_runtime.session.native_session_management import create_mcp_session_manager
from chuk_mcp_runtime.tools import register_artifacts_tools, register_session_tools

# First-Party
from mcpgateway.plugins.framework import ExternalPluginServer
from mcpgateway.plugins.framework.external.mcp.server.runtime import (
    get_plugin_config,
    get_plugin_configs,
    prompt_post_fetch,
    prompt_pre_fetch,
    resource_post_fetch,
    resource_pre_fetch,
    tool_post_invoke,
    tool_pre_invoke,
)

load_dotenv()

logger = get_logger(__name__)

SERVER: Optional[ExternalPluginServer] = None


async def run_with_mtls(
    config_paths: Optional[list[str]] = None,
    default_config: Optional[Dict[str, Any]] = None,
) -> None:
    """Run the MCP runtime with mTLS support.

    This function provides direct control over uvicorn configuration to enable
    mutual TLS authentication. SSL/TLS settings are read from environment variables
    or the configuration file.

    Environment Variables:
        MCP_SSL_ENABLED: Enable SSL/TLS (true/false)
        MCP_SSL_KEYFILE: Path to server private key
        MCP_SSL_CERTFILE: Path to server certificate
        MCP_SSL_KEYFILE_PASSWORD: Optional password for encrypted key
        MCP_SSL_CA_CERTS: Path to CA bundle for verifying client certificates
        MCP_SSL_CERT_REQS: Certificate verification mode:
            - 0 (CERT_NONE): No client certificate required (default TLS)
            - 1 (CERT_OPTIONAL): Client certificate optional
            - 2 (CERT_REQUIRED): Client certificate required (mTLS)

    Args:
        config_paths: Optional list of configuration file paths.
        default_config: Optional default configuration dictionary.

    Raises:
        RuntimeError: If SSL is enabled but required certificate files are missing.
        Exception: If server initialization or startup fails.
    """
    global SERVER  # pylint: disable=global-statement

    # 1) Configuration and logging setup
    cfg = load_config(config_paths, default_config)
    configure_logging(cfg)
    project_root = find_project_root()
    logger.debug("Project root resolved to %s", project_root)

    # 2) Initialize external plugin server
    SERVER = ExternalPluginServer()
    if not await SERVER.initialize():
        raise RuntimeError("Failed to initialize external plugin server")

    # 3) Native session management initialization
    session_manager = create_mcp_session_manager(cfg)
    logger.info("Native session manager initialized for sandbox: %s", session_manager.sandbox_id)

    # 4) Optional component bootstrap
    if not os.getenv("NO_BOOTSTRAP"):
        await ServerRegistry(project_root, cfg).load_server_components()

    # 5) Tool registry initialization
    await initialize_tool_registry()

    # 6) Artifact management tools
    await register_artifacts_tools(cfg)
    logger.debug("Artifact tools registration completed")

    # 7) Session management tools
    session_cfg = cfg.copy()
    session_cfg.setdefault("session_tools", {})["session_manager"] = session_manager
    await register_session_tools(session_cfg)
    logger.debug("Session tools registration completed")

    # 8) Create MCP server instance
    mcp_server = MCPServer(cfg, tools_registry=TOOLS_REGISTRY)
    logger.debug("Local MCP server '%s' starting with native sessions", getattr(mcp_server, "server_name", "local"))

    # 9) Get server configuration
    server_mode = cfg.get("server", {}).get("type", "sse")
    if server_mode != "sse":
        raise ValueError(f"Only 'sse' server mode is supported with mTLS wrapper, got: {server_mode}")

    sse_config = cfg.get("sse", {})
    host = sse_config.get("host", "0.0.0.0")  # nosec B104 - Intentional binding for server as default
    port = sse_config.get("port", 8000)
    sse_path = sse_config.get("sse_path", "/sse")
    msg_path = sse_config.get("message_path", "/messages/")
    health_path = sse_config.get("health_path", "/health")

    # 10) Setup SSL/TLS configuration
    ssl_enabled = os.getenv("MCP_SSL_ENABLED", "false").lower() == "true"
    ssl_keyfile = os.getenv("MCP_SSL_KEYFILE")
    ssl_certfile = os.getenv("MCP_SSL_CERTFILE")
    ssl_keyfile_password = os.getenv("MCP_SSL_KEYFILE_PASSWORD")
    ssl_ca_certs = os.getenv("MCP_SSL_CA_CERTS")
    ssl_cert_reqs_str = os.getenv("MCP_SSL_CERT_REQS", "0")

    try:
        ssl_cert_reqs = int(ssl_cert_reqs_str)
    except ValueError:
        logger.warning("Invalid MCP_SSL_CERT_REQS value '%s', defaulting to 0 (CERT_NONE)", ssl_cert_reqs_str)
        ssl_cert_reqs = ssl.CERT_NONE

    # Validate SSL configuration
    if ssl_enabled:
        if not ssl_keyfile or not ssl_certfile:
            raise RuntimeError("MCP_SSL_ENABLED=true requires MCP_SSL_KEYFILE and MCP_SSL_CERTFILE to be set")

        if ssl_cert_reqs in (ssl.CERT_REQUIRED, ssl.CERT_OPTIONAL) and not ssl_ca_certs:
            raise RuntimeError(f"MCP_SSL_CERT_REQS={ssl_cert_reqs} requires MCP_SSL_CA_CERTS to be set for client certificate verification")

        cert_reqs_name = {0: "CERT_NONE", 1: "CERT_OPTIONAL", 2: "CERT_REQUIRED"}.get(ssl_cert_reqs, "UNKNOWN")
        logger.info("SSL/TLS enabled: cert_reqs=%s (%d), keyfile=%s, certfile=%s, ca_certs=%s", cert_reqs_name, ssl_cert_reqs, ssl_keyfile, ssl_certfile, ssl_ca_certs or "None")

        if ssl_cert_reqs == ssl.CERT_REQUIRED:
            logger.info("mTLS ENABLED - Client certificates are REQUIRED")
        elif ssl_cert_reqs == ssl.CERT_OPTIONAL:
            logger.info("mTLS OPTIONAL - Client certificates are optional")
        else:
            logger.info("Standard TLS - No client certificate verification")

    # 11) Create MCP server with SSE transport
    server = Server(mcp_server.server_name)

    # Register list_tools handler (from MCPServer)
    @server.list_tools()
    async def list_tools():
        """List available tools."""
        tools = []
        for tool_name, func in TOOLS_REGISTRY.items():
            if hasattr(func, "_mcp_tool"):
                tool_obj = func._mcp_tool
                if hasattr(tool_obj, "name") and hasattr(tool_obj, "description"):
                    tools.append(tool_obj)
        return tools

    # Register call_tool handler (delegates to MCPServer's implementation)
    @server.call_tool()
    async def call_tool(name: str, arguments: Dict[str, Any]):
        """Execute a tool."""
        # Reuse the MCPServer's call_tool logic by creating a temporary instance
        # and invoking its internal handler
        from mcp.types import TextContent

        try:
            # Use the existing mcp_server instance
            handler = mcp_server._MCPServer__call_tool if hasattr(mcp_server, "_MCPServer__call_tool") else None

            # Since we can't easily access the private handler, we'll replicate the logic
            # by importing and calling the tool directly
            resolved = name if name in TOOLS_REGISTRY else name
            if resolved not in TOOLS_REGISTRY:
                raise ValueError(f"Tool not found: {name}")

            func = TOOLS_REGISTRY[resolved]
            result = await func(**arguments)

            if isinstance(result, str):
                return [TextContent(type="text", text=result)]
            elif isinstance(result, dict):
                import json

                return [TextContent(type="text", text=json.dumps(result, indent=2))]
            else:
                return result
        except Exception as e:
            logger.error("Error in call_tool for '%s': %s", name, e)
            return [TextContent(type="text", text=f"Tool execution error: {str(e)}")]

    # 12) Create SSE transport
    transport = SseServerTransport(msg_path)
    opts = server.create_initialization_options()

    async def handle_sse(request: Request):
        """Handle SSE connections."""
        async with transport.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], opts)
        return Response()

    async def health(request: Request):
        """Health check endpoint."""
        return PlainTextResponse("OK")

    # 13) Create Starlette application
    app = Starlette(
        routes=[
            Route(sse_path, handle_sse, methods=["GET"]),
            Mount(msg_path, app=transport.handle_post_message),
            Route(health_path, health, methods=["GET"]),
        ],
        middleware=[
            Middleware(
                AuthMiddleware,
                auth=cfg.get("server", {}).get("auth"),
                health_path=health_path,
            )
        ],
    )

    # 14) Configure uvicorn with SSL/TLS settings
    uvicorn_config_kwargs = {
        "app": app,
        "host": host,
        "port": port,
        "log_level": "info",
    }

    if ssl_enabled:
        uvicorn_config_kwargs.update(
            {
                "ssl_keyfile": ssl_keyfile,
                "ssl_certfile": ssl_certfile,
                "ssl_keyfile_password": ssl_keyfile_password,
                "ssl_cert_reqs": ssl_cert_reqs,
                "ssl_ca_certs": ssl_ca_certs,
            }
        )

    uvicorn_cfg = uvicorn.Config(**uvicorn_config_kwargs)

    # 15) Start server
    protocol = "https" if ssl_enabled else "http"
    logger.info("Starting MCP (SSE) on %s://%s:%s with mTLS support", protocol, host, port)

    try:
        await uvicorn.Server(uvicorn_cfg).serve()
    finally:
        if SERVER:
            await SERVER.shutdown()


async def main_async(default_config: Optional[Dict[str, Any]] = None) -> None:
    """Async entry point for mTLS runtime.

    Args:
        default_config: Optional default configuration dictionary.
    """
    try:
        import sys

        # Parse command line arguments for config file
        argv = sys.argv[1:]
        cfg_path = (
            os.getenv("CHUK_MCP_CONFIG_PATH")
            or (argv[argv.index("-c") + 1] if "-c" in argv else None)
            or (argv[argv.index("--config") + 1] if "--config" in argv else None)
            or (argv[0] if argv else None)
        )

        await run_with_mtls(config_paths=[cfg_path] if cfg_path else None, default_config=default_config)
    except Exception as exc:
        logger.error("Error starting CHUK MCP server with mTLS: %s", exc, exc_info=True)
        import sys

        sys.exit(1)


def main(default_config: Optional[Dict[str, Any]] = None) -> None:
    """Main entry point for mTLS runtime.

    Args:
        default_config: Optional default configuration dictionary.
    """
    try:
        asyncio.run(main_async(default_config))
    except KeyboardInterrupt:
        logger.warning("Received Ctrl-C → shutting down")
    except Exception as exc:
        logger.error("Uncaught exception: %s", exc, exc_info=True)
        import sys

        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()