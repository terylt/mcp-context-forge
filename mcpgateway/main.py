# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/main.py
Copyright 2025
SPDX-License-Identifier: Apache-2.0
Authors: Mihai Criveti

MCP Gateway - Main FastAPI Application.

This module defines the core FastAPI application for the Model Context Protocol (MCP) Gateway.
It serves as the entry point for handling all HTTP and WebSocket traffic.

Features and Responsibilities:
- Initializes and orchestrates services for tools, resources, prompts, servers, gateways, and roots.
- Supports full MCP protocol operations: initialize, ping, notify, complete, and sample.
- Integrates authentication (JWT and basic), CORS, caching, and middleware.
- Serves a rich Admin UI for managing gateway entities via HTMX-based frontend.
- Exposes routes for JSON-RPC, SSE, and WebSocket transports.
- Manages application lifecycle including startup and graceful shutdown of all services.

Structure:
- Declares routers for MCP protocol operations and administration.
- Registers dependencies (e.g., DB sessions, auth handlers).
- Applies middleware including custom documentation protection.
- Configures resource caching and session registry using pluggable backends.
- Provides OpenAPI metadata and redirect handling depending on UI feature flags.
"""

# Standard
import asyncio
from contextlib import asynccontextmanager
import json
import os as _os  # local alias to avoid collisions
import time
from typing import Any, AsyncIterator, Dict, List, Optional, Union
from urllib.parse import urlparse, urlunparse
import uuid

# Third-Party
from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Query, Request, status, WebSocket, WebSocketDisconnect
from fastapi.background import BackgroundTasks
from fastapi.exception_handlers import request_validation_exception_handler as fastapi_default_validation_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

# First-Party
from mcpgateway import __version__
from mcpgateway.admin import admin_router, set_logging_service
from mcpgateway.auth import get_current_user
from mcpgateway.bootstrap_db import main as bootstrap_db
from mcpgateway.cache import ResourceCache, SessionRegistry
from mcpgateway.config import jsonpath_modifier, settings
from mcpgateway.db import Prompt as DbPrompt
from mcpgateway.db import PromptMetric, refresh_slugs_on_startup, SessionLocal
from mcpgateway.db import Tool as DbTool
from mcpgateway.handlers.sampling import SamplingHandler
from mcpgateway.middleware.rbac import get_current_user_with_permissions, require_permission
from mcpgateway.middleware.security_headers import SecurityHeadersMiddleware
from mcpgateway.middleware.token_scoping import token_scoping_middleware
from mcpgateway.models import InitializeResult, ListResourceTemplatesResult, LogLevel, Root
from mcpgateway.observability import init_telemetry
from mcpgateway.plugins.framework import PluginError, PluginManager, PluginViolationError
from mcpgateway.routers.well_known import router as well_known_router
from mcpgateway.schemas import (
    A2AAgentCreate,
    A2AAgentRead,
    A2AAgentUpdate,
    GatewayCreate,
    GatewayRead,
    GatewayUpdate,
    JsonPathModifier,
    PromptCreate,
    PromptExecuteArgs,
    PromptRead,
    PromptUpdate,
    ResourceCreate,
    ResourceRead,
    ResourceUpdate,
    RPCRequest,
    ServerCreate,
    ServerRead,
    ServerUpdate,
    TaggedEntity,
    TagInfo,
    ToolCreate,
    ToolRead,
    ToolUpdate,
)
from mcpgateway.services.a2a_service import A2AAgentError, A2AAgentNameConflictError, A2AAgentNotFoundError, A2AAgentService
from mcpgateway.services.completion_service import CompletionService
from mcpgateway.services.export_service import ExportError, ExportService
from mcpgateway.services.gateway_service import GatewayConnectionError, GatewayNameConflictError, GatewayNotFoundError, GatewayService
from mcpgateway.services.import_service import ConflictStrategy, ImportConflictError
from mcpgateway.services.import_service import ImportError as ImportServiceError
from mcpgateway.services.import_service import ImportService, ImportValidationError
from mcpgateway.services.logging_service import LoggingService
from mcpgateway.services.prompt_service import PromptError, PromptNameConflictError, PromptNotFoundError, PromptService
from mcpgateway.services.resource_service import ResourceError, ResourceNotFoundError, ResourceService, ResourceURIConflictError
from mcpgateway.services.root_service import RootService
from mcpgateway.services.server_service import ServerError, ServerNameConflictError, ServerNotFoundError, ServerService
from mcpgateway.services.tag_service import TagService
from mcpgateway.services.tool_service import ToolError, ToolNameConflictError, ToolNotFoundError, ToolService
from mcpgateway.transports.sse_transport import SSETransport
from mcpgateway.transports.streamablehttp_transport import SessionManagerWrapper, streamable_http_auth
from mcpgateway.utils.db_isready import wait_for_db_ready
from mcpgateway.utils.error_formatter import ErrorFormatter
from mcpgateway.utils.metadata_capture import MetadataCapture
from mcpgateway.utils.passthrough_headers import set_global_passthrough_headers
from mcpgateway.utils.redis_isready import wait_for_redis_ready
from mcpgateway.utils.retry_manager import ResilientHttpClient
from mcpgateway.utils.verify_credentials import require_auth, require_docs_auth_override, verify_jwt_token
from mcpgateway.validation.jsonrpc import JSONRPCError

# Import the admin routes from the new module
from mcpgateway.version import router as version_router

# Initialize logging service first
logging_service = LoggingService()
logger = logging_service.get_logger("mcpgateway")

# Share the logging service with admin module
set_logging_service(logging_service)

# Note: Logging configuration is handled by LoggingService during startup
# Don't use basicConfig here as it conflicts with our dual logging setup

# Wait for database to be ready before creating tables
wait_for_db_ready(max_tries=int(settings.db_max_retries), interval=int(settings.db_retry_interval_ms) / 1000, sync=True)  # Converting ms to s

# Create database tables
try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    asyncio.run(bootstrap_db())
else:
    loop.create_task(bootstrap_db())

# Initialize plugin manager as a singleton (honor env overrides for tests)
_env_flag = _os.getenv("PLUGINS_ENABLED")
if _env_flag is not None:
    _env_enabled = _env_flag.strip().lower() in {"1", "true", "yes", "on"}
    _PLUGINS_ENABLED = _env_enabled
else:
    _PLUGINS_ENABLED = settings.plugins_enabled
_config_file = _os.getenv("PLUGIN_CONFIG_FILE", settings.plugin_config_file)
plugin_manager: PluginManager | None = PluginManager(_config_file) if _PLUGINS_ENABLED else None

# Initialize services
tool_service = ToolService()
resource_service = ResourceService()
prompt_service = PromptService()
gateway_service = GatewayService()
root_service = RootService()
completion_service = CompletionService()
sampling_handler = SamplingHandler()
server_service = ServerService()
tag_service = TagService()
export_service = ExportService()
import_service = ImportService()
# Initialize A2A service only if A2A features are enabled
a2a_service = A2AAgentService() if settings.mcpgateway_a2a_enabled else None

# Initialize session manager for Streamable HTTP transport
streamable_http_session = SessionManagerWrapper()

# Wait for redis to be ready
if settings.cache_type == "redis":
    wait_for_redis_ready(redis_url=settings.redis_url, max_retries=int(settings.redis_max_retries), retry_interval_ms=int(settings.redis_retry_interval_ms), sync=True)

# Initialize session registry
session_registry = SessionRegistry(
    backend=settings.cache_type,
    redis_url=settings.redis_url if settings.cache_type == "redis" else None,
    database_url=settings.database_url if settings.cache_type == "database" else None,
    session_ttl=settings.session_ttl,
    message_ttl=settings.message_ttl,
)


# Helper function for authentication compatibility
def get_user_email(user):
    """Extract email from user object, handling both string and dict formats.

    Args:
        user: User object, can be either a dict (new RBAC format) or string (legacy format)

    Returns:
        str: User email address or 'unknown' if not available

    Examples:
        Test with dictionary user containing email:
        >>> from mcpgateway import main
        >>> user_dict = {'email': 'alice@example.com', 'role': 'admin'}
        >>> main.get_user_email(user_dict)
        'alice@example.com'

        Test with dictionary user without email:
        >>> user_dict_no_email = {'username': 'bob', 'role': 'user'}
        >>> main.get_user_email(user_dict_no_email)
        'unknown'

        Test with string user (legacy format):
        >>> user_string = 'charlie@company.com'
        >>> main.get_user_email(user_string)
        'charlie@company.com'

        Test with None user:
        >>> main.get_user_email(None)
        'unknown'

        Test with empty dictionary:
        >>> main.get_user_email({})
        'unknown'

        Test with integer (non-string, non-dict):
        >>> main.get_user_email(123)
        '123'

        Test with user object having various data types:
        >>> user_complex = {'email': 'david@test.org', 'id': 456, 'active': True}
        >>> main.get_user_email(user_complex)
        'david@test.org'

        Test with empty string user:
        >>> main.get_user_email('')
        'unknown'

        Test with boolean user:
        >>> main.get_user_email(True)
        'True'
        >>> main.get_user_email(False)
        'unknown'
    """
    if isinstance(user, dict):
        return user.get("email", "unknown")
    return str(user) if user else "unknown"


# Initialize cache
resource_cache = ResourceCache(max_size=settings.resource_cache_size, ttl=settings.resource_cache_ttl)


####################
# Startup/Shutdown #
####################
@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """
    Manage the application's startup and shutdown lifecycle.

    The function initialises every core service on entry and then
    shuts them down in reverse order on exit.

    Args:
        _app (FastAPI): FastAPI app

    Yields:
        None

    Raises:
        Exception: Any unhandled error that occurs during service
            initialisation or shutdown is re-raised to the caller.
    """
    # Initialize logging service FIRST to ensure all logging goes to dual output
    await logging_service.initialize()
    logger.info("Starting MCP Gateway services")

    # Initialize observability (Phoenix tracing)
    init_telemetry()
    logger.info("Observability initialized")

    try:
        if plugin_manager:
            await plugin_manager.initialize()
            logger.info(f"Plugin manager initialized with {plugin_manager.plugin_count} plugins")

        if settings.enable_header_passthrough:
            db_gen = get_db()
            db = next(db_gen)  # pylint: disable=stop-iteration-return
            try:
                await set_global_passthrough_headers(db)
            finally:
                db.close()

        await tool_service.initialize()
        await resource_service.initialize()
        await prompt_service.initialize()
        await gateway_service.initialize()
        await root_service.initialize()
        await completion_service.initialize()
        await sampling_handler.initialize()
        await export_service.initialize()
        await import_service.initialize()
        if a2a_service:
            await a2a_service.initialize()
        await resource_cache.initialize()
        await streamable_http_session.initialize()
        refresh_slugs_on_startup()

        # Bootstrap SSO providers from environment configuration
        if settings.sso_enabled:
            try:
                # First-Party
                from mcpgateway.utils.sso_bootstrap import bootstrap_sso_providers  # pylint: disable=import-outside-toplevel

                bootstrap_sso_providers()
                logger.info("SSO providers bootstrapped successfully")
            except Exception as e:
                logger.warning(f"Failed to bootstrap SSO providers: {e}")

        logger.info("All services initialized successfully")

        # Reconfigure uvicorn loggers after startup to capture access logs in dual output
        logging_service.configure_uvicorn_after_startup()

        yield
    except Exception as e:
        logger.error(f"Error during startup: {str(e)}")
        raise
    finally:
        # Shutdown plugin manager
        if plugin_manager:
            try:
                await plugin_manager.shutdown()
                logger.info("Plugin manager shutdown complete")
            except Exception as e:
                logger.error(f"Error shutting down plugin manager: {str(e)}")
        logger.info("Shutting down MCP Gateway services")
        # await stop_streamablehttp()
        # Build service list conditionally
        services_to_shutdown = [
            resource_cache,
            sampling_handler,
            import_service,
            export_service,
            logging_service,
            completion_service,
            root_service,
            gateway_service,
            prompt_service,
            resource_service,
            tool_service,
            streamable_http_session,
        ]

        if a2a_service:
            services_to_shutdown.insert(4, a2a_service)  # Insert after export_service

        for service in services_to_shutdown:
            try:
                await service.shutdown()
            except Exception as e:
                logger.error(f"Error shutting down {service.__class__.__name__}: {str(e)}")
        logger.info("Shutdown complete")


# Initialize FastAPI app
app = FastAPI(
    title=settings.app_name,
    version=__version__,
    description="A FastAPI-based MCP Gateway with federation support",
    root_path=settings.app_root_path,
    lifespan=lifespan,
)


# Global exceptions handlers
@app.exception_handler(ValidationError)
async def validation_exception_handler(_request: Request, exc: ValidationError):
    """Handle Pydantic validation errors globally.

    Intercepts ValidationError exceptions raised anywhere in the application
    and returns a properly formatted JSON error response with detailed
    validation error information.

    Args:
        _request: The FastAPI request object that triggered the validation error.
                  (Unused but required by FastAPI's exception handler interface)
        exc: The Pydantic ValidationError exception containing validation
             failure details.

    Returns:
        JSONResponse: A 422 Unprocessable Entity response with formatted
                      validation error details.

    Examples:
        >>> from pydantic import ValidationError, BaseModel
        >>> from fastapi import Request
        >>> import asyncio
        >>>
        >>> class TestModel(BaseModel):
        ...     name: str
        ...     age: int
        >>>
        >>> # Create a validation error
        >>> try:
        ...     TestModel(name="", age="invalid")
        ... except ValidationError as e:
        ...     # Test our handler
        ...     result = asyncio.run(validation_exception_handler(None, e))
        ...     result.status_code
        422
    """
    return JSONResponse(status_code=422, content=ErrorFormatter.format_validation_error(exc))


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(_request: Request, exc: RequestValidationError):
    """Handle FastAPI request validation errors (automatic request parsing).

    This handles ValidationErrors that occur during FastAPI's automatic request
    parsing before the request reaches your endpoint.

    Args:
        _request: The FastAPI request object that triggered validation error.
        exc: The RequestValidationError exception containing failure details.

    Returns:
        JSONResponse: A 422 Unprocessable Entity response with error details.
    """
    if _request.url.path.startswith("/tools"):
        error_details = []

        for error in exc.errors():
            loc = error.get("loc", [])
            msg = error.get("msg", "Unknown error")
            ctx = error.get("ctx", {"error": {}})
            type_ = error.get("type", "value_error")
            # Ensure ctx is JSON serializable
            if isinstance(ctx, dict):
                ctx_serializable = {k: (str(v) if isinstance(v, Exception) else v) for k, v in ctx.items()}
            else:
                ctx_serializable = str(ctx)
            error_detail = {"type": type_, "loc": loc, "msg": msg, "ctx": ctx_serializable}
            error_details.append(error_detail)

        response_content = {"detail": error_details}
        return JSONResponse(status_code=422, content=response_content)
    return await fastapi_default_validation_handler(_request, exc)


@app.exception_handler(IntegrityError)
async def database_exception_handler(_request: Request, exc: IntegrityError):
    """Handle SQLAlchemy database integrity constraint violations globally.

    Intercepts IntegrityError exceptions (e.g., unique constraint violations,
    foreign key constraints) and returns a properly formatted JSON error response.
    This provides consistent error handling for database constraint violations
    across the entire application.

    Args:
        _request: The FastAPI request object that triggered the database error.
                  (Unused but required by FastAPI's exception handler interface)
        exc: The SQLAlchemy IntegrityError exception containing constraint
             violation details.

    Returns:
        JSONResponse: A 409 Conflict response with formatted database error details.

    Examples:
        >>> from sqlalchemy.exc import IntegrityError
        >>> from fastapi import Request
        >>> import asyncio
        >>>
        >>> # Create a mock integrity error
        >>> mock_error = IntegrityError("statement", {}, Exception("duplicate key"))
        >>> result = asyncio.run(database_exception_handler(None, mock_error))
        >>> result.status_code
        409
        >>> # Verify ErrorFormatter.format_database_error is called
        >>> hasattr(result, 'body')
        True
    """
    return JSONResponse(status_code=409, content=ErrorFormatter.format_database_error(exc))


@app.exception_handler(PluginViolationError)
async def plugin_violation_exception_handler(_request: Request, exc: PluginViolationError):
    """Handle plugins violations globally.

    Intercepts PluginViolationError exceptions (e.g., OPA policy violation) and returns a properly formatted JSON error response.
    This provides consistent error handling for plugin violation across the entire application.

    Args:
        _request: The FastAPI request object that triggered the database error.
                  (Unused but required by FastAPI's exception handler interface)
        exc: The PluginViolationError exception containing constraint
             violation details.

    Returns:
        JSONResponse: A 403 response with access forbidden.

    Examples:
        >>> from sqlalchemy.exc import IntegrityError
        >>> from fastapi import Request
        >>> import asyncio
        >>>
        >>> # Create a mock integrity error
        >>> mock_error = IntegrityError("statement", {}, Exception("duplicate key"))
        >>> result = asyncio.run(database_exception_handler(None, mock_error))
        >>> result.status_code
        409
        >>> # Verify ErrorFormatter.format_database_error is called
        >>> hasattr(result, 'body')
        True
    """
    return JSONResponse(status_code=403, content={"detail": "policy_deny"})


@app.exception_handler(PluginError)
async def plugin_exception_handler(_request: Request, exc: PluginError):
    """Handle plugins errors globally.

    Intercepts PluginError exceptions and returns a properly formatted JSON error response.
    This provides consistent error handling for plugin violation across the entire application.

    Args:
        _request: The FastAPI request object that triggered the database error.
                  (Unused but required by FastAPI's exception handler interface)
        exc: The PluginError exception containing constraint
             violation details.

    Returns:
        JSONResponse: A 403 response with access forbidden.

    Examples:
        >>> from sqlalchemy.exc import IntegrityError
        >>> from fastapi import Request
        >>> import asyncio
        >>>
        >>> # Create a mock integrity error
        >>> mock_error = IntegrityError("statement", {}, Exception("duplicate key"))
        >>> result = asyncio.run(database_exception_handler(None, mock_error))
        >>> result.status_code
        409
        >>> # Verify ErrorFormatter.format_database_error is called
        >>> hasattr(result, 'body')
        True
    """
    # error_str = str(exc.message) if hasattr(exc, "message") else str(exc)
    return JSONResponse(status_code=500, content=exc.error.message)


class DocsAuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware to protect FastAPI's auto-generated documentation routes
    (/docs, /redoc, and /openapi.json) using Bearer token authentication.

    If a request to one of these paths is made without a valid token,
    the request is rejected with a 401 or 403 error.

    Note:
        When DOCS_ALLOW_BASIC_AUTH is enabled, Basic Authentication
        is also accepted using BASIC_AUTH_USER and BASIC_AUTH_PASSWORD credentials.
    """

    async def dispatch(self, request: Request, call_next):
        """
        Intercepts incoming requests to check if they are accessing protected documentation routes.
        If so, it requires a valid Bearer token; otherwise, it allows the request to proceed.

        Args:
            request (Request): The incoming HTTP request.
            call_next (Callable): The function to call the next middleware or endpoint.

        Returns:
            Response: Either the standard route response or a 401/403 error response.

        Examples:
            >>> import asyncio
            >>> from unittest.mock import Mock, AsyncMock, patch
            >>> from fastapi import HTTPException
            >>> from fastapi.responses import JSONResponse
            >>>
            >>> # Test unprotected path - should pass through
            >>> middleware = DocsAuthMiddleware(None)
            >>> request = Mock()
            >>> request.url.path = "/api/tools"
            >>> request.headers.get.return_value = None
            >>> call_next = AsyncMock(return_value="response")
            >>>
            >>> result = asyncio.run(middleware.dispatch(request, call_next))
            >>> result
            'response'
            >>>
            >>> # Test that middleware checks protected paths
            >>> request.url.path = "/docs"
            >>> isinstance(middleware, DocsAuthMiddleware)
            True
        """
        protected_paths = ["/docs", "/redoc", "/openapi.json"]

        if any(request.url.path.startswith(p) for p in protected_paths):
            try:
                token = request.headers.get("Authorization")
                cookie_token = request.cookies.get("jwt_token")

                # Use dedicated docs authentication that bypasses global auth settings
                await require_docs_auth_override(token, cookie_token)
            except HTTPException as e:
                return JSONResponse(status_code=e.status_code, content={"detail": e.detail}, headers=e.headers if e.headers else None)

        # Proceed to next middleware or route
        return await call_next(request)


class MCPPathRewriteMiddleware:
    """
    Supports requests like '/servers/<server_id>/mcp' by rewriting the path to '/mcp'.

    - Only rewrites paths ending with '/mcp' but not exactly '/mcp'.
    - Performs authentication before rewriting.
    - Passes rewritten requests to `streamable_http_session`.
    - All other requests are passed through without change.
    """

    def __init__(self, application):
        """
        Initialize the middleware with the ASGI application.

        Args:
            application (Callable): The next ASGI application in the middleware stack.
        """
        self.application = application

    async def __call__(self, scope, receive, send):
        """
        Intercept and potentially rewrite the incoming HTTP request path.

        Args:
            scope (dict): The ASGI connection scope.
            receive (Callable): Awaitable that yields events from the client.
            send (Callable): Awaitable used to send events to the client.

        Examples:
            >>> import asyncio
            >>> from unittest.mock import AsyncMock, patch
            >>>
            >>> # Test non-HTTP request passthrough
            >>> app_mock = AsyncMock()
            >>> middleware = MCPPathRewriteMiddleware(app_mock)
            >>> scope = {"type": "websocket", "path": "/ws"}
            >>> receive = AsyncMock()
            >>> send = AsyncMock()
            >>>
            >>> asyncio.run(middleware(scope, receive, send))
            >>> app_mock.assert_called_once_with(scope, receive, send)
            >>>
            >>> # Test path rewriting for /servers/123/mcp
            >>> app_mock.reset_mock()
            >>> scope = {"type": "http", "path": "/servers/123/mcp"}
            >>> with patch('mcpgateway.main.streamable_http_auth', return_value=True):
            ...     with patch.object(streamable_http_session, 'handle_streamable_http') as mock_handler:
            ...         asyncio.run(middleware(scope, receive, send))
            ...         scope["path"]
            '/mcp'
            >>>
            >>> # Test regular path (no rewrite)
            >>> scope = {"type": "http", "path": "/tools"}
            >>> with patch('mcpgateway.main.streamable_http_auth', return_value=True):
            ...     asyncio.run(middleware(scope, receive, send))
            ...     scope["path"]
            '/tools'
        """
        # Only handle HTTP requests, HTTPS uses scope["type"] == "http" in ASGI
        if scope["type"] != "http":
            await self.application(scope, receive, send)
            return

        # Call auth check first
        auth_ok = await streamable_http_auth(scope, receive, send)
        if not auth_ok:
            return

        original_path = scope.get("path", "")
        scope["modified_path"] = original_path
        if (original_path.endswith("/mcp") and original_path != "/mcp") or (original_path.endswith("/mcp/") and original_path != "/mcp/"):
            # Rewrite path so mounted app at /mcp handles it
            scope["path"] = "/mcp"
            await streamable_http_session.handle_streamable_http(scope, receive, send)
            return
        await self.application(scope, receive, send)


# Configure CORS with environment-aware origins
cors_origins = list(settings.allowed_origins) if settings.allowed_origins else []

# Ensure we never use wildcard in production
if settings.environment == "production" and not cors_origins:
    logger.warning("No CORS origins configured for production environment. CORS will be disabled.")
    cors_origins = []

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Length", "X-Request-ID"],
)


# Add security headers middleware
app.add_middleware(SecurityHeadersMiddleware)

# Add token scoping middleware (only when email auth is enabled)
if settings.email_auth_enabled:
    app.add_middleware(BaseHTTPMiddleware, dispatch=token_scoping_middleware)

# Add custom DocsAuthMiddleware
app.add_middleware(DocsAuthMiddleware)

# Add streamable HTTP middleware for /mcp routes
app.add_middleware(MCPPathRewriteMiddleware)

# Trust all proxies (or lock down with a list of host patterns)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")


# Set up Jinja2 templates and store in app state for later use
templates = Jinja2Templates(directory=str(settings.templates_dir))
app.state.templates = templates

# Create API routers
protocol_router = APIRouter(prefix="/protocol", tags=["Protocol"])
tool_router = APIRouter(prefix="/tools", tags=["Tools"])
resource_router = APIRouter(prefix="/resources", tags=["Resources"])
prompt_router = APIRouter(prefix="/prompts", tags=["Prompts"])
gateway_router = APIRouter(prefix="/gateways", tags=["Gateways"])
root_router = APIRouter(prefix="/roots", tags=["Roots"])
utility_router = APIRouter(tags=["Utilities"])
server_router = APIRouter(prefix="/servers", tags=["Servers"])
metrics_router = APIRouter(prefix="/metrics", tags=["Metrics"])
tag_router = APIRouter(prefix="/tags", tags=["Tags"])
export_import_router = APIRouter(tags=["Export/Import"])
a2a_router = APIRouter(prefix="/a2a", tags=["A2A Agents"])

# Basic Auth setup


# Database dependency
def get_db():
    """
    Dependency function to provide a database session.

    Yields:
        Session: A SQLAlchemy session object for interacting with the database.

    Ensures:
        The database session is closed after the request completes, even in the case of an exception.

    Examples:
        >>> # Test that get_db returns a generator
        >>> db_gen = get_db()
        >>> hasattr(db_gen, '__next__')
        True
        >>> # Test cleanup happens
        >>> try:
        ...     db = next(db_gen)
        ...     type(db).__name__
        ... finally:
        ...     try:
        ...         next(db_gen)
        ...     except StopIteration:
        ...         pass  # Expected - generator cleanup
        'Session'
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def require_api_key(api_key: str) -> None:
    """Validates the provided API key.

    This function checks if the provided API key matches the expected one
    based on the settings. If the validation fails, it raises an HTTPException
    with a 401 Unauthorized status.

    Args:
        api_key (str): The API key provided by the user or client.

    Raises:
        HTTPException: If the API key is invalid, a 401 Unauthorized error is raised.

    Examples:
        >>> from mcpgateway.config import settings
        >>> settings.auth_required = True
        >>> settings.basic_auth_user = "admin"
        >>> settings.basic_auth_password = "secret"
        >>>
        >>> # Valid API key
        >>> require_api_key("admin:secret")  # Should not raise
        >>>
        >>> # Invalid API key
        >>> try:
        ...     require_api_key("wrong:key")
        ... except HTTPException as e:
        ...     e.status_code
        401
    """
    if settings.auth_required:
        expected = f"{settings.basic_auth_user}:{settings.basic_auth_password}"
        if api_key != expected:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


async def invalidate_resource_cache(uri: Optional[str] = None) -> None:
    """
    Invalidates the resource cache.

    If a specific URI is provided, only that resource will be removed from the cache.
    If no URI is provided, the entire resource cache will be cleared.

    Args:
        uri (Optional[str]): The URI of the resource to invalidate from the cache. If None, the entire cache is cleared.

    Examples:
        >>> import asyncio
        >>> # Test clearing specific URI from cache
        >>> resource_cache.set("/test/resource", {"content": "test data"})
        >>> resource_cache.get("/test/resource") is not None
        True
        >>> asyncio.run(invalidate_resource_cache("/test/resource"))
        >>> resource_cache.get("/test/resource") is None
        True
        >>>
        >>> # Test clearing entire cache
        >>> resource_cache.set("/resource1", {"content": "data1"})
        >>> resource_cache.set("/resource2", {"content": "data2"})
        >>> asyncio.run(invalidate_resource_cache())
        >>> resource_cache.get("/resource1") is None and resource_cache.get("/resource2") is None
        True
    """
    if uri:
        resource_cache.delete(uri)
    else:
        resource_cache.clear()


def get_protocol_from_request(request: Request) -> str:
    """
    Return "https" or "http" based on:
     1) X-Forwarded-Proto (if set by a proxy)
     2) request.url.scheme  (e.g. when Gunicorn/Uvicorn is terminating TLS)

    Args:
        request (Request): The FastAPI request object.

    Returns:
        str: The protocol used for the request, either "http" or "https".

    Examples:
        Test with X-Forwarded-Proto header (proxy scenario):
        >>> from mcpgateway import main
        >>> from fastapi import Request
        >>> from urllib.parse import urlparse
        >>>
        >>> # Mock request with X-Forwarded-Proto
        >>> scope = {
        ...     'type': 'http',
        ...     'scheme': 'http',
        ...     'headers': [(b'x-forwarded-proto', b'https')],
        ...     'server': ('testserver', 80),
        ...     'path': '/',
        ... }
        >>> req = Request(scope)
        >>> main.get_protocol_from_request(req)
        'https'

        Test with comma-separated X-Forwarded-Proto:
        >>> scope_multi = {
        ...     'type': 'http',
        ...     'scheme': 'http',
        ...     'headers': [(b'x-forwarded-proto', b'https,http')],
        ...     'server': ('testserver', 80),
        ...     'path': '/',
        ... }
        >>> req_multi = Request(scope_multi)
        >>> main.get_protocol_from_request(req_multi)
        'https'

        Test without X-Forwarded-Proto (direct connection):
        >>> scope_direct = {
        ...     'type': 'http',
        ...     'scheme': 'https',
        ...     'headers': [],
        ...     'server': ('testserver', 443),
        ...     'path': '/',
        ... }
        >>> req_direct = Request(scope_direct)
        >>> main.get_protocol_from_request(req_direct)
        'https'

        Test with HTTP direct connection:
        >>> scope_http = {
        ...     'type': 'http',
        ...     'scheme': 'http',
        ...     'headers': [],
        ...     'server': ('testserver', 80),
        ...     'path': '/',
        ... }
        >>> req_http = Request(scope_http)
        >>> main.get_protocol_from_request(req_http)
        'http'
    """
    forwarded = request.headers.get("x-forwarded-proto")
    if forwarded:
        # may be a comma-separated list; take the first
        return forwarded.split(",")[0].strip()
    return request.url.scheme


def update_url_protocol(request: Request) -> str:
    """
    Update the base URL protocol based on the request's scheme or forwarded headers.

    Args:
        request (Request): The FastAPI request object.

    Returns:
        str: The base URL with the correct protocol.

    Examples:
        Test URL protocol update with HTTPS proxy:
        >>> from mcpgateway import main
        >>> from fastapi import Request
        >>>
        >>> # Mock request with HTTPS forwarded proto
        >>> scope_https = {
        ...     'type': 'http',
        ...     'scheme': 'http',
        ...     'server': ('example.com', 80),
        ...     'path': '/',
        ...     'headers': [(b'x-forwarded-proto', b'https')],
        ... }
        >>> req_https = Request(scope_https)
        >>> url = main.update_url_protocol(req_https)
        >>> url.startswith('https://example.com')
        True

        Test URL protocol update with HTTP direct:
        >>> scope_http = {
        ...     'type': 'http',
        ...     'scheme': 'http',
        ...     'server': ('localhost', 8000),
        ...     'path': '/',
        ...     'headers': [],
        ... }
        >>> req_http = Request(scope_http)
        >>> url = main.update_url_protocol(req_http)
        >>> url.startswith('http://localhost:8000')
        True

        Test URL protocol update preserves host and port:
        >>> scope_port = {
        ...     'type': 'http',
        ...     'scheme': 'https',
        ...     'server': ('api.test.com', 443),
        ...     'path': '/',
        ...     'headers': [],
        ... }
        >>> req_port = Request(scope_port)
        >>> url = main.update_url_protocol(req_port)
        >>> 'api.test.com' in url and url.startswith('https://')
        True

        Test trailing slash removal:
        >>> # URL should not end with trailing slash
        >>> url = main.update_url_protocol(req_http)
        >>> url.endswith('/')
        False
    """
    parsed = urlparse(str(request.base_url))
    proto = get_protocol_from_request(request)
    new_parsed = parsed._replace(scheme=proto)
    # urlunparse keeps netloc and path intact
    return urlunparse(new_parsed).rstrip("/")


# Protocol APIs #
@protocol_router.post("/initialize")
async def initialize(request: Request, user=Depends(get_current_user)) -> InitializeResult:
    """
    Initialize a protocol.

    This endpoint handles the initialization process of a protocol by accepting
    a JSON request body and processing it. The `require_auth` dependency ensures that
    the user is authenticated before proceeding.

    Args:
        request (Request): The incoming request object containing the JSON body.
        user (str): The authenticated user (from `require_auth` dependency).

    Returns:
        InitializeResult: The result of the initialization process.

    Raises:
        HTTPException: If the request body contains invalid JSON, a 400 Bad Request error is raised.
    """
    try:
        body = await request.json()

        logger.debug(f"Authenticated user {user} is initializing the protocol.")
        return await session_registry.handle_initialize_logic(body)

    except json.JSONDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid JSON in request body",
        )


@protocol_router.post("/ping")
async def ping(request: Request, user=Depends(get_current_user)) -> JSONResponse:
    """
    Handle a ping request according to the MCP specification.

    This endpoint expects a JSON-RPC request with the method "ping" and responds
    with a JSON-RPC response containing an empty result, as required by the protocol.

    Args:
        request (Request): The incoming FastAPI request.
        user (str): The authenticated user (dependency injection).

    Returns:
        JSONResponse: A JSON-RPC response with an empty result or an error response.

    Raises:
        HTTPException: If the request method is not "ping".
    """
    try:
        body: dict = await request.json()
        if body.get("method") != "ping":
            raise HTTPException(status_code=400, detail="Invalid method")
        req_id: str = body.get("id")
        logger.debug(f"Authenticated user {user} sent ping request.")
        # Return an empty result per the MCP ping specification.
        response: dict = {"jsonrpc": "2.0", "id": req_id, "result": {}}
        return JSONResponse(content=response)
    except Exception as e:
        error_response: dict = {
            "jsonrpc": "2.0",
            "id": body.get("id") if "body" in locals() else None,
            "error": {"code": -32603, "message": "Internal error", "data": str(e)},
        }
        return JSONResponse(status_code=500, content=error_response)


@protocol_router.post("/notifications")
async def handle_notification(request: Request, user=Depends(get_current_user)) -> None:
    """
    Handles incoming notifications from clients. Depending on the notification method,
    different actions are taken (e.g., logging initialization, cancellation, or messages).

    Args:
        request (Request): The incoming request containing the notification data.
        user (str): The authenticated user making the request.
    """
    body = await request.json()
    logger.debug(f"User {user} sent a notification")
    if body.get("method") == "notifications/initialized":
        logger.info("Client initialized")
        await logging_service.notify("Client initialized", LogLevel.INFO)
    elif body.get("method") == "notifications/cancelled":
        request_id = body.get("params", {}).get("requestId")
        logger.info(f"Request cancelled: {request_id}")
        await logging_service.notify(f"Request cancelled: {request_id}", LogLevel.INFO)
    elif body.get("method") == "notifications/message":
        params = body.get("params", {})
        await logging_service.notify(
            params.get("data"),
            LogLevel(params.get("level", "info")),
            params.get("logger"),
        )


@protocol_router.post("/completion/complete")
async def handle_completion(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)):
    """
    Handles the completion of tasks by processing a completion request.

    Args:
        request (Request): The incoming request with completion data.
        db (Session): The database session used to interact with the data store.
        user (str): The authenticated user making the request.

    Returns:
        The result of the completion process.
    """
    body = await request.json()
    logger.debug(f"User {user['email']} sent a completion request")
    return await completion_service.handle_completion(db, body)


@protocol_router.post("/sampling/createMessage")
async def handle_sampling(request: Request, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)):
    """
    Handles the creation of a new message for sampling.

    Args:
        request (Request): The incoming request with sampling data.
        db (Session): The database session used to interact with the data store.
        user (str): The authenticated user making the request.

    Returns:
        The result of the message creation process.
    """
    logger.debug(f"User {user['email']} sent a sampling request")
    body = await request.json()
    return await sampling_handler.create_message(db, body)


###############
# Server APIs #
###############
@server_router.get("", response_model=List[ServerRead])
@server_router.get("/", response_model=List[ServerRead])
@require_permission("servers.read")
async def list_servers(
    include_inactive: bool = False,
    tags: Optional[str] = None,
    team_id: Optional[str] = None,
    visibility: Optional[str] = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> List[ServerRead]:
    """
    Lists servers accessible to the user, with team filtering support.

    Args:
        include_inactive (bool): Whether to include inactive servers in the response.
        tags (Optional[str]): Comma-separated list of tags to filter by.
        team_id (Optional[str]): Filter by specific team ID.
        visibility (Optional[str]): Filter by visibility (private, team, public).
        db (Session): The database session used to interact with the data store.
        user (str): The authenticated user making the request.

    Returns:
        List[ServerRead]: A list of server objects the user has access to.
    """
    # Parse tags parameter if provided
    tags_list = None
    if tags:
        tags_list = [tag.strip() for tag in tags.split(",") if tag.strip()]
    # Get user email for team filtering
    user_email = get_user_email(user)
    # Use team-filtered server listing
    if team_id or visibility:
        data = await server_service.list_servers_for_user(db=db, user_email=user_email, team_id=team_id, visibility=visibility, include_inactive=include_inactive)
        # Apply tag filtering to team-filtered results if needed
        if tags_list:
            data = [server for server in data if any(tag in server.tags for tag in tags_list)]
    else:
        # Use existing method for backward compatibility when no team filtering
        data = await server_service.list_servers(db, include_inactive=include_inactive, tags=tags_list)
    return data


@server_router.get("/{server_id}", response_model=ServerRead)
@require_permission("servers.read")
async def get_server(server_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)) -> ServerRead:
    """
    Retrieves a server by its ID.

    Args:
        server_id (str): The ID of the server to retrieve.
        db (Session): The database session used to interact with the data store.
        user (str): The authenticated user making the request.

    Returns:
        ServerRead: The server object with the specified ID.

    Raises:
        HTTPException: If the server is not found.
    """
    try:
        logger.debug(f"User {user} requested server with ID {server_id}")
        return await server_service.get_server(db, server_id)
    except ServerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@server_router.post("", response_model=ServerRead, status_code=201)
@server_router.post("/", response_model=ServerRead, status_code=201)
@require_permission("servers.create")
async def create_server(
    server: ServerCreate,
    request: Request,
    team_id: Optional[str] = Body(None, description="Team ID to assign server to"),
    visibility: str = Body("private", description="Server visibility: private, team, public"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> ServerRead:
    """
    Creates a new server.

    Args:
        server (ServerCreate): The data for the new server.
        request (Request): The incoming request object for extracting metadata.
        team_id (Optional[str]): Team ID to assign the server to.
        visibility (str): Server visibility level (private, team, public).
        db (Session): The database session used to interact with the data store.
        user (str): The authenticated user making the request.

    Returns:
        ServerRead: The created server object.

    Raises:
        HTTPException: If there is a conflict with the server name or other errors.
    """
    try:
        # Extract metadata from request
        metadata = MetadataCapture.extract_creation_metadata(request, user)

        # Get user email and handle team assignment
        user_email = get_user_email(user)

        # If no team specified, get user's personal team
        if not team_id:
            # First-Party
            from mcpgateway.services.team_management_service import TeamManagementService  # pylint: disable=import-outside-toplevel

            team_service = TeamManagementService(db)
            user_teams = await team_service.get_user_teams(user_email, include_personal=True)
            personal_team = next((team for team in user_teams if team.is_personal), None)
            team_id = personal_team.id if personal_team else None

        logger.debug(f"User {user_email} is creating a new server for team {team_id}")
        return await server_service.register_server(
            db,
            server,
            created_by=metadata["created_by"],
            created_from_ip=metadata["created_from_ip"],
            created_via=metadata["created_via"],
            created_user_agent=metadata["created_user_agent"],
            team_id=team_id,
            owner_email=user_email,
            visibility=visibility,
        )
    except ServerNameConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ServerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValidationError as e:
        logger.error(f"Validation error while creating server: {e}")
        raise HTTPException(status_code=422, detail=ErrorFormatter.format_validation_error(e))
    except IntegrityError as e:
        logger.error(f"Integrity error while creating server: {e}")
        raise HTTPException(status_code=409, detail=ErrorFormatter.format_database_error(e))


@server_router.put("/{server_id}", response_model=ServerRead)
@require_permission("servers.update")
async def update_server(
    server_id: str,
    server: ServerUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> ServerRead:
    """
    Updates the information of an existing server.

    Args:
        server_id (str): The ID of the server to update.
        server (ServerUpdate): The updated server data.
        request (Request): The incoming request object containing metadata.
        db (Session): The database session used to interact with the data store.
        user (str): The authenticated user making the request.

    Returns:
        ServerRead: The updated server object.

    Raises:
        HTTPException: If the server is not found, there is a name conflict, or other errors.
    """
    try:
        logger.debug(f"User {user} is updating server with ID {server_id}")
        # Extract modification metadata
        mod_metadata = MetadataCapture.extract_modification_metadata(request, user, 0)  # Version will be incremented in service

        return await server_service.update_server(
            db,
            server_id,
            server,
            modified_by=mod_metadata["modified_by"],
            modified_from_ip=mod_metadata["modified_from_ip"],
            modified_via=mod_metadata["modified_via"],
            modified_user_agent=mod_metadata["modified_user_agent"],
        )
    except ServerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ServerNameConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ServerError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValidationError as e:
        logger.error(f"Validation error while updating server {server_id}: {e}")
        raise HTTPException(status_code=422, detail=ErrorFormatter.format_validation_error(e))
    except IntegrityError as e:
        logger.error(f"Integrity error while updating server {server_id}: {e}")
        raise HTTPException(status_code=409, detail=ErrorFormatter.format_database_error(e))


@server_router.post("/{server_id}/toggle", response_model=ServerRead)
@require_permission("servers.update")
async def toggle_server_status(
    server_id: str,
    activate: bool = True,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> ServerRead:
    """
    Toggles the status of a server (activate or deactivate).

    Args:
        server_id (str): The ID of the server to toggle.
        activate (bool): Whether to activate or deactivate the server.
        db (Session): The database session used to interact with the data store.
        user (str): The authenticated user making the request.

    Returns:
        ServerRead: The server object after the status change.

    Raises:
        HTTPException: If the server is not found or there is an error.
    """
    try:
        logger.debug(f"User {user} is toggling server with ID {server_id} to {'active' if activate else 'inactive'}")
        return await server_service.toggle_server_status(db, server_id, activate)
    except ServerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ServerError as e:
        raise HTTPException(status_code=400, detail=str(e))


@server_router.delete("/{server_id}", response_model=Dict[str, str])
@require_permission("servers.delete")
async def delete_server(server_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)) -> Dict[str, str]:
    """
    Deletes a server by its ID.

    Args:
        server_id (str): The ID of the server to delete.
        db (Session): The database session used to interact with the data store.
        user (str): The authenticated user making the request.

    Returns:
        Dict[str, str]: A success message indicating the server was deleted.

    Raises:
        HTTPException: If the server is not found or there is an error.
    """
    try:
        logger.debug(f"User {user} is deleting server with ID {server_id}")
        await server_service.delete_server(db, server_id)
        return {
            "status": "success",
            "message": f"Server {server_id} deleted successfully",
        }
    except ServerNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ServerError as e:
        raise HTTPException(status_code=400, detail=str(e))


@server_router.get("/{server_id}/sse")
@require_permission("servers.use")
async def sse_endpoint(request: Request, server_id: str, user=Depends(get_current_user_with_permissions)):
    """
    Establishes a Server-Sent Events (SSE) connection for real-time updates about a server.

    Args:
        request (Request): The incoming request.
        server_id (str): The ID of the server for which updates are received.
        user (str): The authenticated user making the request.

    Returns:
        The SSE response object for the established connection.

    Raises:
        HTTPException: If there is an error in establishing the SSE connection.
    """
    try:
        logger.debug(f"User {user} is establishing SSE connection for server {server_id}")
        base_url = update_url_protocol(request)
        server_sse_url = f"{base_url}/servers/{server_id}"

        transport = SSETransport(base_url=server_sse_url)
        await transport.connect()
        await session_registry.add_session(transport.session_id, transport)
        response = await transport.create_sse_response(request)

        asyncio.create_task(session_registry.respond(server_id, user, session_id=transport.session_id, base_url=base_url))

        tasks = BackgroundTasks()
        tasks.add_task(session_registry.remove_session, transport.session_id)
        response.background = tasks
        logger.info(f"SSE connection established: {transport.session_id}")
        return response
    except Exception as e:
        logger.error(f"SSE connection error: {e}")
        raise HTTPException(status_code=500, detail="SSE connection failed")


@server_router.post("/{server_id}/message")
@require_permission("servers.use")
async def message_endpoint(request: Request, server_id: str, user=Depends(get_current_user_with_permissions)):
    """
    Handles incoming messages for a specific server.

    Args:
        request (Request): The incoming message request.
        server_id (str): The ID of the server receiving the message.
        user (str): The authenticated user making the request.

    Returns:
        JSONResponse: A success status after processing the message.

    Raises:
        HTTPException: If there are errors processing the message.
    """
    try:
        logger.debug(f"User {user} sent a message to server {server_id}")
        session_id = request.query_params.get("session_id")
        if not session_id:
            logger.error("Missing session_id in message request")
            raise HTTPException(status_code=400, detail="Missing session_id")

        message = await request.json()

        await session_registry.broadcast(
            session_id=session_id,
            message=message,
        )

        return JSONResponse(content={"status": "success"}, status_code=202)
    except ValueError as e:
        logger.error(f"Invalid message format: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Message handling error: {e}")
        raise HTTPException(status_code=500, detail="Failed to process message")


@server_router.get("/{server_id}/tools", response_model=List[ToolRead])
@require_permission("servers.read")
async def server_get_tools(
    server_id: str,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> List[ToolRead]:
    """
    List tools for the server  with an option to include inactive tools.

    This endpoint retrieves a list of tools from the database, optionally including
    those that are inactive. The inactive filter helps administrators manage tools
    that have been deactivated but not deleted from the system.

    Args:
        server_id (str): ID of the server
        include_inactive (bool): Whether to include inactive tools in the results.
        db (Session): Database session dependency.
        user (str): Authenticated user dependency.

    Returns:
        List[ToolRead]: A list of tool records formatted with by_alias=True.
    """
    logger.debug(f"User: {user} has listed tools for the server_id: {server_id}")
    tools = await tool_service.list_server_tools(db, server_id=server_id, include_inactive=include_inactive)
    return [tool.model_dump(by_alias=True) for tool in tools]


@server_router.get("/{server_id}/resources", response_model=List[ResourceRead])
@require_permission("servers.read")
async def server_get_resources(
    server_id: str,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> List[ResourceRead]:
    """
    List resources for the server with an option to include inactive resources.

    This endpoint retrieves a list of resources from the database, optionally including
    those that are inactive. The inactive filter is useful for administrators who need
    to view or manage resources that have been deactivated but not deleted.

    Args:
        server_id (str): ID of the server
        include_inactive (bool): Whether to include inactive resources in the results.
        db (Session): Database session dependency.
        user (str): Authenticated user dependency.

    Returns:
        List[ResourceRead]: A list of resource records formatted with by_alias=True.
    """
    logger.debug(f"User: {user} has listed resources for the server_id: {server_id}")
    resources = await resource_service.list_server_resources(db, server_id=server_id, include_inactive=include_inactive)
    return [resource.model_dump(by_alias=True) for resource in resources]


@server_router.get("/{server_id}/prompts", response_model=List[PromptRead])
@require_permission("servers.read")
async def server_get_prompts(
    server_id: str,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> List[PromptRead]:
    """
    List prompts for the server with an option to include inactive prompts.

    This endpoint retrieves a list of prompts from the database, optionally including
    those that are inactive. The inactive filter helps administrators see and manage
    prompts that have been deactivated but not deleted from the system.

    Args:
        server_id (str): ID of the server
        include_inactive (bool): Whether to include inactive prompts in the results.
        db (Session): Database session dependency.
        user (str): Authenticated user dependency.

    Returns:
        List[PromptRead]: A list of prompt records formatted with by_alias=True.
    """
    logger.debug(f"User: {user} has listed prompts for the server_id: {server_id}")
    prompts = await prompt_service.list_server_prompts(db, server_id=server_id, include_inactive=include_inactive)
    return [prompt.model_dump(by_alias=True) for prompt in prompts]


##################
# A2A Agent APIs #
##################
@a2a_router.get("", response_model=List[A2AAgentRead])
@a2a_router.get("/", response_model=List[A2AAgentRead])
@require_permission("a2a.read")
async def list_a2a_agents(
    include_inactive: bool = False,
    tags: Optional[str] = None,
    team_id: Optional[str] = Query(None, description="Filter by team ID"),
    visibility: Optional[str] = Query(None, description="Filter by visibility (private, team, public)"),
    skip: int = Query(0, ge=0, description="Number of agents to skip for pagination"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of agents to return"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> List[A2AAgentRead]:
    """
    Lists A2A agents user has access to with team filtering.

    Args:
        include_inactive (bool): Whether to include inactive agents in the response.
        tags (Optional[str]): Comma-separated list of tags to filter by.
        team_id (Optional[str]): Team ID to filter by.
        visibility (Optional[str]): Visibility level to filter by.
        skip (int): Number of agents to skip for pagination.
        limit (int): Maximum number of agents to return.
        db (Session): The database session used to interact with the data store.
        user (str): The authenticated user making the request.

    Returns:
        List[A2AAgentRead]: A list of A2A agent objects the user has access to.
    """
    # Parse tags parameter if provided (keeping for backward compatibility)
    tags_list = None
    if tags:
        tags_list = [tag.strip() for tag in tags.split(",") if tag.strip()]

    logger.debug(f"User {user} requested A2A agent list with team_id={team_id}, visibility={visibility}, tags={tags_list}")

    # Use team-aware filtering
    return await a2a_service.list_agents_for_user(db, user_email=user, team_id=team_id, visibility=visibility, include_inactive=include_inactive, skip=skip, limit=limit)


@a2a_router.get("/{agent_id}", response_model=A2AAgentRead)
@require_permission("a2a.read")
async def get_a2a_agent(agent_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)) -> A2AAgentRead:
    """
    Retrieves an A2A agent by its ID.

    Args:
        agent_id (str): The ID of the agent to retrieve.
        db (Session): The database session used to interact with the data store.
        user (str): The authenticated user making the request.

    Returns:
        A2AAgentRead: The agent object with the specified ID.

    Raises:
        HTTPException: If the agent is not found.
    """
    try:
        logger.debug(f"User {user} requested A2A agent with ID {agent_id}")
        return await a2a_service.get_agent(db, agent_id)
    except A2AAgentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))


@a2a_router.post("", response_model=A2AAgentRead, status_code=201)
@a2a_router.post("/", response_model=A2AAgentRead, status_code=201)
@require_permission("a2a.create")
async def create_a2a_agent(
    agent: A2AAgentCreate,
    request: Request,
    team_id: Optional[str] = Body(None, description="Team ID to assign agent to"),
    visibility: str = Body("private", description="Agent visibility: private, team, public"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> A2AAgentRead:
    """
    Creates a new A2A agent.

    Args:
        agent (A2AAgentCreate): The data for the new agent.
        request (Request): The FastAPI request object for metadata extraction.
        team_id (Optional[str]): Team ID to assign the agent to.
        visibility (str): Agent visibility level (private, team, public).
        db (Session): The database session used to interact with the data store.
        user (str): The authenticated user making the request.

    Returns:
        A2AAgentRead: The created agent object.

    Raises:
        HTTPException: If there is a conflict with the agent name or other errors.
    """
    try:
        # Extract metadata from request
        metadata = MetadataCapture.extract_creation_metadata(request, user)

        # Get user email and handle team assignment
        user_email = get_user_email(user)

        # If no team specified, get user's personal team
        if not team_id:
            # First-Party
            from mcpgateway.services.team_management_service import TeamManagementService  # pylint: disable=import-outside-toplevel

            team_service = TeamManagementService(db)
            user_teams = await team_service.get_user_teams(user_email, include_personal=True)
            personal_team = next((team for team in user_teams if team.is_personal), None)
            team_id = personal_team.id if personal_team else None

        logger.debug(f"User {user_email} is creating a new A2A agent for team {team_id}")
        return await a2a_service.register_agent(
            db,
            agent,
            created_by=metadata["created_by"],
            created_from_ip=metadata["created_from_ip"],
            created_via=metadata["created_via"],
            created_user_agent=metadata["created_user_agent"],
            import_batch_id=metadata["import_batch_id"],
            federation_source=metadata["federation_source"],
            team_id=team_id,
            owner_email=user_email,
            visibility=visibility,
        )
    except A2AAgentNameConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except A2AAgentError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValidationError as e:
        logger.error(f"Validation error while creating A2A agent: {e}")
        raise HTTPException(status_code=422, detail=ErrorFormatter.format_validation_error(e))
    except IntegrityError as e:
        logger.error(f"Integrity error while creating A2A agent: {e}")
        raise HTTPException(status_code=409, detail=ErrorFormatter.format_database_error(e))


@a2a_router.put("/{agent_id}", response_model=A2AAgentRead)
@require_permission("a2a.update")
async def update_a2a_agent(
    agent_id: str,
    agent: A2AAgentUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> A2AAgentRead:
    """
    Updates the information of an existing A2A agent.

    Args:
        agent_id (str): The ID of the agent to update.
        agent (A2AAgentUpdate): The updated agent data.
        request (Request): The FastAPI request object for metadata extraction.
        db (Session): The database session used to interact with the data store.
        user (str): The authenticated user making the request.

    Returns:
        A2AAgentRead: The updated agent object.

    Raises:
        HTTPException: If the agent is not found, there is a name conflict, or other errors.
    """
    try:
        logger.debug(f"User {user} is updating A2A agent with ID {agent_id}")
        # Extract modification metadata
        mod_metadata = MetadataCapture.extract_modification_metadata(request, user, 0)  # Version will be incremented in service

        return await a2a_service.update_agent(
            db,
            agent_id,
            agent,
            modified_by=mod_metadata["modified_by"],
            modified_from_ip=mod_metadata["modified_from_ip"],
            modified_via=mod_metadata["modified_via"],
            modified_user_agent=mod_metadata["modified_user_agent"],
        )
    except A2AAgentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except A2AAgentNameConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except A2AAgentError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValidationError as e:
        logger.error(f"Validation error while updating A2A agent {agent_id}: {e}")
        raise HTTPException(status_code=422, detail=ErrorFormatter.format_validation_error(e))
    except IntegrityError as e:
        logger.error(f"Integrity error while updating A2A agent {agent_id}: {e}")
        raise HTTPException(status_code=409, detail=ErrorFormatter.format_database_error(e))


@a2a_router.post("/{agent_id}/toggle", response_model=A2AAgentRead)
@require_permission("a2a.update")
async def toggle_a2a_agent_status(
    agent_id: str,
    activate: bool = True,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> A2AAgentRead:
    """
    Toggles the status of an A2A agent (activate or deactivate).

    Args:
        agent_id (str): The ID of the agent to toggle.
        activate (bool): Whether to activate or deactivate the agent.
        db (Session): The database session used to interact with the data store.
        user (str): The authenticated user making the request.

    Returns:
        A2AAgentRead: The agent object after the status change.

    Raises:
        HTTPException: If the agent is not found or there is an error.
    """
    try:
        logger.debug(f"User {user} is toggling A2A agent with ID {agent_id} to {'active' if activate else 'inactive'}")
        return await a2a_service.toggle_agent_status(db, agent_id, activate)
    except A2AAgentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except A2AAgentError as e:
        raise HTTPException(status_code=400, detail=str(e))


@a2a_router.delete("/{agent_id}", response_model=Dict[str, str])
@require_permission("a2a.delete")
async def delete_a2a_agent(agent_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)) -> Dict[str, str]:
    """
    Deletes an A2A agent by its ID.

    Args:
        agent_id (str): The ID of the agent to delete.
        db (Session): The database session used to interact with the data store.
        user (str): The authenticated user making the request.

    Returns:
        Dict[str, str]: A success message indicating the agent was deleted.

    Raises:
        HTTPException: If the agent is not found or there is an error.
    """
    try:
        logger.debug(f"User {user} is deleting A2A agent with ID {agent_id}")
        await a2a_service.delete_agent(db, agent_id)
        return {
            "status": "success",
            "message": f"A2A Agent {agent_id} deleted successfully",
        }
    except A2AAgentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except A2AAgentError as e:
        raise HTTPException(status_code=400, detail=str(e))


@a2a_router.post("/{agent_name}/invoke", response_model=Dict[str, Any])
@require_permission("a2a.invoke")
async def invoke_a2a_agent(
    agent_name: str,
    parameters: Dict[str, Any] = Body(default_factory=dict),
    interaction_type: str = Body(default="query"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> Dict[str, Any]:
    """
    Invokes an A2A agent with the specified parameters.

    Args:
        agent_name (str): The name of the agent to invoke.
        parameters (Dict[str, Any]): Parameters for the agent interaction.
        interaction_type (str): Type of interaction (query, execute, etc.).
        db (Session): The database session used to interact with the data store.
        user (str): The authenticated user making the request.

    Returns:
        Dict[str, Any]: The response from the A2A agent.

    Raises:
        HTTPException: If the agent is not found or there is an error during invocation.
    """
    try:
        logger.debug(f"User {user} is invoking A2A agent '{agent_name}' with type '{interaction_type}'")
        return await a2a_service.invoke_agent(db, agent_name, parameters, interaction_type)
    except A2AAgentNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except A2AAgentError as e:
        raise HTTPException(status_code=400, detail=str(e))


#############
# Tool APIs #
#############
@tool_router.get("", response_model=Union[List[ToolRead], List[Dict], Dict, List])
@tool_router.get("/", response_model=Union[List[ToolRead], List[Dict], Dict, List])
@require_permission("tools.read")
async def list_tools(
    cursor: Optional[str] = None,
    include_inactive: bool = False,
    tags: Optional[str] = None,
    team_id: Optional[str] = Query(None, description="Filter by team ID"),
    visibility: Optional[str] = Query(None, description="Filter by visibility: private, team, public"),
    db: Session = Depends(get_db),
    apijsonpath: JsonPathModifier = Body(None),
    user=Depends(get_current_user_with_permissions),
) -> Union[List[ToolRead], List[Dict], Dict]:
    """List all registered tools with team-based filtering and pagination support.

    Args:
        cursor: Pagination cursor for fetching the next set of results
        include_inactive: Whether to include inactive tools in the results
        tags: Comma-separated list of tags to filter by (e.g., "api,data")
        team_id: Optional team ID to filter tools by specific team
        visibility: Optional visibility filter (private, team, public)
        db: Database session
        apijsonpath: JSON path modifier to filter or transform the response
        user: Authenticated user with permissions

    Returns:
        List of tools or modified result based on jsonpath
    """

    # Parse tags parameter if provided
    tags_list = None
    if tags:
        tags_list = [tag.strip() for tag in tags.split(",") if tag.strip()]

    # Get user email for team filtering
    user_email = get_user_email(user)

    # Use team-filtered tool listing
    if team_id or visibility:
        data = await tool_service.list_tools_for_user(db=db, user_email=user_email, team_id=team_id, visibility=visibility, include_inactive=include_inactive)

        # Apply tag filtering to team-filtered results if needed
        if tags_list:
            data = [tool for tool in data if any(tag in tool.tags for tag in tags_list)]
    else:
        # Use existing method for backward compatibility when no team filtering
        data = await tool_service.list_tools(db, cursor=cursor, include_inactive=include_inactive, tags=tags_list)

    if apijsonpath is None:
        return data

    tools_dict_list = [tool.to_dict(use_alias=True) for tool in data]

    return jsonpath_modifier(tools_dict_list, apijsonpath.jsonpath, apijsonpath.mapping)


@tool_router.post("", response_model=ToolRead)
@tool_router.post("/", response_model=ToolRead)
@require_permission("tools.create")
async def create_tool(
    tool: ToolCreate,
    request: Request,
    team_id: Optional[str] = Body(None, description="Team ID to assign tool to"),
    visibility: str = Body("private", description="Tool visibility: private, team, public"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> ToolRead:
    """
    Creates a new tool in the system with team assignment support.

    Args:
        tool (ToolCreate): The data needed to create the tool.
        request (Request): The FastAPI request object for metadata extraction.
        team_id (Optional[str]): Team ID to assign the tool to.
        visibility (str): Tool visibility (private, team, public).
        db (Session): The database session dependency.
        user: The authenticated user making the request.

    Returns:
        ToolRead: The created tool data.

    Raises:
        HTTPException: If the tool name already exists or other validation errors occur.
    """
    try:
        # Extract metadata from request
        metadata = MetadataCapture.extract_creation_metadata(request, user)

        # Get user email and handle team assignment
        user_email = get_user_email(user)

        # If no team specified, get user's personal team
        if not team_id:
            # First-Party
            from mcpgateway.services.team_management_service import TeamManagementService  # pylint: disable=import-outside-toplevel

            team_service = TeamManagementService(db)
            user_teams = await team_service.get_user_teams(user_email, include_personal=True)
            personal_team = next((team for team in user_teams if team.is_personal), None)
            team_id = personal_team.id if personal_team else None

        logger.debug(f"User {user_email} is creating a new tool for team {team_id}")
        return await tool_service.register_tool(
            db,
            tool,
            created_by=metadata["created_by"],
            created_from_ip=metadata["created_from_ip"],
            created_via=metadata["created_via"],
            created_user_agent=metadata["created_user_agent"],
            import_batch_id=metadata["import_batch_id"],
            federation_source=metadata["federation_source"],
            team_id=team_id,
            owner_email=user_email,
            visibility=visibility,
        )
    except Exception as ex:
        logger.error(f"Error while creating tool: {ex}")
        if isinstance(ex, ToolNameConflictError):
            if not ex.enabled and ex.tool_id:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Tool name already exists but is inactive. Consider activating it with ID: {ex.tool_id}",
                )
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(ex))
        if isinstance(ex, (ValidationError, ValueError)):
            logger.error(f"Validation error while creating tool: {ex}")
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=ErrorFormatter.format_validation_error(ex))
        if isinstance(ex, IntegrityError):
            logger.error(f"Integrity error while creating tool: {ex}")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ErrorFormatter.format_database_error(ex))
        if isinstance(ex, ToolError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ex))
        logger.error(f"Unexpected error while creating tool: {ex}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred while creating the tool")


@tool_router.get("/{tool_id}", response_model=Union[ToolRead, Dict])
@require_permission("tools.read")
async def get_tool(
    tool_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
    apijsonpath: JsonPathModifier = Body(None),
) -> Union[ToolRead, Dict]:
    """
    Retrieve a tool by ID, optionally applying a JSONPath post-filter.

    Args:
        tool_id: The numeric ID of the tool.
        db:     Active SQLAlchemy session (dependency).
        user:   Authenticated username (dependency).
        apijsonpath: Optional JSON-Path modifier supplied in the body.

    Returns:
        The raw ``ToolRead`` model **or** a JSON-transformed ``dict`` if
        a JSONPath filter/mapping was supplied.

    Raises:
        HTTPException: If the tool does not exist or the transformation fails.
    """
    try:
        logger.debug(f"User {user} is retrieving tool with ID {tool_id}")
        data = await tool_service.get_tool(db, tool_id)
        if apijsonpath is None:
            return data

        data_dict = data.to_dict(use_alias=True)

        return jsonpath_modifier(data_dict, apijsonpath.jsonpath, apijsonpath.mapping)
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))


@tool_router.put("/{tool_id}", response_model=ToolRead)
@require_permission("tools.update")
async def update_tool(
    tool_id: str,
    tool: ToolUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> ToolRead:
    """
    Updates an existing tool with new data.

    Args:
        tool_id (str): The ID of the tool to update.
        tool (ToolUpdate): The updated tool information.
        request (Request): The FastAPI request object for metadata extraction.
        db (Session): The database session dependency.
        user (str): The authenticated user making the request.

    Returns:
        ToolRead: The updated tool data.

    Raises:
        HTTPException: If an error occurs during the update.
    """
    try:
        # Get current tool to extract current version
        current_tool = db.get(DbTool, tool_id)
        current_version = getattr(current_tool, "version", 0) if current_tool else 0

        # Extract modification metadata
        mod_metadata = MetadataCapture.extract_modification_metadata(request, user, current_version)

        logger.debug(f"User {user} is updating tool with ID {tool_id}")
        return await tool_service.update_tool(
            db,
            tool_id,
            tool,
            modified_by=mod_metadata["modified_by"],
            modified_from_ip=mod_metadata["modified_from_ip"],
            modified_via=mod_metadata["modified_via"],
            modified_user_agent=mod_metadata["modified_user_agent"],
        )
    except Exception as ex:
        if isinstance(ex, ToolNotFoundError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(ex))
        if isinstance(ex, ValidationError):
            logger.error(f"Validation error while creating tool: {ex}")
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=ErrorFormatter.format_validation_error(ex))
        if isinstance(ex, IntegrityError):
            logger.error(f"Integrity error while creating tool: {ex}")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ErrorFormatter.format_database_error(ex))
        if isinstance(ex, ToolError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(ex))
        logger.error(f"Unexpected error while creating tool: {ex}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred while creating the tool")


@tool_router.delete("/{tool_id}")
@require_permission("tools.delete")
async def delete_tool(tool_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)) -> Dict[str, str]:
    """
    Permanently deletes a tool by ID.

    Args:
        tool_id (str): The ID of the tool to delete.
        db (Session): The database session dependency.
        user (str): The authenticated user making the request.

    Returns:
        Dict[str, str]: A confirmation message upon successful deletion.

    Raises:
        HTTPException: If an error occurs during deletion.
    """
    try:
        logger.debug(f"User {user} is deleting tool with ID {tool_id}")
        await tool_service.delete_tool(db, tool_id)
        return {"status": "success", "message": f"Tool {tool_id} permanently deleted"}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@tool_router.post("/{tool_id}/toggle")
@require_permission("tools.update")
async def toggle_tool_status(
    tool_id: str,
    activate: bool = True,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> Dict[str, Any]:
    """
    Activates or deactivates a tool.

    Args:
        tool_id (str): The ID of the tool to toggle.
        activate (bool): Whether to activate (`True`) or deactivate (`False`) the tool.
        db (Session): The database session dependency.
        user (str): The authenticated user making the request.

    Returns:
        Dict[str, Any]: The status, message, and updated tool data.

    Raises:
        HTTPException: If an error occurs during status toggling.
    """
    try:
        logger.debug(f"User {user} is toggling tool with ID {tool_id} to {'active' if activate else 'inactive'}")
        tool = await tool_service.toggle_tool_status(db, tool_id, activate, reachable=activate)
        return {
            "status": "success",
            "message": f"Tool {tool_id} {'activated' if activate else 'deactivated'}",
            "tool": tool.model_dump(),
        }
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


#################
# Resource APIs #
#################
# --- Resource templates endpoint - MUST come before variable paths ---
@resource_router.get("/templates/list", response_model=ListResourceTemplatesResult)
@require_permission("resources.read")
async def list_resource_templates(
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> ListResourceTemplatesResult:
    """
    List all available resource templates.

    Args:
        db (Session): Database session.
        user (str): Authenticated user.

    Returns:
        ListResourceTemplatesResult: A paginated list of resource templates.
    """
    logger.debug(f"User {user} requested resource templates")
    resource_templates = await resource_service.list_resource_templates(db)
    # For simplicity, we're not implementing real pagination here
    return ListResourceTemplatesResult(_meta={}, resource_templates=resource_templates, next_cursor=None)  # No pagination for now


@resource_router.post("/{resource_id}/toggle")
@require_permission("resources.update")
async def toggle_resource_status(
    resource_id: int,
    activate: bool = True,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> Dict[str, Any]:
    """
    Activate or deactivate a resource by its ID.

    Args:
        resource_id (int): The ID of the resource.
        activate (bool): True to activate, False to deactivate.
        db (Session): Database session.
        user (str): Authenticated user.

    Returns:
        Dict[str, Any]: Status message and updated resource data.

    Raises:
        HTTPException: If toggling fails.
    """
    logger.debug(f"User {user} is toggling resource with ID {resource_id} to {'active' if activate else 'inactive'}")
    try:
        resource = await resource_service.toggle_resource_status(db, resource_id, activate)
        return {
            "status": "success",
            "message": f"Resource {resource_id} {'activated' if activate else 'deactivated'}",
            "resource": resource.model_dump(),
        }
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@resource_router.get("", response_model=List[ResourceRead])
@resource_router.get("/", response_model=List[ResourceRead])
@require_permission("resources.read")
async def list_resources(
    cursor: Optional[str] = None,
    include_inactive: bool = False,
    tags: Optional[str] = None,
    team_id: Optional[str] = None,
    visibility: Optional[str] = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> List[ResourceRead]:
    """
    Retrieve a list of resources accessible to the user, with team filtering support.

    Args:
        cursor (Optional[str]): Optional cursor for pagination.
        include_inactive (bool): Whether to include inactive resources.
        tags (Optional[str]): Comma-separated list of tags to filter by.
        team_id (Optional[str]): Filter by specific team ID.
        visibility (Optional[str]): Filter by visibility (private, team, public).
        db (Session): Database session.
        user (str): Authenticated user.

    Returns:
        List[ResourceRead]: List of resources the user has access to.
    """
    # Parse tags parameter if provided
    tags_list = None
    if tags:
        tags_list = [tag.strip() for tag in tags.split(",") if tag.strip()]
    # Get user email for team filtering
    user_email = get_user_email(user)

    # Use team-filtered resource listing
    if team_id or visibility:
        data = await resource_service.list_resources_for_user(db=db, user_email=user_email, team_id=team_id, visibility=visibility, include_inactive=include_inactive)
        # Apply tag filtering to team-filtered results if needed
        if tags_list:
            data = [resource for resource in data if any(tag in resource.tags for tag in tags_list)]
    else:
        # Use existing method for backward compatibility when no team filtering
        logger.debug(f"User {user_email} requested resource list with cursor {cursor}, include_inactive={include_inactive}, tags={tags_list}")
        if cached := resource_cache.get("resource_list"):
            return cached
        data = await resource_service.list_resources(db, include_inactive=include_inactive, tags=tags_list)
        resource_cache.set("resource_list", data)
    return data


@resource_router.post("", response_model=ResourceRead)
@resource_router.post("/", response_model=ResourceRead)
@require_permission("resources.create")
async def create_resource(
    resource: ResourceCreate,
    request: Request,
    team_id: Optional[str] = Body(None, description="Team ID to assign resource to"),
    visibility: str = Body("private", description="Resource visibility: private, team, public"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> ResourceRead:
    """
    Create a new resource.

    Args:
        resource (ResourceCreate): Data for the new resource.
        request (Request): FastAPI request object for metadata extraction.
        team_id (Optional[str]): Team ID to assign the resource to.
        visibility (str): Resource visibility level (private, team, public).
        db (Session): Database session.
        user (str): Authenticated user.

    Returns:
        ResourceRead: The created resource.

    Raises:
        HTTPException: On conflict or validation errors or IntegrityError.
    """
    try:
        # Extract metadata from request
        metadata = MetadataCapture.extract_creation_metadata(request, user)

        # Get user email and handle team assignment
        user_email = get_user_email(user)

        # If no team specified, get user's personal team
        if not team_id:
            # First-Party
            from mcpgateway.services.team_management_service import TeamManagementService  # pylint: disable=import-outside-toplevel

            team_service = TeamManagementService(db)
            user_teams = await team_service.get_user_teams(user_email, include_personal=True)
            personal_team = next((team for team in user_teams if team.is_personal), None)
            team_id = personal_team.id if personal_team else None

        logger.debug(f"User {user_email} is creating a new resource for team {team_id}")
        return await resource_service.register_resource(
            db,
            resource,
            created_by=metadata["created_by"],
            created_from_ip=metadata["created_from_ip"],
            created_via=metadata["created_via"],
            created_user_agent=metadata["created_user_agent"],
            import_batch_id=metadata["import_batch_id"],
            federation_source=metadata["federation_source"],
            team_id=team_id,
            owner_email=user_email,
            visibility=visibility,
        )
    except ResourceURIConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ResourceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValidationError as e:
        # Handle validation errors from Pydantic
        logger.error(f"Validation error while creating resource: {e}")
        raise HTTPException(status_code=422, detail=ErrorFormatter.format_validation_error(e))
    except IntegrityError as e:
        logger.error(f"Integrity error while creating resource: {e}")
        raise HTTPException(status_code=409, detail=ErrorFormatter.format_database_error(e))


@resource_router.get("/{uri:path}")
@require_permission("resources.read")
async def read_resource(uri: str, request: Request, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)) -> Any:
    """
    Read a resource by its URI with plugin support.

    Args:
        uri (str): URI of the resource.
        request (Request): FastAPI request object for context.
        db (Session): Database session.
        user (str): Authenticated user.

    Returns:
        Any: The content of the resource.

    Raises:
        HTTPException: If the resource cannot be found or read.
    """
    # Get request ID from headers or generate one
    request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
    server_id = request.headers.get("X-Server-ID")

    logger.debug(f"User {user} requested resource with URI {uri} (request_id: {request_id})")

    # Check cache
    if cached := resource_cache.get(uri):
        return cached

    try:
        # Call service with context for plugin support
        content = await resource_service.read_resource(db, uri, request_id=request_id, user=user, server_id=server_id)
    except (ResourceNotFoundError, ResourceError) as exc:
        # Translate to FastAPI HTTP error
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    resource_cache.set(uri, content)
    # Ensure a plain JSON-serializable structure
    try:
        # First-Party
        from mcpgateway.models import ResourceContent  # pylint: disable=import-outside-toplevel
        from mcpgateway.models import TextContent  # pylint: disable=import-outside-toplevel

        # If already a ResourceContent, serialize directly
        if isinstance(content, ResourceContent):
            return content.model_dump()

        # If TextContent, wrap into resource envelope with text
        if isinstance(content, TextContent):
            return {"type": "resource", "uri": uri, "text": content.text}
    except Exception:
        pass

    if isinstance(content, bytes):
        return {"type": "resource", "uri": uri, "blob": content.decode("utf-8", errors="ignore")}
    if isinstance(content, str):
        return {"type": "resource", "uri": uri, "text": content}

    # Objects with a 'text' attribute (e.g., mocks) – best-effort mapping
    if hasattr(content, "text"):
        return {"type": "resource", "uri": uri, "text": getattr(content, "text")}

    return {"type": "resource", "uri": uri, "text": str(content)}


@resource_router.put("/{uri:path}", response_model=ResourceRead)
@require_permission("resources.update")
async def update_resource(
    uri: str,
    resource: ResourceUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> ResourceRead:
    """
    Update a resource identified by its URI.

    Args:
        uri (str): URI of the resource.
        resource (ResourceUpdate): New resource data.
        request (Request): The FastAPI request object for metadata extraction.
        db (Session): Database session.
        user (str): Authenticated user.

    Returns:
        ResourceRead: The updated resource.

    Raises:
        HTTPException: If the resource is not found or update fails.
    """
    try:
        logger.debug(f"User {user} is updating resource with URI {uri}")
        # Extract modification metadata
        mod_metadata = MetadataCapture.extract_modification_metadata(request, user, 0)  # Version will be incremented in service

        result = await resource_service.update_resource(
            db,
            uri,
            resource,
            modified_by=mod_metadata["modified_by"],
            modified_from_ip=mod_metadata["modified_from_ip"],
            modified_via=mod_metadata["modified_via"],
            modified_user_agent=mod_metadata["modified_user_agent"],
        )
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValidationError as e:
        logger.error(f"Validation error while updating resource {uri}: {e}")
        raise HTTPException(status_code=422, detail=ErrorFormatter.format_validation_error(e))
    except IntegrityError as e:
        logger.error(f"Integrity error while updating resource {uri}: {e}")
        raise HTTPException(status_code=409, detail=ErrorFormatter.format_database_error(e))
    await invalidate_resource_cache(uri)
    return result


@resource_router.delete("/{uri:path}")
@require_permission("resources.delete")
async def delete_resource(uri: str, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)) -> Dict[str, str]:
    """
    Delete a resource by its URI.

    Args:
        uri (str): URI of the resource to delete.
        db (Session): Database session.
        user (str): Authenticated user.

    Returns:
        Dict[str, str]: Status message indicating deletion success.

    Raises:
        HTTPException: If the resource is not found or deletion fails.
    """
    try:
        logger.debug(f"User {user} is deleting resource with URI {uri}")
        await resource_service.delete_resource(db, uri)
        await invalidate_resource_cache(uri)
        return {"status": "success", "message": f"Resource {uri} deleted"}
    except ResourceNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ResourceError as e:
        raise HTTPException(status_code=400, detail=str(e))


@resource_router.post("/subscribe/{uri:path}")
@require_permission("resources.read")
async def subscribe_resource(uri: str, user=Depends(get_current_user_with_permissions)) -> StreamingResponse:
    """
    Subscribe to server-sent events (SSE) for a specific resource.

    Args:
        uri (str): URI of the resource to subscribe to.
        user (str): Authenticated user.

    Returns:
        StreamingResponse: A streaming response with event updates.
    """
    logger.debug(f"User {user} is subscribing to resource with URI {uri}")
    return StreamingResponse(resource_service.subscribe_events(uri), media_type="text/event-stream")


###############
# Prompt APIs #
###############
@prompt_router.post("/{prompt_id}/toggle")
@require_permission("prompts.update")
async def toggle_prompt_status(
    prompt_id: int,
    activate: bool = True,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> Dict[str, Any]:
    """
    Toggle the activation status of a prompt.

    Args:
        prompt_id: ID of the prompt to toggle.
        activate: True to activate, False to deactivate.
        db: Database session.
        user: Authenticated user.

    Returns:
        Status message and updated prompt details.

    Raises:
        HTTPException: If the toggle fails (e.g., prompt not found or database error); emitted with *400 Bad Request* status and an error message.
    """
    logger.debug(f"User: {user} requested toggle for prompt {prompt_id}, activate={activate}")
    try:
        prompt = await prompt_service.toggle_prompt_status(db, prompt_id, activate)
        return {
            "status": "success",
            "message": f"Prompt {prompt_id} {'activated' if activate else 'deactivated'}",
            "prompt": prompt.model_dump(),
        }
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@prompt_router.get("", response_model=List[PromptRead])
@prompt_router.get("/", response_model=List[PromptRead])
@require_permission("prompts.read")
async def list_prompts(
    cursor: Optional[str] = None,
    include_inactive: bool = False,
    tags: Optional[str] = None,
    team_id: Optional[str] = None,
    visibility: Optional[str] = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> List[PromptRead]:
    """
    List prompts accessible to the user, with team filtering support.

    Args:
        cursor: Cursor for pagination.
        include_inactive: Include inactive prompts.
        tags: Comma-separated list of tags to filter by.
        team_id: Filter by specific team ID.
        visibility: Filter by visibility (private, team, public).
        db: Database session.
        user: Authenticated user.

    Returns:
        List of prompt records the user has access to.
    """
    # Parse tags parameter if provided
    tags_list = None
    if tags:
        tags_list = [tag.strip() for tag in tags.split(",") if tag.strip()]
    # Get user email for team filtering
    user_email = get_user_email(user)

    # Use team-filtered prompt listing
    if team_id or visibility:
        data = await prompt_service.list_prompts_for_user(db=db, user_email=user_email, team_id=team_id, visibility=visibility, include_inactive=include_inactive)
        # Apply tag filtering to team-filtered results if needed
        if tags_list:
            data = [prompt for prompt in data if any(tag in prompt.tags for tag in tags_list)]
    else:
        # Use existing method for backward compatibility when no team filtering
        logger.debug(f"User: {user_email} requested prompt list with include_inactive={include_inactive}, cursor={cursor}, tags={tags_list}")
        data = await prompt_service.list_prompts(db, cursor=cursor, include_inactive=include_inactive, tags=tags_list)
    return data


@prompt_router.post("", response_model=PromptRead)
@prompt_router.post("/", response_model=PromptRead)
@require_permission("prompts.create")
async def create_prompt(
    prompt: PromptCreate,
    request: Request,
    team_id: Optional[str] = Body(None, description="Team ID to assign prompt to"),
    visibility: str = Body("private", description="Prompt visibility: private, team, public"),
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> PromptRead:
    """
    Create a new prompt.

    Args:
        prompt (PromptCreate): Payload describing the prompt to create.
        request (Request): The FastAPI request object for metadata extraction.
        team_id (Optional[str]): Team ID to assign the prompt to.
        visibility (str): Prompt visibility level (private, team, public).
        db (Session): Active SQLAlchemy session.
        user (str): Authenticated username.

    Returns:
        PromptRead: The newly-created prompt.

    Raises:
        HTTPException: * **409 Conflict** - another prompt with the same name already exists.
            * **400 Bad Request** - validation or persistence error raised
                by :pyclass:`~mcpgateway.services.prompt_service.PromptService`.
    """
    try:
        # Extract metadata from request
        metadata = MetadataCapture.extract_creation_metadata(request, user)

        # Get user email and handle team assignment
        user_email = get_user_email(user)

        # If no team specified, get user's personal team
        if not team_id:
            # First-Party
            from mcpgateway.services.team_management_service import TeamManagementService  # pylint: disable=import-outside-toplevel

            team_service = TeamManagementService(db)
            user_teams = await team_service.get_user_teams(user_email, include_personal=True)
            personal_team = next((team for team in user_teams if team.is_personal), None)
            team_id = personal_team.id if personal_team else None

        logger.debug(f"User {user_email} is creating a new prompt for team {team_id}")
        return await prompt_service.register_prompt(
            db,
            prompt,
            created_by=metadata["created_by"],
            created_from_ip=metadata["created_from_ip"],
            created_via=metadata["created_via"],
            created_user_agent=metadata["created_user_agent"],
            import_batch_id=metadata["import_batch_id"],
            federation_source=metadata["federation_source"],
            team_id=team_id,
            owner_email=user_email,
            visibility=visibility,
        )
    except Exception as e:
        if isinstance(e, PromptNameConflictError):
            # If the prompt name already exists, return a 409 Conflict error
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        if isinstance(e, PromptError):
            # If there is a general prompt error, return a 400 Bad Request error
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        if isinstance(e, ValidationError):
            # If there is a validation error, return a 422 Unprocessable Entity error
            logger.error(f"Validation error while creating prompt: {e}")
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=ErrorFormatter.format_validation_error(e))
        if isinstance(e, IntegrityError):
            # If there is an integrity error, return a 409 Conflict error
            logger.error(f"Integrity error while creating prompt: {e}")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ErrorFormatter.format_database_error(e))
        # For any other unexpected errors, return a 500 Internal Server Error
        logger.error(f"Unexpected error while creating prompt: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred while creating the prompt")


@prompt_router.post("/{name}")
@require_permission("prompts.read")
async def get_prompt(
    name: str,
    args: Dict[str, str] = Body({}),
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> Any:
    """Get a prompt by name with arguments.

    This implements the prompts/get functionality from the MCP spec,
    which requires a POST request with arguments in the body.


    Args:
        name: Name of the prompt.
        args: Template arguments.
        db: Database session.
        user: Authenticated user.

    Returns:
        Rendered prompt or metadata.

    Raises:
        Exception: Re-raised if not a handled exception type.
    """
    logger.debug(f"User: {user} requested prompt: {name} with args={args}")
    start_time = time.monotonic()
    success = False
    error_message = None
    result = None

    try:
        PromptExecuteArgs(args=args)
        result = await prompt_service.get_prompt(db, name, args)
        success = True
        logger.debug(f"Prompt execution successful for '{name}'")
    except Exception as ex:
        error_message = str(ex)
        logger.error(f"Could not retrieve prompt {name}: {ex}")
        if isinstance(ex, PluginViolationError):
            # Return the actual plugin violation message
            result = JSONResponse(content={"message": ex.message, "details": str(ex.violation) if hasattr(ex, "violation") else None}, status_code=422)
        elif isinstance(ex, (ValueError, PromptError)):
            # Return the actual error message
            result = JSONResponse(content={"message": str(ex)}, status_code=422)
        else:
            raise

    # Record metrics (moved outside try/except/finally to ensure it runs)
    end_time = time.monotonic()
    response_time = end_time - start_time

    # Get the prompt from database to get its ID
    prompt = db.execute(select(DbPrompt).where(DbPrompt.name == name)).scalar_one_or_none()

    if prompt:
        metric = PromptMetric(
            prompt_id=prompt.id,
            response_time=response_time,
            is_success=success,
            error_message=error_message,
        )
        db.add(metric)
        db.commit()

    return result


@prompt_router.get("/{name}")
@require_permission("prompts.read")
async def get_prompt_no_args(
    name: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> Any:
    """Get a prompt by name without arguments.

    This endpoint is for convenience when no arguments are needed.

    Args:
        name: The name of the prompt to retrieve
        db: Database session
        user: Authenticated user

    Returns:
        The prompt template information

    Raises:
        Exception: Re-raised from prompt service.
    """
    logger.debug(f"User: {user} requested prompt: {name} with no arguments")
    start_time = time.monotonic()
    success = False
    error_message = None
    result = None

    try:
        result = await prompt_service.get_prompt(db, name, {})
        success = True
    except Exception as ex:
        error_message = str(ex)
        raise

    # Record metrics
    end_time = time.monotonic()
    response_time = end_time - start_time

    # Get the prompt from database to get its ID
    prompt = db.execute(select(DbPrompt).where(DbPrompt.name == name)).scalar_one_or_none()

    if prompt:
        metric = PromptMetric(
            prompt_id=prompt.id,
            response_time=response_time,
            is_success=success,
            error_message=error_message,
        )
        db.add(metric)
        db.commit()

    return result


@prompt_router.put("/{name}", response_model=PromptRead)
@require_permission("prompts.update")
async def update_prompt(
    name: str,
    prompt: PromptUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> PromptRead:
    """
    Update (overwrite) an existing prompt definition.

    Args:
        name (str): Identifier of the prompt to update.
        prompt (PromptUpdate): New prompt content and metadata.
        request (Request): The FastAPI request object for metadata extraction.
        db (Session): Active SQLAlchemy session.
        user (str): Authenticated username.

    Returns:
        PromptRead: The updated prompt object.

    Raises:
        HTTPException: * **409 Conflict** - a different prompt with the same *name* already exists and is still active.
            * **400 Bad Request** - validation or persistence error raised by :pyclass:`~mcpgateway.services.prompt_service.PromptService`.
    """
    logger.info(f"User: {user} requested to update prompt: {name} with data={prompt}")
    logger.debug(f"User: {user} requested to update prompt: {name} with data={prompt}")
    try:
        # Extract modification metadata
        mod_metadata = MetadataCapture.extract_modification_metadata(request, user, 0)  # Version will be incremented in service

        return await prompt_service.update_prompt(
            db,
            name,
            prompt,
            modified_by=mod_metadata["modified_by"],
            modified_from_ip=mod_metadata["modified_from_ip"],
            modified_via=mod_metadata["modified_via"],
            modified_user_agent=mod_metadata["modified_user_agent"],
        )
    except Exception as e:
        if isinstance(e, PromptNotFoundError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
        if isinstance(e, ValidationError):
            logger.error(f"Validation error while updating prompt: {e}")
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=ErrorFormatter.format_validation_error(e))
        if isinstance(e, IntegrityError):
            logger.error(f"Integrity error while updating prompt: {e}")
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ErrorFormatter.format_database_error(e))
        if isinstance(e, PromptNameConflictError):
            # If the prompt name already exists, return a 409 Conflict error
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))
        if isinstance(e, PromptError):
            # If there is a general prompt error, return a 400 Bad Request error
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        # For any other unexpected errors, return a 500 Internal Server Error
        logger.error(f"Unexpected error while updating prompt: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred while updating the prompt")


@prompt_router.delete("/{name}")
@require_permission("prompts.delete")
async def delete_prompt(name: str, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)) -> Dict[str, str]:
    """
    Delete a prompt by name.

    Args:
        name: Name of the prompt.
        db: Database session.
        user: Authenticated user.

    Returns:
        Status message.

    Raises:
        HTTPException: If the prompt is not found, a prompt error occurs, or an unexpected error occurs during deletion.
    """
    logger.debug(f"User: {user} requested deletion of prompt {name}")
    try:
        await prompt_service.delete_prompt(db, name)
        return {"status": "success", "message": f"Prompt {name} deleted"}
    except Exception as e:
        if isinstance(e, PromptNotFoundError):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(e))
        if isinstance(e, PromptError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        logger.error(f"Unexpected error while deleting prompt {name}: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="An unexpected error occurred while deleting the prompt")

    # except PromptNotFoundError as e:
    #     return {"status": "error", "message": str(e)}
    # except PromptError as e:
    #     return {"status": "error", "message": str(e)}


################
# Gateway APIs #
################
@gateway_router.post("/{gateway_id}/toggle")
@require_permission("gateways.update")
async def toggle_gateway_status(
    gateway_id: str,
    activate: bool = True,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> Dict[str, Any]:
    """
    Toggle the activation status of a gateway.

    Args:
        gateway_id (str): String ID of the gateway to toggle.
        activate (bool): ``True`` to activate, ``False`` to deactivate.
        db (Session): Active SQLAlchemy session.
        user (str): Authenticated username.

    Returns:
        Dict[str, Any]: A dict containing the operation status, a message, and the updated gateway object.

    Raises:
        HTTPException: Returned with **400 Bad Request** if the toggle operation fails (e.g., the gateway does not exist or the database raises an unexpected error).
    """
    logger.debug(f"User '{user}' requested toggle for gateway {gateway_id}, activate={activate}")
    try:
        gateway = await gateway_service.toggle_gateway_status(
            db,
            gateway_id,
            activate,
        )
        return {
            "status": "success",
            "message": f"Gateway {gateway_id} {'activated' if activate else 'deactivated'}",
            "gateway": gateway.model_dump(),
        }
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@gateway_router.get("", response_model=List[GatewayRead])
@gateway_router.get("/", response_model=List[GatewayRead])
@require_permission("gateways.read")
async def list_gateways(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> List[GatewayRead]:
    """
    List all gateways.

    Args:
        include_inactive: Include inactive gateways.
        db: Database session.
        user: Authenticated user.

    Returns:
        List of gateway records.
    """
    logger.debug(f"User '{user}' requested list of gateways with include_inactive={include_inactive}")
    return await gateway_service.list_gateways(db, include_inactive=include_inactive)


@gateway_router.post("", response_model=GatewayRead)
@gateway_router.post("/", response_model=GatewayRead)
@require_permission("gateways.create")
async def register_gateway(
    gateway: GatewayCreate,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> GatewayRead:
    """
    Register a new gateway.

    Args:
        gateway: Gateway creation data.
        request: The FastAPI request object for metadata extraction.
        db: Database session.
        user: Authenticated user.

    Returns:
        Created gateway.
    """
    logger.debug(f"User '{user}' requested to register gateway: {gateway}")
    try:
        # Extract metadata from request
        metadata = MetadataCapture.extract_creation_metadata(request, user)

        # Get user email and handle team assignment
        user_email = get_user_email(user)
        team_id = gateway.team_id
        visibility = gateway.visibility

        # If no team specified, get user's personal team
        if not team_id:
            # First-Party
            from mcpgateway.services.team_management_service import TeamManagementService  # pylint: disable=import-outside-toplevel

            team_service = TeamManagementService(db)
            user_teams = await team_service.get_user_teams(user_email, include_personal=True)
            personal_team = next((team for team in user_teams if team.is_personal), None)
            team_id = personal_team.id if personal_team else None

        logger.debug(f"User {user_email} is creating a new gateway for team {team_id}")

        return await gateway_service.register_gateway(
            db,
            gateway,
            created_by=metadata["created_by"],
            created_from_ip=metadata["created_from_ip"],
            created_via=metadata["created_via"],
            created_user_agent=metadata["created_user_agent"],
            team_id=team_id,
            owner_email=user_email,
            visibility=visibility,
        )
    except Exception as ex:
        if isinstance(ex, GatewayConnectionError):
            return JSONResponse(content={"message": "Unable to connect to gateway"}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        if isinstance(ex, ValueError):
            return JSONResponse(content={"message": "Unable to process input"}, status_code=status.HTTP_400_BAD_REQUEST)
        if isinstance(ex, GatewayNameConflictError):
            return JSONResponse(content={"message": "Gateway name already exists"}, status_code=status.HTTP_409_CONFLICT)
        if isinstance(ex, RuntimeError):
            return JSONResponse(content={"message": "Error during execution"}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        if isinstance(ex, ValidationError):
            return JSONResponse(content=ErrorFormatter.format_validation_error(ex), status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
        if isinstance(ex, IntegrityError):
            return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=ErrorFormatter.format_database_error(ex))
        return JSONResponse(content={"message": "Unexpected error"}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@gateway_router.get("/{gateway_id}", response_model=GatewayRead)
@require_permission("gateways.read")
async def get_gateway(gateway_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)) -> GatewayRead:
    """
    Retrieve a gateway by ID.

    Args:
        gateway_id: ID of the gateway.
        db: Database session.
        user: Authenticated user.

    Returns:
        Gateway data.
    """
    logger.debug(f"User '{user}' requested gateway {gateway_id}")
    return await gateway_service.get_gateway(db, gateway_id)


@gateway_router.put("/{gateway_id}", response_model=GatewayRead)
@require_permission("gateways.update")
async def update_gateway(
    gateway_id: str,
    gateway: GatewayUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> GatewayRead:
    """
    Update a gateway.

    Args:
        gateway_id: Gateway ID.
        gateway: Gateway update data.
        request (Request): The FastAPI request object for metadata extraction.
        db: Database session.
        user: Authenticated user.

    Returns:
        Updated gateway.
    """
    logger.debug(f"User '{user}' requested update on gateway {gateway_id} with data={gateway}")
    try:
        # Extract modification metadata
        mod_metadata = MetadataCapture.extract_modification_metadata(request, user, 0)  # Version will be incremented in service

        return await gateway_service.update_gateway(
            db,
            gateway_id,
            gateway,
            modified_by=mod_metadata["modified_by"],
            modified_from_ip=mod_metadata["modified_from_ip"],
            modified_via=mod_metadata["modified_via"],
            modified_user_agent=mod_metadata["modified_user_agent"],
        )
    except Exception as ex:
        if isinstance(ex, GatewayNotFoundError):
            return JSONResponse(content={"message": "Gateway not found"}, status_code=status.HTTP_404_NOT_FOUND)
        if isinstance(ex, GatewayConnectionError):
            return JSONResponse(content={"message": "Unable to connect to gateway"}, status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        if isinstance(ex, ValueError):
            return JSONResponse(content={"message": "Unable to process input"}, status_code=status.HTTP_400_BAD_REQUEST)
        if isinstance(ex, GatewayNameConflictError):
            return JSONResponse(content={"message": "Gateway name already exists"}, status_code=status.HTTP_409_CONFLICT)
        if isinstance(ex, RuntimeError):
            return JSONResponse(content={"message": "Error during execution"}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
        if isinstance(ex, ValidationError):
            return JSONResponse(content=ErrorFormatter.format_validation_error(ex), status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
        if isinstance(ex, IntegrityError):
            return JSONResponse(status_code=status.HTTP_409_CONFLICT, content=ErrorFormatter.format_database_error(ex))
        return JSONResponse(content={"message": "Unexpected error"}, status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@gateway_router.delete("/{gateway_id}")
@require_permission("gateways.delete")
async def delete_gateway(gateway_id: str, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)) -> Dict[str, str]:
    """
    Delete a gateway by ID.

    Args:
        gateway_id: ID of the gateway.
        db: Database session.
        user: Authenticated user.

    Returns:
        Status message.
    """
    logger.debug(f"User '{user}' requested deletion of gateway {gateway_id}")
    await gateway_service.delete_gateway(db, gateway_id)
    return {"status": "success", "message": f"Gateway {gateway_id} deleted"}


##############
# Root APIs  #
##############
@root_router.get("", response_model=List[Root])
@root_router.get("/", response_model=List[Root])
async def list_roots(
    user=Depends(get_current_user_with_permissions),
) -> List[Root]:
    """
    Retrieve a list of all registered roots.

    Args:
        user: Authenticated user.

    Returns:
        List of Root objects.
    """
    logger.debug(f"User '{user}' requested list of roots")
    return await root_service.list_roots()


@root_router.post("", response_model=Root)
@root_router.post("/", response_model=Root)
async def add_root(
    root: Root,  # Accept JSON body using the Root model from models.py
    user=Depends(get_current_user_with_permissions),
) -> Root:
    """
    Add a new root.

    Args:
        root: Root object containing URI and name.
        user: Authenticated user.

    Returns:
        The added Root object.
    """
    logger.debug(f"User '{user}' requested to add root: {root}")
    return await root_service.add_root(str(root.uri), root.name)


@root_router.delete("/{uri:path}")
async def remove_root(
    uri: str,
    user=Depends(get_current_user_with_permissions),
) -> Dict[str, str]:
    """
    Remove a registered root by URI.

    Args:
        uri: URI of the root to remove.
        user: Authenticated user.

    Returns:
        Status message indicating result.
    """
    logger.debug(f"User '{user}' requested to remove root with URI: {uri}")
    await root_service.remove_root(uri)
    return {"status": "success", "message": f"Root {uri} removed"}


@root_router.get("/changes")
async def subscribe_roots_changes(
    user=Depends(get_current_user_with_permissions),
) -> StreamingResponse:
    """
    Subscribe to real-time changes in root list via Server-Sent Events (SSE).

    Args:
        user: Authenticated user.

    Returns:
        StreamingResponse with event-stream media type.
    """
    logger.debug(f"User '{user}' subscribed to root changes stream")
    return StreamingResponse(root_service.subscribe_changes(), media_type="text/event-stream")


##################
# Utility Routes #
##################
@utility_router.post("/rpc/")
@utility_router.post("/rpc")
async def handle_rpc(request: Request, db: Session = Depends(get_db), user=Depends(require_auth)):
    """Handle RPC requests.

    Args:
        request (Request): The incoming FastAPI request.
        db (Session): Database session.
        user: The authenticated user (dict with RBAC context).

    Returns:
        Response with the RPC result or error.
    """
    try:
        # Extract user identifier from either RBAC user object or JWT payload
        if hasattr(user, "email"):
            user_id = getattr(user, "email", None)  # RBAC user object
        elif isinstance(user, dict):
            user_id = user.get("sub") or user.get("email") or user.get("username", "unknown")  # JWT payload
        else:
            user_id = str(user)  # String username from basic auth

        logger.debug(f"User {user_id} made an RPC request")
        body = await request.json()
        method = body["method"]
        req_id = body.get("id") if "body" in locals() else None
        params = body.get("params", {})
        server_id = params.get("server_id", None)
        cursor = params.get("cursor")  # Extract cursor parameter

        RPCRequest(jsonrpc="2.0", method=method, params=params)  # Validate the request body against the RPCRequest model

        if method == "initialize":
            result = await session_registry.handle_initialize_logic(body.get("params", {}))
            if hasattr(result, "model_dump"):
                result = result.model_dump(by_alias=True, exclude_none=True)
        elif method == "tools/list":
            if server_id:
                tools = await tool_service.list_server_tools(db, server_id, cursor=cursor)
            else:
                tools = await tool_service.list_tools(db, cursor=cursor)
            result = {"tools": [t.model_dump(by_alias=True, exclude_none=True) for t in tools]}
        elif method == "list_tools":  # Legacy endpoint
            if server_id:
                tools = await tool_service.list_server_tools(db, server_id, cursor=cursor)
            else:
                tools = await tool_service.list_tools(db, cursor=cursor)
            result = {"tools": [t.model_dump(by_alias=True, exclude_none=True) for t in tools]}
        elif method == "list_gateways":
            gateways = await gateway_service.list_gateways(db, include_inactive=False)
            result = {"gateways": [g.model_dump(by_alias=True, exclude_none=True) for g in gateways]}
        elif method == "list_roots":
            roots = await root_service.list_roots()
            result = {"roots": [r.model_dump(by_alias=True, exclude_none=True) for r in roots]}
        elif method == "resources/list":
            if server_id:
                resources = await resource_service.list_server_resources(db, server_id)
            else:
                resources = await resource_service.list_resources(db)
            result = {"resources": [r.model_dump(by_alias=True, exclude_none=True) for r in resources]}
        elif method == "resources/read":
            uri = params.get("uri")
            request_id = params.get("requestId", None)
            if not uri:
                raise JSONRPCError(-32602, "Missing resource URI in parameters", params)
            result = await resource_service.read_resource(db, uri, request_id=request_id, user=get_user_email(user))
            if hasattr(result, "model_dump"):
                result = {"contents": [result.model_dump(by_alias=True, exclude_none=True)]}
            else:
                result = {"contents": [result]}
        elif method == "prompts/list":
            if server_id:
                prompts = await prompt_service.list_server_prompts(db, server_id, cursor=cursor)
            else:
                prompts = await prompt_service.list_prompts(db, cursor=cursor)
            result = {"prompts": [p.model_dump(by_alias=True, exclude_none=True) for p in prompts]}
        elif method == "prompts/get":
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not name:
                raise JSONRPCError(-32602, "Missing prompt name in parameters", params)
            result = await prompt_service.get_prompt(db, name, arguments)
            if hasattr(result, "model_dump"):
                result = result.model_dump(by_alias=True, exclude_none=True)
        elif method == "ping":
            # Per the MCP spec, a ping returns an empty result.
            result = {}
        elif method == "tools/call":
            # Get request headers
            headers = {k.lower(): v for k, v in request.headers.items()}
            name = params.get("name")
            arguments = params.get("arguments", {})
            if not name:
                raise JSONRPCError(-32602, "Missing tool name in parameters", params)
            try:
                result = await tool_service.invoke_tool(db=db, name=name, arguments=arguments, request_headers=headers)
                if hasattr(result, "model_dump"):
                    result = result.model_dump(by_alias=True, exclude_none=True)
            except ValueError:
                result = await gateway_service.forward_request(db, method, params)
                if hasattr(result, "model_dump"):
                    result = result.model_dump(by_alias=True, exclude_none=True)
        # TODO: Implement methods  # pylint: disable=fixme
        elif method == "resources/templates/list":
            result = {}
        elif method.startswith("roots/"):
            result = {}
        elif method.startswith("notifications/"):
            result = {}
        elif method.startswith("sampling/"):
            result = {}
        elif method.startswith("elicitation/"):
            result = {}
        elif method.startswith("completion/"):
            result = {}
        elif method.startswith("logging/"):
            result = {}
        else:
            # Backward compatibility: Try to invoke as a tool directly
            # This allows both old format (method=tool_name) and new format (method=tools/call)
            # Standard
            headers = {k.lower(): v for k, v in request.headers.items()}
            try:
                result = await tool_service.invoke_tool(db=db, name=method, arguments=params, request_headers=headers)
                if hasattr(result, "model_dump"):
                    result = result.model_dump(by_alias=True, exclude_none=True)
            except PluginError:
                raise
            except PluginViolationError:
                raise
            except (ValueError, Exception):
                # If not a tool, try forwarding to gateway
                try:
                    result = await gateway_service.forward_request(db, method, params)
                    if hasattr(result, "model_dump"):
                        result = result.model_dump(by_alias=True, exclude_none=True)
                except Exception:
                    # If all else fails, return invalid method error
                    raise JSONRPCError(-32000, "Invalid method", params)

        return {"jsonrpc": "2.0", "result": result, "id": req_id}

    except PluginError:
        raise
    except PluginViolationError:
        raise
    except JSONRPCError as e:
        error = e.to_dict()
        return {"jsonrpc": "2.0", "error": error["error"], "id": req_id}
    except Exception as e:
        if isinstance(e, ValueError):
            return JSONResponse(content={"message": "Method invalid"}, status_code=422)
        logger.error(f"RPC error: {str(e)}")
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32000, "message": "Internal error", "data": str(e)},
            "id": req_id,
        }


@utility_router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Handle WebSocket connection to relay JSON-RPC requests to the internal RPC endpoint.

    Accepts incoming text messages, parses them as JSON-RPC requests, sends them to /rpc,
    and returns the result to the client over the same WebSocket.

    Args:
        websocket: The WebSocket connection instance.
    """
    try:
        # Authenticate WebSocket connection
        if settings.mcp_client_auth_enabled or settings.auth_required:
            # Extract auth from query params or headers
            token = None
            # Try to get token from query parameter
            if "token" in websocket.query_params:
                token = websocket.query_params["token"]
            # Try to get token from Authorization header
            elif "authorization" in websocket.headers:
                auth_header = websocket.headers["authorization"]
                if auth_header.startswith("Bearer "):
                    token = auth_header[7:]

            # Check for proxy auth if MCP client auth is disabled
            if not settings.mcp_client_auth_enabled and settings.trust_proxy_auth:
                proxy_user = websocket.headers.get(settings.proxy_user_header)
                if not proxy_user and not token:
                    await websocket.close(code=1008, reason="Authentication required")
                    return
            elif settings.auth_required and not token:
                await websocket.close(code=1008, reason="Authentication required")
                return

            # Verify JWT token if provided and MCP client auth is enabled
            if token and settings.mcp_client_auth_enabled:
                try:
                    await verify_jwt_token(token)
                except Exception:
                    await websocket.close(code=1008, reason="Invalid authentication")
                    return

        await websocket.accept()
        while True:
            try:
                data = await websocket.receive_text()
                client_args = {"timeout": settings.federation_timeout, "verify": not settings.skip_ssl_verify}
                async with ResilientHttpClient(client_args=client_args) as client:
                    response = await client.post(
                        f"http://localhost:{settings.port}{settings.app_root_path}/rpc",
                        json=json.loads(data),
                        headers={"Content-Type": "application/json"},
                    )
                    await websocket.send_text(response.text)
            except JSONRPCError as e:
                await websocket.send_text(json.dumps(e.to_dict()))
            except json.JSONDecodeError:
                await websocket.send_text(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "error": {"code": -32700, "message": "Parse error"},
                            "id": None,
                        }
                    )
                )
            except Exception as e:
                logger.error(f"WebSocket error: {str(e)}")
                await websocket.close(code=1011)
                break
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except Exception as e:
        logger.error(f"WebSocket connection error: {str(e)}")
        try:
            await websocket.close(code=1011)
        except Exception as er:
            logger.error(f"Error while closing WebSocket: {er}")


