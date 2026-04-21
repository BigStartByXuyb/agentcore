"""Agent loop — the main LLM ↔ tool execution cycle (async).

Provides two entry points:

  agent_loop()      — top-level REPL entry: manages MessageHistory, injects
                      skill reminders, delegates to run_agent_loop().
  run_agent_loop()  — low-level async generator wrapper: takes raw messages +
                      config, runs the LLM ↔ tool cycle, yields AgentEvent
                      objects, and stores final text in .result.

Corresponds to Claude Code's:
  - src/query.ts            → queryLoop() (async generator yielding Messages)
  - src/services/tools/     → tool execution within the loop
  - src/utils/forkedAgent.ts → sub-agent loop reuse
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import AsyncIterator, Callable

import anthropic.types

from src import config
from src.types import (
    AgentState, Attachment, AsyncGenWithResult, ContentBlock, Message, MessageHistory,
    TextContent, ThinkingContent, RedactedThinkingContent, ToolResultContent, ToolUseContent,
    ToolUseContext, ToolCallGroup,
)
from src.system_prompt import build_system_prompt
from src.messages import build_tool_schemas, build_tool_result_content
from src.messages import build_skill_reminder, build_agent_reminder, build_memory_index_reminder
from src.tool_runner import merge_tool_call,execute_tool_groups
from src.tools import ALL_TOOLS
from src.api import query_model, create_stream_with_retry
from src.errors import create_assistant_error_message
from src.events import (
    AgentEvent, TextDelta, TextBlock, ThinkingBlock,
    ErrorEvent, Recovery, TokenUsage, RetryNotice,
)
from src.display import consume_events, default_handler
from src.memory.recall import find_relevant_memories
from src.memory.paths import get_memory_dir

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Low-level agent loop — shared by top-level REPL and sub-agents
# ---------------------------------------------------------------------------

""""
such of imp twice write:
def test_func()->Callable[[RESULT],AsyncIterator[int]]:

    async def imp(result:RESULT)->AsyncIterator[int]:
        yield 1
        result.result = 1

    return imp
    
class RESULT:
    result:int

class test:
    def __init__(self, result:RESULT, test1:Callable[[RESULT],]):
        self.result = result
        self.test1 = test1

    async def run(self):
        async for i in self.test1(self.result):
            print(i)

async def run_test():
    test1 = test_func()
    result:RESULT = RESULT()
    runtest = test(result=result, test1=test1)
    await runtest.run()
