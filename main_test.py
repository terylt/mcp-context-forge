import asyncio

# First-Party
from mcpgateway.plugins.framework.manager import PluginManager
from mcpgateway.plugins.framework.models import GlobalContext, ToolPreInvokePayload


async def main():
    manager = PluginManager("/Users/shritipriya/Documents/2025/TT-sept10-sre/sre-demo/mcp-context-forge/plugins/config.yaml")
    await manager.initialize()
    print(manager.config)
    prompt = ToolPreInvokePayload(name="test_prompt", args = {"text": "What a crapshow!"})
    global_context = GlobalContext(request_id="1", server_id="2")
    result, _ = await manager.tool_pre_invoke(prompt, global_context=global_context)
    print(result.modified_payload)
    await manager.shutdown()



asyncio.run(main())