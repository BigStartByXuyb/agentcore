"""AskUserQuestion tool — lets the LLM ask the user structured questions.

Corresponds to Claude Code's AskUserQuestionTool. The LLM passes
questions with options as tool input; the executor emits a
UserQuestionRequest event whose Future is resolved by the display
handler with the user's answers.

Unlike permission-based tools, check_permissions returns "allow" —
the tool's entire purpose is user interaction, handled inside the
executor via the event system.
"""

from __future__ import annotations

import asyncio

from src.types import ToolDef, ToolPermissionResult, ToolResult, ToolUseContext
from src.events import UserQuestionRequest


SCHEMA: dict = {
    "name": "AskUserQuestion",
    "description": (
        "Use this tool when you need to ask the user a question that has specific options "
        "or requires clarification. Present clear, distinct options for the user to choose "
        "from. Each question can have 2-4 options. The user can always provide a custom "
        "response via the 'Other' option (added automatically). "
        "Do NOT use this for simple yes/no confirmations — just ask in text."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "description": "Questions to ask the user (1-4 questions).",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "The question to ask.",
                        },
                        "header": {
                            "type": "string",
                            "description": "Short label for the question (max 12 chars).",
                        },
                        "options": {
                            "type": "array",
                            "description": "Available choices (2-4 options).",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {
                                        "type": "string",
                                        "description": "Display text (1-5 words).",
                                    },
                                    "description": {
                                        "type": "string",
                                        "description": "Explanation of this option.",
                                    },
                                },
                                "required": ["label", "description"],
                            },
                            "minItems": 2,
                            "maxItems": 4,
                        },
                        "multiSelect": {
                            "type": "boolean",
                            "default": False,
                            "description": "Allow multiple selections.",
                        },
                    },
                    "required": ["question", "header", "options", "multiSelect"],
                },
                "minItems": 1,
                "maxItems": 4,
            },
        },
        "required": ["questions"],
    },
}


def check_permissions(_inputs: dict, _context: ToolUseContext) -> ToolPermissionResult:
    return ToolPermissionResult(behavior="allow")


async def executor(inputs: dict, context: ToolUseContext) -> ToolResult:
    questions = inputs.get("questions", [])
    if not questions:
        return ToolResult(data={
            "type": "error",
            "content": "No questions provided.",
        })

    for i, q in enumerate(questions):
        if not q.get("question") or not q.get("options"):
            return ToolResult(data={
                "type": "error",
                "content": f"Question {i + 1} is missing required fields (question, options).",
            })

    future: asyncio.Future[dict] = asyncio.get_event_loop().create_future()
    context.on_event(UserQuestionRequest(
        label="main",
        questions=questions,
        future=future,
    ))

    answers = await future

    return ToolResult(data={
        "type": "answers",
        "answers": answers,
    })


def map_result(data: dict) -> str:
    if data.get("type") == "error":
        return data["content"]
    answers = data.get("answers", {})
    if not answers:
        return "No answers received."
    parts = []
    for question, answer in answers.items():
        parts.append(f"Q: {question}\nA: {answer}")
    return "\n\n".join(parts)


def is_read_only(_inputs: dict) -> bool:
    return True


tool = ToolDef(
    schema=SCHEMA,
    executor=executor,
    map_result=map_result,
    is_read_only=is_read_only,
    check_permissions=check_permissions,
)
