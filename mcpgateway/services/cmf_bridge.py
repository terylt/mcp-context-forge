# -*- coding: utf-8 -*-
"""Location: ./mcpgateway/services/cmf_bridge.py
Copyright 2026
SPDX-License-Identifier: Apache-2.0
Authors: Teryl Taylor


CMF Bridge — constructs CMF messages from gateway request context
and dispatches them through CMF hooks.

Bridges the existing tool_service flow into the CMF/APL world. Returns
the same (PluginResult, PluginContextTable) tuple that tool_service
already handles for legacy hooks.

Usage in tool_service.py:

    # Pre-invoke
    cmf_result, context_table = await self._cmf_bridge.pre_invoke(
        tool_name=name, arguments=arguments,
        request_headers=request_headers, user_email=app_user_email,
        token_teams=token_teams, global_context=global_context,
        context_table=context_table,
    )
    if not cmf_result.continue_processing:
        raise PluginViolationError(cmf_result.violation)

    # Post-invoke
    cmf_result, context_table = await self._cmf_bridge.post_invoke(
        tool_name=name, result_data=tool_result.model_dump(by_alias=True),
        request_headers=request_headers, user_email=app_user_email,
        token_teams=token_teams, global_context=global_context,
        context_table=context_table,
    )
    if not cmf_result.continue_processing:
        # handle denial (result already executed, but session labels applied)
"""

import logging
from typing import Any, Optional

from cpex.framework import GlobalContext, PluginManager
from cpex.framework.cmf.message import (
    Channel,
    Message,
    Role,
    ToolCall,
    ToolCallContentPart,
    ToolResult as CmfToolResult,
    ToolResultContentPart,
)
from cpex.framework.extensions.extensions import Extensions
from cpex.framework.extensions.request import RequestExtension
from cpex.framework.extensions.security import (
    SecurityExtension,
    SubjectExtension,
    SubjectType,
)
from cpex.framework.extensions.delegation import DelegationExtension, DelegationHop
from cpex.framework.hooks.identity import (
    IdentityHookType,
    IdentityPayload,
    IdentityResult,
    DelegationPayload,
    DelegationResult,
)
from cpex.framework.hooks.message import CmfHookType, MessageHookType, MessagePayload
from cpex.framework.cmf.view import ViewKind, iter_views
from cpex.framework.models import PluginContextTable, PluginResult

from cpex.framework.errors import PluginViolationError
from cpex.framework.models import PluginViolation

logger = logging.getLogger(__name__)

# Header for explicit user identity delegation
ON_BEHALF_OF_HEADER = "x-on-behalf-of"
# Header carrying the end-user's token for identity resolution
USER_TOKEN_HEADER = "x-user-token"


class IdentityRejectedError(PluginViolationError):
    """Raised when the identity_resolve hook explicitly rejects a token."""

    def __init__(self, reason: str, status: int = 401):
        violation = PluginViolation(
            reason=reason,
            description=f"Identity rejected: {reason}",
            code="IDENTITY_REJECTED",
            details={"status": status},
        )
        super().__init__(reason, violation=violation)
        self.status = status


class TokenDelegationError(PluginViolationError):
    """Raised when the token_delegate hook explicitly rejects a delegation."""

    def __init__(self, reason: str, tool_name: str):
        violation = PluginViolation(
            reason=reason,
            description=f"Token delegation failed for {tool_name}: {reason}",
            code="TOKEN_DELEGATION_FAILED",
            details={"tool": tool_name},
        )
        super().__init__(reason, violation=violation)
        self.tool_name = tool_name


