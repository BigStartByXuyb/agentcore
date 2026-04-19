"""Core data structures: ToolResult, ToolUseContext, AgentState, MessageHistory, ToolDef, MemoryHeader."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Generic, Literal, TypeVar, Union


# ---------------------------------------------------------------------------
# Memory types
# ---------------------------------------------------------------------------

MemoryType = Literal["user", "feedback", "project", "reference"]


@dataclass
class MemoryHeader:
    """Parsed header of a single memory file (frontmatter metadata + path).

    Corresponds to Claude Code's MemoryHeader in memoryScan.ts.
    """

    filename: str                   # relative to memory dir (e.g. "user_role.md")
    file_path: str                  # absolute path
    mtime: float                    # modification timestamp (seconds since epoch)
    name: str | None = None         # frontmatter 'name' field
    description: str | None = None  # frontmatter 'description' field
    type: MemoryType | None = None  # frontmatter 'type' field


# ---------------------------------------------------------------------------
# MessageHistory — conversation state manager
# ---------------------------------------------------------------------------

class MessageHistory:
    """Manages the persistent conversation message list.

    Wraps the raw list[dict] that the Anthropic API expects, providing
    typed helper methods for common operations.  The internal list is
    mutated in-place so callers that hold a reference to `self.messages`
    (e.g. ToolUseContext) automatically see updates.

    Corresponds to Claude Code's internal Message[] array.  Claude Code
    adds rich per-message metadata (uuid, timestamp, isMeta, isVirtual …)
    and strips it via normalizeMessagesForAPI() before sending.  We keep
    the raw API format internally (no need to normalise) but centralise
    all mutations here so a richer Message type can be added later without
    touching call-sites.

    Future extension points (left as no-ops for now):
      - compact / summarise old turns
      - clear / reset
      - search by tool name or content
      - token budget tracking
    """

    def __init__(self) -> None:
        self._messages: list[dict] = []

    # -- read access --------------------------------------------------------

    @property
    def messages(self) -> list[dict]:
        """Raw list for the API and ToolUseContext — read-only alias.

        Callers must NOT append directly; use the add_* helpers.
        Returning the live list (not a copy) so ToolUseContext.messages
        stays in sync without re-assignment.
        """
        return self._messages

    def __len__(self) -> int:
        return len(self._messages)

    # -- write helpers ------------------------------------------------------

    def add_user(self, content: str | list[dict]) -> None:
        """Append a user message (plain text or content blocks)."""
        self._messages.append({"role": "user", "content": content})

    def add_assistant(self, content: list[dict]) -> None:
        """Append an assistant message (always content-block array)."""
        self._messages.append({"role": "assistant", "content": content})

    def add_tool_results(self, tool_result_blocks: list[dict]) -> None:
        """Append a user message containing tool_result content blocks.

        Per the API spec, tool results are sent as a user message whose
        content is an array of {type: "tool_result", …} blocks.
        """
        self._messages.append({"role": "user", "content": tool_result_blocks})

    def add_assistant_placeholder(self, text: str = "I've loaded the requested content.") -> None:
        """Insert a minimal assistant message to maintain user/assistant alternation.

        Needed when injecting Skill new_messages (which are user messages)
        right after a user tool_result message — the API requires strict
        alternation.
        """
        self._messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        })

    def inject_messages(self, new_messages: list[dict]) -> None:
        """Bulk-append messages from ToolResult.new_messages (Skill inline mode)."""
        for msg in new_messages:
            self._messages.append(msg)

    # -- normalization --------------------------------------------------------

    def normalized_for_api(self) -> list[dict]:
        """Return a message list safe for the Anthropic API.

        Merges consecutive same-role messages by concatenating their
        content-block arrays.  This mirrors Claude Code's
        normalizeMessagesForAPI() → mergeUserMessages() which folds
        adjacent user messages into a single turn (content blocks stay
        separate, so LLM still sees clear boundaries between e.g. two
        Skill payloads).

        The internal _messages list is NOT mutated — callers that need
        the raw history for bookkeeping still see the un-merged version.

        Content handling:
          - list + list → concatenated (text-text seam gets '\\n')
          - str + str   → joined with '\\n'
          - str + list / list + str → str normalised to [{type:text}]
        """
        if not self._messages:
            return []

        result: list[dict] = [self._messages[0]]
        for msg in self._messages[1:]:
            prev = result[-1]
            if msg["role"] == prev["role"]:
                # Merge content blocks
                prev_content = _normalize_content(prev["content"])
                cur_content = _normalize_content(msg["content"])
                merged = _join_at_seam(prev_content, cur_content)
                result[-1] = {**prev, "content": merged}
            else:
                result.append(msg)
        return result

    # -- future extension stubs ---------------------------------------------

    def clear(self) -> None:
        """Reset conversation history (e.g. user /clear command)."""
        self._messages.clear()


# ---------------------------------------------------------------------------
# ToolUseContext, ToolResult, AgentState
# ---------------------------------------------------------------------------

@dataclass
class ToolUseContext:
    """Execution environment passed to tool executors."""

    messages: list[dict]
    tools: list[str]
    depth: int = 0
    abort_signal: bool = False
    tool_overrides: dict | None = None  # Optional: {name: ToolDef} overrides for ALL_TOOLS lookup


@dataclass
class ToolResult:
    """Unified return type for all tool executors.

    - data:             structured result (consumed by map_result)
    - new_messages:     extra messages to inject (used by Skill inline mode)
    - context_modifier: callback to mutate ToolUseContext (used by Skill)
    """

    data: Any
    new_messages: list[dict] = field(default_factory=list)
    context_modifier: Callable[[ToolUseContext], ToolUseContext] | None = None


# ---------------------------------------------------------------------------
# Tool executor contract
#
# Simple tools are `async def executor(...) -> ToolResult` (returns a
# coroutine). Generator-like tools (Skill fork, Agent subagent) are sync
# functions that build and return an AsyncGenWithResult so the runner can
# iterate events AND recover a terminal ToolResult.
# ---------------------------------------------------------------------------

ToolExecutorReturn = Union[
    Awaitable[ToolResult],
    "AsyncGenWithResult[Any, ToolResult]",
]


@dataclass
class AgentState:
    """Statistics maintained by the agent loop (not exposed to tools)."""

    agent_id: str = "main"
    subagent_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_thinking_tokens: int = 0


class ToolDef:
    """Tool definition — each tool constructs one instance and exports it.

    Required: schema, executor, map_result.
    Optional fields have safe defaults matching Claude Code's buildTool():
      - is_enabled:           default True
      - is_concurrency_safe:  default False  (fail-closed)
      - is_read_only:         default False  (fail-closed)
      - is_destructive:       default False
      - max_result_size_chars: default 30_000
    """

    schema: dict
    executor: Callable[[dict, ToolUseContext], ToolExecutorReturn]
    map_result: Callable[[Any], str]
    is_enabled: Callable[[], bool]
    is_concurrency_safe: Callable[[dict], bool]
    is_read_only: Callable[[dict], bool]
    is_destructive: Callable[[dict], bool]
    max_result_size_chars: int

    def __init__(
        self,
        *,
        schema: dict,
        executor: Callable[[dict, ToolUseContext], ToolExecutorReturn],
        map_result: Callable[[Any], str],
        is_enabled: Callable[[], bool] | None = None,
        is_concurrency_safe: Callable[[dict], bool] | None = None,
        is_read_only: Callable[[dict], bool] | None = None,
        is_destructive: Callable[[dict], bool] | None = None,
        max_result_size_chars: int = 30_000,
    ) -> None:
        self.schema = schema
        self.executor = executor
        self.map_result = map_result
        self.is_enabled = is_enabled or (lambda: True)
        self.is_concurrency_safe = is_concurrency_safe or (lambda _: False)
        self.is_read_only = is_read_only or (lambda _: False)
        self.is_destructive = is_destructive or (lambda _: False)
        self.max_result_size_chars = max_result_size_chars

    @property
    def name(self) -> str:
        return self.schema["name"]


# ---------------------------------------------------------------------------
# AsyncGenWithResult — async generator + final value in one package
#
# Python's PEP 525 forbids `return value` inside `async def + yield`, so we
# cannot directly port the sync `Generator[Y, None, R]` pattern (where the
# final return value is captured via StopIteration.value / `yield from`).
#
# This wrapper is the substitute: it bundles an async event stream
# (`events()`) with a result slot that the impl sets before finishing.
# The same pattern is used at every site that needs "yield events + return
# a value" — run_agent_loop, run_tool_use, Skill fork, Agent tool, etc.
# ---------------------------------------------------------------------------

E = TypeVar("E")
R = TypeVar("R")


class AsyncGenWithResult(Generic[E, R]):
    """Wraps an async iterator impl that also produces a final result.

    Construct with an impl callable `(self) -> AsyncIterator[E]` that yields
    events and sets the final value via `self.set_result(value)` before
    returning.  Callers consume `events()` with `async for`, then read
    `.result` after the iteration completes.

    Example:
        async def _impl(run: AsyncGenWithResult[str, int]):
            yield "starting"
            yield "working"
            run.set_result(42)

        run = AsyncGenWithResult(_impl)
        async for ev in run.events():
            print(ev)
        print(run.result)   # 42
    """

    def __init__(
        self,
        impl: Callable[["AsyncGenWithResult[E, R]"], AsyncIterator[E]],
    ) -> None:
        self._impl = impl
        self._result: R | None = None
        self._result_set = False

    async def events(self) -> AsyncIterator[E]:
        async for ev in self._impl(self):
            yield ev

    def set_result(self, value: R) -> None:
        self._result = value
        self._result_set = True

    @property
    def result(self) -> R:
        if not self._result_set:
            raise RuntimeError(
                "AsyncGenWithResult.result read before impl called set_result(). "
                "Did events() finish iterating?"
            )
        return self._result  # type: ignore[return-value]

    @classmethod
    def of_value(cls, value: R) -> "AsyncGenWithResult[Any, R]":
        """Shortcut for the trivial case — no events, just a result.

        Useful when a function normally yields events but hits an early
        return (e.g., unknown skill, depth exceeded) and wants to stay
        on the same return-type signature without launching a sub-loop.
        """
        async def _impl(run: "AsyncGenWithResult[Any, R]") -> AsyncIterator[Any]:
            run.set_result(value)
            if False:  # pragma: no cover — marks this as an async generator
                yield

        return cls(_impl)


# ---------------------------------------------------------------------------
# Module-level helpers for MessageHistory.normalized_for_api()
# Mirrors Claude Code's joinTextAtSeam() + normalizeUserTextContent()
# ---------------------------------------------------------------------------

def _normalize_content(content: str | list[dict]) -> list[dict]:
    """Ensure content is always a list of content blocks.

    Mirrors normalizeUserTextContent() in messages.ts.
    """
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content


def _join_at_seam(a: list[dict], b: list[dict]) -> list[dict]:
    """Concatenate two content-block arrays, adding '\\n' at text-text seams.

    Mirrors joinTextAtSeam() in messages.ts:
      - If a's last block and b's first block are both text, append '\\n'
        to a's last text so the two don't smash together.
      - Blocks remain separate objects — LLM still sees distinct boundaries.
    """
    if not a:
        return b
    if not b:
        return a

    last_a = a[-1]
    first_b = b[0]
    if last_a.get("type") == "text" and first_b.get("type") == "text":
        patched_last = {**last_a, "text": last_a["text"] + "\n"}
        return [*a[:-1], patched_last, *b]
    return [*a, *b]

@dataclass
class ToolCall:
    """Represents a single tool call with its input and metadata."""
    name: str
    input: dict

@dataclass
class ToolCallGroup:
    """Context for a tool call, including the call details and conversation state."""
    tool_call: list[ToolCall]
    type: str