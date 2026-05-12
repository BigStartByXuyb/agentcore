"""Edit file tool — find-and-replace within an existing file (async)."""

from __future__ import annotations

import asyncio
import os

from src.types import ToolResult, ToolDef, ToolUseContext
from src.file_state_cache import FileState

# --- quote normalization (matches Claude Code FileEditTool/utils.ts) ---

_CURLY_QUOTE_MAP = str.maketrans({
    '‘': "'",   # left single curly  → straight
    '’': "'",   # right single curly → straight
    '“': '"',   # left double curly  → straight
    '”': '"',   # right double curly → straight
})


def _normalize_quotes(s: str) -> str:
    return s.translate(_CURLY_QUOTE_MAP)


def _find_actual_string(file_content: str, search: str) -> str | None:
    """Find *search* in *file_content*, tolerating curly/straight quote mismatch.

    Returns the actual substring from file_content (with original quotes intact),
    or None if not found even after normalization.
    """
    if search in file_content:
        return search

    norm_search = _normalize_quotes(search)
    norm_file = _normalize_quotes(file_content)
    pos = norm_file.find(norm_search)
    if pos != -1:
        return file_content[pos: pos + len(search)]

    return None


def _is_opening_context(chars: list[str], index: int) -> bool:
    if index == 0:
        return True
    prev = chars[index - 1]
    return prev in (' ', '\t', '\n', '\r', '(', '[', '{', '—', '–')


def _preserve_quote_style(old_string: str, actual_old: str, new_string: str) -> str:
    """If old_string matched via normalization, apply the file's curly quote style to new_string."""
    if old_string == actual_old:
        return new_string

    has_double = '“' in actual_old or '”' in actual_old
    has_single = '‘' in actual_old or '’' in actual_old

    if not has_double and not has_single:
        return new_string

    chars = list(new_string)
    result: list[str] = []
    for i, c in enumerate(chars):
        if c == '"' and has_double:
            result.append('“' if _is_opening_context(chars, i) else '”')
        elif c == "'" and has_single:
            prev_is_letter = i > 0 and chars[i - 1].isalpha()
            next_is_letter = i < len(chars) - 1 and chars[i + 1].isalpha()
            if prev_is_letter and next_is_letter:
                result.append('’')  # contraction: don't → don't
            else:
                result.append('‘' if _is_opening_context(chars, i) else '’')
        else:
            result.append(c)
    return ''.join(result)

SCHEMA: dict = {
    "name": "edit_file",
    "description": (
        "Make targeted edits to an existing file using string replacement. "
        "Provide the exact text to find (old_string) and what to replace it with (new_string). "
        "The old_string must match exactly — include enough surrounding context to be unique. "
        "Prefer this over write_file for modifying existing files."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "The absolute path to the file to edit.",
            },
            "old_string": {
                "type": "string",
                "description": "The exact text to find in the file. Must be unique unless replace_all is true.",
            },
            "new_string": {
                "type": "string",
                "description": "The text to replace old_string with. Must differ from old_string.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "If true, replace all occurrences. Default false.",
            },
        },
        "required": ["file_path", "old_string", "new_string"],
    },
}


def _read_file_sync(file_path: str) -> str:
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _write_file_sync(file_path: str, content: str) -> None:
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)


