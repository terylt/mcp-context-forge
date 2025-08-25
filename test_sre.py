import asyncio

# First-Party
from mcpgateway.plugins.framework.manager import PluginManager
from mcpgateway.plugins.framework.models import GlobalContext, ToolPreInvokePayload

sample_tool = {
  "name": "test_tool",
  "args": {
    "repo_path": "https://github.com/IBM/mcp-context-forge.git"
  }
}

sample_input = {
    "input": {
        "command: 0": {
            "command": "kubectl get",
            "resource_type": "",
            "name": "",
            "exec_command": "",
            "full_command": "kubectl get service frontend -o yaml",
            "timeout": "",
            "ops": "",
            "replicas": 1,
            "cpu": 1,
            "memory": 1,
            "legal": True,
            "image": ""
        },
        "command: 1": {
            "command": "",
            "resource_type": "",
            "name": "",
            "exec_command": "",
            "full_command": "grep -E 'clusterIP|port'",
            "timeout": "",
            "ops": "",
            "replicas": 1,
            "cpu": 1,
            "memory": 1,
            "legal": True,
            "image": ""
        },
        "command: 2": {
            "command": "kubectl run",
            "resource_type": "",
            "name": "",
            "exec_command": "",
            "full_command": "kubectl run -it --rm --image=busybox --restart=Never test-pod --namespace=$\\(kubectl get service frontend -o jsonpath='{.metadata.namespace}'\\) -- sh -c \"nc -zv 100.67.244.159 8080\"",
            "timeout": "",
            "ops": "",
            "replicas": 1,
            "cpu": 1,
            "memory": 1,
            "legal": True,
            "image": ""
        }
    }
}



sample_context = {
  "request_id": "1"
}

async def main():
    manager = PluginManager("/Users/shritipriya/Documents/2025/TT-sept10-sre/mcp-context-forge/plugins/config.yaml")
    await manager.initialize()
    print(manager.config)
    prompt = ToolPreInvokePayload(name="kubectl-executor", args = sample_input)
    global_context = GlobalContext(request_id="1", server_id="2")
    result, context = await manager.prompt_pre_fetch(prompt, global_context=global_context)
    print("Final result")
    print(result.modified_payload)
    print(result)
    print(context)
    await manager.shutdown()



asyncio.run(main())