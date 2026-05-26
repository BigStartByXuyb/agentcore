"""Select relevant memories via a side LLM query.

Corresponds to Claude Code's src/memdir/findRelevantMemories.ts.

Flow:
  1. scan_memory_files() → get headers
  2. format_memory_manifest() → build text listing
  3. side_query() with Haiku → select up to N relevant filenames
  4. Parse JSON response → return matching MemoryHeader list
"""

from __future__ import annotations

import json
import logging

from src import config
from src.types import MemoryHeader, MessageHistory, Message
from src.api import side_query
from src.memory.scan import scan_memory_files, format_memory_manifest

logger = logging.getLogger(__name__)

_SELECT_MEMORIES_SYSTEM_PROMPT = """\
You are selecting memories that will be useful to an AI assistant as it \
processes a user's query. You will be given the user's query and a list of \
available memory files with their filenames and descriptions.

Return a JSON object with a single key "selected_memories" containing a list \
of filenames for the memories that will clearly be useful (up to 5).
- Only include memories you are certain will be helpful based on their name \
and description.
- If unsure, do not include it. Be selective.
- If no memories are relevant, return an empty list.

Example response: {"selected_memories": ["user_role.md", "project_auth.md"]}
"""

async def find_relevant_memories(
    query: str,
    memory_dir: str,
    history: MessageHistory,
) -> list[MemoryHeader]:
    """Find memory files relevant to *query* by asking a cheap model.

    Returns up to MEMORY_MAX_RELEVANT MemoryHeader objects.
    Returns empty list on any failure (non-critical path).
    """
    if not config.MEMORY_ENABLED:
        return []

    headers = scan_memory_files(memory_dir)

    try:
        already_recalled: set[str] = set()
        for msg in history.messages:
            for attach in msg.attachments:
                if attach.type == "relevant_memories":
                    already_recalled.update(attach.metadata.get("files", []))

        headers = [h for h in headers if h.file_path not in already_recalled]

        if not headers:
            return []

        manifest = format_memory_manifest(headers)

        response = await side_query(
            model=config.MEMORY_SIDE_QUERY_MODEL,
            system=_SELECT_MEMORIES_SYSTEM_PROMPT,
            messages=[Message(
                role="user",
                content=f"Query: {query}\n\nAvailable memories:\n{manifest}",
            )],
            max_tokens=256,
        )

        # Extract text from response
        text_block = next(
            (b for b in response.content if b.type == "text"),
            None,
        )
        if text_block is None:
            return []

        parsed = json.loads(text_block.text)
        selected_filenames: list[str] = parsed.get("selected_memories", [])

        # Map filenames back to headers
        by_filename = {h.filename: h for h in headers}
        result = [
            by_filename[fn]
            for fn in selected_filenames
            if fn in by_filename
        ]
        return result[:config.MEMORY_MAX_RELEVANT]

    except Exception as e:
        logger.debug("find_relevant_memories failed (non-critical): %s", e)
        return []
