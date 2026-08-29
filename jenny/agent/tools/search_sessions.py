"""Search sessions tool: search across all conversation sessions.

Allows the agent to find specific topics, facts, or discussions from
previous sessions. Searches session metadata (titles, summaries) and
message content across all user-facing session files.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from jenny.agent.tools.base import Tool
from jenny.agent.tools.schema import IntegerSchema, StringSchema, tool_parameters_schema
from jenny.security.workspace_access import current_tool_workspace
from jenny.session.keys import is_webui_session_key


class SearchSessionsTool(Tool):
    """Tool for searching across all conversation sessions."""

    @property
    def name(self) -> str:
        return "search_sessions"

    @property
    def description(self) -> str:
        return (
            "Search across all conversation sessions to find specific topics, "
            "facts, or discussions from past conversations. Returns session "
            "titles, dates, and relevant message snippets. Use this when you "
            "need to find information from a previous conversation that may "
            "not be in the current session's context."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return tool_parameters_schema(
            query=StringSchema(
                description=(
                    "Search query (case-insensitive substring match). "
                    "Searches through session titles, summaries, and message content."
                ),
            ),
            max_results=IntegerSchema(
                description="Maximum number of results to return (default: 5, max: 20)",
                minimum=1,
                maximum=20,
            ),
            context_lines=IntegerSchema(
                description=(
                    "Number of context lines around matches in message search "
                    "(default: 2, max: 10). Shows surrounding messages."
                ),
                minimum=0,
                maximum=10,
            ),
            session_key=StringSchema(
                description=(
                    "Optional: limit search to a specific session key. "
                    "If not provided, searches all sessions."
                ),
            ),
        )

    def __init__(self, workspace: Path | None = None):
        self._workspace = workspace

    async def execute(self, **kwargs: Any) -> str:
        query = kwargs.get("query", "").strip()
        max_results = min(kwargs.get("max_results", 5), 20)
        context_lines = min(kwargs.get("context_lines", 2), 10)
        session_key_filter = kwargs.get("session_key", "").strip()

        if not query:
            return "Error: search query is required"

        workspace = self._workspace or current_tool_workspace()
        if workspace is None:
            return "Error: no workspace available"

        sessions_dir = workspace / "sessions"
        if not sessions_dir.exists():
            return "No sessions directory found."

        try:
            results = self._search_sessions(
                sessions_dir, query, max_results,
                context_lines=context_lines,
                session_key_filter=session_key_filter,
            )
            if not results:
                return f"No results found for '{query}' across sessions."

            output_lines = [f"Found {len(results)} session(s) matching '{query}':\n"]
            for i, result in enumerate(results, 1):
                key = result.get("key", "?")
                title = result.get("title", "(untitled)")
                updated = result.get("updated_at", "?")[:10]
                msg_count = result.get("msg_count", "?")
                snippet = result.get("snippet", "")
                output_lines.append(
                    f"{i}. {title} ({updated}, {msg_count} msgs) — session: {key}"
                )
                if snippet:
                    output_lines.append(f"   \"{snippet}\"")
                output_lines.append("")

            return "\n".join(output_lines)

        except Exception as e:
            logger.exception("Error searching sessions")
            return f"Error searching sessions: {e}"

    def _search_sessions(
        self,
        sessions_dir: Path,
        query: str,
        max_results: int,
        *,
        context_lines: int = 2,
        session_key_filter: str = "",
    ) -> list[dict[str, Any]]:
        """Search session files for entries matching the query."""
        results: list[dict[str, Any]] = []
        query_lower = query.lower()

        for path in sessions_dir.glob("*.jsonl"):
            stem = path.stem
            key = stem.replace("_", ":", 1)
            if not is_webui_session_key(key):
                continue
            if session_key_filter and session_key_filter != key:
                continue

            result = self._search_single_session(
                path, key, query_lower, context_lines=context_lines,
            )
            if result is not None:
                results.append(result)
                if len(results) >= max_results:
                    break

        # Sort by most recently updated first
        results.sort(key=lambda r: r.get("updated_at", ""), reverse=True)
        return results

    def _search_single_session(
        self,
        path: Path,
        key: str,
        query_lower: str,
        *,
        context_lines: int = 2,
    ) -> dict[str, Any] | None:
        """Search a single session file for the query."""
        try:
            metadata_record: dict[str, Any] = {}
            messages: list[dict[str, Any]] = []

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("_type") == "metadata":
                        metadata_record = data
                    else:
                        messages.append(data)

            meta = metadata_record.get("metadata", {})
            title = meta.get("title", "")
            summary_text = meta.get("_last_summary", {})
            if isinstance(summary_text, dict):
                summary_text = summary_text.get("text", "")

            # Check title match
            if query_lower in title.lower():
                return {
                    "key": key,
                    "title": title,
                    "updated_at": metadata_record.get("updated_at", ""),
                    "msg_count": len(messages),
                    "snippet": f"Title matches: {title}",
                }

            # Check summary match
            if isinstance(summary_text, str) and query_lower in summary_text.lower():
                idx = summary_text.lower().index(query_lower)
                start = max(0, idx - 40)
                end = min(len(summary_text), idx + len(query_lower) + 40)
                snippet = summary_text[start:end]
                if start > 0:
                    snippet = "..." + snippet
                if end < len(summary_text):
                    snippet = snippet + "..."
                return {
                    "key": key,
                    "title": title or "(untitled)",
                    "updated_at": metadata_record.get("updated_at", ""),
                    "msg_count": len(messages),
                    "snippet": snippet,
                }

            # Check message content with context window
            for idx, msg in enumerate(reversed(messages)):
                content = msg.get("content", "")
                if not isinstance(content, str):
                    continue
                if query_lower in content.lower():
                    pos = content.lower().index(query_lower)
                    snippet_start = max(0, pos - 40)
                    snippet_end = min(len(content), pos + len(query_lower) + 40)
                    snippet = content[snippet_start:snippet_end]
                    if snippet_start > 0:
                        snippet = "..." + snippet
                    if snippet_end < len(content):
                        snippet = snippet + "..."

                    # Add surrounding message context if requested
                    if context_lines > 0 and len(messages) > 1:
                        actual_idx = len(messages) - 1 - idx
                        ctx_before = max(0, actual_idx - context_lines)
                        ctx_after = min(len(messages), actual_idx + context_lines + 1)
                        context_parts = []
                        for cmsg in messages[ctx_before:ctx_after]:
                            crole = cmsg.get("role", "?")
                            ccontent = cmsg.get("content", "")
                            if isinstance(ccontent, str) and ccontent:
                                if len(ccontent) > 80:
                                    ccontent = ccontent[:80] + "..."
                                marker = " >>" if cmsg is msg else "   "
                                context_parts.append(f"{marker} [{crole}] {ccontent}")
                        if context_parts:
                            snippet += "\n    Context:\n    " + "\n    ".join(context_parts)

                    return {
                        "key": key,
                        "title": title or "(untitled)",
                        "updated_at": metadata_record.get("updated_at", ""),
                        "msg_count": len(messages),
                        "snippet": snippet,
                    }

            return None

        except Exception:
            logger.debug("Failed to search session {}", key, exc_info=True)
            return None


TOOLS = [SearchSessionsTool]
