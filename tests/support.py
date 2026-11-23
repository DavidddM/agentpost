"""Small helpers shared by the test modules."""

from mcp import Client


async def call(client: Client, tool: str, **args):
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content
    return result.structured_content


async def call_text(client: Client, tool: str, **args) -> str:
    """The reply exactly as an agent receives it."""
    result = await client.call_tool(tool, args)
    assert not result.is_error, result.content
    return result.content[0].text


async def call_error(client: Client, tool: str, **args) -> str:
    result = await client.call_tool(tool, args)
    assert result.is_error
    return result.content[0].text


def bodies(read: dict) -> list[str]:
    return [m["body"] for m in read["messages"]]


def titles(listing: dict) -> list[str]:
    return [t["title"] for t in listing["threads"]]