@utility_router.get("/sse")
@require_permission("tools.invoke")
async def utility_sse_endpoint(request: Request, user=Depends(get_current_user_with_permissions)):
    """
    Establish a Server-Sent Events (SSE) connection for real-time updates.

    Args:
        request (Request): The incoming HTTP request.
        user (str): Authenticated username.

    Returns:
        StreamingResponse: A streaming response that keeps the connection
        open and pushes events to the client.

    Raises:
        HTTPException: Returned with **500 Internal Server Error** if the SSE connection cannot be established or an unexpected error occurs while creating the transport.
    """
    try:
        logger.debug("User %s requested SSE connection", user)
        base_url = update_url_protocol(request)

        transport = SSETransport(base_url=base_url)
        await transport.connect()
        await session_registry.add_session(transport.session_id, transport)

        asyncio.create_task(session_registry.respond(None, user, session_id=transport.session_id, base_url=base_url))

        response = await transport.create_sse_response(request)
        tasks = BackgroundTasks()
        tasks.add_task(session_registry.remove_session, transport.session_id)
        response.background = tasks
        logger.info("SSE connection established: %s", transport.session_id)
        return response
    except Exception as e:
        logger.error("SSE connection error: %s", e)
        raise HTTPException(status_code=500, detail="SSE connection failed")


