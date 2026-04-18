"""Agent loop — the main LLM ↔ tool execution cycle.

Provides two entry points:

  agent_loop()      — top-level REPL entry: manages MessageHistory, injects
                      skill reminders, delegates to run_agent_loop().
  run_agent_loop()  — low-level generator: takes raw messages + config, runs
                      the LLM ↔ tool cycle, yields AgentEvent objects, and
                      returns final text via StopIteration.value.

Corresponds to Claude Code's:
  - src/query.ts            → queryLoop() (async generator yielding Messages)
  - src/services/tools/     → tool execution within the loop
  - src/utils/forkedAgent.ts → sub-agent loop reuse
"""

from __future__ import annotations

from typing import Generator

from src import config
from src.types import AgentState, MessageHistory, ToolUseContext
from src.system_prompt import build_system_prompt
from src.messages import build_tool_schemas, build_tool_result_content
from src.messages import strip_thinking_blocks, filter_orphaned_thinking_messages
from src.messages import build_metadata_reminders
from src.tool_runner import run_tool_use
from src.tools import ALL_TOOLS
from src.api import query_model, create_stream_with_retry
from src.errors import create_assistant_error_message
from src.events import (
    AgentEvent, TextDelta, TextBlock, ThinkingBlock, ToolStart, ToolEnd,
    ErrorEvent, Recovery, TokenUsage, RetryNotice,
)
from src.display import consume_events, default_handler
from src.memory.recall import find_relevant_memories
from src.memory.paths import get_memory_dir
from src.memory.prompt import build_memory_user_message

# ---------------------------------------------------------------------------
# Low-level agent loop — shared by top-level REPL and sub-agents
# ---------------------------------------------------------------------------

