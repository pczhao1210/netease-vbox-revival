import os

from mcp.server.fastmcp import FastMCP

from .network import bounded_json, remote_client

server = FastMCP("Brave read-only search")


@server.tool()
async def search(query: str, count: int = 5) -> dict:
    """Search the web and return titles, HTTP(S) URLs and snippets."""
    if not query.strip() or len(query) > 500 or not 1 <= count <= 10:
        raise ValueError("Invalid search parameters")
    key = os.getenv("BRAVE_API_KEY", "")
    if not key:
        raise ValueError("Brave Search API key is not configured")
    async with remote_client(timeout=10) as client:
        async with client.stream(
            "GET",
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": count, "safesearch": "strict"},
            headers={"X-Subscription-Token": key},
        ) as response:
            result = await bounded_json(response, 131072)
    return {
        "results": [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("description", ""),
                "age": item.get("age", ""),
            }
            for item in result.get("web", {}).get("results", [])[:count]
        ]
    }


if __name__ == "__main__":
    server.run(transport="stdio")
