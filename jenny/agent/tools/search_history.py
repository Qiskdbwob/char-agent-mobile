"""Search history tool: search past conversations in history.jsonl.

Allows the agent to find specific topics or facts from past conversations
that have been compacted but not yet processed by Dream.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from jenny.agent.tools.base import Tool
from jenny.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema
from jenny.security.workspace_access import current_tool_workspace


class SearchHistoryTool(Tool):
    """Tool for searching past conversations in history.jsonl."""

    @property
    def name(self) -> str:
        return "search_history"

    @property
    def description(self) -> str:
        return (
            "Search past conversations stored in memory/history.jsonl. "
            "Use this to find specific topics, facts, or discussions from "
            "earlier sessions that may have been compacted but not yet "
            "processed by Dream into long-term memory files."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return tool_parameters_schema(
            query=StringSchema(
                description=(
                    "Search query (case-insensitive substring match). "
                    "Searches through conversation content."
                ),
            ),
            max_results=IntegerSchema(
                description="Maximum number of results to return (default: 10, max: 50)",
                minimum=1,
                maximum=50,
            ),
        )

    def __init__(self, workspace: Path | None = None):
        self._workspace = workspace

    async def execute(self, **kwargs: Any) -> str:
        query = kwargs.get("query", "").strip()
        max_results = min(kwargs.get("max_results", 10), 50)

        if not query:
            return "Error: search query is required"

        workspace = self._workspace or current_tool_workspace()
        if workspace is None:
            return "Error: no workspace available"

        history_file = workspace / "memory" / "history.jsonl"
        if not history_file.exists():
            return "No conversation history found."

        try:
            results = self._search_history(history_file, query, max_results)
            if not results:
                return f"No results found for '{query}'."

            output_lines = [f"Found {len(results)} result(s) for '{query}':\n"]
            for i, entry in enumerate(results, 1):
                cursor = entry.get("cursor", "?")
                timestamp = entry.get("timestamp", "?")
                content = entry.get("content", "")
                # Truncate long content
                if len(content) > 200:
                    content = content[:197] + "..."
                output_lines.append(f"{i}. [{timestamp}] (cursor {cursor})")
                output_lines.append(f"   {content}\n")

            return "\n".join(output_lines)

        except Exception as e:
            logger.exception("Error searching history")
            return f"Error searching history: {e}"

    def _search_history(
        self, history_file: Path, query: str, max_results: int
    ) -> list[dict[str, Any]]:
        """Search history.jsonl for entries matching the query."""
        results: list[dict[str, Any]] = []
        query_lower = query.lower()

        try:
            with open(history_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        content = entry.get("content", "")
                        if query_lower in content.lower():
                            results.append(entry)
                            if len(results) >= max_results:
                                break
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            pass

        # Return most recent results first
        return list(reversed(results))


TOOLS = [SearchHistoryTool]
