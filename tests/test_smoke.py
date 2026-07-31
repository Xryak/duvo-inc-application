import asyncio

from src.server import mcp

EXPECTED_TOOLS = {
    "list_stores",
    "get_stock_position",
    "get_sales_history",
    "create_replenishment_order",
    "get_replenishment_order",
}


def test_server_exposes_exactly_the_curated_tools():
    tools = asyncio.run(mcp.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS


def test_every_tool_has_an_agent_facing_description():
    tools = asyncio.run(mcp.list_tools())
    for t in tools:
        assert t.description and len(t.description) > 40, t.name
