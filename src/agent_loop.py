"""Agent loop — the main LLM <-> tool execution cycle (async, callback-based).

Provides two entry points:

  agent_loop()      — top-level REPL entry: manages MessageHistory, injects
                      skill reminders, delegates to run_agent_loop().
  run_agent_loop()  — low-level async function: takes raw messages + config,
                      runs the LLM <-> tool cycle, emits AgentEvent via
                      on_event callback, returns final text.

Corresponds to Claude Code's:
  - src/query.ts            -> queryLoop()
  - src/services/tools/     -> tool execution within the loop
  - src/utils/forkedAgent.ts -> sub-agent loop reuse
"""

from __future__ import annotations

import asyncio
import copy
import logging
from typing import Any, Callable

import anthropic.types

from src import config
from src.types import (
    AgentState, Attachment, ContentBlock, EventCallback, Message, MessageHistory,
    TextContent, ThinkingContent, RedactedThinkingContent, ToolResultContent, ToolUseContent,
    ToolUseContext, ToolCallGroup,
)
from src.system_prompt import build_system_prompt
from src.messages import build_tool_schemas, build_tool_result_content
from src.messages import build_skill_reminder, build_agent_reminder, build_memory_index_reminder
from src.tool_runner import merge_tool_call, execute_tool_groups, StreamingToolExecutor
from src.tools import registry as tool_registry
from src.api import query_model, create_stream_with_retry
from src.errors import create_assistant_error_message
from src.events import (
    AgentEvent, TextDelta, TextBlock, ThinkingBlock, ThinkingDelta,
    ErrorEvent, Recovery, TokenUsage, RetryNotice,
)
from src.display import make_interactive_handler, default_handler
from src.memory.recall import find_relevant_memories
from src.memory.paths import get_memory_dir
from src.compact.micro_compact import should_micro_compact, micro_compact
from src.compact.auto_compact import should_auto_compact, auto_compact, estimate_token_count

logger = logging.getLogger(__name__)

POST_COMPACT_MAX_TOKENS_PER_SKILL = 5_000
POST_COMPACT_SKILLS_TOKEN_BUDGET = 25_000
POST_COMPACT_MAX_FILES = 5
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000
POST_COMPACT_FILES_TOKEN_BUDGET = 50_000


def _build_invoked_skills_attachment(
    invoked_skills: dict[str, Any],
) -> Attachment | None:
    """Build an attachment containing all inline skills invoked this session.

    Re-injected after compaction so the LLM retains the skill guidelines
    even though the original skill-content messages were summarized away.
    """
    if not invoked_skills:
        return None

    skills = sorted(
        invoked_skills.values(),
        key=lambda s: s.invoked_at,
        reverse=True,
    )

    parts: list[str] = []
    used_tokens = 0
    for sk in skills:
        content = sk.content
        tokens = config.estimate_tokens(content)
        if tokens > POST_COMPACT_MAX_TOKENS_PER_SKILL:
            char_budget = config.tokens_to_chars(POST_COMPACT_MAX_TOKENS_PER_SKILL)
            content = content[:char_budget] + (
                "\n\n[... skill content truncated for compaction; "
                f"use Read on the skill path ({sk.skill_path}) if you need the full text]"
            )
            tokens = POST_COMPACT_MAX_TOKENS_PER_SKILL
        if used_tokens + tokens > POST_COMPACT_SKILLS_TOKEN_BUDGET:
            break
        used_tokens += tokens
        parts.append(f"### Skill: {sk.skill_name}\nPath: {sk.skill_path}\n\n{content}")

    if not parts:
        return None

    body = "\n\n---\n\n".join(parts)
    text = (
        "<system-reminder>\n"
        "The following skills were invoked in this session. "
        "Continue to follow these guidelines:\n\n"
        f"{body}\n"
        "</system-reminder>"
    )
    return Attachment(type="invoked_skills", content=text)


