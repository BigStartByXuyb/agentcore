---
name: my-agent project
description: User's self-built Python AI Agent framework at D:\my_object\my-agent
type: project
---

Project path: D:\my_object\my-agent

A Python AI Agent framework modeled after Claude Code's architecture.

Key structure:
- src/main.py — CLI entry, REPL loop
- src/agent_loop.py — core LLM ↔ tool loop
- src/tools/ — 5 built-in tools: bash, read_file, grep, skill, agent
- src/skills/ — dynamic skill loading from skills/ dir (currently empty)
- src/mcp_tool/ — MCP client supporting http and pipeline protocols; reads ~/.my-agent/mcp.json or .mcp.json
- src/memory/ — persistent memory system (extract/recall/inject), uses haiku as side query model
- src/providers/ — LLM provider abstraction (Anthropic only so far)
- src/agents/ — sub-agents (explore agent exists)

Notable features:
- Extended Thinking support with budget_tokens control
- Concurrent tool execution via execute_tool_groups
- MCP code is implemented but no MCP server configured yet (no .mcp.json present)
- Skills directory is empty, so Skill tool has no available skills at runtime
