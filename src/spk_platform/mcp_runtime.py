import asyncio
import hashlib
import json
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .llm import Tool
from .network import remote_client


def tool_hash(tool):
    value = {
        "name": tool.name,
        "description": tool.description or "",
        "parameters": tool.inputSchema,
        "output": tool.outputSchema,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def search_results(value, limit):
    entries = (
        value.get("results", value.get("web", {}).get("results", []))
        if isinstance(value, dict)
        else []
    )
    output = []
    for entry in entries[:limit]:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url", ""))
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            continue
        output.append(
            {
                "title": str(entry.get("title", ""))[:300],
                "url": url[:2000],
                "summary": str(entry.get("summary", entry.get("description", "")))[:2000],
                "published": str(entry.get("published", entry.get("age", "")))[:80],
            }
        )
    return output


class MCPGateway:
    def __init__(self):
        self.slots = asyncio.Semaphore(2)
        self.cache = {}
        self.failures = {}

    @asynccontextmanager
    async def session(self, server, key, private_hosts=()):
        if server.auth == "oauth":
            raise ValueError("OAuth requires a separate authorization implementation")
        if server.transport == "stdio":
            if server.template != "brave-search":
                raise ValueError("Unapproved stdio template")
            with tempfile.TemporaryDirectory(prefix="spk-mcp-") as working:
                parameters = StdioServerParameters(
                    command=sys.executable,
                    args=["-m", "spk_platform.search_server"],
                    cwd=working,
                    env={
                        "PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin",
                        "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                        "HOME": working,
                        "BRAVE_API_KEY": key,
                    },
                )
                async with stdio_client(parameters) as (read, write):
                    async with ClientSession(
                        read, write, read_timeout_seconds=timedelta(seconds=server.timeout)
                    ) as session:
                        await session.initialize()
                        yield session
        else:
            async with remote_client(
                private_hosts, server.timeout, response_limit=2 * 1048576
            ) as client:
                if key and server.auth == "bearer":
                    client.headers["Authorization"] = "Bearer " + key
                elif key and server.auth == "api-key":
                    client.headers[server.header_name] = key
                async with streamable_http_client(server.endpoint, http_client=client) as (
                    read,
                    write,
                    session_id,
                ):
                    async with ClientSession(
                        read, write, read_timeout_seconds=timedelta(seconds=server.timeout)
                    ) as session:
                        await session.initialize()
                        yield session

    async def list_tools(self, session, server):
        tools = []
        cursors = set()
        cursor = None
        for _page_number in range(16):
            page = await session.list_tools(cursor=cursor)
            for entry in page.tools:
                name = "mcp_%s_%s" % (server.server_id, entry.name)
                if (
                    len(entry.description or "") > 4000
                    or len(json.dumps(entry.inputSchema)) > 16384
                ):
                    raise ValueError("MCP tool description or schema exceeds limit")
                tool = Tool(name, entry.description or "", entry.inputSchema)
                tools.append(
                    {
                        "name": entry.name,
                        "qualified_name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                        "hash": tool_hash(entry),
                    }
                )
                if len(tools) > 128:
                    raise ValueError("MCP tool catalog exceeds limit")
            cursor = page.nextCursor
            if not cursor:
                break
            if cursor in cursors:
                raise ValueError("MCP pagination cursor repeated")
            cursors.add(cursor)
        else:
            raise ValueError("MCP pagination exceeds limit")
        if len({tool["qualified_name"] for tool in tools}) != len(tools):
            raise ValueError("Duplicate MCP tool name")
        return tools

    async def discover(self, server, key="", private_hosts=()):
        async with self.slots, asyncio.timeout(server.timeout):
            async with self.session(server, key, private_hosts) as session:
                return await self.list_tools(session, server)

    async def call(self, server, name, arguments, key="", private_hosts=(), sensitive=True):
        if not server.enabled or name not in server.approved_tools:
            raise ValueError("MCP service or tool is not authorized")
        if self.failures.get(server.server_id, (0, 0))[0] >= 3:
            count, timestamp = self.failures[server.server_id]
            if time.monotonic() - timestamp < 30:
                raise ValueError("MCP service circuit is open")
        cache_key = hashlib.sha256(
            (server.model_dump_json() + key + name + json.dumps(arguments, sort_keys=True)).encode()
        ).hexdigest()
        cached = self.cache.get(cache_key)
        if not sensitive and cached and cached[0] > time.monotonic():
            return {**cached[1], "cached": True}
        try:
            async with self.slots, asyncio.timeout(server.timeout):
                async with self.session(server, key, private_hosts) as session:
                    tools = await self.list_tools(session, server)
                    tool = next((tool for tool in tools if tool["name"] == name), None)
                    if not tool or tool["hash"] != server.approved_tools[name]:
                        raise ValueError(
                            "MCP tool schema or description changed; administrator review required"
                        )
                    Draft202012Validator(tool["parameters"]).validate(arguments)
                    result = await session.call_tool(
                        name, arguments, read_timeout_seconds=timedelta(seconds=server.timeout)
                    )
                    if result.isError:
                        raise ValueError("MCP tool reported an error")
                    serialized = result.model_dump_json()
                    if len(serialized.encode()) > server.max_bytes:
                        raise ValueError("MCP result exceeds configured byte limit")
                    value = result.structuredContent
                    if value is None:
                        text = "\n".join(
                            item.text for item in result.content if item.type == "text"
                        )
                        try:
                            value = json.loads(text)
                        except ValueError:
                            value = {"text": text}
                    output = {
                        "results": search_results(value, server.max_results),
                        "retrieved_at": time.time(),
                        "server_id": server.server_id,
                        "tool": name,
                        "cached": False,
                        "untrusted": True,
                    }
                    if not output["results"]:
                        output["notice"] = "No usable HTTP(S) sources were returned."
                    self.failures.pop(server.server_id, None)
                    if not sensitive and server.cache_ttl:
                        self.cache[cache_key] = (time.monotonic() + server.cache_ttl, output)
                        if len(self.cache) > 128:
                            self.cache.pop(next(iter(self.cache)))
                    return output
        except asyncio.CancelledError:
            raise
        except Exception:
            count = self.failures.get(server.server_id, (0, 0))[0]
            self.failures[server.server_id] = (count + 1, time.monotonic())
            raise
