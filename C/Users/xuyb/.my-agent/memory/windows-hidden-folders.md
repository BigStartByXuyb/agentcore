---
name: Windows Hidden Dot Folders
description: User was confused that .my-agent folder wasn't visible in Explorer
type: feedback
---

On Windows, folders starting with "." (like .my-agent) are hidden by default in File Explorer.
User needs to enable "Show hidden items" in Explorer, or use PowerShell with -Force flag.
User initially suspected the shell was running in a container/sandbox — it is not, it writes to the real local filesystem.
