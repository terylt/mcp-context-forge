"""A linux command preprocessing plugin for OPA policies..

Copyright 2025
SPDX-License-Identifier: Apache-2.0
Authors: Teryl Taylor

This module loads configurations for plugins.
"""
import json
import re
from typing import Any

import bashlex


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


"""Models for the Linux command pre processing module."""
from pydantic import BaseModel


class Command(BaseModel):
    """The output command format after preprocessing.
    
    Attributes:
    """

    command: str = ""
    resource_type: str = ""
    name: str = ""
    exec_command: str = ""
    full_command: str = ""
    timeout: str = ""
    ops: str = ""
    replicas: int = 1
    cpu: int = 1
    memory: int = 1
    legal: bool = True
    image: str = ''

CONTEXT_KEY_POLICY_CONTEXT = "policy_context"

def quote_angle_brackets(text: str) -> str:
    """Add quotes to inner angle bracket pairs of a string.
    
    Args:
        text: string containing bracket content.
    
    Returns:
        A string with quotes.
    """
    pattern = r'<[^<>]+>'
    def replacer(match):
        content = match.group(0)
        return "'" + content + "'"
    return re.sub(pattern, replacer, text)

## Process the string to extract all commands, because some commands can have several commands inside or in a for loop
def extract_commands_recursive(node: bashlex.ast.node | list, script: str, commands: list[str])-> None:
    """Process a command string to extract all commands.
    
    Args:
        node: a lexical bash command or list of commands.
        script: parsed string.
        commands: the list of commands to be returned.
    """
    if isinstance(node, bashlex.ast.node):
        if node.kind == 'command':
            start, end = node.pos
            command_str = script[start:end].strip()
            commands.append(command_str)
        for attr in ('parts', 'list', 'command', 'wordlist'):
            if hasattr(node, attr):
                subnodes = getattr(node, attr)
                if isinstance(subnodes, list):
                    for subnode in subnodes:
                        extract_commands_recursive(subnode, script, commands)
                else:
                    extract_commands_recursive(subnodes, script, commands)

    elif isinstance(node, list):
        for item in node:
            extract_commands_recursive(item, script, commands)

def get_commands(command_text:str) -> list[str]:
    """Unravels all the commands in a string into a list.
    
    Args:
        command_text: the command string.
    
    Returns:
        A list of strings.
    """

    command_text=quote_angle_brackets(command_text)
    command_text = command_text.replace('(', '\\(').replace(')', '\\)')
    try:
        parts = bashlex.parse(command_text)
    except Exception as e:
        return ['illegal commands: '+command_text]
    parts = bashlex.parse(command_text)
    commands = []
    extract_commands_recursive(parts, command_text, commands)
    return commands

## Match the values of paramaters start with '-'+param_name
def match_param(command: str, param_name: str) -> str:
    """Matches a command's parameter.
    
    Args:
        command: the command string.
        param_name: the parameter name.
    
    Returns:
        The parsed parameter if found.
    """
    pattern = (
        r'(?:^|\s)--?' + re.escape(param_name) + r'(?:[=\s])'  
        r'(?:'                                  
        r'"([^"]+)"'                                
        r'|'                                  
        r'\'([^\']+)\''                              
        r'|'                                        
        r'([^\s]+)'                                         
        r')'
    )
    match = re.search(pattern, command)
    if match:
        return next(g for g in match.groups() if g is not None)
    return ''

def process_cpu(cpu_str: str) -> int:
    """Parses a CPU string and returns the corresponding integer.
    
    Args:
        cpu_str: the CPU information as a string.
    
    Returns:
        CPU as an integer.
    """
    if not cpu_str.endswith('m') or len(cpu_str.split('m')[0])==0:
        return -1
    else:
        return int(cpu_str.split('m')[0])

def process_memory(mem_str: str) -> int:
    """Parses memory string and normalizes it to megabytes.
    
    Args:
        mem_str: The memory value as a string.
    
    Returns:
        The amount of memory in megabytes.
    """
    mem_str = mem_str.strip().upper()
    match = re.match(r"(\d+(?:\.\d+)?)([A-Z]+)?", mem_str)
    if not match:
        return -1
    value, unit = match.groups()
    value = int(value)
    unit_map = {
        "M": 1,
        "MB": 1,
        "G": 1024,
        "GB": 1024,
        "GI": 1024,
        "T": 1024 * 1024,
        "TB": 1024 * 1024,
        "TI": 1024 * 1024,
    }
    if unit not in unit_map:
        return -1
    return value * unit_map[unit]


def process_patch_command(patch_command: str, processed_command: Command) -> Command:
    """Parses a kubectl patch command.
    
    Args:
        patch_command - a kubectl patch command as a string.
        processed_command - the command object to update with the patch values.
    
    Returns:
        The updated command object.
    """
    if " -p=" in patch_command:
        pattern = r'\{[^{}]+\}'
        matches = re.findall(pattern, patch_command)
        for match in matches:
            try:             
                parameter=json.loads(match)
                print(parameter)
                print(parameter.keys())
                if 'value' not in parameter.keys() or 'path' not in parameter.keys():
                    continue
                if len(parameter['value'])>0:
                    if parameter['path'].lower().endswith("cpu"):
                        processed_command.cpu=process_cpu(parameter['value'])
                    if parameter['path'].lower().endswith("memory"):
                        processed_command.memory=process_memory(parameter['value'])
                    if parameter['path'].lower().endswith("replicas"):
                        processed_command.replicas=int(parameter['value'])
                    if parameter['path'].lower().endswith("image"):
                        processed_command.image=parameter['value']
            except json.JSONDecodeError as e:
                processed_command.legal=False
    if " --patch " in patch_command:
        match = re.search(r'"memory"\s*:\s*"([^"]+)"', patch_command)
        if match:
            processed_command.memory = process_memory(match.group(1))
        match = re.search(r'"cpu"\s*:\s*"([^"]+)"', patch_command)
        if match:
            processed_command.cpu = process_cpu(match.group(1))
        match = re.search(r'"replicas"\s*:\s*"([^"]+)"', patch_command)
        if match:
            processed_command.replicas = int(match.group(1))
        match = re.search(r'"image"\s*:\s*"([^"]+)"', patch_command)
        if match:
            processed_command.image = match.group(1)
    return processed_command