@utility_router.post("/message")
@require_permission("tools.invoke")
async def utility_message_endpoint(request: Request, user=Depends(get_current_user_with_permissions)):
    """
    Handle a JSON-RPC message directed to a specific SSE session.

    Args:
        request (Request): Incoming request containing the JSON-RPC payload.
        user (str): Authenticated user.

    Returns:
        JSONResponse: ``{"status": "success"}`` with HTTP 202 on success.

    Raises:
        HTTPException: * **400 Bad Request** - ``session_id`` query parameter is missing or the payload cannot be parsed as JSON.
            * **500 Internal Server Error** - An unexpected error occurs while broadcasting the message.
    """
    try:
        logger.debug("User %s sent a message to SSE session", user)

        session_id = request.query_params.get("session_id")
        if not session_id:
            logger.error("Missing session_id in message request")
            raise HTTPException(status_code=400, detail="Missing session_id")

        message = await request.json()

        await session_registry.broadcast(
            session_id=session_id,
            message=message,
        )

        return JSONResponse(content={"status": "success"}, status_code=202)

    except ValueError as e:
        logger.error("Invalid message format: %s", e)
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Message handling error: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to process message")


@utility_router.post("/logging/setLevel")
@require_permission("admin.system_config")
async def set_log_level(request: Request, user=Depends(get_current_user_with_permissions)) -> None:
    """
    Update the server's log level at runtime.

    Args:
        request: HTTP request with log level JSON body.
        user: Authenticated user.

    Returns:
        None
    """
    logger.debug(f"User {user} requested to set log level")
    body = await request.json()
    level = LogLevel(body["level"])
    await logging_service.set_level(level)
    return None


