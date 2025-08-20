"""An OPA plugin that enforces rego policies on requests and allows/denies requests as per policies.

Copyright 2025
SPDX-License-Identifier: Apache-2.0
Authors: Shriti Priya

This module loads configurations for plugins and applies hooks on pre/post requests for tools, prompts and resources.
"""

# Standard
from typing import Any

# Third-Party
import requests

# First-Party
from mcpgateway.plugins.framework import (
    Plugin,
    PluginConfig,
    PluginContext,
    PromptPosthookPayload,
    PromptPosthookResult,
    PromptPrehookPayload,
    PromptPrehookResult,
    ToolPostInvokePayload,
    ToolPostInvokeResult,
    ToolPreInvokePayload,
    ToolPreInvokeResult,
)
from mcpgateway.plugins.framework.models import PluginConfig, PluginViolation
from mcpgateway.services.logging_service import LoggingService
from opapluginfilter.schema import (
    BaseOPAInputKeys,
    OPAConfig,
    OPAInput
)

# Initialize logging service first
logging_service = LoggingService()
logger = logging_service.get_logger(__name__)


class OPAPluginFilter(Plugin):
    """An OPA plugin that enforces rego policies on requests and allows/denies requests as per policies."""

    def __init__(self, config: PluginConfig):
        """Entry init block for plugin.

        Args:
          logger: logger that the skill can make use of
          config: the skill configuration
        """
        super().__init__(config)
        self.opa_config = OPAConfig.model_validate(self._config.config)

    def _evaluate_opa_policy(self, url: str, input_dict: OPAInput) -> tuple[bool,Any]:
        payload = input_dict.model_dump()
        logger.info(f"OPA url {url}, OPA payload {payload}")
        rsp = requests.post(url, json=payload)
        logger.info(f"OPA connection response '{rsp}'")
        if rsp.status_code == 200:
            json_response = rsp.json()
            decision = json_response.get("result",None)
            logger.info(f"OPA server response '{json_response}'")
            if isinstance(decision,bool):
                return decision, json_response
            else:
                logger.debug(f"OPA sent a none response {json_response}")
        else:
            logger.debug(f"OPA error: {rsp}")

    async def prompt_pre_fetch(self, payload: PromptPrehookPayload, context: PluginContext) -> PromptPrehookResult:
        """The plugin hook run before a prompt is retrieved and rendered.

        Args:
            payload: The prompt payload to be analyzed.
            context: contextual information about the hook call.

        Returns:
            The result of the plugin's analysis, including whether the prompt can proceed.
        """
        return PromptPrehookResult(continue_processing=True)

    async def prompt_post_fetch(self, payload: PromptPosthookPayload, context: PluginContext) -> PromptPosthookResult:
        """Plugin hook run after a prompt is rendered.

        Args:
            payload: The prompt payload to be analyzed.
            context: Contextual information about the hook call.

        Returns:
            The result of the plugin's analysis, including whether the prompt can proceed.
        """
        return PromptPosthookResult(continue_processing=True)

    async def tool_pre_invoke(self, payload: ToolPreInvokePayload, context: PluginContext) -> ToolPreInvokeResult:
        """OPA Plugin hook run before a tool is invoked. This hook takes in payload and context and further evaluates rego
        policies on the input by sending the request to opa server.

        Args:
            payload: The tool payload to be analyzed.
            context: Contextual information about the hook call.

        Returns:
            The result of the plugin's analysis, including whether the tool can proceed.
        """

        logger.debug(f"Processing tool pre-invoke for tool '{payload.name}' with {len(payload.args) if payload.args else 0} arguments")
        
        if not payload.args:
            return ToolPreInvokeResult()

        opa_input = BaseOPAInputKeys(kind="tools/call", user = "none", tool = {"name" : payload.name, "args" : payload.args}, request_ip = "none", headers = {}, response = {})
        opa_server_url = self.opa_config.server_url
        policy_url = opa_server_url + "/allow_pre_tool"
        decision, decision_context = self._evaluate_opa_policy(policy_url,input_dict=OPAInput(input=opa_input))
        if not decision:
            violation = PluginViolation(
                reason="tool invocation not allowed",
                description="OPA policy failed on tool preinvocation",
                code="deny",
                details=decision_context,)
            return ToolPreInvokeResult(modified_payload=payload, violation=violation, continue_processing=False)
        return ToolPreInvokeResult(continue_processing=True)

    async def tool_post_invoke(self, payload: ToolPostInvokePayload, context: PluginContext) -> ToolPostInvokeResult:
        """Plugin hook run after a tool is invoked. The response of the tool passes through this hook and opa policy is evaluated on it
         for it to be allowed or denied.

        Args:
            payload: The tool result payload to be analyzed.
            context: Contextual information about the hook call.

        Returns:
            The result of the plugin's analysis, including whether the tool result should proceed.
        """
        logger.info(f"OPA tool post request {payload} , {context}")
        result = payload.result
        opa_server_url = self.opa_config.server_url
        policy_url = opa_server_url + "/allow_post_tool"
        for content in result.content:
            opa_input = BaseOPAInputKeys(kind="tools/call", user = "none", tool = {"name" : payload.name, "args" : content}, request_ip = "none", headers = {}, response = {})
            decision, decision_context = self._evaluate_opa_policy(policy_url,input_dict=OPAInput(input=opa_input))
            if not decision:
                violation = PluginViolation(
                    reason="tool invocation not allowed",
                    description="OPA policy failed on tool postinvocation",
                    code="deny",
                    details=decision_context,)
                return ToolPreInvokeResult(modified_payload=payload, violation=violation, continue_processing=False)
        
        return ToolPostInvokeResult(continue_processing=True)
