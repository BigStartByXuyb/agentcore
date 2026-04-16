"""Explore agent — fast read-only codebase search specialist.

Corresponds to Claude Code's src/tools/AgentTool/built-in/exploreAgent.ts.

Restricted to read-only tools (bash, read_file, grep).  Cannot spawn
sub-agents or invoke skills.
"""

from src.agents import AgentDefinition

explore_agent = AgentDefinition(
    name="Explore",
    description=(
        "Fast agent specialized for exploring codebases. Use this when you "
        "need to quickly find files by patterns, search code for keywords, "
        "or answer questions about the codebase. This agent is read-only "
        "and cannot modify files."
    ),
    system_prompt=(
        "You are a code exploration agent. Your job is to search and read "
        "code to answer questions about the codebase.\n\n"
        "Guidelines:\n"
        "- Use grep to search for patterns across files\n"
        "- Use read_file to examine specific files\n"
        "- Use bash for ls, find, or other read-only commands\n"
        "- Be thorough but concise in your findings\n"
        "- Do NOT spawn sub-agents or invoke skills; execute directly\n"
        "- Do NOT modify any files\n"
        "- When finished, provide a clear summary of what you found\n"
    ),
    max_turns=12,
    allowed_tools=["bash", "read_file", "grep"],
)
