import asyncio

# First-Party
from mcpgateway.models import Message, PromptResult, Role, TextContent
from mcpgateway.plugins.framework.manager import PluginManager
from mcpgateway.plugins.framework import GlobalContext, PromptPosthookPayload, PromptPrehookPayload
async def main():
    manager = PluginManager("/Users/shritipriya/Documents/2025/TT-sept10-sre/sre-demo/mcp-context-forge/plugins/config.yaml")
    await manager.initialize()
    print(manager.config)
    prompt = PromptPrehookPayload(name="test_prompt", args = {"text": "kubectl exec -it thepod -- /bin/bash"})
    global_context = GlobalContext(request_id="1", server_id="2")
    result, contexts = await manager.prompt_pre_fetch(prompt, global_context=global_context)

    print(result)
    print(contexts)

asyncio.run(main())