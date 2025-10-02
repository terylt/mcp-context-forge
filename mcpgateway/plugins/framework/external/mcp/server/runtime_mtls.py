# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/plugins/framework/external/mcp/server/runtime_mtls.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0
Authors: Teryl Taylor

Runtime MCP server for external plugins with mTLS support.

This module extends MCPServer from chuk_mcp_runtime to add SSL/TLS support
by reading UVICORN_SSL_* environment variables and injecting them into the
uvicorn.Config creation.
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
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.types import Scope, Receive, Send

# chuk_mcp_runtime imports
from chuk_mcp_runtime.common.mcp_tool_decorator import initialize_tool_registry
from chuk_mcp_runtime.server.config_loader import find_project_root, load_config
from chuk_mcp_runtime.server.event_store import InMemoryEventStore
from chuk_mcp_runtime.server.logging_config import configure_logging, get_logger
from chuk_mcp_runtime.server.server import MCPServer, AuthMiddleware
from chuk_mcp_runtime.server.server_registry import ServerRegistry
from chuk_mcp_runtime.session.native_session_management import create_mcp_session_manager
from chuk_mcp_runtime.tools import register_artifacts_tools, register_session_tools

# First-Party
from mcpgateway.plugins.framework import ExternalPluginServer

load_dotenv()

logger = get_logger(__name__)

SERVER: Optional[ExternalPluginServer] = None