def run_agent_loop(
    *,
    messages: list[dict],
    system_prompt: str,
    tool_use_context: ToolUseContext,
    max_turns: int,
    state: AgentState | None = None,
    label: str = "main",
    stream: bool = False,
    thinking: dict | None = None,
) -> Generator[AgentEvent, None, str]:
    """Run the core LLM ↔ tool execution cycle as a generator.

    Yields AgentEvent objects for display; returns the final text via
    StopIteration.value.  Callers use consume_events() to drain the
    generator and collect the return value.

    Tools are derived from tool_use_context.tools (the single source of
    truth), matching Claude Code's pattern where tools live exclusively
    in toolUseContext.options.tools.
    """
    if state is None:
        state = AgentState(agent_id=label)

    history = _wrap_messages(messages)

    # Retry callback — yields RetryNotice events from inside api.py
    pending_retry_events: list[RetryNotice] = []

    def _on_retry(delay: float, attempt: int, max_attempts: int) -> None:
        pending_retry_events.append(RetryNotice(label=label, delay=delay, attempt=attempt, max_attempts=max_attempts))

    for _turn in range(max_turns):
        # Build tool schemas from the single source of truth each turn
        tools = build_tool_schemas(tool_use_context.tools, tool_use_context.tool_overrides)

        # --- Call LLM ---
        pending_retry_events.clear()

        try:
            if stream:
                response = yield from _stream_call(
                    history, system_prompt, tools, thinking, _on_retry, label,
                )
            else:
                response = query_model(
                    messages=history.normalized_for_api(),
                    system=system_prompt,
                    tools=tools,
                    thinking=thinking,
                    on_retry=_on_retry,
                )
        except Exception as api_error:
            # Yield any pending retry events first
            yield from pending_retry_events

            if _is_thinking_400(api_error) and thinking is not None:
                yield Recovery(label=label, message="Stripping thinking blocks and retrying...")
                _clean_thinking_history(history.messages)
                pending_retry_events.clear()
                try:
                    if stream:
                        response = yield from _stream_call(
                            history, system_prompt, tools, thinking, _on_retry, label,
                        )
                    else:
                        response = query_model(
                            messages=history.normalized_for_api(),
                            system=system_prompt,
                            tools=tools,
                            thinking=thinking,
                            on_retry=_on_retry,
                        )
                except Exception as retry_error:
                    yield from pending_retry_events
                    error_msg = create_assistant_error_message(retry_error)
                    history.add_assistant(error_msg["content"])
                    error_text = error_msg["content"][0]["text"]
                    yield ErrorEvent(label=label, error_text=error_text)
                    return error_text
            else:
                error_msg = create_assistant_error_message(api_error)
                history.add_assistant(error_msg["content"])
                error_text = error_msg["content"][0]["text"]
                yield ErrorEvent(label=label, error_text=error_text)
                return error_text

        # Yield any pending retry events
        yield from pending_retry_events

        # Track token usage
        state.total_input_tokens += response.usage.input_tokens
        state.total_output_tokens += response.usage.output_tokens
        thinking_tokens = getattr(response.usage, "cache_creation_input_tokens", 0) or 0
        state.total_thinking_tokens += thinking_tokens

        # Yield thinking blocks
        for block in response.content:
            if getattr(block, "type", None) == "thinking":
                text = getattr(block, "thinking", "") or ""
                if text:
                    yield ThinkingBlock(label=label, thinking=text)

        # Append assistant message
        assistant_content = _serialize_content(response.content)
        history.add_assistant(assistant_content)

        # --- end_turn: done ---
        if response.stop_reason == "end_turn":
            yield TokenUsage(
                label=label,
                input_tokens=state.total_input_tokens,
                output_tokens=state.total_output_tokens,
                thinking_tokens=state.total_thinking_tokens,
            )
            return extract_text(response.content)

        # --- tool_use: execute and loop ---
        if response.stop_reason == "tool_use":
            tool_result_blocks: list[dict] = []
            all_new_messages: list[dict] = []
            pending_context_modifier = None

            for block in response.content:
                if block.type == "tool_use":
                    yield ToolStart(label=label, tool_name=block.name, tool_input=block.input)
                    result, llm_text, is_error = yield from run_tool_use(block.name, block.input, block.id, tool_use_context)
                    yield ToolEnd(label=label, tool_name=block.name, is_error=is_error,
                                  result_summary=llm_text[:120] if llm_text else "")
                    tool_result_blocks.append(
                        build_tool_result_content(block.id, llm_text, is_error=is_error)
                    )
                    all_new_messages.extend(result.new_messages)
                    if result.context_modifier is not None:
                        pending_context_modifier = result.context_modifier
                elif block.type == "text" and not stream:
                    yield TextBlock(label=label, text=block.text)

            _recover_orphan_tool_results(assistant_content, tool_result_blocks)
            history.add_tool_results(tool_result_blocks)

            if all_new_messages:
                history.add_assistant_placeholder()
                history.inject_messages(all_new_messages)

            if pending_context_modifier is not None:
                tool_use_context = pending_context_modifier(tool_use_context)
                # tools will be rebuilt from tool_use_context.tools at the
                # top of the next loop iteration — no manual sync needed.

            continue

        return f"[Unexpected stop_reason: {response.stop_reason}]"

    return f"[Agent loop '{label}' reached max turns ({max_turns})]"


# ---------------------------------------------------------------------------
# Top-level entry point — called from main.py REPL
# ---------------------------------------------------------------------------

