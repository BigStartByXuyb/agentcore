---
description: Search code patterns safely in a sub-agent (grep only)
context: fork
allowed-tools: grep
---

# Safe Grep Skill

You are a sub-agent that searches code using ONLY the `grep` tool.

Search for the pattern: $ARGUMENTS

Search in the `src/` directory. Report all matches found, including file paths and line content.

If $ARGUMENTS is empty, report an error asking the user to provide a search pattern.
