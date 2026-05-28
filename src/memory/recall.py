"""Select relevant memories via an LLM query.

Corresponds to Claude Code's src/memdir/findRelevantMemories.ts.

Flow:
  1. scan_memory_files() → get headers
  2. format_memory_manifest() → build text listing
  3. query_model(tools=[]) with Haiku → select up to N relevant filenames
  4. Parse JSON response → return matching MemoryHeader list
"""

from __future__ import annotations

import asyncio
import json
import logging

from src.core import config
from src.core.types import Attachment, MemoryHeader, MessageHistory, Message
from src.api import query_model
from src.memory.scan import scan_memory_files, format_memory_manifest
from src.memory.paths import get_memory_dir

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

        response = await query_model(
            model=config.MODELS.side_query,
            system=_SELECT_MEMORIES_SYSTEM_PROMPT,
            messages=[Message(
                role="user",
                content=f"Query: {query}\n\nAvailable memories:\n{manifest}",
            )],
            tools=[],
            max_tokens=256,
        )

        if response.is_error:
            logger.debug("find_relevant_memories: LLM call failed: %s", response.error_code)
            return []

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


# ---------------------------------------------------------------------------
# Memory context preparation — called from agent_loop as async task
# ---------------------------------------------------------------------------

async def _read_memory_files(headers: list[MemoryHeader]) -> list[str]:
    """Read body content (without frontmatter) of selected memory files."""
    from src.frontmatter import parse_frontmatter

    def _read_sync() -> list[str]:
        texts: list[str] = []
        for h in headers:
            try:
                with open(h.file_path, "r", encoding="utf-8", errors="replace") as f:
                    raw = f.read(4000)
                _, body = parse_frontmatter(raw)
                body = body.strip()
                if body:
                    texts.append(f"[{h.filename}]\n{body}")
            except OSError:
                continue
        return texts

    return await asyncio.to_thread(_read_sync)


async def prepare_memory_context(user_input: str, history: MessageHistory) -> Attachment | None:
    """Select and read relevant memories, return as Attachment."""
    mem_dir = get_memory_dir()

    relevant_memories = await find_relevant_memories(user_input, mem_dir, history)
    recalled_texts = await _read_memory_files(relevant_memories) if relevant_memories else []

    if not recalled_texts:
        return None

    parts: list[str] = ["<memory-recalled>"]
    parts.append("\n\n---\n\n".join(recalled_texts))
    parts.append("</memory-recalled>")

    memory_files = {"files": [h.file_path for h in relevant_memories]}
    return Attachment(type="relevant_memories", content="\n".join(parts), metadata=memory_files)
