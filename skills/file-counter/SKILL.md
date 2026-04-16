---
description: Count files in current directory (runs in isolated sub-agent)
context: fork
---

# File Counter Skill

You are a sub-agent tasked with counting files.

Use the bash tool to run:
```
ls -la | tail -n +2 | wc -l
```

Report the total number of files and directories found.
