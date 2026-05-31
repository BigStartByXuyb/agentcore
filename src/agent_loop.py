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



from src.core import config
from src.core.types import (
    AgentState, Attachment, ContentBlock, EventCallback, LoopResult, Message, MessageHistory,
    TextContent, ThinkingContent, RedactedThinkingContent, ToolResultContent, ToolUseContent,
    ToolUseContext, make_missing_tool_results,
)
from src.system_prompt import build_system_prompt
from src.messages import build_tool_schemas, build_tool_result_content, clean_thinking_history
from src.messages import build_skill_reminder, build_agent_reminder, build_memory_index_reminder
from src.tool_runner import StreamingToolExecutor, DenialTracker
from src.permissions import get_permission_engine
from src.tools import registry as tool_registry
from src.api import query_model, query_model_stream
from src.providers.stream import StreamEvent, StreamingFallbackEvent
from src.providers.types import ProviderMessage
from src.providers.retry import RetryEvent
from src.core.errors import AgentErrorCode
from src.core.events import (
    AgentEvent, TextDelta, ThinkingDelta,
    ErrorEvent, Recovery, TokenUsage, RetryNotice,
    CompactCircuitBreaker, BlockingLimitReached,
)
from src.display import make_interactive_handler, default_handler
from src.memory.recall import prepare_memory_context
from src.memory.paths import get_memory_dir
from src.compact.micro_compact import should_micro_compact, micro_compact
from src.compact.auto_compact import (
    should_auto_compact, auto_compact,
    is_at_blocking_limit, MAX_CONSECUTIVE_COMPACT_FAILURES,
)
from src.utils.tokens import estimate_token_count
from src.task_store import get_task_store

logger = logging.getLogger(__name__)

POST_COMPACT_MAX_TOKENS_PER_SKILL = 5_000
POST_COMPACT_SKILLS_TOKEN_BUDGET = 25_000
POST_COMPACT_MAX_FILES = 5
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000
POST_COMPACT_FILES_TOKEN_BUDGET = 50_000
TASK_REMINDER_INTERVAL = 10
MAX_CONSECUTIVE_THINKING_FAILURES = 3


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
        tokens = config.get().estimate_tokens(content)
        if tokens > POST_COMPACT_MAX_TOKENS_PER_SKILL:
            char_budget = config.get().tokens_to_chars(POST_COMPACT_MAX_TOKENS_PER_SKILL)
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
        try:
            from src.utils.file_encoding import read_file_streaming
            lines, _, _ = read_file_streaming(path)
            content = "\n".join(lines)
        except Exception:
            content = state.content
        tokens = config.get().estimate_tokens(content)
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


async def _try_compaction(
    *,
    tool_use_context: ToolUseContext,
    state: AgentState,
    on_event: EventCallback,
    on_compact_rebuild: Callable[[dict[str, Any]], list[Attachment]] | None = None,
    session_storage: Any | None = None,
) -> None:
    """Micro compact + auto compact with circuit breaker."""
    history = tool_use_context.messages
    label = tool_use_context.label
    cache = tool_use_context.file_state_cache

    if should_micro_compact(history.messages):
        micro_compact(history.messages)

    estimated = estimate_token_count(history, state)
    if should_auto_compact(estimated):
        if state.compact_consecutive_failures >= MAX_CONSECUTIVE_COMPACT_FAILURES:
            pass  # circuit breaker open — skip auto compact
        else:
            file_snapshot = cache.snapshot() if cache is not None else {}
            try:
                ok = await auto_compact(tool_use_context)
            except Exception:
                ok = False
            if ok:
                state.compact_consecutive_failures = 0
                if cache is not None:
                    cache.clear()
                _reinject_after_compact(history, on_compact_rebuild, file_snapshot)
                if session_storage:
                    session_storage.append_compaction_marker()
                    if history.messages:
                        session_storage.append(history.messages[0])
            else:
                state.compact_consecutive_failures += 1
                if state.compact_consecutive_failures >= MAX_CONSECUTIVE_COMPACT_FAILURES:
                    on_event(CompactCircuitBreaker(
                        label=label,
                        failures=state.compact_consecutive_failures,
                        message="Auto-compact failed 3 consecutive times. Use /compact or /clear.",
                    ))


