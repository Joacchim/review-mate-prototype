"""The MCP server — a thin FastMCP wrapper exposing AgentBridge methods as MCP tools.

Each tool delegates to the bridge; the bridge (not this module) holds the logic, so the tools stay
declarative. This is what a Claude Code session connects to as the agent seam.
"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from review_mate.mcp.bridge import AgentBridge


def build_mcp_server(bridge: AgentBridge, *, mountable: bool = False) -> FastMCP:
    # `mountable` configures the streamable-HTTP app to serve at the mount root, statelessly,
    # so it can be mounted inside bridge-server and share its SessionManager.
    if mountable:
        mcp = FastMCP("review-mate", stateless_http=True, streamable_http_path="/")
    else:
        mcp = FastMCP("review-mate")

    @mcp.tool()
    def list_sessions() -> list[dict]:
        """List the review sessions currently open."""
        return [s.model_dump(mode="json") for s in bridge.list_sessions()]

    @mcp.tool()
    def get_session(session_id: str) -> dict:
        """Get the full current state of a session (mr, files, highlights, cards, …)."""
        return bridge.snapshot(session_id).model_dump(mode="json")

    @mcp.tool()
    def get_diff(session_id: str) -> list[dict]:
        """Get the session's changed files (the diff)."""
        return [f.model_dump(mode="json") for f in bridge.diff(session_id)]

    @mcp.tool()
    async def wait_for_highlight(session_id: str, since: int = 0,
                                 timeout: float | None = 30.0) -> dict | None:
        """Wait for the reviewer's next highlight after `since`; returns {seq, highlight} or null."""
        result = await bridge.wait_for_highlight(session_id, since=since, timeout=timeout)
        if result is None:
            return None
        return {"seq": result["seq"], "highlight": result["highlight"].model_dump(mode="json")}

    @mcp.tool()
    async def emit_card(session_id: str, body: str, highlight_id: str | None = None,
                        citations: list[str] | None = None) -> dict:
        """Post a context card (markdown). Anchor it to a highlight by id, or omit highlight_id
        for an MR-level insight (a standalone card not tied to any zone)."""
        return (await bridge.emit_card(session_id, highlight_id, body, citations)).model_dump()

    @mcp.tool()
    async def add_insight(session_id: str, file: str, start_line: int, end_line: int, body: str,
                          side: str = "new", citations: list[str] | None = None) -> dict:
        """Proactively flag a zone the reviewer did not highlight: creates an agent-authored
        highlight on file:start_line-end_line and a card on it. Returns {highlight_id, card}.
        Use sparingly for things genuinely worth the reviewer's attention; they can dismiss it."""
        return await bridge.add_insight(session_id, file, start_line, end_line, body,
                                        side=side, citations=citations)

    @mcp.tool()
    async def update_card(session_id: str, card_id: str, body: str | None = None,
                          status: str | None = None) -> dict:
        """Update a previously emitted card; status is 'streaming' or 'complete'."""
        from review_mate.session.state import CardStatus
        st = CardStatus(status) if status else None
        return (await bridge.update_card(session_id, card_id, body=body, status=st)).model_dump()

    @mcp.tool()
    async def request_access(session_id: str, repo: str, reason: str) -> dict:
        """Ask the reviewer to approve read access to another local repository."""
        return (await bridge.request_access(session_id, repo, reason)).model_dump()

    @mcp.tool()
    async def post_message(session_id: str, body: str) -> dict:
        """Post a chat message to the reviewer (the agent side of the conversation)."""
        return (await bridge.post_message(session_id, body)).model_dump()

    @mcp.tool()
    async def wait_for_message(session_id: str, since: int = 0,
                               timeout: float | None = 60.0) -> dict | None:
        """Wait for the reviewer's next chat message after `since`; returns {seq, message} or null."""
        return await bridge.wait_for_message(session_id, since=since, timeout=timeout)

    return mcp