def _build_file_restore_attachments(
    snapshot: dict[str, Any],
) -> Attachment | None:
    """Build an attachment restoring recently-read files after compaction.

    Takes a snapshot of the file state cache (captured before clearing).
    Selects the most recent files within token budget and re-injects their
    content as fake Read tool results.
    """
    if not snapshot:
        return None

    # snapshot preserves OrderedDict insertion order (LRU: oldest→newest).
    # Reverse to get most-recently-accessed first.
    recent = list(reversed(list(snapshot.items())))[:POST_COMPACT_MAX_FILES]

    parts: list[str] = []
    used_tokens = 0
    for path, state in recent:
        content = state.content
        tokens = config.estimate_tokens(content)
        if tokens > POST_COMPACT_MAX_TOKENS_PER_FILE:
            ratio = POST_COMPACT_MAX_TOKENS_PER_FILE / tokens
            content = content[: int(len(content) * ratio)]
            tokens = POST_COMPACT_MAX_TOKENS_PER_FILE
        if used_tokens + tokens > POST_COMPACT_FILES_TOKEN_BUDGET:
            break
        used_tokens += tokens
        parts.append(
            f'<system-reminder>\n'
            f'Called the Read tool with the following input: {{"file_path":"{path}"}}\n'
            f'</system-reminder>\n'
            f'<system-reminder>\n'
            f'Result of calling the Read tool:\n{content}\n'
            f'</system-reminder>'
        )

    if not parts:
        return None

    return Attachment(type="system_reminder", content="\n".join(parts))


# ---------------------------------------------------------------------------
# Low-level agent loop — shared by top-level REPL and sub-agents
# ---------------------------------------------------------------------------


def _reinject_after_compact(
    history: MessageHistory,
    rebuild_fn: Callable[[dict[str, Any]], list[Attachment]] | None,
    file_snapshot: dict[str, Any] | None = None,
) -> None:
    """Re-build and inject all post-compact attachments."""
    if rebuild_fn is None:
        return
    attachments = rebuild_fn(file_snapshot or {})
    if not attachments:
        return
    last_msg = history.last_user_message()
    if last_msg is not None:
        last_msg.attach(attachments)