def process_command(current_command: str) -> Command:
    """Parses a string command into a Command object.
    
    Args:
        current_command: A bash command in string form.
    
    Returns:
        The parsed command as an object.
    """
    processed_command=Command.model_construct()
    processed_command.full_command=current_command
    if current_command.startswith("ilegal commands: "):
        processed_command.full_command=current_command.split("ilegal commands: ")[1]
        processed_command.legal=False
    elif current_command.startswith("kubectl wait"):
        if len(current_command.split(" "))>3:
            processed_command.timeout=int(match_param(current_command,'timeout').split('s')[0])
    elif current_command.startswith("sleep"):
        if len(current_command.split(" "))>2:
            processed_command.timeout=int(current_command.split(' ')[1])
    elif current_command.startswith("kubectl rollout"):
        if len(current_command.split(" "))>3:
            processed_command.ops=current_command.split(' ')[2]
    elif current_command.startswith("kubectl delete"):
        current_command=current_command.split(" ")
        if len(current_command) > 4:
            processed_command.command="kubectl delete"
            processed_command.resource_type=current_command[2]
            processed_command.name=current_command[3]
    elif current_command.startswith("kubectl exec"):
        processed_command.command="kubectl exec"
        match = re.search(r'\s--\s+(\S+)', current_command)
        if match:
            vars_value = match.group(1)
            processed_command.exec_command=vars_value
    elif current_command.startswith("kubectl apply"):
        if len(match_param(current_command,"f"))>0:
            processed_command.command="kubectl apply"
            processed_command.apply_file=match_param(current_command,"f")
    elif current_command.startswith("kubectl config"):
        if len(current_command.split(" "))>3:
            processed_command.command="kubectl apply"
            processed_command.ops=current_command.split(" ")[2]
    elif current_command.startswith("kubectl scale"):
        processed_command.command="kubectl scale"
        match = match_param(current_command,'replicas')
        if len(match)>0:
            processed_command.replicas=int(match)
    elif current_command.startswith("kubectl create"):
        if len(current_command.split(" "))>3:
            processed_command.ops=current_command.split(" ")[2]
            processed_command.image=match_param(current_command,"image")
    elif current_command.startswith("kubectl patch"):
        processed_command.command="kubectl patch"
        processed_command=process_patch_command(current_command, processed_command)
    elif current_command.startswith("kubectl "):
        processed_command.command=" ".join(current_command.split(" ")[:2])
    return processed_command

async def generate_command_json(cmd: str) -> dict[str, Any]:
    """Generates a command list in JSON format.
    
    Args:
        lines: a list of command lines.
    
    Returns:
        A JSON representation of the parsed commands as a dictionary.
    """

    commands=get_commands(cmd)
    full_command_dict={}
    full_command_dict['input']={}
    cmds: list[dict] = []
    for command in commands:
        command_obj = process_command(command)
        cmd_dict = command_obj.model_dump()
        cmds.append(cmd_dict)
    
    full_command_dict['input']['commands'] = cmds
    full_command_dict['original command'] = cmd
    return full_command_dict



class CmdPreProcess(Plugin):
    """A linux command preprocessing plugin for OPA policies.."""

    async def process_args_in_state(self, args: dict[str, Any], context: PluginContext) -> None:
        """Process arguments and store them in process context state."""
        if len(args) > 1:
            raise ValueError(f"Plugin: {self.name} only works on single argument functions.")
        for arg in args.values():
            context.global_context.state[CONTEXT_KEY_POLICY_CONTEXT] = await generate_command_json(str(arg))


    async def prompt_pre_fetch(self, payload: PromptPrehookPayload, context: PluginContext) -> PromptPrehookResult:
        """The plugin hook run before a prompt is retrieved and rendered.

        Args:
            payload: The prompt payload to be analyzed.
            context: contextual information about the hook call.

        Returns:
            The result of the plugin's analysis, including whether the prompt can proceed.
        """
        if not payload.args:
            return PromptPrehookResult(continue_processing=True)
        await self.process_args_in_state(payload.args, context)
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
        """Plugin hook run before a tool is invoked.

        Args:
            payload: The tool payload to be analyzed.
            context: Contextual information about the hook call.

        Returns:
            The result of the plugin's analysis, including whether the tool can proceed.
        """
        if not payload.args:
            return ToolPreInvokeResult(continue_processing=True)
        await self.process_args_in_state(payload.args, context)
        return ToolPreInvokeResult(continue_processing=True)

    async def tool_post_invoke(self, payload: ToolPostInvokePayload, context: PluginContext) -> ToolPostInvokeResult:
        """Plugin hook run after a tool is invoked.

        Args:
            payload: The tool result payload to be analyzed.
            context: Contextual information about the hook call.

        Returns:
            The result of the plugin's analysis, including whether the tool result should proceed.
        """
        return ToolPostInvokeResult(continue_processing=True)