####################
# Metrics          #
####################
@metrics_router.get("", response_model=dict)
@require_permission("admin.metrics")
async def get_metrics(db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)) -> dict:
    """
    Retrieve aggregated metrics for all entity types (Tools, Resources, Servers, Prompts, A2A Agents).

    Args:
        db: Database session
        user: Authenticated user

    Returns:
        A dictionary with keys for each entity type and their aggregated metrics.
    """
    logger.debug(f"User {user} requested aggregated metrics")
    tool_metrics = await tool_service.aggregate_metrics(db)
    resource_metrics = await resource_service.aggregate_metrics(db)
    server_metrics = await server_service.aggregate_metrics(db)
    prompt_metrics = await prompt_service.aggregate_metrics(db)

    metrics_result = {
        "tools": tool_metrics,
        "resources": resource_metrics,
        "servers": server_metrics,
        "prompts": prompt_metrics,
    }

    # Include A2A metrics only if A2A features are enabled
    if a2a_service and settings.mcpgateway_a2a_metrics_enabled:
        a2a_metrics = await a2a_service.aggregate_metrics(db)
        metrics_result["a2a_agents"] = a2a_metrics

    return metrics_result


@metrics_router.post("/reset", response_model=dict)
@require_permission("admin.metrics")
async def reset_metrics(entity: Optional[str] = None, entity_id: Optional[int] = None, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)) -> dict:
    """
    Reset metrics for a specific entity type and optionally a specific entity ID,
    or perform a global reset if no entity is specified.

    Args:
        entity: One of "tool", "resource", "server", "prompt", "a2a_agent", or None for global reset.
        entity_id: Specific entity ID to reset metrics for (optional).
        db: Database session
        user: Authenticated user

    Returns:
        A success message in a dictionary.

    Raises:
        HTTPException: If an invalid entity type is specified.
    """
    logger.debug(f"User {user} requested metrics reset for entity: {entity}, id: {entity_id}")
    if entity is None:
        # Global reset
        await tool_service.reset_metrics(db)
        await resource_service.reset_metrics(db)
        await server_service.reset_metrics(db)
        await prompt_service.reset_metrics(db)
        if a2a_service and settings.mcpgateway_a2a_metrics_enabled:
            await a2a_service.reset_metrics(db)
    elif entity.lower() == "tool":
        await tool_service.reset_metrics(db, entity_id)
    elif entity.lower() == "resource":
        await resource_service.reset_metrics(db)
    elif entity.lower() == "server":
        await server_service.reset_metrics(db)
    elif entity.lower() == "prompt":
        await prompt_service.reset_metrics(db)
    elif entity.lower() in ("a2a_agent", "a2a"):
        if a2a_service and settings.mcpgateway_a2a_metrics_enabled:
            await a2a_service.reset_metrics(db, entity_id)
        else:
            raise HTTPException(status_code=400, detail="A2A features are disabled")
    else:
        raise HTTPException(status_code=400, detail="Invalid entity type for metrics reset")
    return {"status": "success", "message": f"Metrics reset for {entity if entity else 'all entities'}"}