class CmfBridge:
    """Bridges gateway request context into CMF messages for APL evaluation.

    Handles the two-layer identity model:
    - Layer 1 (gateway access): Already handled by auth.py
    - Layer 2 (end-user identity): Extracted here from the auth context,
      mapped to CMF SubjectExtension + DelegationExtension

    Returns (PluginResult, PluginContextTable) — the same tuple that
    tool_service handles for legacy hooks. The caller checks
    result.continue_processing and result.modified_payload just like
    it does for ToolPreInvokePayload / ToolPostInvokePayload results.
    """

    def __init__(self, plugin_manager: Optional[PluginManager]):
        self._pm = plugin_manager

    def _has_hooks(self, hook_type: str) -> bool:
        return bool(self._pm and self._pm.has_hooks_for(hook_type))

    # -----------------------------------------------------------------
    # Identity extraction
    # -----------------------------------------------------------------

    async def resolve_identity(
        self,
        request_headers: Optional[dict[str, str]],
        user_email: Optional[str],
        token_teams: Optional[list[str]],
        global_context: Optional[GlobalContext] = None,
    ) -> tuple[SubjectExtension | None, DelegationExtension | None]:
        """Resolve end-user identity via the identity_resolve hook or fallback.

        Resolution priority:
        1. X-User-Token header → identity_resolve hook (plugin decodes token)
        2. X-On-Behalf-Of header (explicit delegation, no token)
        3. Gateway authenticated user (direct caller)

        The identity_resolve hook allows plugins to decode a user token
        (JWT, opaque, etc.) into a full SubjectExtension with roles,
        permissions, and delegation chain. This supports the two-credential
        pattern where the agent authenticates to the gateway with its own
        token, and the end-user's identity rides in a separate header.

        Raises:
            IdentityRejectedError: If the identity_resolve hook explicitly
                rejects the token (invalid, expired, etc.). This is a hard
                stop — not a fallback condition.
        """
        # Priority 1: User token header → invoke identity_resolve hook
        # Header lookup is case-insensitive (transports may normalize to lowercase)
        user_token = None
        if request_headers:
            headers_lower = {k.lower(): v for k, v in request_headers.items()}
            user_token = headers_lower.get(USER_TOKEN_HEADER)
        if (
            user_token
            and self._has_hooks(IdentityHookType.IDENTITY_RESOLVE.value)
        ):
            raw_token = user_token
            if raw_token.lower().startswith("bearer "):
                raw_token = raw_token[7:]

            try:
                result, _ = await self._pm.invoke_hook(
                    hook_type=IdentityHookType.IDENTITY_RESOLVE.value,
                    payload=IdentityPayload(
                        raw_token=raw_token,
                        source="bearer",
                        headers=request_headers or {},
                    ),
                    global_context=global_context or GlobalContext(request_id="unknown"),
                    local_contexts=None,
                    violations_as_exceptions=False,
                )

                if result.modified_payload:
                    identity: IdentityResult = result.modified_payload
                    if identity.rejected:
                        raise IdentityRejectedError(
                            identity.reject_reason or "Identity rejected",
                            status=identity.reject_status,
                        )
                    if identity.subject:
                        logger.debug(
                            "Identity resolved via hook: %s (%s)",
                            identity.subject.id,
                            identity.subject.type.value if hasattr(identity.subject.type, "value") else identity.subject.type,
                        )
                        # Build delegation: agent (gateway user) acting on behalf of resolved user
                        delegation = identity.delegation
                        if not delegation and user_email and user_email != identity.subject.id:
                            delegation = DelegationExtension(
                                chain=(
                                    DelegationHop(
                                        subject_id=identity.subject.id,
                                        subject_type=identity.subject.type.value if hasattr(identity.subject.type, "value") else str(identity.subject.type),
                                    ),
                                ),
                                depth=1,
                                origin_subject_id=identity.subject.id,
                                actor_subject_id=user_email,
                                delegated=True,
                            )
                        return identity.subject, delegation
            except IdentityRejectedError:
                raise
            except Exception as e:
                # Hook infrastructure failure — fall back to gateway identity.
                # A broken plugin shouldn't block a request that the gateway
                # already authenticated.
                logger.warning("Identity resolve hook failed, falling back: %s", e)

        # Priority 2 & 3: fallback to header/email-based resolution
        return self._build_subject(user_email, token_teams, request_headers)

    def _build_subject(
        self,
        user_email: Optional[str],
        token_teams: Optional[list[str]],
        request_headers: Optional[dict[str, str]],
    ) -> tuple[SubjectExtension | None, DelegationExtension | None]:
        """Build SubjectExtension from gateway auth context (fallback).

        Used when no identity_resolve hook is registered or no user
        token header is present.

        Resolution priority:
        1. X-On-Behalf-Of header (explicit delegation from agent framework)
        2. Gateway authenticated user (direct caller)
        """
        delegation = None

        # Priority 1: Explicit on-behalf-of header
        if request_headers and ON_BEHALF_OF_HEADER in request_headers:
            end_user = request_headers[ON_BEHALF_OF_HEADER]
            subject = SubjectExtension(
                id=end_user,
                type=SubjectType.USER,
                teams=frozenset(token_teams) if token_teams else frozenset(),
            )
            if user_email and user_email != end_user:
                delegation = DelegationExtension(
                    chain=(
                        DelegationHop(
                            subject_id=end_user,
                            subject_type="user",
                        ),
                    ),
                    depth=1,
                    origin_subject_id=end_user,
                    actor_subject_id=user_email,
                    delegated=True,
                )
            return subject, delegation

        # Priority 2: Gateway user IS the end user
        if user_email:
            subject = SubjectExtension(
                id=user_email,
                type=SubjectType.USER,
                teams=frozenset(token_teams) if token_teams else frozenset(),
            )
            return subject, None

        return None, None

    # -----------------------------------------------------------------
    # Message construction
    # -----------------------------------------------------------------

    def _build_message(
        self,
        role: Role,
        content_part: Any,
    ) -> Message:
        return Message(
            role=role,
            content=[content_part],
            channel=Channel.FINAL,
        )

    @staticmethod
    def _build_extensions(
        subject: SubjectExtension | None,
        delegation: DelegationExtension | None,
        request_id: str | None,
    ) -> Extensions:
        return Extensions(
            request=RequestExtension(
                request_id=request_id or "unknown",
                environment="gateway",
            ),
            security=SecurityExtension(subject=subject) if subject else None,
            delegation=delegation,
        )

    # -----------------------------------------------------------------
    # Hook dispatch
    # -----------------------------------------------------------------

    async def pre_invoke(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        request_headers: Optional[dict[str, str]] = None,
        user_email: Optional[str] = None,
        token_teams: Optional[list[str]] = None,
        global_context: Optional[GlobalContext] = None,
        context_table: Optional[PluginContextTable] = None,
        violations_as_exceptions: bool = True,
    ) -> tuple[PluginResult, Optional[PluginContextTable]]:
        """Fire cmf.tool_pre_invoke and token_delegate. Returns (PluginResult, context_table).

        Pipeline: resolve identity → evaluate policy → delegate token.

        The PluginResult follows the same contract as legacy hooks:
        - continue_processing=False + violation → tool_service raises or blocks
        - modified_payload (MessagePayload) → contains modified CMF message
          with potentially transformed tool call arguments
        - metadata → additional context from plugins
        - metadata["delegated_headers"] → forwarded headers from token delegation

        Raises PluginViolationError if violations_as_exceptions=True and
        the plugin denies.
        """
        if not self._has_hooks(CmfHookType.TOOL_PRE_INVOKE.value):
            return PluginResult(), context_table

        subject, delegation = await self.resolve_identity(
            request_headers, user_email, token_teams, global_context,
        )

        request_id = global_context.request_id if global_context else None
        tool_call = ToolCall(
            tool_call_id=f"tc-{tool_name}-{request_id or 'unknown'}",
            name=tool_name,
            arguments=arguments,
        )
        message = self._build_message(
            Role.ASSISTANT,
            ToolCallContentPart(content=tool_call),
        )
        extensions = self._build_extensions(subject, delegation, request_id)

        gc = global_context or GlobalContext(request_id="unknown")
        result, context_table = await self._pm.invoke_hook(
            hook_type=CmfHookType.TOOL_PRE_INVOKE.value,
            payload=MessagePayload(message=message, hook=MessageHookType.TOOL_PRE_INVOKE),
            global_context=gc,
            local_contexts=context_table,
            violations_as_exceptions=violations_as_exceptions,
            extensions=extensions,
        )

        # If policy denied, skip delegation
        if not result.continue_processing:
            return result, context_table

        # Token delegation: exchange user token for narrower downstream token.
        # The delegator sets modified_extensions.http with forwarded headers.
        deleg_result = await self._delegate_token(
            tool_name, request_headers, gc, extensions,
        )
        if deleg_result and deleg_result.modified_extensions:
            result.modified_extensions = deleg_result.modified_extensions

        return result, context_table

    async def _delegate_token(
        self,
        tool_name: str,
        request_headers: Optional[dict[str, str]],
        global_context: GlobalContext,
        extensions: Optional[Extensions] = None,
    ) -> Optional[PluginResult]:
        """Fire token_delegate hook if registered and user token is present.

        Returns the PluginResult (with modified_extensions) or None.

        Raises:
            TokenDelegationError: If the plugin explicitly rejects the exchange.
        """
        if not self._has_hooks(IdentityHookType.TOKEN_DELEGATE.value):
            return None

        user_token = None
        if request_headers:
            headers_lower = {k.lower(): v for k, v in request_headers.items()}
            user_token = headers_lower.get(USER_TOKEN_HEADER)
        if not user_token:
            return None

        if user_token.lower().startswith("bearer "):
            user_token = user_token[7:]

        try:
            result, _ = await self._pm.invoke_hook(
                hook_type=IdentityHookType.TOKEN_DELEGATE.value,
                payload=DelegationPayload(
                    target_name=tool_name,
                    target_type="tool",
                    bearer_token=user_token,
                    auth_enforced_by="target",
                ),
                global_context=global_context,
                local_contexts=None,
                violations_as_exceptions=False,
                extensions=extensions,
            )

            if not result.continue_processing:
                raise TokenDelegationError(
                    "Delegation rejected by plugin",
                    tool_name=tool_name,
                )

            return result
        except TokenDelegationError:
            raise
        except Exception as e:
            logger.error("Token delegation hook failed for %s: %s", tool_name, e)

        return None

    @staticmethod
    def extract_modified_args(result: PluginResult) -> Optional[dict[str, Any]]:
        """Extract modified tool arguments from a CMF PluginResult.

        Call this after pre_invoke to get updated arguments, mirroring
        how tool_service reads pre_result.modified_payload.args for
        the legacy hook.

        Returns:
            Modified args dict, or None if no modifications.
        """
        if not result.modified_payload:
            return None
        try:
            message = result.modified_payload.message
            for view in iter_views(message):
                if view.kind == ViewKind.TOOL_CALL and view.args:
                    return dict(view.args)
        except Exception:
            pass
        return None

    @staticmethod
    def extract_modified_result(result: PluginResult) -> Optional[dict[str, Any]]:
        """Extract modified tool result from a CMF PluginResult.

        Call this after post_invoke to get transformed result, mirroring
        how tool_service reads post_result.modified_payload.result for
        the legacy hook.

        Returns:
            Modified result dict, or None if no modifications.
        """
        if not result.modified_payload:
            return None
        try:
            message = result.modified_payload.message
            for view in iter_views(message):
                if view.kind == ViewKind.TOOL_RESULT:
                    inner = view.raw.content if hasattr(view.raw, "content") else None
                    if inner and hasattr(inner, "content"):
                        content = inner.content
                        if isinstance(content, dict):
                            return content
        except Exception:
            pass
        return None

    @staticmethod
    def extract_delegated_headers(result: PluginResult) -> Optional[dict[str, str]]:
        """Extract delegated HTTP headers from modified_extensions.

        Call this after pre_invoke to get forwarded headers from token delegation.

        Returns:
            Headers dict, or None if no delegation occurred.
        """
        if not result.modified_extensions:
            return None
        if result.modified_extensions.http and result.modified_extensions.http.headers:
            return dict(result.modified_extensions.http.headers)
        return None

    async def post_invoke(
        self,
        tool_name: str,
        result_data: Any,
        request_headers: Optional[dict[str, str]] = None,
        user_email: Optional[str] = None,
        token_teams: Optional[list[str]] = None,
        global_context: Optional[GlobalContext] = None,
        context_table: Optional[PluginContextTable] = None,
        violations_as_exceptions: bool = False,
    ) -> tuple[PluginResult, Optional[PluginContextTable]]:
        """Fire cmf.tool_post_invoke. Returns (PluginResult, context_table).

        Same contract as pre_invoke. Post-invoke defaults to
        violations_as_exceptions=False since the tool already executed —
        session labels still get applied regardless.
        """
        if not self._has_hooks(CmfHookType.TOOL_POST_INVOKE.value):
            return PluginResult(), context_table

        subject, delegation = await self.resolve_identity(
            request_headers, user_email, token_teams, global_context,
        )

        request_id = global_context.request_id if global_context else None
        tool_result = CmfToolResult(
            tool_call_id=f"tc-{tool_name}-{request_id or 'unknown'}",
            tool_name=tool_name,
            content=result_data,
        )
        message = self._build_message(
            Role.TOOL,
            ToolResultContentPart(content=tool_result),
        )
        extensions = self._build_extensions(subject, delegation, request_id)

        return await self._pm.invoke_hook(
            hook_type=CmfHookType.TOOL_POST_INVOKE.value,
            payload=MessagePayload(message=message, hook=MessageHookType.TOOL_POST_INVOKE),
            global_context=global_context or GlobalContext(request_id="unknown"),
            local_contexts=context_table,
            violations_as_exceptions=violations_as_exceptions,
            extensions=extensions,
        )

    async def delegate_token(
        self,
        tool_name: str,
        request_headers: Optional[dict[str, str]] = None,
        global_context: Optional[GlobalContext] = None,
    ) -> Optional[dict[str, str]]:
        """Fire token_delegate hook to exchange the user token for a narrower downstream token.

        Called after pre_invoke succeeds, before the actual downstream tool call.
        Returns forwarded_headers dict (with Authorization header) or None if
        no delegation hook is registered or no user token is present.

        Raises:
            TokenDelegationError: If the delegation plugin explicitly rejects
                the exchange (invalid token, exchange failed, etc.).
        """
        if not self._has_hooks(IdentityHookType.TOKEN_DELEGATE.value):
            return None

        # Extract user token from headers
        user_token = None
        if request_headers:
            headers_lower = {k.lower(): v for k, v in request_headers.items()}
            user_token = headers_lower.get(USER_TOKEN_HEADER)
        if not user_token:
            return None

        if user_token.lower().startswith("bearer "):
            user_token = user_token[7:]

        try:
            result, _ = await self._pm.invoke_hook(
                hook_type=IdentityHookType.TOKEN_DELEGATE.value,
                payload=DelegationPayload(
                    target_name=tool_name,
                    target_type="tool",
                    bearer_token=user_token,
                    auth_enforced_by="target",
                ),
                global_context=global_context or GlobalContext(request_id="unknown"),
                local_contexts=None,
                violations_as_exceptions=False,
            )

            if not result.continue_processing:
                raise TokenDelegationError(
                    "Delegation rejected by plugin",
                    tool_name=tool_name,
                )

            if result.modified_payload:
                deleg_result: DelegationResult = result.modified_payload
                if deleg_result.delegated_token:
                    logger.debug(
                        "Token delegated for %s: %d forwarded headers",
                        tool_name,
                        len(deleg_result.forwarded_headers),
                    )
                    return deleg_result.forwarded_headers or None
        except TokenDelegationError:
            raise
        except Exception as e:
            # Infrastructure failure — log but don't block the request
            logger.warning("Token delegation hook failed for %s: %s", tool_name, e)

        return None