def _inject_attachments(
    *,
    tool_use_context: ToolUseContext,
    state: AgentState,
) -> None:
    """Inject changed-files diffs and task reminders into the last user message."""
    history = tool_use_context.messages
    cache = tool_use_context.file_state_cache

    if cache is not None:
        changed_atts = cache.get_changed_files()
        if changed_atts:
            last_user = history.last_user_message()
            if last_user is not None:
                last_user.attach(changed_atts)

    if "TaskUpdate" in tool_use_context.tools:
        _task_store = get_task_store()
        if (
            _task_store.has_active()
            and state.turns_since_task_write >= TASK_REMINDER_INTERVAL
            and state.turns_since_task_reminder >= TASK_REMINDER_INTERVAL
        ):
            reminder = Attachment(
                type="system_reminder",
                content=(
                    "<system-reminder>\n"
                    "The task tools haven't been used recently. If you're working on "
                    "tasks that would benefit from tracking progress, consider using "
                    "TaskCreate to add new tasks and TaskUpdate to update task status "
                    "(set to in_progress when starting, completed when done). "
                    "Also consider cleaning up the task list if it has become stale. "
                    "Only use these if relevant to the current work. "
                    "This is just a gentle reminder - ignore if not applicable.\n"
                    "</system-reminder>"
                ),
            )
            last_user = history.last_user_message()
            if last_user is not None:
                last_user.attach([reminder])
            state.turns_since_task_reminder = 0


async def _pre_turn_maintenance(
    *,
    tool_use_context: ToolUseContext,
    state: AgentState,
    skip_compaction: bool,
    on_event: EventCallback,
    on_compact_rebuild: Callable[[dict[str, Any]], list[Attachment]] | None = None,
    session_storage: Any | None = None,
) -> str | None:
    """Per-turn housekeeping before API call.

    Returns None on success, or an error message string if the loop should exit.
    """
    if not skip_compaction:
        await _try_compaction(
            tool_use_context=tool_use_context,
            state=state,
            on_event=on_event,
            on_compact_rebuild=on_compact_rebuild,
            session_storage=session_storage,
        )

    _inject_attachments(tool_use_context=tool_use_context, state=state)

    history = tool_use_context.messages
    if is_at_blocking_limit(estimate_token_count(history, state)):
        on_event(BlockingLimitReached(
            label=tool_use_context.label,
            estimated_tokens=estimate_token_count(history, state),
            message="Context window nearly full. Use /compact or /clear.",
        ))
        return "Context window nearly full. Use /compact or /clear."

    return None