async def run_agent_loop(
    *,
    memory_task: asyncio.Task[Attachment | None] | None = None,
    system_prompt: str,
    tool_use_context: ToolUseContext,
    max_turns: int,
    label: str = "main",
    stream: bool = False,
    thinking: dict | None = None,
    on_event: EventCallback,
    on_compact_rebuild: Callable[[dict[str, Any]], list[Attachment]] | None = None,
) -> str:
    """Run the core LLM <-> tool execution cycle.

    Emits AgentEvent objects via on_event callback.
    Returns the final assistant text.

    The caller must set tool_use_context.agent_state before calling.
    """
    _state = tool_use_context.agent_state or AgentState(agent_id=label)
    history = tool_use_context.messages
    _msg_count_at_usage = len(history)

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
        # --- Micro Compact: clear old tool_result content before API call ---
        if should_micro_compact(history.messages):
            micro_compact(history.messages)

        # --- Auto Compact: full LLM summarization if near context limit ---
        if should_auto_compact(estimate_token_count(history, _state)):
            cache = tool_use_context.file_state_cache
            file_snapshot = cache.snapshot() if cache is not None else {}
            if await auto_compact(history):
                if cache is not None:
                    cache.clear()
                _reinject_after_compact(history, on_compact_rebuild, file_snapshot)

        # --- Changed files: detect external modifications and inject diffs ---
        cache = tool_use_context.file_state_cache
        changed_atts = cache.get_changed_files() if cache is not None else []
        if changed_atts:
            last_user = history.last_user_message()
            if last_user is not None:
                last_user.attach(changed_atts)

        tools = build_tool_schemas(
            tool_registry,
            allowed_tools=tool_use_context.tools,
            tool_overrides=tool_use_context.tool_overrides,
        )
        pending_retry_events.clear()

        # --- Call LLM ---
        streaming_executor: StreamingToolExecutor | None = None

        async def _call_llm():
            nonlocal streaming_executor
            if stream:
                streaming_executor = StreamingToolExecutor(label, tool_use_context, on_event)
                return await _stream_call(
                    history, system_prompt, tools, thinking, _on_retry, label,
                    tool_executor=streaming_executor, on_event=on_event,
                )
            return await query_model(
                messages=history.normalized_for_api(),
                system=system_prompt, tools=tools,
                thinking=thinking, on_retry=_on_retry,
            )

        def _emit_error(err: Exception) -> None:
            """Inject error into history and emit ErrorEvent."""
            error_msg = create_assistant_error_message(err)
            history.add_assistant(error_msg["content"])
            on_event(ErrorEvent(label=label, error_text=error_msg["content"][0]["text"]))

        try:
            response = await _call_llm()
        except Exception as api_error:
            for ev in pending_retry_events:
                on_event(ev)

            # Layer 3: reactive compact on prompt_too_long
            if _is_prompt_too_long(api_error):
                on_event(Recovery(label=label, message="Prompt too long, compacting conversation..."))
                cache = tool_use_context.file_state_cache
                file_snapshot = cache.snapshot() if cache is not None else {}
                if await auto_compact(history):
                    if cache is not None:
                        cache.clear()
                    _reinject_after_compact(history, on_compact_rebuild, file_snapshot)
                    continue

            if _is_thinking_400(api_error) and thinking is not None:
                on_event(Recovery(label=label, message="Stripping thinking blocks and retrying..."))
                _clean_thinking_history(history.messages)
                pending_retry_events.clear()
                try:
                    response = await _call_llm()
                except Exception as retry_error:
                    for ev in pending_retry_events:
                        on_event(ev)
                    _emit_error(retry_error)
                    continue
            else:
                _emit_error(api_error)
                continue

        for ev in pending_retry_events:
            on_event(ev)

        # Track token usage
        _state.total_input_tokens += response.usage.input_tokens
        _state.total_output_tokens += response.usage.output_tokens
        thinking_tokens = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        _state.total_thinking_tokens += thinking_tokens
        # Record for hybrid token estimation (auto compact threshold)
        cache_tokens = (
            (getattr(response.usage, "cache_creation_input_tokens", 0) or 0)
            + (getattr(response.usage, "cache_read_input_tokens", 0) or 0)
        )
        _state.last_usage_tokens = response.usage.input_tokens + response.usage.output_tokens + cache_tokens
        _state.messages_since_last_usage = 0
        _msg_count_at_usage = len(history)

        assistant_content = _serialize_content(response.content)
        history.add_assistant(assistant_content)

        # --- Stream path: tools already executed by StreamingToolExecutor ---
        if stream and streaming_executor is not None and streaming_executor.has_tools():
            tool_use_context = _apply_tool_results(
                streaming_executor.collect_results(),
                assistant_content, history, tool_use_context,
            )
            continue

        # --- Non-stream path: emit thinking/text, collect tool_use into groups ---
        tool_groups: list[ToolCallGroup] = []
        for block in response.content:
            if getattr(block, "type", None) == "thinking":
                text = getattr(block, "thinking", "") or ""
                if text:
                    on_event(ThinkingBlock(label=label, thinking=text))
            elif block.type == "text":
                on_event(TextBlock(label=label, text=block.text))
            elif block.type == "tool_use":
                merge_tool_call(id=block.id, tool_name=block.name,
                                tool_input=block.input, groups=tool_groups)

        if not tool_groups:
            on_event(TokenUsage(
                label=label,
                input_tokens=_state.total_input_tokens,
                output_tokens=_state.total_output_tokens,
                thinking_tokens=_state.total_thinking_tokens,
            ))
            return extract_text(response.content)

        # --- Batch execute tools + collect results ---
        tool_results = await execute_tool_groups(label, tool_groups, tool_use_context, on_event)

        tool_use_context = _apply_tool_results(
            tool_results, assistant_content, history, tool_use_context,
        )
        # Track messages added since last API usage snapshot
        # (assistant response + tool_results + any injected messages)
        _state.messages_since_last_usage = len(history) - _msg_count_at_usage
        continue

    return f"[Agent loop '{label}' reached max turns ({max_turns})]"