def agent_loop(
    user_input: str,
    history: MessageHistory,
    state: AgentState,
) -> str:
    """Run the agent loop for a single user turn.

    `history` is the persistent conversation state shared across turns.
    The caller (main.py REPL) owns it; we mutate via its typed helpers.
    This mirrors Claude Code's queryLoop() which receives params.messages
    from the outer REPL and accumulates across the session.

    1. Append user message to the shared history
    2. Inject skill listing as <system-reminder> user message
    3. Delegate to run_agent_loop() for the LLM ↔ tool cycle
    """
    history.add_user(user_input)

    # Inject skill + agent listings as <system-reminder> user messages.
    # Mirrors Claude Code's getSkillListingAttachments() pipeline:
    #   - On the first turn of a session, all skills/agents are announced.
    #   - On subsequent turns, only newly discovered entries are sent
    #     (global _sent_* trackers dedupe).
    #   - The listing is a user message (not system prompt) so the
    #     system prompt stays stable and cacheable.
    #
    # Because the user message we just added and these reminders are
    # both role=user, normalized_for_api() will merge them into a
    # single user turn (content blocks concatenated) — so the API's
    # strict user/assistant alternation is preserved.
    
    main_tools = list(ALL_TOOLS.keys())
    for reminder in build_metadata_reminders(main_tools, use_sent_tracking=True):
        history.inject_messages([reminder])

    # Inject memory context as a <memory-context> user message.
    # Contains: <memory-index> (MEMORY.md index) + <memory-recalled>
    # (relevant memory file contents selected by side query).
    # Keeping this in user messages instead of system prompt ensures
    # the system prompt stays stable and cacheable.
    _inject_memory_context(user_input, history)

    system = build_system_prompt()
    tool_use_context = ToolUseContext(
        messages=history.messages,
        tools=list(ALL_TOOLS.keys()),
    )

    # Record message count before this turn — used by memory extraction
    # to only check messages added during this turn.
    turn_start_index = len(history)

    gen = run_agent_loop(
        messages=history.messages,
        system_prompt=system,
        tool_use_context=tool_use_context,
        max_turns=config.MAX_TURNS,
        state=state,
        label="main",
        stream=True,
        thinking=_build_thinking_param(),
    )
    result = consume_events(gen, default_handler)

    # Trigger background memory extraction after the main agent finishes.
    # Runs in a background thread so the user can start typing immediately.
    # Mirrors Claude Code's handleStopHooks() → extractMemories() pipeline
    # which runs as a forked (non-blocking) agent.
    # Snapshot messages to avoid race with the next user turn.
    # Deep copy because message dicts contain mutable nested structures
    # (content block lists) that could theoretically be modified.
    import copy
    import threading
    from src.memory.extract import run_memory_extraction

    messages_snapshot = copy.deepcopy(history.messages)

    def _run_extraction():
        try:
            extraction_gen = run_memory_extraction(
                messages_snapshot, get_memory_dir(), since_index=turn_start_index,
            )
            consume_events(extraction_gen, lambda _e: None)
        except Exception as e:
            import logging
            logging.getLogger(__name__).debug("Background memory extraction failed: %s", e)

    threading.Thread(target=_run_extraction, daemon=True).start()

    return result


# ---------------------------------------------------------------------------
# Streaming helper — real-time text delta yielding
# ---------------------------------------------------------------------------

def _stream_call(
    history: MessageHistory,
    system_prompt: str,
    tools: list[dict],
    thinking: dict | None,
    on_retry: callable | None,
    label: str,
) -> Generator[AgentEvent, None, object]:
    """Call the streaming API and yield TextDelta events in real-time.

    Returns the final Message object via StopIteration.value.
    This is a sub-generator used by run_agent_loop via `yield from`.
    """
    stream_cm = create_stream_with_retry(
        messages=history.normalized_for_api(),
        system=system_prompt,
        tools=tools,
        thinking=thinking,
        on_retry=on_retry,
    )
    with stream_cm as api_stream:
        _first = True
        for text in api_stream.text_stream:
            yield TextDelta(label=label, delta=text, first=_first)
            _first = False
        return api_stream.get_final_message()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _recover_orphan_tool_results(
    assistant_content: list[dict],
    tool_result_blocks: list[dict],
) -> None:
    """Ensure every tool_use in assistant_content has a matching tool_result.

    If a tool_use block was somehow skipped (e.g. an earlier tool raised and
    broke the loop), we append an is_error=True placeholder so the API never
    sees a tool_use without its corresponding tool_result.

    Mirrors Claude Code's yieldMissingToolResultBlocks() in query.ts:123-149.
    Mutates tool_result_blocks in place.
    """
    # Collect tool_use ids from the assistant response
    expected_ids = {
        block["id"]
        for block in assistant_content
        if block.get("type") == "tool_use"
    }
    # Collect tool_use ids we already have results for
    seen_ids = {
        block["tool_use_id"]
        for block in tool_result_blocks
    }
    # Fill in any missing ones
    for missing_id in expected_ids - seen_ids:
        tool_result_blocks.append(
            build_tool_result_content(
                missing_id,
                "Tool execution was interrupted before this tool could run.",
                is_error=True,
            )
        )