####################
# Healthcheck      #
####################
@app.get("/health")
async def healthcheck(db: Session = Depends(get_db)):
    """
    Perform a basic health check to verify database connectivity.

    Args:
        db: SQLAlchemy session dependency.

    Returns:
        A dictionary with the health status and optional error message.
    """
    try:
        # Execute the query using text() for an explicit textual SQL expression.
        db.execute(text("SELECT 1"))
    except Exception as e:
        error_message = f"Database connection error: {str(e)}"
        logger.error(error_message)
        return {"status": "unhealthy", "error": error_message}
    return {"status": "healthy"}


@app.get("/ready")
async def readiness_check(db: Session = Depends(get_db)):
    """
    Perform a readiness check to verify if the application is ready to receive traffic.

    Args:
        db: SQLAlchemy session dependency.

    Returns:
        JSONResponse with status 200 if ready, 503 if not.
    """
    try:
        # Run the blocking DB check in a thread to avoid blocking the event loop
        await asyncio.to_thread(db.execute, text("SELECT 1"))
        return JSONResponse(content={"status": "ready"}, status_code=200)
    except Exception as e:
        error_message = f"Readiness check failed: {str(e)}"
        logger.error(error_message)
        return JSONResponse(content={"status": "not ready", "error": error_message}, status_code=503)