# ---------------------------------------------------------------------------
# Top-level entry point — called from main.py REPL
# ---------------------------------------------------------------------------

async def agent_loop(
    user_input: str,
    history: MessageHistory,
    state: AgentState,
    file_state_cache: Any | None = None,
) -> str:
    """Run the agent loop for a single user turn.

    `history` is the persistent conversation state shared across turns.
    The caller (main.py REPL) owns it; we mutate via its typed helpers.
    """
    user_msg = history.add_user(user_input)

    main_tools = tool_registry.list_names()

    # --- Independent reminder channels (each can be reloaded separately) ---
    from src.types import PlanPhase

    def _build_attachments() -> list[Attachment]:
        s = build_skill_reminder(main_tools, use_sent_tracking=True)
        a = build_agent_reminder(main_tools, use_sent_tracking=True)
        m = build_memory_index_reminder()
        parts: list[Attachment | None] = [s, a, m]
        if state.plan_phase == PlanPhase.ACTIVE:
            from src.plan_mode import build_plan_mode_attachment
            parts.append(build_plan_mode_attachment(state.plan_file_path))
        elif state.plan_phase == PlanPhase.EXITING:
            from src.plan_mode import build_plan_exit_attachment
            parts.append(build_plan_exit_attachment(state.plan_file_path))
            state.plan_phase = PlanPhase.INACTIVE
        return [x for x in parts if x]

    attachments = _build_attachments()
    if attachments:
        user_msg.attach(attachments)

    def _rebuild_after_compact(file_snapshot: dict[str, Any]) -> list[Attachment]:
        s = build_skill_reminder(main_tools, use_sent_tracking=True, force=True)
        a = build_agent_reminder(main_tools, use_sent_tracking=True, force=True)
        m = build_memory_index_reminder()
        k = _build_invoked_skills_attachment(tool_use_context.invoked_skills)
        f = _build_file_restore_attachments(file_snapshot)
        parts = [s, a, m, k, f]
        if state.plan_file_path:
            from src.plan_mode import build_plan_mode_attachment, build_plan_content_attachment
            if state.plan_phase == PlanPhase.ACTIVE:
                parts.append(build_plan_mode_attachment(state.plan_file_path))
            parts.append(build_plan_content_attachment(state.plan_file_path))
        return [x for x in parts if x]

    history_copy = copy.copy(history)
    memory_task = asyncio.create_task(_prepare_memory_context(user_input=user_input, history=history_copy))

    system = build_system_prompt()
    tool_use_context = ToolUseContext(
        messages=history,
        tools=tool_registry.list_names(),
        permissions=_get_permission_engine(),
        file_state_cache=file_state_cache,
    )
    tool_use_context.agent_state = state

    turn_start_index = len(history)

    handler = make_interactive_handler(default_handler)
    result = await run_agent_loop(
        memory_task=memory_task,
        system_prompt=system,
        tool_use_context=tool_use_context,
        max_turns=config.MAX_TURNS,
        label="main",
        stream=True,
        thinking=_build_thinking_param(),
        on_event=handler,
        on_compact_rebuild=_rebuild_after_compact,
    )

    # Background memory extraction — fire-and-forget asyncio task
    from src.memory.extract import run_memory_extraction

    messages_snapshot = copy.deepcopy(history.messages)

    async def _run_extraction():
        try:
            await run_memory_extraction(
                messages_snapshot, get_memory_dir(), since_index=turn_start_index,
                on_event=lambda _: None,
            )
        except Exception as e:
            logger.debug("Background memory extraction failed: %s", e)

    asyncio.create_task(_run_extraction())

    return result