def _wrap_messages(messages: list[dict]) -> MessageHistory:
    """Create a MessageHistory wrapping an *existing* list (no copy).

    This lets run_agent_loop() use MessageHistory's normalized_for_api()
    and add_* helpers while operating on the caller's message list.
    """
    h = MessageHistory()
    h._messages = messages  # share the same list object
    return h


def _serialize_content(content_blocks: list) -> list[dict]:
    """Convert SDK content block objects to plain dicts for re-sending."""
    result: list[dict] = []
    for block in content_blocks:
        if block.type == "text":
            result.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            result.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })
        elif block.type == "thinking":
            # Preserve thinking blocks with their signature — the signature
            # is bound to the API key + model and must be sent back verbatim.
            result.append({
                "type": "thinking",
                "thinking": block.thinking,
                "signature": block.signature,
            })
        elif block.type == "redacted_thinking":
            result.append({"type": "redacted_thinking", "data": block.data})
    return result


def extract_text(content_blocks: list) -> str:
    """Pull plain text from the response content blocks."""
    parts: list[str] = []
    for block in content_blocks:
        if block.type == "text":
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


def _clean_thinking_history(messages: list[dict]) -> None:
    """In-place strip thinking blocks from message history.

    Applies strip_thinking_blocks + filter_orphaned_thinking_messages,
    replacing the list contents in-place so all references (MessageHistory,
    ToolUseContext) see the cleaned version.
    """
    cleaned = strip_thinking_blocks(messages)
    cleaned = filter_orphaned_thinking_messages(cleaned)
    messages[:] = cleaned


# ---------------------------------------------------------------------------
# Memory helpers
# ---------------------------------------------------------------------------

def _read_memory_files(headers: list) -> list[str]:
    """Read body content (without frontmatter) of selected memory files.

    Returns a list of `[filename] body` strings.  Skips files that
    can't be read.  Each file is capped at 4000 chars to avoid
    blowing up context.  Frontmatter is stripped because the index
    already carries name/type/description — no need to repeat it.
    """
    from src.frontmatter import parse_frontmatter

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


def _inject_memory_context(user_input: str, history: MessageHistory) -> None:
    """Build and inject the <memory-context> user message for this turn.

    Combines two parts:
      - <memory-index>: MEMORY.md index (always present if memory enabled)
      - <memory-recalled>: full content of relevant memory files (side query)

    Both are wrapped in <memory-context> and injected as a single user
    message.  The system prompt explains these tags so the LLM knows
    what they mean.
    """
    mem_dir = get_memory_dir()

    # Part 1: memory index
    index_content = build_memory_user_message(mem_dir)

    # Part 2: recalled memory file contents
    relevant_memories = find_relevant_memories(user_input, mem_dir)
    recalled_texts = _read_memory_files(relevant_memories) if relevant_memories else []

    # Nothing to inject
    if not index_content and not recalled_texts:
        return

    # Build combined <memory-context> body
    parts: list[str] = ["<memory-context>"]

    if index_content:
        parts.append("<memory-index>")
        parts.append(index_content)
        parts.append("</memory-index>")

    if recalled_texts:
        parts.append("<memory-recalled>")
        parts.append("\n\n---\n\n".join(recalled_texts))
        parts.append("</memory-recalled>")

    parts.append("</memory-context>")

    history.inject_messages([{
        "role": "user",
        "content": "\n".join(parts),
    }])
