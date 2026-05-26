"""Core data structures: ToolResult, ToolUseContext, AgentState, MessageHistory, ToolDef, MemoryHeader."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Literal,
)

if TYPE_CHECKING:
    from src.events import AgentEvent

EventCallback = Callable[["AgentEvent"], None]


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
# ContentBlock — provider-agnostic content block types
# ---------------------------------------------------------------------------

@dataclass
class TextContent:
    text: str
    type: str = field(default="text", init=False)

@dataclass
class ToolUseContent:
    id: str
    name: str
    input: dict
    type: str = field(default="tool_use", init=False)

@dataclass
class ToolResultContent:
    tool_use_id: str
    content: str
    is_error: bool = False
    type: str = field(default="tool_result", init=False)

@dataclass
class ThinkingContent:
    thinking: str
    signature: str
    type: str = field(default="thinking", init=False)

@dataclass
class RedactedThinkingContent:
    data: str
    type: str = field(default="redacted_thinking", init=False)

ContentBlock = TextContent | ToolUseContent | ToolResultContent | ThinkingContent | RedactedThinkingContent


# ---------------------------------------------------------------------------
# Attachment — metadata attached to a Message
# ---------------------------------------------------------------------------

AttachmentType = Literal["relevant_memories", "system_reminder", "memory_index", "invoked_skills", "plan_mode"]


@dataclass
class Attachment:
    """Data attached to a Message, expanded into content before API calls.

    Attachments are NOT sent to the API directly — prepare_messages()
    expands them into the message's content field.
    """

    type: AttachmentType
    content: str
    """ Use by memery file """
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Message — rich wrapper around the raw API message dict
# ---------------------------------------------------------------------------

MessageType = Literal["human", "assistant", "tool_result", "meta"]


@dataclass
class Message:
    """A single conversation message with metadata and attachments.

    The msg_type field distinguishes messages that share the same API
    role ("user") but have different semantic meanings:
      - "human":       actual user input
      - "tool_result": tool execution results (API role is "user")
      - "meta":        system-injected content (skill listings, etc.)
      - "assistant":   LLM responses
    """

    role: str                                   # "user" | "assistant"
    content: str | list[ContentBlock]
    msg_type: MessageType = "human"
    attachments: list[Attachment] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)

    def attach(self, attachments: list[Attachment]) -> None:
        self.attachments.extend(attachments)

    # -- serialization for persistence --------------------------------------

    def to_serializable(self) -> dict:
        if isinstance(self.content, str):
            serialized_content = self.content
        else:
            serialized_content = [_content_block_to_dict(b) for b in self.content]
        return {
            "role": self.role,
            "content": serialized_content,
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
        raw_content = data["content"]
        if isinstance(raw_content, str):
            content: str | list[ContentBlock] = raw_content
        else:
            content = [_dict_to_content_block(d) for d in raw_content]
        return cls(
            role=data["role"],
            content=content,
            msg_type=data.get("msg_type", "human"),
            attachments=attachments,
            timestamp=data.get("timestamp", 0),
        )


# ---------------------------------------------------------------------------
# ContentBlock ↔ dict conversion helpers
# ---------------------------------------------------------------------------

def _content_block_to_dict(block: ContentBlock) -> dict:
    """Convert a ContentBlock to a plain dict for API / serialization."""
    if isinstance(block, TextContent):
        return {"type": "text", "text": block.text}
    if isinstance(block, ToolUseContent):
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if isinstance(block, ToolResultContent):
        d: dict = {"type": "tool_result", "tool_use_id": block.tool_use_id, "content": block.content}
        if block.is_error:
            d["is_error"] = True
        return d
    if isinstance(block, ThinkingContent):
        return {"type": "thinking", "thinking": block.thinking, "signature": block.signature}
    if isinstance(block, RedactedThinkingContent):
        return {"type": "redacted_thinking", "data": block.data}
    raise TypeError(f"Unknown content block type: {type(block)}")


def _dict_to_content_block(d: dict) -> ContentBlock:
    """Convert a plain dict to a ContentBlock (for deserialization)."""
    t = d.get("type")
    if t == "text":
        return TextContent(text=d["text"])
    if t == "tool_use":
        return ToolUseContent(id=d["id"], name=d["name"], input=d["input"])
    if t == "tool_result":
        return ToolResultContent(
            tool_use_id=d["tool_use_id"], content=d.get("content", ""),
            is_error=d.get("is_error", False),
        )
    if t == "thinking":
        return ThinkingContent(thinking=d["thinking"], signature=d["signature"])
    if t == "redacted_thinking":
        return RedactedThinkingContent(data=d["data"])
    raise ValueError(f"Unknown content block type: {t}")


# ---------------------------------------------------------------------------
# MessageHistory — conversation state manager
# ---------------------------------------------------------------------------

class MessageHistory:
    """Manages the persistent conversation message list.

    Internally stores a list[Message] with rich metadata and attachments.
    prepare_messages() expands attachments and merges consecutive same-role
    messages, returning list[Message] for provider adapters to convert.

    Corresponds to Claude Code's internal Message[] + normalizeMessagesForAPI().
    """

    def __init__(self, messages: list[Message] | None = None) -> None:
        self._messages: list[Message] = messages if messages is not None else []

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

    def add_user(self, content: str | list[ContentBlock], *, msg_type: MessageType = "human") -> Message:
        msg = Message(role="user", content=content, msg_type=msg_type)
        self._messages.append(msg)
        return msg

    def add_assistant(self, content: list[ContentBlock]) -> Message:
        msg = Message(role="assistant", content=content, msg_type="assistant")
        self._messages.append(msg)
        return msg

    def add_tool_results(self, blocks: list[ToolResultContent]) -> Message:
        content: list[ContentBlock] = list(blocks)
        msg = Message(role="user", content=content, msg_type="tool_result")
        self._messages.append(msg)
        return msg

    def add_assistant_placeholder(self, text: str = "I've loaded the requested content.") -> Message:
        msg = Message(
            role="assistant",
            content=[TextContent(text=text)],
            msg_type="assistant",
        )
        self._messages.append(msg)
        return msg

    def inject_messages(self, new_messages: list[Message]) -> None:
        self._messages.extend(new_messages)

    def prepare_messages(self) -> list[Message]:
        """Expand attachments + merge consecutive same-role messages.

        Returns a NEW list[Message] ready for per-adapter format conversion.
        No ContentBlock → dict conversion is done here — that's the adapter's job.
        The internal _messages list is NOT mutated.
        """
        if not self._messages:
            return []

        expanded: list[Message] = []
        for msg in self._messages:
            content = msg.content
            if msg.attachments:
                content = _expand_attachments_to_content(content, msg.attachments)
            expanded.append(Message(
                role=msg.role,
                content=content,
                msg_type=msg.msg_type,
                timestamp=msg.timestamp,
            ))

        result: list[Message] = [expanded[0]]
        for m in expanded[1:]:
            prev = result[-1]
            if m.role == prev.role:
                prev_blocks = _normalize_content_blocks(prev.content)
                cur_blocks = _normalize_content_blocks(m.content)
                merged = _join_content_blocks(prev_blocks, cur_blocks)
                result[-1] = Message(
                    role=prev.role,
                    content=merged,
                    msg_type=prev.msg_type,
                    timestamp=prev.timestamp,
                )
            else:
                result.append(m)
        return result

    # -- persistence --------------------------------------------------------

    def to_serializable(self) -> dict:
        return {
            "messages": [m.to_serializable() for m in self._messages],
        }

    @classmethod
    def from_serializable(cls, data: dict) -> MessageHistory:
        h = cls()
        h._messages = [Message.from_serializable(m) for m in data.get("messages", [])]
        return h

    # -- lifecycle ----------------------------------------------------------

    def replace_with_summary(self, summary_text: str) -> None:
        """Replace all messages with a single summary (auto-compact Layer 2)."""
        self._messages.clear()
        self._messages.append(Message(
            role="user", content=summary_text, msg_type="human",
        ))

    def clear(self) -> None:
        """Reset conversation history (e.g. user /clear command)."""
        self._messages.clear()


# ---------------------------------------------------------------------------
# ToolUseContext, ToolResult, AgentState
# ---------------------------------------------------------------------------

@dataclass
class ToolUseContext:
    """Execution environment passed to tool executors."""

    messages: MessageHistory
    tools: list[str]
    depth: int = 0
    abort_signal: bool = False
    tool_overrides: dict | None = None  # Optional: {name: ToolDef} overrides for registry lookup
    permissions: Any | None = None      # PermissionEngine instance (avoid circular import)
    on_event: EventCallback = field(default=lambda ev: None)
    # --- session-level caches (cleared on /clear, consumed by compaction) ---
    file_state_cache: Any | None = None                                # FileStateCache instance
    invoked_skills: dict[str, "InvokedSkillInfo"] = field(default_factory=dict)  # inline skills active this session
    # --- task store (LLM self-managed todo list) ---
    task_store: Any | None = None  # TaskStore instance (avoid circular import)
    # --- denial tracking (prevents infinite permission-deny loops) ---
    denial_tracker: Any | None = None  # DenialTracker instance (avoid circular import)
    # --- agent state back-reference ---
    agent_state: "AgentState | None" = None # back-reference so tools (e.g. ExitPlanMode) can read/mutate state


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
# All tool executors are `async def executor(inputs, ctx) -> ToolResult`.
# Tools that need to emit events (Skill fork, Agent subagent) use
# ctx.on_event callback instead of yielding.
# ---------------------------------------------------------------------------

ToolExecutorReturn = Awaitable[ToolResult]


@dataclass
class ToolPermissionResult:
    """Unified permission decision — used by both tool-level and engine-level checks.

    Behaviors:
      - allow:       proceed to executor
      - ask:         needs user confirmation (with optional custom_prompt)
      - deny:        reject this invocation
      - passthrough: defer to PermissionEngine rules (default for most tools)

    ask_mode controls the interactive behavior when behavior='ask':
      - standard:  show preview + [y/n/always], "always" adds session rule
      - review:    show custom_prompt + [y/n], collect feedback on rejection,
                   no "always" option (for one-off approvals like plan review)
    """
    behavior: Literal["allow", "deny", "ask", "passthrough"]
    ask_mode: Literal["standard", "review"] = "standard"
    custom_prompt: str | None = None
    deny_message: str = ""
    engine_content: str | None = None


@dataclass
class InvokedSkillInfo:
    """Tracks an inline skill invoked during this session.

    Stored on AgentState so the skill content can be re-injected after compaction.
    Only inline skills are tracked — fork skills run to completion and don't need
    re-injection.
    """
    skill_name: str
    skill_path: str
    content: str
    invoked_at: float = field(default_factory=time.time)


TaskStatus = Literal["pending", "in_progress", "completed"]


@dataclass
class TaskItem:
    """A single task in the LLM's self-managed todo list."""
    id: int
    content: str
    status: TaskStatus = "pending"
    active_form: str = ""


