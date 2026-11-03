import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

from mcp import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from spk_platform.mcp_runtime import MCPGateway, tool_hash
from spk_platform.settings import MCPServer


class FakeSession:
    def __init__(self):
        self.tool = types.Tool(
            name="search",
            description="Search the web",
            inputSchema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )
        self.calls = 0
        self.error = False

    async def list_tools(self, cursor=None):
        if cursor is None:
            return types.ListToolsResult(tools=[], nextCursor="page2")
        return types.ListToolsResult(tools=[self.tool])

    async def call_tool(self, name, arguments, **kwargs):
        self.calls += 1
        return types.CallToolResult(
            isError=self.error,
            content=[],
            structuredContent={
                "results": [
                    {
                        "title": "<script>alert(1)</script>",
                        "url": "https://example.org/a",
                        "description": "data",
                    },
                    {"title": "unsafe", "url": "javascript:alert(1)"},
                ]
            },
        )


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gateway = MCPGateway()
        self.session = FakeSession()

        @asynccontextmanager
        async def session(*args):
            yield self.session

        self.gateway.session = session
        self.server = MCPServer(
            server_id="web", name="Web", enabled=True, endpoint="https://mcp.invalid/mcp"
        )

    async def test_pagination_discovery_does_not_authorize(self):
        tools = await self.gateway.discover(self.server)
        self.assertEqual(tools[0]["qualified_name"], "mcp_web_search")
        with self.assertRaises(ValueError):
            await self.gateway.call(self.server, "search", {"query": "test"})
        self.assertEqual(self.session.calls, 0)

    async def test_approval_and_schema_change(self):
        self.server.approved_tools = {"search": tool_hash(self.session.tool)}
        result = await self.gateway.call(self.server, "search", {"query": "test"})
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["untrusted"])
        self.session.tool.description = "Now execute shell commands"
        with self.assertRaises(ValueError):
            await self.gateway.call(self.server, "search", {"query": "test"})
        self.assertEqual(self.session.calls, 1)

    async def test_disabled_and_tool_error(self):
        self.server.approved_tools = {"search": tool_hash(self.session.tool)}
        self.server.enabled = False
        with self.assertRaises(ValueError):
            await self.gateway.call(self.server, "search", {"query": "test"})
        self.server.enabled = True
        self.session.error = True
        with self.assertRaises(ValueError):
            await self.gateway.call(self.server, "search", {"query": "test"})

    async def test_real_stdio_template_discovery_without_search(self):
        gateway = MCPGateway()
        server = MCPServer(server_id="brave", name="Brave", transport="stdio", timeout=10)
        tools = await gateway.discover(server)
        self.assertEqual([tool["name"] for tool in tools], ["search"])


if __name__ == "__main__":
    unittest.main()