####################
# Tag Endpoints    #
####################


@tag_router.get("", response_model=List[TagInfo])
@tag_router.get("/", response_model=List[TagInfo])
@require_permission("tags.read")
async def list_tags(
    entity_types: Optional[str] = None,
    include_entities: bool = False,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> List[TagInfo]:
    """
    Retrieve all unique tags across specified entity types.

    Args:
        entity_types: Comma-separated list of entity types to filter by
                     (e.g., "tools,resources,prompts,servers,gateways").
                     If not provided, returns tags from all entity types.
        include_entities: Whether to include the list of entities that have each tag
        db: Database session
        user: Authenticated user

    Returns:
        List of TagInfo objects containing tag names, statistics, and optionally entities

    Raises:
        HTTPException: If tag retrieval fails
    """
    # Parse entity types parameter if provided
    entity_types_list = None
    if entity_types:
        entity_types_list = [et.strip().lower() for et in entity_types.split(",") if et.strip()]

    logger.debug(f"User {user} is retrieving tags for entity types: {entity_types_list}, include_entities: {include_entities}")

    try:
        tags = await tag_service.get_all_tags(db, entity_types=entity_types_list, include_entities=include_entities)
        return tags
    except Exception as e:
        logger.error(f"Failed to retrieve tags: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve tags: {str(e)}")


@tag_router.get("/{tag_name}/entities", response_model=List[TaggedEntity])
@require_permission("tags.read")
async def get_entities_by_tag(
    tag_name: str,
    entity_types: Optional[str] = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> List[TaggedEntity]:
    """
    Get all entities that have a specific tag.

    Args:
        tag_name: The tag to search for
        entity_types: Comma-separated list of entity types to filter by
                     (e.g., "tools,resources,prompts,servers,gateways").
                     If not provided, returns entities from all types.
        db: Database session
        user: Authenticated user

    Returns:
        List of TaggedEntity objects

    Raises:
        HTTPException: If entity retrieval fails
    """
    # Parse entity types parameter if provided
    entity_types_list = None
    if entity_types:
        entity_types_list = [et.strip().lower() for et in entity_types.split(",") if et.strip()]

    logger.debug(f"User {user} is retrieving entities for tag '{tag_name}' with entity types: {entity_types_list}")

    try:
        entities = await tag_service.get_entities_by_tag(db, tag_name=tag_name, entity_types=entity_types_list)
        return entities
    except Exception as e:
        logger.error(f"Failed to retrieve entities for tag '{tag_name}': {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to retrieve entities: {str(e)}")


####################
# Export/Import    #
####################


@export_import_router.get("/export", response_model=Dict[str, Any])
@require_permission("admin.export")
async def export_configuration(
    request: Request,
    export_format: str = "json",  # pylint: disable=unused-argument
    types: Optional[str] = None,
    exclude_types: Optional[str] = None,
    tags: Optional[str] = None,
    include_inactive: bool = False,
    include_dependencies: bool = True,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> Dict[str, Any]:
    """
    Export gateway configuration to JSON format.

    Args:
        request: FastAPI request object for extracting root path
        export_format: Export format (currently only 'json' supported)
        types: Comma-separated list of entity types to include (tools,gateways,servers,prompts,resources,roots)
        exclude_types: Comma-separated list of entity types to exclude
        tags: Comma-separated list of tags to filter by
        include_inactive: Whether to include inactive entities
        include_dependencies: Whether to include dependent entities
        db: Database session
        user: Authenticated user

    Returns:
        Export data in the specified format

    Raises:
        HTTPException: If export fails
    """
    try:
        logger.info(f"User {user} requested configuration export")
        username: Optional[str] = None
        # Parse parameters
        include_types = None
        if types:
            include_types = [t.strip() for t in types.split(",") if t.strip()]

        exclude_types_list = None
        if exclude_types:
            exclude_types_list = [t.strip() for t in exclude_types.split(",") if t.strip()]

        tags_list = None
        if tags:
            tags_list = [t.strip() for t in tags.split(",") if t.strip()]

        # Extract username from user (which is now an EmailUser object)
        if hasattr(user, "email"):
            username = getattr(user, "email", None)
        elif isinstance(user, dict):
            username = user.get("email", None)
        else:
            username = None

        # Get root path for URL construction
        root_path = request.scope.get("root_path", "") if request else ""

        # Perform export
        export_data = await export_service.export_configuration(
            db=db,
            include_types=include_types,
            exclude_types=exclude_types_list,
            tags=tags_list,
            include_inactive=include_inactive,
            include_dependencies=include_dependencies,
            exported_by=username,
            root_path=root_path,
        )

        return export_data

    except ExportError as e:
        logger.error(f"Export failed for user {user}: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected export error for user {user}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@export_import_router.post("/export/selective", response_model=Dict[str, Any])
@require_permission("admin.export")
async def export_selective_configuration(
    entity_selections: Dict[str, List[str]] = Body(...), include_dependencies: bool = True, db: Session = Depends(get_db), user=Depends(get_current_user_with_permissions)
) -> Dict[str, Any]:
    """
    Export specific entities by their IDs/names.

    Args:
        entity_selections: Dict mapping entity types to lists of IDs/names to export
        include_dependencies: Whether to include dependent entities
        db: Database session
        user: Authenticated user

    Returns:
        Selective export data

    Raises:
        HTTPException: If export fails

    Example request body:
        {
            "tools": ["tool1", "tool2"],
            "servers": ["server1"],
            "prompts": ["prompt1"]
        }
    """
    try:
        logger.info(f"User {user} requested selective configuration export")

        username: Optional[str] = None
        # Extract username from user (which is now an EmailUser object)
        if hasattr(user, "email"):
            username = getattr(user, "email", None)
        elif isinstance(user, dict):
            username = user.get("email")

        export_data = await export_service.export_selective(db=db, entity_selections=entity_selections, include_dependencies=include_dependencies, exported_by=username)

        return export_data

    except ExportError as e:
        logger.error(f"Selective export failed for user {user}: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected selective export error for user {user}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")


@export_import_router.post("/import", response_model=Dict[str, Any])
@require_permission("admin.import")
async def import_configuration(
    import_data: Dict[str, Any] = Body(...),
    conflict_strategy: str = "update",
    dry_run: bool = False,
    rekey_secret: Optional[str] = None,
    selected_entities: Optional[Dict[str, List[str]]] = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user_with_permissions),
) -> Dict[str, Any]:
    """
    Import configuration data with conflict resolution.

    Args:
        import_data: The configuration data to import
        conflict_strategy: How to handle conflicts: skip, update, rename, fail
        dry_run: If true, validate but don't make changes
        rekey_secret: New encryption secret for cross-environment imports
        selected_entities: Dict of entity types to specific entity names/ids to import
        db: Database session
        user: Authenticated user

    Returns:
        Import status and results

    Raises:
        HTTPException: If import fails or validation errors occur
    """
    try:
        logger.info(f"User {user} requested configuration import (dry_run={dry_run})")

        # Validate conflict strategy
        try:
            strategy = ConflictStrategy(conflict_strategy.lower())
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid conflict strategy. Must be one of: {[s.value for s in ConflictStrategy]}")

        # Extract username from user (which is now an EmailUser object)
        if hasattr(user, "email"):
            username = getattr(user, "email", None)
        elif isinstance(user, dict):
            username = user.get("email", None)
        else:
            username = None

        # Perform import
        import_status = await import_service.import_configuration(
            db=db, import_data=import_data, conflict_strategy=strategy, dry_run=dry_run, rekey_secret=rekey_secret, imported_by=username, selected_entities=selected_entities
        )

        return import_status.to_dict()

    except ImportValidationError as e:
        logger.error(f"Import validation failed for user {user}: {str(e)}")
        raise HTTPException(status_code=422, detail=f"Validation error: {str(e)}")
    except ImportConflictError as e:
        logger.error(f"Import conflict for user {user}: {str(e)}")
        raise HTTPException(status_code=409, detail=f"Conflict error: {str(e)}")
    except ImportServiceError as e:
        logger.error(f"Import failed for user {user}: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Unexpected import error for user {user}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")


@export_import_router.get("/import/status/{import_id}", response_model=Dict[str, Any])
@require_permission("admin.import")
async def get_import_status(import_id: str, user=Depends(get_current_user_with_permissions)) -> Dict[str, Any]:
    """
    Get the status of an import operation.

    Args:
        import_id: The import operation ID
        user: Authenticated user

    Returns:
        Import status information

    Raises:
        HTTPException: If import not found
    """
    logger.debug(f"User {user} requested import status for {import_id}")

    import_status = import_service.get_import_status(import_id)
    if not import_status:
        raise HTTPException(status_code=404, detail=f"Import {import_id} not found")

    return import_status.to_dict()


@export_import_router.get("/import/status", response_model=List[Dict[str, Any]])
@require_permission("admin.import")
async def list_import_statuses(user=Depends(get_current_user_with_permissions)) -> List[Dict[str, Any]]:
    """
    List all import operation statuses.

    Args:
        user: Authenticated user

    Returns:
        List of import status information
    """
    logger.debug(f"User {user} requested all import statuses")

    statuses = import_service.list_import_statuses()
    return [status.to_dict() for status in statuses]


@export_import_router.post("/import/cleanup", response_model=Dict[str, Any])
@require_permission("admin.import")
async def cleanup_import_statuses(max_age_hours: int = 24, user=Depends(get_current_user_with_permissions)) -> Dict[str, Any]:
    """
    Clean up completed import statuses older than specified age.

    Args:
        max_age_hours: Maximum age in hours for keeping completed imports
        user: Authenticated user

    Returns:
        Cleanup results
    """
    logger.info(f"User {user} requested import status cleanup (max_age_hours={max_age_hours})")

    removed_count = import_service.cleanup_completed_imports(max_age_hours)
    return {"status": "success", "message": f"Cleaned up {removed_count} completed import statuses", "removed_count": removed_count}


# Mount static files
# app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")

# Include routers
app.include_router(version_router)
app.include_router(protocol_router)
app.include_router(tool_router)
app.include_router(resource_router)
app.include_router(prompt_router)
app.include_router(gateway_router)
app.include_router(root_router)
app.include_router(utility_router)
app.include_router(server_router)
app.include_router(metrics_router)
app.include_router(tag_router)
app.include_router(export_import_router)

# Conditionally include A2A router if A2A features are enabled
if settings.mcpgateway_a2a_enabled:
    app.include_router(a2a_router)
    logger.info("A2A router included - A2A features enabled")
else:
    logger.info("A2A router not included - A2A features disabled")

app.include_router(well_known_router)

# Include Email Authentication router if enabled
if settings.email_auth_enabled:
    try:
        # First-Party
        from mcpgateway.routers.auth import auth_router
        from mcpgateway.routers.email_auth import email_auth_router

        app.include_router(email_auth_router, prefix="/auth/email", tags=["Email Authentication"])
        app.include_router(auth_router, tags=["Main Authentication"])
        logger.info("Authentication routers included - Auth enabled")

        # Include SSO router if enabled
        if settings.sso_enabled:
            try:
                # First-Party
                from mcpgateway.routers.sso import sso_router

                app.include_router(sso_router, tags=["SSO Authentication"])
                logger.info("SSO router included - SSO authentication enabled")
            except ImportError as e:
                logger.error(f"SSO router not available: {e}")
        else:
            logger.info("SSO router not included - SSO authentication disabled")
    except ImportError as e:
        logger.error(f"Authentication routers not available: {e}")
else:
    logger.info("Email authentication router not included - Email auth disabled")

# Include Team Management router if email auth is enabled
if settings.email_auth_enabled:
    try:
        # First-Party
        from mcpgateway.routers.teams import teams_router

        app.include_router(teams_router, prefix="/teams", tags=["Teams"])
        logger.info("Team management router included - Teams enabled with email auth")
    except ImportError as e:
        logger.error(f"Team management router not available: {e}")
else:
    logger.info("Team management router not included - Email auth disabled")

# Include JWT Token Catalog router if email auth is enabled
if settings.email_auth_enabled:
    try:
        # First-Party
        from mcpgateway.routers.tokens import router as tokens_router

        app.include_router(tokens_router, tags=["JWT Token Catalog"])
        logger.info("JWT Token Catalog router included - Token management enabled with email auth")
    except ImportError as e:
        logger.error(f"JWT Token Catalog router not available: {e}")
else:
    logger.info("JWT Token Catalog router not included - Email auth disabled")

# Include RBAC router if email auth is enabled
if settings.email_auth_enabled:
    try:
        # First-Party
        from mcpgateway.routers.rbac import router as rbac_router

        app.include_router(rbac_router, tags=["RBAC"])
        logger.info("RBAC router included - Role-based access control enabled")
    except ImportError as e:
        logger.error(f"RBAC router not available: {e}")
else:
    logger.info("RBAC router not included - Email auth disabled")

# Include OAuth router
try:
    # First-Party
    from mcpgateway.routers.oauth_router import oauth_router

    app.include_router(oauth_router)
    logger.info("OAuth router included")
except ImportError:
    logger.debug("OAuth router not available")

# Include reverse proxy router if enabled
try:
    # First-Party
    from mcpgateway.routers.reverse_proxy import router as reverse_proxy_router

    app.include_router(reverse_proxy_router)
    logger.info("Reverse proxy router included")
except ImportError:
    logger.debug("Reverse proxy router not available")

# Feature flags for admin UI and API
UI_ENABLED = settings.mcpgateway_ui_enabled
ADMIN_API_ENABLED = settings.mcpgateway_admin_api_enabled
logger.info(f"Admin UI enabled: {UI_ENABLED}")
logger.info(f"Admin API enabled: {ADMIN_API_ENABLED}")

# Conditional UI and admin API handling
if ADMIN_API_ENABLED:
    logger.info("Including admin_router - Admin API enabled")
    app.include_router(admin_router)  # Admin routes imported from admin.py
else:
    logger.warning("Admin API routes not mounted - Admin API disabled via MCPGATEWAY_ADMIN_API_ENABLED=False")

# Streamable http Mount
app.mount("/mcp", app=streamable_http_session.handle_streamable_http)

# Conditional static files mounting and root redirect
if UI_ENABLED:
    # Mount static files for UI
    logger.info("Mounting static files - UI enabled")
    try:
        # Create a sub-application for static files that will respect root_path
        static_app = StaticFiles(directory=str(settings.static_dir))
        STATIC_PATH = f"{settings.app_root_path}/static" if settings.app_root_path else "/static"

        app.mount(
            STATIC_PATH,
            static_app,
            name="static",
        )
        logger.info("Static assets served from %s at %s", settings.static_dir, STATIC_PATH)
    except RuntimeError as exc:
        logger.warning(
            "Static dir %s not found - Admin UI disabled (%s)",
            settings.static_dir,
            exc,
        )

    # Redirect root path to admin UI
    @app.get("/")
    async def root_redirect(request: Request):
        """
        Redirects the root path ("/") to "/admin".

        Logs a debug message before redirecting.

        Args:
            request (Request): The incoming HTTP request (used only to build the
                target URL via :pymeth:`starlette.requests.Request.url_for`).

        Returns:
            RedirectResponse: Redirects to /admin.
        """
        logger.debug("Redirecting root path to /admin")
        root_path = request.scope.get("root_path", "")
        return RedirectResponse(f"{root_path}/admin", status_code=303)
        # return RedirectResponse(request.url_for("admin_home"))

else:
    # If UI is disabled, provide API info at root
    logger.warning("Static files not mounted - UI disabled via MCPGATEWAY_UI_ENABLED=False")

    @app.get("/")
    async def root_info():
        """
        Returns basic API information at the root path.

        Logs an info message indicating UI is disabled and provides details
        about the app, including its name, version, and whether the UI and
        admin API are enabled.

        Returns:
            dict: API info with app name, version, and UI/admin API status.
        """
        logger.info("UI disabled, serving API info at root path")
        return {"name": settings.app_name, "version": __version__, "description": f"{settings.app_name} API - UI is disabled", "ui_enabled": False, "admin_api_enabled": ADMIN_API_ENABLED}


# Expose some endpoints at the root level as well
app.post("/initialize")(initialize)
app.post("/notifications")(handle_notification)