class PlanPhase(str, Enum):
    """Plan mode lifecycle — single state variable, no illegal combinations.

    INACTIVE → ACTIVE → EXITING → INACTIVE
                 ↑          ↓ (rejected via permission system)
                 └──────────┘

    Approval is handled mid-turn by the permission system (check_permissions
    on ExitPlanMode returns "ask"). On rejection, the tool never executes and
    the LLM receives an error tool_result with user feedback.
    """
    INACTIVE = "inactive"
    ACTIVE = "active"
    EXITING = "exiting"


@dataclass
class AgentState:
    """Statistics maintained by the agent loop (not exposed to tools)."""

    agent_id: str = "main"
    subagent_count: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_thinking_tokens: int = 0
    last_usage_tokens: int = 0
    messages_since_last_usage: int = 0
    plan_phase: PlanPhase = PlanPhase.INACTIVE
    plan_file_path: str | None = None
    turns_since_task_write: int = 0
    turns_since_task_reminder: int = 0
    compact_consecutive_failures: int = 0
    _task_store: Any | None = field(default=None, repr=False)


class ToolDef:
    """Tool definition — each tool constructs one instance and exports it.

    Required: schema, executor, map_result.
    Optional fields have safe defaults matching Claude Code's buildTool():
      - is_enabled:           default True
      - is_read_only:         default False  (fail-closed)
      - is_destructive:       default False
      - max_result_size_chars: default 30_000
    """

    schema: dict
    executor: Callable[[dict, ToolUseContext], ToolExecutorReturn]
    map_result: Callable[[Any], str]
    is_enabled: Callable[[], bool]
    is_read_only: Callable[[dict], bool]
    is_destructive: Callable[[dict], bool]
    check_permissions: Callable[[dict, ToolUseContext], "ToolPermissionResult"]
    max_result_size_chars: int

    def __init__(
        self,
        *,
        schema: dict,
        executor: Callable[[dict, ToolUseContext], ToolExecutorReturn],
        map_result: Callable[[Any], str],
        display_result: Callable[[Any], str] | None = None,
        build_preview: Callable[[dict], str] | None = None,
        is_enabled: Callable[[], bool] | None = None,
        is_read_only: Callable[[dict], bool] | None = None,
        is_destructive: Callable[[dict], bool] | None = None,
        check_permissions: Callable[[dict, ToolUseContext], "ToolPermissionResult"] | None = None,
        max_result_size_chars: int = 30_000,
    ) -> None:
        self.schema = schema
        self.executor = executor
        self.map_result = map_result
        self.display_result = display_result
        self.build_preview = build_preview
        self.is_enabled = is_enabled or (lambda: True)
        self.is_read_only = is_read_only or (lambda _: False)
        self.is_destructive = is_destructive or (lambda _: False)
        self.check_permissions = check_permissions or (
            lambda _inputs, _ctx: ToolPermissionResult(behavior="passthrough")
        )
        self.max_result_size_chars = max_result_size_chars

    @property
    def name(self) -> str:
        return self.schema["name"]




# ---------------------------------------------------------------------------
# Module-level helpers for MessageHistory.normalized_for_api()
# ---------------------------------------------------------------------------
# ContentBlock-level helpers for MessageHistory.prepare_messages()
# ---------------------------------------------------------------------------

def _normalize_content_blocks(content: str | list[ContentBlock]) -> list[ContentBlock]:
    """Ensure content is always a list of ContentBlock objects."""
    if isinstance(content, str):
        return [TextContent(text=content)]
    return list(content)


def _expand_attachments_to_content(
    content: str | list[ContentBlock],
    attachments: list[Attachment],
) -> list[ContentBlock]:
    """Expand attachments into ContentBlock list (no dict conversion)."""
    blocks = _normalize_content_blocks(content)
    for att in attachments:
        blocks.append(TextContent(text=att.content))
    return blocks


def _join_content_blocks(a: list[ContentBlock], b: list[ContentBlock]) -> list[ContentBlock]:
    """Concatenate two ContentBlock arrays, adding '\\n' at text-text seams."""
    if not a:
        return b
    if not b:
        return a

    last_a = a[-1]
    first_b = b[0]
    if isinstance(last_a, TextContent) and isinstance(first_b, TextContent):
        patched_last = TextContent(text=last_a.text + "\n")
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