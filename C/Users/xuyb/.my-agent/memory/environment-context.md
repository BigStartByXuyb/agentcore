---
name: environment context
description: User is on Windows but bash runs in WSL; file paths differ
type: feedback
---

The bash execution environment runs inside WSL (Linux subsystem), not native Windows.

- WSL home: /home/xuyb
- Windows home: C:\Users\xuyb

When writing files for the user to access on Windows, use the Windows path (C:/Users/xuyb/...) not /home/xuyb.
The user's my-agent Python app runs on Windows and reads Windows paths.

Always confirm file writes landed in the correct Windows-accessible location.
