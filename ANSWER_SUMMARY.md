# SKILL LISTING INJECTION ORDER - FINAL ANSWER

## The Question You Asked
"When the user sends a message, does the skill listing (system-reminder) go BEFORE or AFTER the user's actual text in the message array?"

## The Answer
**The skill listing goes AFTER (on a separate API call), but the system-reminder with user context goes BEFORE.**

Let me clarify this confusion:

---

## TWO DIFFERENT THINGS

### 1. SYSTEM-REMINDER (User Context Message) ✅ GOES BEFORE
This is injected in `prependUserContext()` at `api.ts:462-474`:
```
<system-reminder>
As you answer the user's questions, you can use the following context:
# currentDate
# gitStatus
# claudeMd
...
</system-reminder>
```
**Order in first API call:**
- [0] = System-reminder user message (isMeta: true)
- [1] = Original user message text
- [REST] = Prior conversation

### 2. SKILL LISTING ❌ DOES NOT GO IN FIRST API CALL
The skill listing (list of available /skills) is assembled LATER:
```
{
  type: 'attachment',
  attachment: {
    type: 'skill_listing',
    content: "The following skills are available: /update-config, /keybindings-help, /simplify, ...",
    skillCount: 42,
    isInitial: true
  }
}
```
**Order in second API call:**
- [0-3] = All messages from first call
- [4] = Skill listing attachment message
- [5+] = Other attachments

---

## WHEN DOES EACH ONE HAPPEN?

### System-Reminder (User Context) - FIRST API CALL
```
Location: src/query.ts:660 and src/utils/api.ts:462-474

for await (const message of deps.callModel({
  messages: prependUserContext(messagesForQuery, userContext),  ← SYSTEM-REMINDER ADDED HERE
  systemPrompt: fullSystemPrompt,
  ...
}))

Timing: IMMEDIATELY before API call
Status: BLOCKING - happens before model is called
```

### Skill Listing - SECOND API CALL (after tools)
```
Location: src/query.ts:1580-1590, src/utils/attachments.ts:2937-2970, line 875

for await (const attachment of getAttachmentMessages(...)) {
  yield attachment  ← SKILL LISTING YIELDED HERE
  toolResults.push(attachment)
}

Timing: AFTER assistant response and tool execution
Status: ASYNC - prefetched during model streaming
```

---

## VISUAL TIMELINE

```
┌─────────────────────────────────────────────────────────────────┐
│ API CALL #1                                                     │
├─────────────────────────────────────────────────────────────────┤
│ System Prompt:                                                  │
│ • Base instructions                                             │
│ • Git status (system context)                                   │
│                                                                 │
│ Messages:                                                       │
│ [0] ✅ <system-reminder> user context message                  │
│ [1] ✅ Original user text ("In D:\my_object\...")              │
│ [2] Assistant message (if continuation)                         │
│ [3] Tool results (if continuation)                              │
│                                                                 │
│ ❌ NO SKILL LISTING IN THIS CALL                               │
└─────────────────────────────────────────────────────────────────┘
         ↓ Model streams response ↓
         ↓ Tools execute ↓
         ↓ Skill discovery happens (async) ↓
┌─────────────────────────────────────────────────────────────────┐
│ API CALL #2                                                     │
├─────────────────────────────────────────────────────────────────┤
│ Messages:                                                       │
│ [0] <system-reminder> (carried forward)                         │
│ [1] Original user message (carried forward)                     │
│ [2] Assistant message (from call #1)                            │
│ [3] Tool results (if used)                                      │
│ [4] ✅ SKILL LISTING ATTACHMENT (type: 'attachment')           │
│ [5+] Other attachments                                          │
│                                                                 │
│ ✅ SKILL LISTING IS NOW PRESENT                                │
└─────────────────────────────────────────────────────────────────┘
```

---

## CODE LOCATIONS - QUICK REFERENCE

| What | Where | Lines | When |
|------|-------|-------|------|
| **System-reminder** | api.ts | 462-474 | prependUserContext() |
| **Prepended to** | query.ts | 660 | Before first API call |
| **Skill listing** | attachments.ts | 2661-2751 | getSkillListingAttachments() |
| **Called from** | attachments.ts | 875 | Within getAttachments() |
| **Yielded at** | query.ts | 1588 | After tools complete |
| **For API call** | query.ts | 659 | Second call (iteration) |

---

## THE INTERLEAVING

### Question: How does getAttachmentMessages() relate to user message construction?

**Answer:** They run at different times:

1. **User message construction** (line 660):
   ```typescript
   messages: prependUserContext(messagesForQuery, userContext)
   // Synchronous, happens immediately
   // System-reminder prepended to messages array
   // Sent to API in first call
   ```

2. **getAttachmentMessages** (line 1580):
   ```typescript
   for await (const attachment of getAttachmentMessages(...)) {
     yield attachment
     toolResults.push(attachment)
   }
   // Asynchronous, happens AFTER first API call completes
   // Skill listing collected and yielded
   // Available for second API call
   ```

### Why this order?

**Performance & Optimization:**
- ✅ User message doesn't wait for skill discovery
- ✅ Skills are prefetched while model responds
- ✅ Skills available for follow-up tool calls
- ✅ No blocking on expensive attachment collection

---

## WHICH IS INJECTED RELATIVE TO USER TEXT?

| Component | Relative Position | API Call | Code Location |
|-----------|------------------|----------|----------------|
| System-reminder | **BEFORE user text** | #1 | api.ts:462-474 |
| User text | ORIGINAL | #1 | messagesForQuery |
| Skill listing | **AFTER user text** | #2 | attachments.ts:2661 |

---

## FINAL SUMMARY

Your original question: "does the skill listing go BEFORE or AFTER the user's actual text?"

**BOTH, but at different times:**

1. **System-reminder (user context)** goes BEFORE user text in first API call
2. **Skill listing** goes AFTER user text, but in a separate API call

The **skill listing is NOT in the same message array as the user's text** - it arrives via attachment messages on the next API call iteration.

```
Timeline:
User sends → prependUserContext() adds context → First API call (NO skills)
                                                        ↓
                                                  Model responds
                                                        ↓
                                                  Tools execute
                                                        ↓
                                                  Skills discovered
                                                        ↓
                                                  Second API call (WITH skills)
```

---

## Documents Created

1. **SKILL_LISTING_INJECTION_ORDER.md** - Comprehensive 5-phase explanation
2. **SKILL_LISTING_COMPARISON.txt** - Quick reference table
3. **EXACT_LINE_NUMBERS.md** - Complete code flow with line-by-line references
4. **This file** - Final summary and answer

All files include exact line numbers from the open-claude-code repository.
