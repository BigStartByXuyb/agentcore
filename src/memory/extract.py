"""Background memory extraction — runs after each turn (async).

Corresponds to Claude Code's src/services/extractMemories/extractMemories.ts.

After the main agent produces a final response (end_turn), this module
runs a forked agent that reviews the conversation and extracts durable
memories worth saving.  The forked agent uses a restricted tool set
(only bash for writing files).
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from src import config
from src.types import AgentState, AsyncGenWithResult, ToolResult, ToolDef, ToolUseContext
from src.events import AgentEvent
from src.memory.paths import get_memory_dir, get_memory_dir_display, ensure_memory_dir
from src.memory.scan import scan_memory_files, format_memory_manifest

logger = logging.getLogger(__name__)

# Minimum messages in history before extraction triggers
_MIN_MESSAGES = 4

# ---------------------------------------------------------------------------
# Sandboxed bash tool — only allows writes to the memory directory
# ---------------------------------------------------------------------------

def _make_sandboxed_bash(memory_dir: str) -> ToolDef:
    """Create a bash ToolDef that rejects commands targeting paths outside memory_dir."""
    from src.tools.bash import SCHEMA, map_result, executor as real_executor

    mem_dir_fwd = memory_dir.replace("\\", "/")

    async def sandboxed_executor(inputs: dict, context: ToolUseContext) -> ToolResult:
        command: str = inputs.get("command", "")
        cmd_normalized = command.replace("\\", "/")

        if mem_dir_fwd not in cmd_normalized:
            return ToolResult(data={
                "stdout": "",
                "stderr": (
                    f"Permission denied: extraction agent can only write to {mem_dir_fwd}\n"
                    f"Command was: {command}"
                ),
                "exit_code": -1,
                "interrupted": False,
            })

        return await real_executor(inputs, context)

    return ToolDef(
        schema=SCHEMA,
        executor=sandboxed_executor,
        map_result=map_result,
    )

_EXTRACTION_SYSTEM_PROMPT = """\
You are a memory extraction agent. Your job is to review a conversation \
transcript and extract durable memories worth saving for future sessions.

Memory directory: {memory_dir}

## What to extract
- User preferences, role, expertise (type: user)
- Corrections or confirmations of approach (type: feedback)
- Project context, decisions, deadlines (type: project)
- External resource pointers (type: reference)

## What NOT to extract
- Code patterns or architecture (derivable from code)
- Git history (use git log)
- Ephemeral task details
- Anything already in existing memories

## How to save
Use the bash tool to write files. Each memory is a .md file with frontmatter:

```markdown
---
name: {{name}}
description: {{one-line description}}
type: {{user|feedback|project|reference}}
---

{{content}}
```

Then update MEMORY.md index with a one-line pointer:
`- [Title](filename.md) — one-line hook`

## Existing memories
{manifest}

## Rules
- Do NOT create duplicate memories
- Do NOT spawn sub-agents
- Be selective — only save what will clearly be useful in future sessions
- If nothing worth saving, do nothing
"""


def run_memory_extraction(
    messages: list[dict],
    memory_dir: str | None = None,
    since_index: int = 0,
) -> AsyncGenWithResult[AgentEvent, None]:
    """Extract memories from conversation, yielding events silently.

    Returns an AsyncGenWithResult — the caller should consume it with
    await consume_events(gen, lambda e: None) for silent execution.
    """
    if not config.MEMORY_ENABLED:
        return AsyncGenWithResult.of_value(None)

    mem_dir = memory_dir or get_memory_dir()

    if len(messages) < _MIN_MESSAGES:
        return AsyncGenWithResult.of_value(None)

    if _has_memory_writes(messages, mem_dir, since_index):
        logger.debug("Skipping extraction: main agent already wrote to memory")
        return AsyncGenWithResult.of_value(None)

    ensure_memory_dir()

    headers = scan_memory_files(mem_dir)
    manifest = format_memory_manifest(headers) if headers else "(no existing memories)"

    system_prompt = _EXTRACTION_SYSTEM_PROMPT.format(
        memory_dir=get_memory_dir_display(),
        manifest=manifest,
    )

    conversation_text = _summarize_conversation(messages)

    extraction_messages = [{
        "role": "user",
        "content": (
            "Review this conversation and extract any durable memories "
            "worth saving for future sessions.\n\n"
            f"<conversation>\n{conversation_text}\n</conversation>"
        ),
    }]

    sandboxed_bash = _make_sandboxed_bash(mem_dir)

    tool_use_context = ToolUseContext(
        messages=extraction_messages,
        tools=["bash"],
        depth=1,
        tool_overrides={"bash": sandboxed_bash},
    )

    async def _impl(run: AsyncGenWithResult) -> AsyncIterator[AgentEvent]:
        from src.agent_loop import run_agent_loop

        gen = run_agent_loop(
            messages=extraction_messages,
            system_prompt=system_prompt,
            tool_use_context=tool_use_context,
            max_turns=5,
            state=AgentState(agent_id="memory-extraction"),
            label="memory-extraction",
            stream=False,
        )
        try:
            async for ev in gen.events():
                yield ev
            run.set_result(None)
        except Exception as e:
            logger.debug("Memory extraction failed (non-critical): %s", e)
            run.set_result(None)

    return AsyncGenWithResult(_impl)


def _has_memory_writes(messages: list[dict], memory_dir: str, since_index: int = 0) -> bool:
    """Check if any assistant message (from since_index onwards) wrote to memory.

    Scans assistant messages for tool_use blocks that target memory paths.
    since_index allows checking only messages added during the current turn,
    avoiding false negatives when memory was written mid-turn but the final
    assistant message is a plain text end_turn.
    """
    mem_dir_normalized = memory_dir.replace("\\", "/")

    for msg in messages[since_index:]:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if isinstance(content, str):
            continue
        for block in content:
            if block.get("type") != "tool_use":
                continue
            if block.get("name") == "bash":
                cmd = block.get("input", {}).get("command", "")
                if mem_dir_normalized in cmd.replace("\\", "/"):
                    return True
    return False


def _summarize_conversation(messages: list[dict], max_chars: int = 8000) -> str:
    """Build a text summary of the conversation for the extraction agent.

    Keeps the most recent messages, truncating older ones if the total
    exceeds max_chars.  Newest messages are most likely to contain
    information worth extracting as memories.
    """
    # Build entries from newest to oldest
    entries: list[str] = []
    total = 0

    for msg in reversed(messages):
        role = msg.get("role", "unknown")
        content = msg.get("content", "")

        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text_parts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        text_parts.append(f"[tool_use: {block.get('name', '?')}]")
                    elif block.get("type") == "tool_result":
                        text_parts.append("[tool_result]")
            text = "\n".join(text_parts)
        else:
            text = str(content)

        entry = f"[{role}]: {text}"
        if total + len(entry) > max_chars:
            remaining = max_chars - total
            if remaining > 100:
                # Truncate the beginning of this entry (keep the tail)
                prefix = "...(truncated)\n"
                entries.append(prefix + entry[-(remaining - len(prefix)):])
            break
        entries.append(entry)
        total += len(entry)

    # Reverse back to chronological order
    entries.reverse()
    return "\n\n".join(entries)