class SSLCapableMCPServer(MCPServer):
    """MCPServer with SSL/TLS support via UVICORN_SSL_* environment variables.

    This class extends the standard MCPServer to support SSL/TLS configuration
    through environment variables, enabling mTLS for external plugin servers.

    Environment Variables:
        UVICORN_SSL_KEYFILE: Path to server private key
        UVICORN_SSL_CERTFILE: Path to server certificate
        UVICORN_SSL_KEYFILE_PASSWORD: Optional password for encrypted key
        UVICORN_SSL_CA_CERTS: Path to CA bundle for verifying client certificates
        UVICORN_SSL_CERT_REQS: Certificate verification mode:
            - 0 (CERT_NONE): No client certificate required (default TLS)
            - 1 (CERT_OPTIONAL): Client certificate optional
            - 2 (CERT_REQUIRED): Client certificate required (mTLS)
    """

    def _get_ssl_config(self) -> Dict[str, Any]:
        """Read SSL configuration from UVICORN_SSL_* environment variables.

        Returns:
            Dictionary of SSL configuration parameters for uvicorn.Config

        Raises:
            RuntimeError: If SSL is enabled but required files are missing
        """
        ssl_config = {}

        ssl_keyfile = os.getenv("UVICORN_SSL_KEYFILE")
        ssl_certfile = os.getenv("UVICORN_SSL_CERTFILE")

        if not ssl_keyfile and not ssl_certfile:
            return ssl_config

        # SSL is enabled
        if not ssl_keyfile or not ssl_certfile:
            raise RuntimeError(
                "Both UVICORN_SSL_KEYFILE and UVICORN_SSL_CERTFILE must be set "
                "when enabling SSL/TLS"
            )

        ssl_config["ssl_keyfile"] = ssl_keyfile
        ssl_config["ssl_certfile"] = ssl_certfile

        # Optional password for encrypted key
        if os.getenv("UVICORN_SSL_KEYFILE_PASSWORD"):
            ssl_config["ssl_keyfile_password"] = os.getenv("UVICORN_SSL_KEYFILE_PASSWORD")

        # Client certificate verification
        ssl_cert_reqs_str = os.getenv("UVICORN_SSL_CERT_REQS", "0")
        try:
            ssl_cert_reqs = int(ssl_cert_reqs_str)
        except ValueError:
            logger.warning(
                "Invalid UVICORN_SSL_CERT_REQS value '%s', defaulting to 0 (CERT_NONE)",
                ssl_cert_reqs_str
            )
            ssl_cert_reqs = ssl.CERT_NONE

        ssl_config["ssl_cert_reqs"] = ssl_cert_reqs

        # CA certificates for client verification
        ssl_ca_certs = os.getenv("UVICORN_SSL_CA_CERTS")
        if ssl_cert_reqs in (ssl.CERT_REQUIRED, ssl.CERT_OPTIONAL):
            if not ssl_ca_certs:
                raise RuntimeError(
                    f"UVICORN_SSL_CERT_REQS={ssl_cert_reqs} requires UVICORN_SSL_CA_CERTS "
                    "to be set for client certificate verification"
                )

        if ssl_ca_certs:
            ssl_config["ssl_ca_certs"] = ssl_ca_certs

        # Log SSL configuration
        cert_reqs_name = {
            0: "CERT_NONE",
            1: "CERT_OPTIONAL",
            2: "CERT_REQUIRED"
        }.get(ssl_cert_reqs, "UNKNOWN")

        logger.info(
            "SSL/TLS enabled: cert_reqs=%s (%d), keyfile=%s, certfile=%s, ca_certs=%s",
            cert_reqs_name,
            ssl_cert_reqs,
            ssl_keyfile,
            ssl_certfile,
            ssl_ca_certs or "None"
        )

        if ssl_cert_reqs == ssl.CERT_REQUIRED:
            logger.info("mTLS ENABLED - Client certificates are REQUIRED")
        elif ssl_cert_reqs == ssl.CERT_OPTIONAL:
            logger.info("mTLS OPTIONAL - Client certificates are optional")
        else:
            logger.info("Standard TLS - No client certificate verification")

        return ssl_config

    async def serve(self, custom_handlers: Optional[Dict[str, Any]] = None) -> None:
        """Boot the MCP server with SSL/TLS support and serve forever.

        This overrides the parent's serve() method to inject SSL configuration
        into uvicorn.Config creation. All other functionality (tool registration,
        session management, etc.) is inherited from the parent MCPServer.

        Args:
            custom_handlers: Optional custom handlers (e.g., proxy text handler)
        """
        # Get SSL configuration from environment variables
        ssl_config = self._get_ssl_config()
        is_ssl_enabled = bool(ssl_config)

        # Reuse parent's setup for everything except uvicorn Config
        await self._setup_artifact_store()

        if not self.tools_registry:
            self.tools_registry = await self._import_tools_registry()

        await initialize_tool_registry()
        from chuk_mcp_runtime.common.tool_naming import update_naming_maps
        update_naming_maps()

        # Create server and register handlers (uses parent's implementation)
        from mcp.server import Server
        from mcp.server.sse import SseServerTransport
        from mcp.types import TextContent, ImageContent, EmbeddedResource
        import json
        from inspect import isasyncgen

        server = Server(self.server_name)

        # Register list_tools (from parent's implementation)
        @server.list_tools()
        async def list_tools():
            """List available tools with robust error handling."""
            try:
                self.logger.info(
                    "list_tools called - %d tools total", len(self.tools_registry)
                )

                tools = []
                for tool_name, func in self.tools_registry.items():
                    try:
                        if hasattr(func, "_mcp_tool"):
                            tool_obj = func._mcp_tool
                            if hasattr(tool_obj, "name") and hasattr(tool_obj, "description"):
                                tools.append(tool_obj)
                                self.logger.debug("Added tool to list: %s", tool_obj.name)
                            else:
                                self.logger.warning(
                                    "Tool %s has invalid _mcp_tool object: %s",
                                    tool_name, tool_obj
                                )
                        else:
                            self.logger.warning(
                                "Tool %s missing _mcp_tool attribute", tool_name
                            )
                    except Exception as e:
                        self.logger.error("Error processing tool %s: %s", tool_name, e)
                        continue

                self.logger.info("Returning %d valid tools", len(tools))
                return tools
            except Exception as e:
                self.logger.error("Error in list_tools: %s", e)
                return []

        # Register call_tool (from parent's implementation)
        @server.call_tool()
        async def call_tool(name: str, arguments: Dict[str, Any]):
            """Execute a tool with native session management."""
            try:
                from chuk_mcp_runtime.server.server import parse_tool_arguments
                from chuk_mcp_runtime.common.tool_naming import resolve_tool_name
                from chuk_mcp_runtime.session.native_session_management import SessionContext

                # Fix concatenated JSON in arguments
                original_args = arguments
                if arguments:
                    if isinstance(arguments, (str, dict)):
                        arguments = parse_tool_arguments(arguments)
                        if arguments != original_args:
                            self.logger.info(
                                "Fixed concatenated JSON arguments for '%s': %s -> %s",
                                name, original_args, arguments
                            )

                self.logger.debug(
                    "call_tool called with name='%s', arguments=%s", name, arguments
                )

                # Tool name resolution
                resolved = name if name in self.tools_registry else resolve_tool_name(name)
                if resolved not in self.tools_registry:
                    matches = [
                        k for k in self.tools_registry
                        if k.endswith(f"_{name}") or k.endswith(f".{name}")
                    ]
                    if len(matches) == 1:
                        resolved = matches[0]

                if resolved not in self.tools_registry:
                    raise ValueError(f"Tool not found: {name}")

                func = self.tools_registry[resolved]
                self.logger.debug("Resolved tool '%s' to function: %s", name, func)

                # Native session injection
                arguments = await self._inject_session_context(resolved, arguments)

                # Execute within session context
                async with SessionContext(
                    self.session_manager,
                    session_id=arguments.get("session_id"),
                    auto_create=True
                ) as session_id:
                    self.logger.debug(
                        "Executing tool '%s' in session %s", resolved, session_id
                    )
                    result = await self._execute_tool_with_timeout(func, resolved, arguments)
                    self.logger.debug(
                        "Tool execution completed, result type: %s", type(result)
                    )

                    # Handle streaming results
                    if isasyncgen(result):
                        self.logger.debug(
                            "Tool returned async generator, collecting chunks for '%s'",
                            resolved
                        )
                        collected_chunks = []
                        chunk_count = 0

                        try:
                            async for part in result:
                                chunk_count += 1
                                self.logger.debug(
                                    "Collecting streaming chunk %d for '%s'",
                                    chunk_count, resolved
                                )

                                if isinstance(part, (TextContent, ImageContent, EmbeddedResource)):
                                    collected_chunks.append(part)
                                elif isinstance(part, str):
                                    collected_chunks.append(TextContent(type="text", text=part))
                                elif isinstance(part, dict) and "delta" in part:
                                    collected_chunks.append(
                                        TextContent(type="text", text=part["delta"])
                                    )
                                else:
                                    collected_chunks.append(
                                        TextContent(type="text", text=str(part))
                                    )
                        except Exception as e:
                            self.logger.error(
                                "Error collecting streaming chunks for '%s': %s",
                                resolved, e
                            )
                            return [
                                TextContent(
                                    type="text",
                                    text=f"Streaming error: {str(e)}"
                                )
                            ]

                        self.logger.info(
                            "Collected %d streaming chunks for '%s'",
                            len(collected_chunks), resolved
                        )
                        result = collected_chunks

                    # Wrap result if needed
                    if not isinstance(result, dict) or "session_id" not in result:
                        if isinstance(result, dict) and not (
                            "content" in result and "isError" in result
                        ):
                            result = {
                                "session_id": session_id,
                                "content": result,
                                "isError": False
                            }
                        elif isinstance(result, str):
                            result = {
                                "session_id": session_id,
                                "content": result,
                                "isError": False
                            }

                    # Format response
                    if isinstance(result, list) and all(
                        isinstance(r, (TextContent, ImageContent, EmbeddedResource))
                        for r in result
                    ):
                        return result
                    elif isinstance(result, str):
                        return [TextContent(type="text", text=result)]
                    else:
                        return [TextContent(type="text", text=json.dumps(result, indent=2))]

            except Exception as e:
                self.logger.error("Error in call_tool for '%s': %s", name, e)
                return [TextContent(type="text", text=f"Tool execution error: {str(e)}")]

        # Transport setup with SSL support
        opts = server.create_initialization_options()
        mode = self.config.get("server", {}).get("type", "stdio")

        if mode == "stdio":
            self.logger.info(
                "Starting MCP (stdio) - global timeout %.1fs", self.tool_timeout
            )
            from mcp.server.stdio import stdio_server
            async with stdio_server() as (r, w):
                await server.run(r, w, opts)

        elif mode == "sse":
            cfg = self.config.get("sse", {})
            host, port = cfg.get("host", "0.0.0.0"), cfg.get("port", 8000)  # nosec B104
            sse_path, msg_path, health_path = (
                cfg.get("sse_path", "/sse"),
                cfg.get("message_path", "/messages/"),
                cfg.get("health_path", "/health")
            )
            transport = SseServerTransport(msg_path)

            async def _handle_sse(request: Request):
                async with transport.connect_sse(
                    request.scope, request.receive, request._send
                ) as streams:
                    await server.run(streams[0], streams[1], opts)
                return Response()

            async def health(request: Request):
                return PlainTextResponse("OK")

            app = Starlette(
                routes=[
                    Route(sse_path, _handle_sse, methods=["GET"]),
                    Mount(msg_path, app=transport.handle_post_message),
                    Route(health_path, health, methods=["GET"])
                ],
                middleware=[
                    Middleware(
                        AuthMiddleware,
                        auth=self.config.get("server", {}).get("auth"),
                        health_path=health_path
                    )
                ]
            )

            protocol = "https" if is_ssl_enabled else "http"
            self.logger.info(
                "Starting MCP (SSE) on %s://%s:%s - global timeout %.1fs",
                protocol, host, port, self.tool_timeout
            )

            # Create uvicorn Config with SSL support
            config_kwargs = {"app": app, "host": host, "port": port, "log_level": "info"}
            config_kwargs.update(ssl_config)

            await uvicorn.Server(uvicorn.Config(**config_kwargs)).serve()

        elif mode == "streamable-http":
            self.logger.info("Starting MCP server over streamable-http")

            streamhttp_config = self.config.get("streamable-http", {})
            host = streamhttp_config.get("host", "127.0.0.1")
            port = streamhttp_config.get("port", 3000)
            mcp_path = streamhttp_config.get("mcp_path", "/mcp")
            json_response = streamhttp_config.get("json_response", True)
            stateless = streamhttp_config.get("stateless", True)

            event_store = None if stateless else InMemoryEventStore()

            session_manager = StreamableHTTPSessionManager(
                app=server,
                event_store=event_store,
                stateless=stateless,
                json_response=json_response
            )

            async def handle_streamable_http(scope: Scope, receive: Receive, send: Send):
                await session_manager.handle_request(scope, receive, send)

            async def health(request: Request):
                return PlainTextResponse("OK")

            @contextlib.asynccontextmanager
            async def lifespan(app: Starlette) -> AsyncIterator[None]:
                async with session_manager.run():
                    self.logger.info("Application started with StreamableHTTP session manager!")
                    try:
                        yield
                    finally:
                        self.logger.info("Application shutting down...")

            app = Starlette(
                debug=True,
                routes=[
                    Mount(mcp_path, handle_streamable_http),
                    Route("/health", health, methods=["GET"])
                ],
                middleware=[
                    Middleware(
                        AuthMiddleware,
                        auth=self.config.get("server", {}).get("auth")
                    )
                ],
                lifespan=lifespan
            )

            protocol = "https" if is_ssl_enabled else "http"
            self.logger.info(
                "Starting MCP (StreamableHTTP) on %s://%s:%s - global timeout %.1fs",
                protocol, host, port, self.tool_timeout
            )

            # Create uvicorn Config with SSL support
            config_kwargs = {"app": app, "host": host, "port": port, "log_level": "info"}
            config_kwargs.update(ssl_config)

            await uvicorn.Server(uvicorn.Config(**config_kwargs)).serve()

        else:
            raise ValueError(f"Unsupported server mode: {mode}")


