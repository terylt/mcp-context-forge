"""Tests for plugin."""

# Third-Party
import pytest

# First-Party
from opapluginfilter.plugin import OPAPluginFilter
from mcpgateway.plugins.framework import (
    PluginConfig,
    PluginContext,
    PromptPrehookPayload,
)


@pytest.mark.asyncio
async def test_opapluginfilter():
    """Test plugin prompt prefetch hook."""
    config = PluginConfig(
        name="test",
        kind="opapluginfilter.OPAPluginFilter",
        hooks=["prompt_pre_fetch"],
        config={"setting_one": "test_value"},
    )

    plugin = OPAPluginFilter(config)

    # Test your plugin logic
    payload = PromptPrehookPayload(name="test_prompt", args={"arg0": "This is an argument"})
    context = PluginContext(request_id="1", server_id="2")
    result = await plugin.prompt_pre_fetch(payload, context)
    assert result.continue_processing
