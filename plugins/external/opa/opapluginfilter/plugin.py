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
from pydantic import BaseModel

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

    def _pre_process_input(self, context: dict = {}) -> dict:
        class BaseInputSRE(BaseModel):
            command : str = ""
            resource_type : str = ""
            name : str = ""
            exec_command : str = ""
            full_command : str = ""
            timeout : str = ""
            ops: str = ""
            replicas : int = 0
            cpu : int = 0
            memory : int = 0
            legal : bool = False
            image : str = ""
        
        class InputSRE(BaseModel):
            original_command : str = ""
            commands: list[BaseInputSRE] = None

        
        result = []
        input_commands_context = context.get("input", {})
        original_command = context.get("original command", "")
        for command in input_commands_context.get("commands",[]):
            result.append(BaseInputSRE(**command).model_dump())
        return InputSRE(original_command=original_command,commands=result).model_dump()

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

        logger.info(f"Processing tool pre-invoke for tool '{payload.name}' with {len(payload.args) if payload.args else 0} arguments")
        logger.info(f"Processing tool context {context}")
        
        if not payload.args:
            return ToolPreInvokeResult()
        
        policy_context = context.global_context.state.get("policy_context",None)
        logger.info(f"Processing policy context {context.global_context.state}")
        if policy_context:
            payload_args = self._pre_process_input(context=policy_context)
            opa_input = BaseOPAInputKeys(kind="tools/call", user = "none", tool = {"name" : payload.name, "args" : payload_args}, request_ip = "none", headers = {}, response = {})
        else:
            opa_input = BaseOPAInputKeys(kind="tools/call", user = "none", tool = {"name" : payload.name, "args" : payload.args}, request_ip = "none", headers = {}, response = {})
        
        logger.info(f"Final opa_input {opa_input}")
        opa_server_url = self.opa_config.server_url
        policy_url = opa_server_url + "/allow"
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
        return ToolPostInvokeResult(continue_processing=True)