async def run_with_mtls(
    config_paths: Optional[list[str]] = None,
    default_config: Optional[Dict[str, Any]] = None,
) -> None:
    """Run the MCP runtime with mTLS support.

    Args:
        config_paths: Optional list of configuration file paths.
        default_config: Optional default configuration dictionary.

    Raises:
        RuntimeError: If server initialization fails.
    """
    global SERVER  # pylint: disable=global-statement

    # Configuration and logging setup
    cfg = load_config(config_paths, default_config)
    configure_logging(cfg)
    project_root = find_project_root()
    logger.debug("Project root resolved to %s", project_root)

    # Initialize external plugin server
    SERVER = ExternalPluginServer()
    if not await SERVER.initialize():
        raise RuntimeError("Failed to initialize external plugin server")

    # Native session management initialization
    session_manager = create_mcp_session_manager(cfg)
    logger.info("Native session manager initialized for sandbox: %s", session_manager.sandbox_id)

    # Optional component bootstrap
    if not os.getenv("NO_BOOTSTRAP"):
        await ServerRegistry(project_root, cfg).load_server_components()

    # Tool registry initialization
    await initialize_tool_registry()

    # Artifact management tools
    await register_artifacts_tools(cfg)
    logger.debug("Artifact tools registration completed")

    # Session management tools
    session_cfg = cfg.copy()
    session_cfg.setdefault("session_tools", {})["session_manager"] = session_manager
    await register_session_tools(session_cfg)
    logger.debug("Session tools registration completed")

    # Create SSL-capable MCP server instance
    from chuk_mcp_runtime.common.mcp_tool_decorator import TOOLS_REGISTRY
    mcp_server = SSLCapableMCPServer(cfg, tools_registry=TOOLS_REGISTRY)
    logger.debug("SSL-capable MCP server '%s' starting", getattr(mcp_server, "server_name", "local"))

    # Serve forever
    try:
        await mcp_server.serve()
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