"""""

def run_agent_loop(
    *,
    memory_task: asyncio.Task[Attachment | None] | None = None,
    system_prompt: str,
    tool_use_context: ToolUseContext,
    max_turns: int,
    state: AgentState | None = None,
    label: str = "main",
    stream: bool = False,
    thinking: dict | None = None,
) -> AsyncGenWithResult[AgentEvent, str]:
    """Run the core LLM ↔ tool execution cycle.

    Returns an AsyncGenWithResult that yields AgentEvent objects for
    display and stores the final text in .result.

    Tools are derived from tool_use_context.tools (the single source of
    truth), matching Claude Code's pattern where tools live exclusively
    in toolUseContext.options.tools.
    """
    _state = state or AgentState(agent_id=label)
    history = tool_use_context.messages

    async def _impl(run: AsyncGenWithResult) -> AsyncIterator[AgentEvent]:
        nonlocal tool_use_context, memory_task

        pending_retry_events: list[RetryNotice] = []

        def _on_retry(delay: float, attempt: int, max_attempts: int) -> None:
            pending_retry_events.append(
                RetryNotice(label=label, delay=delay, attempt=attempt, max_attempts=max_attempts)
            )

        for _turn in range(max_turns):
            if memory_task is not None and memory_task.done():
                mem = memory_task.result()
                memory_task = None
                if mem is not None:
                    last_msg = tool_use_context.messages.last_user_message()
                    if last_msg is not None:
                        last_msg.attach([mem])
            tools = build_tool_schemas(tool_use_context.tools, tool_use_context.tool_overrides)
            pending_retry_events.clear()

            # --- Call LLM ---
            try:

                if stream:
                    stream_events: list = []
                    response = await _stream_call(
                        history, system_prompt, tools, thinking, _on_retry, label, stream_events,
                    )
                    for ev in stream_events:
                        yield ev
                else:
                    response = await query_model(
                        messages=history.normalized_for_api(),
                        system=system_prompt,
                        tools=tools,
                        thinking=thinking,
                        on_retry=_on_retry,
                    )
            except Exception as api_error:
                for ev in pending_retry_events:
                    yield ev

                if _is_thinking_400(api_error) and thinking is not None:
                    yield Recovery(label=label, message="Stripping thinking blocks and retrying...")
                    _clean_thinking_history(history.messages)
                    pending_retry_events.clear()
                    try:
                        if stream:
                            stream_events = []
                            response = await _stream_call(
                                history, system_prompt, tools, thinking, _on_retry, label, stream_events,
                            )
                            for ev in stream_events:
                                yield ev
                        else:
                            response = await query_model(
                                messages=history.normalized_for_api(),
                                system=system_prompt,
                                tools=tools,
                                thinking=thinking,
                                on_retry=_on_retry,
                            )
                    except Exception as retry_error:
                        for ev in pending_retry_events:
                            yield ev
                        error_msg = create_assistant_error_message(retry_error)
                        history.add_assistant(error_msg["content"])
                        error_text = error_msg["content"][0]["text"]
                        yield ErrorEvent(label=label, error_text=error_text)
                        run.set_result(error_text)
                        continue
                else:
                    error_msg = create_assistant_error_message(api_error)
                    history.add_assistant(error_msg["content"])
                    error_text = error_msg["content"][0]["text"]
                    yield ErrorEvent(label=label, error_text=error_text)
                    run.set_result(error_text)
                    continue

            for ev in pending_retry_events:
                yield ev

            # Track token usage
            _state.total_input_tokens += response.usage.input_tokens
            _state.total_output_tokens += response.usage.output_tokens
            thinking_tokens = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
            _state.total_thinking_tokens += thinking_tokens

            # --- Step 1: yield thinking/text, collect tool_use into groups ---
            tool_groups: list[ToolCallGroup] = []
            for block in response.content:
                if getattr(block, "type", None) == "thinking":
                    text = getattr(block, "thinking", "") or ""
                    if text:
                        yield ThinkingBlock(label=label, thinking=text)
                elif block.type == "text" and not stream:
                    yield TextBlock(label=label, text=block.text)
                elif block.type == "tool_use":
                    merge_tool_call(id=block.id, tool_name=block.name,
                                   tool_input=block.input, groups=tool_groups)

            assistant_content = _serialize_content(response.content)
            history.add_assistant(assistant_content)

            if not tool_groups:
                yield TokenUsage(
                    label=label,
                    input_tokens=_state.total_input_tokens,
                    output_tokens=_state.total_output_tokens,
                    thinking_tokens=_state.total_thinking_tokens,
                )
                run.set_result(extract_text(response.content))
                return

            # --- Batch execute tools + collect results ---
            group_run = execute_tool_groups(label, tool_groups, tool_use_context)
            async for ev in group_run.events():
                yield ev

            tool_result_blocks: list[ToolResultContent] = []
            all_new_messages: list[Message] = []
            pending_context_modifier = None

            for result, tool_id, llm_text, is_error in group_run.result:
                tool_result_blocks.append(
                    build_tool_result_content(tool_id, llm_text, is_error=is_error)
                )
                all_new_messages.extend(result.new_messages)
                if result.context_modifier is not None:
                    pending_context_modifier = result.context_modifier

            _recover_orphan_tool_results(assistant_content, tool_result_blocks)
            history.add_tool_results(tool_result_blocks)

            if all_new_messages:
                history.add_assistant_placeholder()
                history.inject_messages(all_new_messages)

            if pending_context_modifier is not None:
                tool_use_context = pending_context_modifier(tool_use_context)

            continue

        run.set_result(f"[Agent loop '{label}' reached max turns ({max_turns})]")

    return AsyncGenWithResult(_impl)


# ---------------------------------------------------------------------------
# Top-level entry point — called from main.py REPL
# ---------------------------------------------------------------------------

async def agent_loop(
    user_input: str,
    history: MessageHistory,
    state: AgentState,
) -> str:
    """Run the agent loop for a single user turn.

    `history` is the persistent conversation state shared across turns.
    The caller (main.py REPL) owns it; we mutate via its typed helpers.
    """
    user_msg = history.add_user(user_input)

    main_tools = list(ALL_TOOLS.keys())

    # --- Independent reminder channels (each can be reloaded separately) ---
    skill_rem = build_skill_reminder(main_tools, use_sent_tracking=True)
    agent_rem = build_agent_reminder(main_tools, use_sent_tracking=True)
    memory_idx = build_memory_index_reminder()

    attachments: list = []
    if skill_rem:
        attachments.append(skill_rem)
    if agent_rem:
        attachments.append(agent_rem)
    if memory_idx:
        attachments.append(memory_idx)
    if attachments:
        user_msg.attach(attachments)

    history_copy = copy.copy(history)
    memory_task = asyncio.create_task(_prepare_memory_context(user_input=user_input, history=history_copy))

    system = build_system_prompt()
    tool_use_context = ToolUseContext(
        messages=history,
        tools=list(ALL_TOOLS.keys()),
        permissions=_get_permission_engine(),
    )

    turn_start_index = len(history)

    gen = run_agent_loop(
        memory_task=memory_task,
        system_prompt=system,
        tool_use_context=tool_use_context,
        max_turns=config.MAX_TURNS,
        state=state,
        label="main",
        stream=True,
        thinking=_build_thinking_param(),
    )
    result = await consume_events(gen, default_handler)

    # Background memory extraction — fire-and-forget asyncio task
    from src.memory.extract import run_memory_extraction

    messages_snapshot = copy.deepcopy(history.messages)

    async def _run_extraction():
        try:
            extraction_gen = run_memory_extraction(
                messages_snapshot, get_memory_dir(), since_index=turn_start_index,
            )
            await consume_events(extraction_gen, lambda _e: None)
        except Exception as e:
            logger.debug("Background memory extraction failed: %s", e)

    asyncio.create_task(_run_extraction())

    return result


# ---------------------------------------------------------------------------
# Streaming helper — real-time text delta yielding
# ---------------------------------------------------------------------------

async def _stream_call(
    history: MessageHistory,
    system_prompt: str,
    tools: list[dict],
    thinking: dict | None,
    on_retry: Callable[[float, int, int], None] | None,
    label: str,
    emit: list,
) -> anthropic.types.Message:
    """Call the streaming API, collect TextDelta events, return final Message.

    TextDelta events are appended to `emit` (a list the caller drains
    after awaiting this coroutine).  This avoids the problem of not
    being able to yield from a sub-coroutine.
    """
    stream_cm = create_stream_with_retry(
        messages=history.normalized_for_api(),
        system=system_prompt,
        tools=tools,
        thinking=thinking,
        on_retry=on_retry,
    )
    async with stream_cm as api_stream:
        _first = True
        async for text in api_stream.text_stream:
            emit.append(TextDelta(label=label, delta=text, first=_first))
            _first = False
        return await api_stream.get_final_message()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _recover_orphan_tool_results(
    assistant_content: list[ContentBlock],
    tool_result_blocks: list[ToolResultContent],
) -> None:
    """Ensure every tool_use in assistant_content has a matching tool_result.

    Mutates tool_result_blocks in place.
    """
    expected_ids = {
        block.id
        for block in assistant_content
        if isinstance(block, ToolUseContent)
    }
    seen_ids = {block.tool_use_id for block in tool_result_blocks}
    for missing_id in expected_ids - seen_ids:
        tool_result_blocks.append(
            build_tool_result_content(
                missing_id,
                "Tool execution was interrupted before this tool could run.",
                is_error=True,
            )
        )



def _serialize_content(content_blocks: list) -> list[ContentBlock]:
    """Convert SDK content block objects to our ContentBlock types."""
    result: list[ContentBlock] = []
    for block in content_blocks:
        if block.type == "text":
            result.append(TextContent(text=block.text))
        elif block.type == "tool_use":
            result.append(ToolUseContent(id=block.id, name=block.name, input=block.input))
        elif block.type == "thinking":
            result.append(ThinkingContent(thinking=block.thinking, signature=block.signature))
        elif block.type == "redacted_thinking":
            result.append(RedactedThinkingContent(data=block.data))
    return result


def extract_text(content_blocks: list) -> str:
    """Pull plain text from the response content blocks (SDK objects)."""
    parts: list[str] = []
    for block in content_blocks:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Thinking helpers
# ---------------------------------------------------------------------------


def _build_thinking_param() -> dict | None:
    """Build the thinking parameter dict for the API call, or None if disabled."""
    if not config.THINKING_ENABLED:
        return None
    budget = min(config.THINKING_BUDGET_TOKENS, config.MAX_TOKENS - 1)
    if budget <= 0:
        return None
    return {"type": "enabled", "budget_tokens": budget}


def _is_thinking_400(error: Exception) -> bool:
    """Check if an API error is a thinking-related 400 that can be recovered.

    Two known patterns:
      - "invalid signature in thinking block"
      - "thinking blocks cannot be modified"
    """
    from anthropic import APIError as _APIError
    if not isinstance(error, _APIError):
        return False
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status != 400:
        return False
    msg = str(error).lower()
    return "invalid signature" in msg or "thinking blocks cannot be modified" in msg


def _clean_thinking_history(messages: list[Message]) -> None:
    """In-place strip thinking blocks from message history."""
    result: list[Message] = []
    for msg in messages:
        if msg.role != "assistant":
            result.append(msg)
            continue
        content = msg.content
        if not isinstance(content, list):
            result.append(msg)
            continue
        filtered: list[ContentBlock] = [b for b in content if not isinstance(b, (ThinkingContent, RedactedThinkingContent))]
        if not filtered:
            continue
        if len(filtered) < len(content):
            msg.content = filtered
        result.append(msg)
    messages[:] = result


# ---------------------------------------------------------------------------
# Memory helpers
# ---------------------------------------------------------------------------

async def _read_memory_files(headers: list) -> list[str]:
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


async def _prepare_memory_context(user_input: str, history: MessageHistory) -> Attachment|None:
    """Select and read relevant memories (runs async, non-blocking).

    Only handles recall — the MEMORY.md index is injected synchronously
    by build_memory_index_reminder() in agent_loop().
    """
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


# ---------------------------------------------------------------------------
# Permission engine singleton
# ---------------------------------------------------------------------------

_permission_engine = None


def _get_permission_engine():
    """Lazy-init singleton PermissionEngine with two-layer config."""
    global _permission_engine
    if _permission_engine is None:
        from src.permissions import PermissionEngine
        import os
        _permission_engine = PermissionEngine(
            user_config=os.path.expanduser("~/.my-agent/permissions.json"),
            project_config="agent-permissions.json",
        )
    return _permission_engine