async def _run_agent_loop_inner(
    *,
    memory_task: asyncio.Task[Attachment | None] | None = None,
    tool_use_context: ToolUseContext,
    max_turns: int,
    query_source: str = "main",
    on_event: EventCallback,
    on_compact_rebuild: Callable[[dict[str, Any]], list[Attachment]] | None = None,
    session_storage: Any | None = None,
) -> LoopResult:
    """Inner implementation — may raise; outer wrapper catches all."""

    _skip_compaction = query_source in ("compact", "memory")
    if tool_use_context.agent_state is None:
        tool_use_context.agent_state = AgentState(agent_id=tool_use_context.label)
    _state = tool_use_context.agent_state
    system_prompt = tool_use_context.system_prompt
    label = tool_use_context.label
    thinking = tool_use_context.thinking
    _thinking_original = thinking
    history = tool_use_context.messages
    _msg_count_at_usage = len(history)

    for _turn in range(max_turns):
        if memory_task is not None and memory_task.done():
            mem = memory_task.result()
            memory_task = None
            if mem is not None:
                last_msg = tool_use_context.messages.last_user_message()
                if last_msg is not None:
                    last_msg.attach([mem])

        error = await _pre_turn_maintenance(
            tool_use_context=tool_use_context,
            state=_state,
            skip_compaction=_skip_compaction,
            on_event=on_event,
            on_compact_rebuild=on_compact_rebuild,
            session_storage=session_storage,
        )
        if error is not None:
            return LoopResult(reason="blocking_limit", text=error)

        tools = build_tool_schemas(
            tool_registry,
            allowed_tools=tool_use_context.tools,
            tool_overrides=tool_use_context.tool_overrides,
        )

        # --- Call LLM ---
        streaming_executor = StreamingToolExecutor(label, tool_use_context, on_event)
        _first_text = True
        _first_thinking = True
        response: ProviderMessage | None = None

        async for item in query_model_stream(
            messages=history.prepare_messages(),
            system=system_prompt, tools=tools,
            thinking=thinking,
        ):
            if isinstance(item, RetryEvent):
                on_event(RetryNotice(
                    label=label,
                    delay=item.delay,
                    attempt=item.attempt,
                    max_attempts=item.max_attempts,
                ))
            elif isinstance(item, StreamingFallbackEvent):
                streaming_executor.discard()
                streaming_executor = StreamingToolExecutor(label, tool_use_context, on_event)
                _first_text = True
                _first_thinking = True
                on_event(Recovery(label=label, message="Streaming failed, falling back to non-streaming..."))
            elif isinstance(item, ProviderMessage):
                response = item
            elif isinstance(item, StreamEvent):
                if item.type == "text":
                    on_event(TextDelta(label=label, delta=item.text, first=_first_text))
                    _first_text = False
                elif item.type == "thinking":
                    on_event(ThinkingDelta(label=label, delta=item.thinking, first=_first_thinking))
                    _first_thinking = False
                elif item.type == "content_block_stop" and item.block:
                    if item.block.type == "tool_use":
                        streaming_executor.add_tool(item.block)
                        await streaming_executor.drain_completed()

        await streaming_executor.drain_remaining()
        assert response is not None

        # --- Handle error responses (all decisions via error_code, not raw exception) ---
        if response.is_error:
            ec = response.error_code

            # Unrecoverable — bail out immediately
            if ec == AgentErrorCode.API_AUTH_ERROR.value:
                error_text = response.content[0].text if response.content else "Unknown error"
                msg = history.add_assistant([TextContent(text=error_text)])
                if session_storage:
                    session_storage.append(msg)
                on_event(ErrorEvent(label=label, error_text=error_text))
                return LoopResult(reason="auth_error", text=error_text)

            # Layer 3: reactive compact on prompt_too_long (bypasses circuit breaker)
            if ec == AgentErrorCode.API_PROMPT_TOO_LONG.value:
                if _skip_compaction:
                    error_text = response.content[0].text if response.content else "Prompt is too long"
                    return LoopResult(reason="prompt_too_long", text=error_text)
                on_event(Recovery(label=label, message="Prompt too long, compacting conversation..."))
                _cache = tool_use_context.file_state_cache
                file_snapshot = _cache.snapshot() if _cache is not None else {}
                if await auto_compact(tool_use_context):
                    _state.compact_consecutive_failures = 0
                    if _cache is not None:
                        _cache.clear()
                    _reinject_after_compact(history, on_compact_rebuild, file_snapshot)
                    if session_storage:
                        session_storage.append_compaction_marker()
                        if history.messages:
                            session_storage.append(history.messages[0])
                    continue
                else:
                    _state.compact_consecutive_failures += 1

            # Thinking-400 recovery: strip stale thinking blocks and retry
            if ec == AgentErrorCode.API_THINKING_ERROR.value and thinking:
                _state.thinking_consecutive_failures += 1
                clean_thinking_history(history.messages)
                if _state.thinking_consecutive_failures >= MAX_CONSECUTIVE_THINKING_FAILURES:
                    thinking = False
                    on_event(Recovery(label=label, message=f"Thinking failed {_state.thinking_consecutive_failures} times, disabling thinking."))
                else:
                    on_event(Recovery(label=label, message=f"Stripping thinking blocks and retrying ({_state.thinking_consecutive_failures}/{MAX_CONSECUTIVE_THINKING_FAILURES})..."))
                continue

            # No recovery matched — inject error message and continue
            error_text = response.content[0].text if response.content else "Unknown error"
            msg = history.add_assistant([TextContent(text=error_text)])
            if session_storage:
                session_storage.append(msg)
            on_event(ErrorEvent(label=label, error_text=error_text))
            continue

        assert not response.is_error
        if _state.thinking_consecutive_failures > 0:
            _state.thinking_consecutive_failures = 0
            thinking = _thinking_original
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
        msg = history.add_assistant(assistant_content)
        if session_storage:
            session_storage.append(msg)

        # --- Execute tools (unified streaming path) ---
        assert streaming_executor is not None
        if streaming_executor.has_tools():
            tool_use_context = _apply_tool_results(
                streaming_executor.collect_results(),
                assistant_content, history, tool_use_context,
                session_storage=session_storage,
            )
            tool_names_used = [t.name for t in streaming_executor._tools]

            # Check denial abort flag after all tools completed
            if tool_use_context.denial_tracker and tool_use_context.denial_tracker.abort_requested:
                on_event(ErrorEvent(label=label, error_text=tool_use_context.denial_tracker.abort_message))
                return LoopResult(reason="completed", text=tool_use_context.denial_tracker.abort_message)
        else:
            on_event(TokenUsage(
                label=label,
                input_tokens=_state.total_input_tokens,
                output_tokens=_state.total_output_tokens,
                thinking_tokens=_state.total_thinking_tokens,
            ))
            return LoopResult(reason="completed", text=extract_text(response.content))

        # --- Shared post-tool logic (both paths) ---
        _state.messages_since_last_usage = len(history) - _msg_count_at_usage

        _task_tool_names = {"TaskCreate", "TaskUpdate", "TaskList"}
        _used_task_tool = any(n in _task_tool_names for n in tool_names_used)
        if _used_task_tool:
            _state.turns_since_task_write = 0
            _state.turns_since_task_reminder = 0
        else:
            _state.turns_since_task_write += 1
            _state.turns_since_task_reminder += 1

    return LoopResult(reason="max_turns", text=f"[Agent loop '{label}' reached max turns ({max_turns})]")


