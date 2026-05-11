"""Prompts for the auto-compact LLM summarization.

Corresponds to Claude Code's src/services/compact/prompt.ts.
"""

NO_TOOLS_PREAMBLE = """CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

- Do NOT use bash, read_file, grep, or ANY other tool.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and will waste your only turn.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.

"""

COMPACT_PROMPT = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.
This summary should be thorough in capturing technical details, code patterns, and architectural decisions that would be essential for continuing development work without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts. In your analysis:

1. Chronologically analyze each message. For each section identify:
   - The user's explicit requests and intents
   - Your approach to addressing the user's requests
   - Key decisions, technical concepts and code patterns
   - Specific details: file names, code snippets, function signatures, file edits
   - Errors encountered and how they were fixed
   - User feedback, especially corrections
   - Skills invoked (via the Skill tool) and their execution progress — which steps were completed, which are pending

Your summary should include:

1. Primary Request and Intent: Capture the user's explicit requests in detail
2. Key Technical Concepts: List important technical concepts, technologies, and frameworks
3. Files and Code Sections: Enumerate files examined, modified, or created with code snippets
4. Errors and Fixes: List errors encountered and how they were fixed
5. Problem Solving: Document problems solved and ongoing troubleshooting
6. All User Messages: List ALL non-tool-result user messages
7. Pending Tasks: Outline pending tasks explicitly asked to work on
8. Active Skills: For each skill invoked in this session, record: skill name, what steps were completed, what steps remain, and whether it is still in progress or finished. If no skills were used, write "None".
9. Current Work: Describe precisely what was being worked on immediately before this summary. If a skill is active, include which step of the skill you were on.
10. Optional Next Step: The next step related to the most recent work. Include direct quotes from the most recent conversation to ensure no drift in task interpretation.

<example>
<analysis>
[Your thought process]
</analysis>

<summary>
1. Primary Request and Intent:
   [Detailed description]

2. Key Technical Concepts:
   - [Concept 1]
   - [Concept 2]

3. Files and Code Sections:
   - [File Name 1]
      - [Summary and code snippets]

4. Errors and Fixes:
   - [Error]: [Fix]

5. Problem Solving:
   [Description]

6. All User Messages:
   - [Message 1]

7. Pending Tasks:
   - [Task 1]

8. Active Skills:
   - [Skill Name]: completed steps 1-3, step 4 in progress / finished
   or: None

9. Current Work:
   [Description, including active skill step if applicable]

10. Optional Next Step:
   [Next step with direct quotes from recent conversation]

</summary>
</example>

Please provide your summary following this structure.
"""

NO_TOOLS_TRAILER = (
    "\n\nREMINDER: Do NOT call any tools. Respond with plain text only — "
    "an <analysis> block followed by a <summary> block. "
    "Tool calls will be rejected."
)


def get_compact_prompt(custom_instructions: str | None = None) -> str:
    """Build the full compact prompt with optional custom instructions."""
    prompt = NO_TOOLS_PREAMBLE + COMPACT_PROMPT
    if custom_instructions and custom_instructions.strip():
        prompt += f"\n\nAdditional Instructions:\n{custom_instructions}"
    prompt += NO_TOOLS_TRAILER
    return prompt


def format_compact_summary(summary: str) -> str:
    """Strip the <analysis> scratchpad and format the <summary> section."""
    import re

    result = re.sub(r"<analysis>[\s\S]*?</analysis>", "", summary)

    match = re.search(r"<summary>([\s\S]*?)</summary>", result)
    if match:
        content = match.group(1).strip()
        result = re.sub(r"<summary>[\s\S]*?</summary>", f"Summary:\n{content}", result)

    result = re.sub(r"\n\n+", "\n\n", result)
    return result.strip()


def get_compact_user_summary(
    summary: str,
    *,
    suppress_follow_up: bool = False,
) -> str:
    """Build the user message that replaces the compacted conversation."""
    formatted = format_compact_summary(summary)

    text = (
        "This session is being continued from a previous conversation that ran out of context. "
        "The summary below covers the earlier portion of the conversation.\n\n"
        f"{formatted}"
    )

    if suppress_follow_up:
        text += (
            "\n\nContinue the conversation from where it left off without asking "
            "the user any further questions. Resume directly — do not acknowledge "
            "the summary, do not recap what was happening. Pick up the last task "
            "as if the break never happened."
        )

    return text