# ---------------------------------------------------------------------------
# Streaming helper — real-time text delta emission
# ---------------------------------------------------------------------------

async def _stream_call(
    history: MessageHistory,
    system_prompt: str,
    tools: list[dict],
    thinking: dict | None,
    on_retry: Callable[[float, int, int], None] | None,
    label: str,
    tool_executor: StreamingToolExecutor | None = None,
    on_event: EventCallback = lambda _: None,
) -> anthropic.types.Message:
    """Call the streaming API, emit events via on_event, optionally execute tools as they arrive.

    Returns the final API Message.
    """
    stream_cm = create_stream_with_retry(
        messages=history.normalized_for_api(),
        system=system_prompt,
        tools=tools,
        thinking=thinking,
        on_retry=on_retry,
    )
    _first_text = True
    _first_thinking = True

    '''
    这个api_stream是一个异步生成器，里面会不断地产生事件（event），这些事件可能是文本块（text）、思考块（thinking）或者工具使用块（tool_use）。我们通过async for循环来迭代这些事件，并根据事件的类型进行不同的处理：
    但是本质上这api stream内部重载了aitable和aiter方法，使得我们可以使用async for来迭代它，就像迭代一个异步生成器一样。
    然后里面依然是用await feture来获取数据，同时注册到select网络监听器上，当数据准备好时就会被触发，继续往下执行。
    这个select就和之前研究的一样，触发了io唤醒之后，就会按照表来set feture唤醒，并把原始数据一起set feture回来，然后解析然后继续往下走。
    '''
    async with stream_cm as api_stream:
        async for event in api_stream:
            if event.type == "text":
                on_event(TextDelta(label=label, delta=event.text, first=_first_text))
                _first_text = False

            elif event.type == "thinking":
                on_event(ThinkingDelta(label=label, delta=event.thinking, first=_first_thinking))
                _first_thinking = False

            elif event.type == "content_block_delta":
                continue

            elif event.type == "content_block_stop":
                snapshot = api_stream.current_message_snapshot
                block = snapshot.content[event.index]
                if block.type == "tool_use" and tool_executor is not None:
                    tool_executor.add_tool(block)
                    await tool_executor.drain_completed()

        if tool_executor is not None:
            await tool_executor.drain_remaining()
        return await api_stream.get_final_message()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _recover_orphan_tool_results(
    assistant_content: list[ContentBlock],
    tool_result_blocks: list[ToolResultContent],
) -> None:
    """Ensure every tool_use in assistant_content has a matching tool_result."""
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


def _apply_tool_results(
    tool_results: list,
    assistant_content: list[ContentBlock],
    history: MessageHistory,
    tool_use_context: ToolUseContext,
) -> ToolUseContext:
    """Process tool execution results: build tool_result messages, inject into history."""
    tool_result_blocks: list[ToolResultContent] = []
    all_new_messages: list[Message] = []
    pending_context_modifier = None

    for result, tool_id, llm_text, is_error in tool_results:
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

    return tool_use_context


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


def _is_prompt_too_long(error: Exception) -> bool:
    """Check if API error is a prompt-too-long error (400/413)."""
    from anthropic import APIError as _APIError
    if not isinstance(error, _APIError):
        return False
    status = getattr(error, "status_code", None) or getattr(error, "status", None)
    if status == 413:
        return True
    if status == 400:
        msg = str(error).lower()
        return "prompt is too long" in msg or "too many tokens" in msg
    return False


def _is_thinking_400(error: Exception) -> bool:
    """Check if an API error is a thinking-related 400 that can be recovered."""
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
    """Select and read relevant memories (runs async, non-blocking)."""
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
        from src.config import get_permission_config_paths
        user_config, project_config = get_permission_config_paths()
        _permission_engine = PermissionEngine(
            user_config=user_config,
            project_config=project_config,
        )
    return _permission_engine