async def run_agent_loop(
    *,
    memory_task: asyncio.Task[Attachment | None] | None = None,
    tool_use_context: ToolUseContext,
    max_turns: int,
    query_source: str = "main",
    on_event: EventCallback,
    on_compact_rebuild: Callable[[dict[str, Any]], list[Attachment]] | None = None,
    session_storage: Any | None = None,
) -> LoopResult:
    """Run the core LLM <-> tool execution cycle.

    Emits AgentEvent objects via on_event callback.
    Returns LoopResult with structured reason + text. Exceptions never escape.

    query_source controls internal behavior:
      - "main" / "subagent": full capabilities including auto-compaction
      - "compact" / "memory": skips compaction detection (prevents recursion)
    """
    try:
        return await _run_agent_loop_inner(
            memory_task=memory_task,
            tool_use_context=tool_use_context,
            max_turns=max_turns,
            query_source=query_source,
            on_event=on_event,
            on_compact_rebuild=on_compact_rebuild,
            session_storage=session_storage,
        )
    except Exception as e:
        logger.exception("Unhandled error in run_agent_loop")
        return LoopResult(reason="error", text=str(e), error=e)


# ---------------------------------------------------------------------------
# Top-level entry point — called from main.py REPL
# ---------------------------------------------------------------------------

async def agent_loop(
    user_input: str,
    history: MessageHistory,
    state: AgentState,
    file_state_cache: Any | None = None,
    session_storage: Any | None = None,
) -> LoopResult:
    """Run the agent loop for a single user turn.

    `history` is the persistent conversation state shared across turns.
    The caller (main.py REPL) owns it; we mutate via its typed helpers.
    """
    user_msg = history.add_user(user_input)

    main_tools = tool_registry.list_names()

    # --- Independent reminder channels (each can be reloaded separately) ---
    from src.core.types import PlanPhase

    def _build_attachments() -> list[Attachment]:
        s = build_skill_reminder(main_tools, use_sent_tracking=True)
        a = build_agent_reminder(main_tools, use_sent_tracking=True)
        m = build_memory_index_reminder()
        parts: list[Attachment | None] = [s, a, m]
        if state.plan_phase == PlanPhase.ACTIVE and state.plan_file_path is not None:
            from src.plan_mode import build_plan_mode_attachment
            parts.append(build_plan_mode_attachment(state.plan_file_path))
        elif state.plan_phase == PlanPhase.EXITING and state.plan_file_path is not None:
            from src.plan_mode import build_plan_exit_attachment
            parts.append(build_plan_exit_attachment(state.plan_file_path))
            state.plan_phase = PlanPhase.INACTIVE
        return [x for x in parts if x]

    attachments = _build_attachments()
    if attachments:
        user_msg.attach(attachments)

    if session_storage:
        session_storage.append(user_msg)

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
    memory_task = asyncio.create_task(prepare_memory_context(user_input=user_input, history=history_copy))

    # --- Task store: use session-global singleton ---
    if state._task_store is None:
        state._task_store = get_task_store()

    system = build_system_prompt()
    perm_engine = get_permission_engine()
    tool_use_context = ToolUseContext(
        messages=history,
        tools=tool_registry.list_names(),
        system_prompt=system,
        label="main",
        thinking=config.get().thinking_enabled,
        permissions=perm_engine,
        file_state_cache=file_state_cache,
        task_store=state._task_store,
        denial_tracker=DenialTracker(headless=bool(
            perm_engine and perm_engine._headless
        )),
    )
    tool_use_context.agent_state = state

    turn_start_index = len(history)

    handler = make_interactive_handler(default_handler)
    result = await run_agent_loop(
        memory_task=memory_task,
        tool_use_context=tool_use_context,
        max_turns=config.get().max_turns,
        on_event=handler,
        on_compact_rebuild=_rebuild_after_compact,
        session_storage=session_storage,
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
    tool_result_blocks.extend(make_missing_tool_results(
        expected_ids, tool_result_blocks,
        error_message="Tool execution was interrupted before this tool could run.",
    ))


def _apply_tool_results(
    tool_results: list,
    assistant_content: list[ContentBlock],
    history: MessageHistory,
    tool_use_context: ToolUseContext,
    session_storage: Any | None = None,
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
    tr_msg = history.add_tool_results(tool_result_blocks)
    if session_storage:
        session_storage.append(tr_msg)

    if all_new_messages:
        ph_msg = history.add_assistant_placeholder()
        if session_storage:
            session_storage.append(ph_msg)
        history.inject_messages(all_new_messages)
        if session_storage:
            for m in all_new_messages:
                session_storage.append(m)

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



