"""Core data structures: ToolResult, ToolUseContext, AgentState, MessageHistory, ToolDef, MemoryHeader."""

from __future__ import annotations

import time
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
# Attachment — metadata attached to a Message
# ---------------------------------------------------------------------------

AttachmentType = Literal["relevant_memories", "system_reminder"]


@dataclass
class Attachment:
    """Data attached to a Message, expanded into content before API calls.

    Corresponds to Claude Code's Attachment system in attachments.ts.
    Attachments are NOT sent to the API directly — normalized_for_api()
    expands them into the message's content field.

    Attributes:
        type:     Category of attachment (for filtering/querying).
        content:  Text to append to the message content during expansion.
        metadata: Structured data for programmatic access (e.g. memory
                  file paths for dedup tracking). Not sent to API.
    """

    type: AttachmentType
    content: str
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Message — rich wrapper around the raw API message dict
# ---------------------------------------------------------------------------

MessageType = Literal["human", "assistant", "tool_result", "meta"]


@dataclass
class Message:
    """A single conversation message with metadata and attachments.

    Corresponds to Claude Code's internal Message type which stores
    uuid, timestamp, costUSD, attachments etc. alongside the raw
    role/content that the API expects.

    The msg_type field distinguishes messages that share the same API
    role ("user") but have different semantic meanings:
      - "human":       actual user input
      - "tool_result": tool execution results (API role is "user")
      - "meta":        system-injected content (skill listings, etc.)
      - "assistant":   LLM responses

    Supports dict-style access (msg["role"], msg.get("content")) for
    backward compatibility with code that expects raw dicts.
    """

    role: str                       # "user" | "assistant"
    content: str | list[dict]
    msg_type: MessageType = "human"
    attachments: list[Attachment] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    # -- dict-compatible access (backward compat) ---------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __getitem__(self, key: str) -> Any:
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    # -- serialization for persistence --------------------------------------

    def to_serializable(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "msg_type": self.msg_type,
            "attachments": [
                {"type": a.type, "content": a.content, "metadata": a.metadata}
                for a in self.attachments
            ],
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_serializable(cls, data: dict) -> Message:
        attachments = [
            Attachment(type=a["type"], content=a["content"], metadata=a.get("metadata", {}))
            for a in data.get("attachments", [])
        ]
        return cls(
            role=data["role"],
            content=data["content"],
            msg_type=data.get("msg_type", "human"),
            attachments=attachments,
            timestamp=data.get("timestamp", 0),
        )


# ---------------------------------------------------------------------------
# MessageHistory — conversation state manager
# ---------------------------------------------------------------------------

class MessageHistory:
    """Manages the persistent conversation message list.

    Internally stores a list[Message] with rich metadata and attachments.
    normalized_for_api() converts to the raw list[dict] format that the
    Anthropic API expects, expanding attachments into message content and
    merging consecutive same-role messages.

    Corresponds to Claude Code's internal Message[] + normalizeMessagesForAPI().
    """

    def __init__(self) -> None:
        self._messages: list[Message] = []
        self.surfaced_memories: set[str] = set()

    # -- read access --------------------------------------------------------

    @property
    def messages(self) -> list[Message]:
        return self._messages

    def __len__(self) -> int:
        return len(self._messages)

    def last_user_message(self) -> Message | None:
        """Find the most recent actual user input (not tool_result or meta)."""
        for msg in reversed(self._messages):
            if msg.role == "user" and msg.msg_type == "human":
                return msg
        return None

    # -- write helpers ------------------------------------------------------

    def add_user(self, content: str | list[dict], *, msg_type: MessageType = "human") -> Message:
        """Append a user message. Returns the Message so caller can add attachments."""
        msg = Message(role="user", content=content, msg_type=msg_type)
        self._messages.append(msg)
        return msg

    def add_assistant(self, content: list[dict]) -> Message:
        msg = Message(role="assistant", content=content, msg_type="assistant")
        self._messages.append(msg)
        return msg

    def add_tool_results(self, tool_result_blocks: list[dict]) -> Message:
        msg = Message(role="user", content=tool_result_blocks, msg_type="tool_result")
        self._messages.append(msg)
        return msg

    def add_assistant_placeholder(self, text: str = "I've loaded the requested content.") -> Message:
        msg = Message(
            role="assistant",
            content=[{"type": "text", "text": text}],
            msg_type="assistant",
        )
        self._messages.append(msg)
        return msg

    def inject_messages(self, new_messages: list[Message]) -> None:
        """Bulk-append Message objects (from ToolResult.new_messages, reminders, etc.)."""
        self._messages.extend(new_messages)

    def attach(self, msg: Message, attachments: list[Attachment]) -> None:
        msg.attachments.extend(attachments)
      

    def normalized_for_api(self) -> list[dict]:
        """Return a message list safe for the Anthropic API.

        Two-step process:
          1. Convert each Message to a plain dict, expanding attachments
             into the content field.
          2. Merge consecutive same-role messages (API requires alternation).

        The internal _messages list is NOT mutated.
        """
        if not self._messages:
            return []

        # Step 1: expand attachments
        raw: list[dict] = []
        for msg in self._messages:
            content = _expand_with_attachments(msg) if msg.attachments else msg.content
            raw.append({"role": msg.role, "content": content})

        # Step 2: merge consecutive same-role
        result: list[dict] = [raw[0]]
        for d in raw[1:]:
            prev = result[-1]
            if d["role"] == prev["role"]:
                prev_content = _normalize_content(prev["content"])
                cur_content = _normalize_content(d["content"])
                merged = _join_at_seam(prev_content, cur_content)
                result[-1] = {**prev, "content": merged}
            else:
                result.append(d)
        return result

    # -- memory dedup -------------------------------------------------------

    def collect_surfaced_memories(self) -> set[str]:
        """Rebuild surfaced_memories from message attachments.

        Used after loading a persisted history to reconstruct the set.
        """
        paths: set[str] = set()
        for msg in self._messages:
            for att in msg.attachments:
                if att.type == "relevant_memories":
                    paths.update(att.metadata.get("files", []))
        return paths

    # -- persistence --------------------------------------------------------

    def to_serializable(self) -> dict:
        return {
            "messages": [m.to_serializable() for m in self._messages],
            "surfaced_memories": list(self.surfaced_memories),
        }

    @classmethod
    def from_serializable(cls, data: dict) -> MessageHistory:
        h = cls()
        h._messages = [Message.from_serializable(m) for m in data.get("messages", [])]
        h.surfaced_memories = set(data.get("surfaced_memories", []))
        return h

    # -- lifecycle ----------------------------------------------------------

    def clear(self) -> None:
        """Reset conversation history (e.g. user /clear command)."""
        self._messages.clear()
        self.surfaced_memories.clear()


# ---------------------------------------------------------------------------
# ToolUseContext, ToolResult, AgentState
# ---------------------------------------------------------------------------

@dataclass
class ToolUseContext:
    """Execution environment passed to tool executors."""

    messages: list[Message]
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
    new_messages: list["Message"] = field(default_factory=list)
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
    id:str
    name: str
    input: dict

@dataclass
class ToolCallGroup:
    """Context for a tool call, including the call details and conversation state."""
    tool_call: list[ToolCall]
    type: str