async def executor(inputs: dict, context: ToolUseContext) -> ToolResult:
    file_path: str = inputs["file_path"]
    old_string: str = inputs["old_string"]
    new_string: str = inputs["new_string"]
    replace_all: bool = inputs.get("replace_all", False)

    abs_path = os.path.normpath(os.path.abspath(file_path))
    cache = context.file_state_cache

    if old_string == new_string:
        return ToolResult(data={
            "type": "error",
            "content": "old_string and new_string are identical. No changes to make.",
        })

    if not os.path.isfile(abs_path):
        if old_string == "":
            try:
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                await asyncio.to_thread(_write_file_sync, abs_path, new_string)
            except Exception as e:
                return ToolResult(data={"type": "error", "content": f"Error creating file: {e}"})
            if cache is not None:
                try:
                    mtime = os.path.getmtime(abs_path)
                except OSError:
                    mtime = 0.0
                cache.set(abs_path, FileState(content=new_string, mtime=mtime, offset=None, limit=None))
            return ToolResult(data={
                "type": "success",
                "file_path": file_path,
                "status": "created",
            })
        return ToolResult(data={
            "type": "error",
            "content": f"File not found: {file_path}",
        })

    # --- stale check ---
    if cache is not None:
        cached = cache.get(abs_path)
        if not cached or cached.isPartialView:
            return ToolResult(data={
                "type": "error",
                "content": "File has not been read yet. Read it first before editing.",
            })
        try:
            disk_mtime = os.path.getmtime(abs_path)
            if disk_mtime > cached.mtime:
                is_full_read = cached.offset is None and cached.limit is None
                if is_full_read:
                    try:
                        disk_content = await asyncio.to_thread(_read_file_sync, abs_path)
                        if disk_content == cached.content:
                            pass  # content unchanged, safe to proceed
                        else:
                            return ToolResult(data={
                                "type": "error",
                                "content": "File has been externally modified since last read. Read it again before editing.",
                            })
                    except Exception:
                        return ToolResult(data={
                            "type": "error",
                            "content": "File has been externally modified since last read. Read it again before editing.",
                        })
                else:
                    return ToolResult(data={
                        "type": "error",
                        "content": "File has been externally modified since last read. Read it again before editing.",
                    })
        except OSError:
            pass

    # --- read current content ---
    try:
        original = await asyncio.to_thread(_read_file_sync, abs_path)
    except Exception as e:
        return ToolResult(data={"type": "error", "content": f"Error reading file: {e}"})

    # --- find matches (with quote normalization fallback) ---
    match_count = original.count(old_string)
    actual_old = old_string
    quote_normalized = False

    if match_count == 0:
        found = _find_actual_string(original, old_string)
        if found is None:
            return ToolResult(data={
                "type": "error",
                "content": f"old_string not found in file.\nString: {old_string}",
            })
        actual_old = found
        match_count = original.count(actual_old)
        new_string = _preserve_quote_style(old_string, actual_old, new_string)
        quote_normalized = True

    if match_count > 1 and not replace_all:
        return ToolResult(data={
            "type": "error",
            "content": (
                f"Found {match_count} matches of old_string, but replace_all is false. "
                "Provide more surrounding context to uniquely identify the instance, "
                "or set replace_all to true."
            ),
        })

    # --- apply replacement ---
    if replace_all:
        updated = original.replace(actual_old, new_string)
    else:
        updated = original.replace(actual_old, new_string, 1)

    # --- write back ---
    try:
        await asyncio.to_thread(_write_file_sync, abs_path, updated)
    except Exception as e:
        return ToolResult(data={"type": "error", "content": f"Error writing file: {e}"})

    # --- update cache ---
    if cache is not None:
        try:
            mtime = os.path.getmtime(abs_path)
        except OSError:
            mtime = 0.0
        cache.set(abs_path, FileState(content=updated, mtime=mtime, offset=None, limit=None))

    # --- build summary ---
    old_lines = old_string.count("\n") + 1
    new_lines = new_string.count("\n") + 1
    replacements = match_count if replace_all else 1

    return ToolResult(data={
        "type": "success",
        "file_path": file_path,
        "replacements": replacements,
        "old_lines": old_lines,
        "new_lines": new_lines,
        "quote_normalized": quote_normalized,
    })


def map_result(data: dict) -> str:
    if data.get("type") == "error":
        return data["content"]

    if data.get("status") == "created":
        return f"Created new file: {data['file_path']}"

    r = data["replacements"]
    msg = (
        f"Replaced {r} occurrence{'s' if r > 1 else ''} in {data['file_path']} "
        f"({data['old_lines']} lines → {data['new_lines']} lines)"
    )
    if data.get("quote_normalized"):
        msg += " [quote-normalized: curly/straight quotes were auto-converted to match file style]"
    return msg


def is_read_only(_inputs: dict) -> bool:
    return False


tool = ToolDef(
    schema=SCHEMA,
    executor=executor,
    map_result=map_result,
    is_read_only=is_read_only,
)
