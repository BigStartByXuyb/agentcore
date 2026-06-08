"""General-purpose agent — full tool access, catch-all for multi-step tasks.

No tool restrictions. Used when the task requires both reading and writing,
or doesn't fit a more specialized agent type.
"""

from src.agents import AgentDefinition

general_purpose_agent = AgentDefinition(
    name="general-purpose",
    description=(
        "General-purpose agent for researching complex questions, searching "
        "for code, and executing multi-step tasks. When you are searching for "
        "a keyword or file and are not confident that you will find the right "
        "match in the first few tries use this agent to perform the search "
        "for you."
    ),
    system_prompt=(
        "You are a general-purpose agent. Your job is to complete the task "
        "given to you using whatever tools are available.\n\n"
        "Guidelines:\n"
        "- Read and understand the relevant code before making changes\n"
        "- Use grep and glob to find related files and patterns\n"
        "- Make targeted, minimal changes to accomplish the task\n"
        "- Verify your changes work by running relevant tests if available\n"
        "- Do NOT spawn sub-agents; execute directly\n"
        "- When finished, provide a clear summary of what you did\n"
    ),
    max_turns=24,
    allowed_tools=None,
    disallowed_tools=["Agent"],
)
