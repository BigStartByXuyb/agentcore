# Exact Order of Skill Listing Injection vs User Message

## EXECUTIVE SUMMARY

The **skill listing is injected AFTER (downstream from) the user message** relative to the API call, but it's assembled **in the attachments pipeline BEFORE the API call is made**.

```
Timeline of a user message:
┌─────────────────────────────────────────────────────────────────┐
│ USER MESSAGE FLOW TO API                                        │
├─────────────────────────────────────────────────────────────────┤
│ 1. User input received                                          │
│ 2. getAttachments() called (collects skill_listing)             │
│ 3. prependUserContext() wraps messages with context reminder    │
│ 4. API call made with:                                          │
│    - System prompt (with system context appended)               │
│    - User context message (FIRST in messages array)             │
│    - Original user message(s) from messagesForQuery             │
│    - Then on NEXT API call iteration:                           │
│       - Attachment messages (including skill_listing)           │
└─────────────────────────────────────────────────────────────────┘
```

---

## DETAILED FLOW WITH LINE NUMBERS

### Phase 1: Initial Query Setup (query.ts:219-365)

```
query.ts:219-228
export async function* query(params: QueryParams): AsyncGenerator {
  const terminal = yield* queryLoop(params, consumedCommandUuids)
  ...
}

query.ts:241-307
async function* queryLoop(params: QueryParams, consumedCommandUuids: string[]): AsyncGenerator {
  let state: State = {
    messages: params.messages,  // User's original messages
    toolUseContext: params.toolUseContext,
    ...
  }
  
  while (true) {  // Main query loop
    // Destructure state at top of iteration
    const { messages, ... } = state
    let messagesForQuery = [...getMessagesAfterCompactBoundary(messages)]
```

**Key point**: `messagesForQuery` is the message history prepared for the API call.

---

### Phase 2: System Prompt Assembly (query.ts:449-451)

```typescript
// query.ts:449-451
const fullSystemPrompt = asSystemPrompt(
  appendSystemContext(systemPrompt, systemContext)
)
```

**What happens in appendSystemContext** (api.ts:437-447):

```typescript
// src/utils/api.ts:437-447
export function appendSystemContext(
  systemPrompt: SystemPrompt,
  context: { [k: string]: string },
): string[] {
  return [
    ...systemPrompt,                           // Base system prompt
    Object.entries(context)
      .map(([key, value]) => `${key}: ${value}`)
      .join('\n'),                             // Append git status, etc.
  ].filter(Boolean)
}
```

**Note**: System context is appended to system prompt, NOT skill listing.
Skill listing comes later in attachments.

---

### Phase 3: API Call with Messages (query.ts:659-708)

```typescript
// query.ts:659-708
for await (const message of deps.callModel({
  messages: prependUserContext(messagesForQuery, userContext),  // <-- KEY LINE
  systemPrompt: fullSystemPrompt,
  thinkingConfig: toolUseContext.options.thinkingConfig,
  tools: toolUseContext.options.tools,
  signal: toolUseContext.abortController.signal,
  options: { ... },
})) {
  // Process streaming response
  ...
}
```

**The `prependUserContext` function** (api.ts:449-474):

```typescript
// src/utils/api.ts:449-474
export function prependUserContext(
  messages: Message[],
  context: { [k: string]: string },
): Message[] {
  if (process.env.NODE_ENV === 'test') {
    return messages
  }
  
  if (Object.entries(context).length === 0) {
    return messages
  }
  
  return [
    createUserMessage({
      content: `<system-reminder>
As you answer the user's questions, you can use the following context:
${Object.entries(context)
  .map(([key, value]) => `# ${key}\n${value}`)
  .join('\n')}

IMPORTANT: this context may or may not be relevant to your tasks. 
You should not respond to this context unless it is highly relevant 
to your task.</system-reminder>
`,
      isMeta: true,
    }),
    ...messages,  // <-- Original user messages come AFTER the reminder
  ]
}
```

**CRUCIAL**: The `<system-reminder>` with user context goes FIRST in the message array,
then the original user messages follow. This is what's sent to the API on the first call.

---

### Phase 4: Attachment Collection (query.ts:1580-1590)

This happens AFTER the initial API call completes tool execution.

```typescript
// query.ts:1580-1590
for await (const attachment of getAttachmentMessages(
  null,                                    // No fresh user input after tools
  updatedToolUseContext,
  null,
  queuedCommandsSnapshot,
  [...messagesForQuery, ...assistantMessages, ...toolResults],  // Messages so far
  querySource,
)) {
  yield attachment                         // Yield attachment message
  toolResults.push(attachment)             // Add to message chain
}
```

**What getAttachmentMessages does** (attachments.ts:2937-2970):

```typescript
// src/utils/attachments.ts:2937-2970
export async function* getAttachmentMessages(
  input: string | null,
  toolUseContext: ToolUseContext,
  ideSelection: IDESelection | null,
  queuedCommands: QueuedCommand[],
  messages?: Message[],
  querySource?: QuerySource,
  options?: { skipSkillDiscovery?: boolean },
): AsyncGenerator<AttachmentMessage, void> {
  // TODO: Compute this upstream
  const attachments = await getAttachments(
    input,
    toolUseContext,
    ideSelection,
    queuedCommands,
    messages,
    querySource,
    options,
  )
  
  if (attachments.length === 0) {
    return
  }
  
  logEvent('tengu_attachments', {
    attachment_types: attachments.map(_ => _.type),
  })
  
  for (const attachment of attachments) {
    yield createAttachmentMessage(attachment)  // <-- Yields each attachment
  }
}
```

---

### Phase 5: Skill Listing Assembly (attachments.ts:743-875)

The skill listing is collected as part of `getAttachments()`:

```typescript
// src/utils/attachments.ts:743-942
export async function getAttachments(
  input: string | null,
  toolUseContext: ToolUseContext,
  ideSelection: IDESelection | null,
  queuedCommands: QueuedCommand[],
  messages?: Message[],
  querySource?: QuerySource,
  options?: { skipSkillDiscovery?: boolean },
): Promise<Attachment[]> {
  
  // ... user input attachments first (lines 773-815) ...
  
  // Thread-safe attachments available in sub-agents
  const allThreadAttachments = [
    // ... other attachments ...
    
    // Line 875: SKILL LISTING IS HERE
    maybe('skill_listing', () => getSkillListingAttachments(context)),
    
    // ... more attachments after ...
  ]
  
  // Process all attachments (Promise.all)
  const results = await Promise.all(allThreadAttachments)
  
  // ... filter and return ...
  return results.filter(Boolean).flat()
}
```

**The `getSkillListingAttachments` function** (attachments.ts:2661-2751):

```typescript
// src/utils/attachments.ts:2661-2751
async function getSkillListingAttachments(
  toolUseContext: ToolUseContext,
): Promise<Attachment[]> {
  if (process.env.NODE_ENV === 'test') {
    return []
  }
  
  // Skip if tool doesn't have Skill tool
  if (
    !toolUseContext.options.tools.some(t => toolMatchesName(t, SKILL_TOOL_NAME))
  ) {
    return []
  }
  
  const cwd = getProjectRoot()
  const localCommands = await getSkillToolCommands(cwd)
  const mcpSkills = getMcpSkillCommands(
    toolUseContext.getAppState().mcp.commands,
  )
  let allCommands =
    mcpSkills.length > 0
      ? uniqBy([...localCommands, ...mcpSkills], 'name')
      : localCommands
  
  // Handle skill search filtering...
  if (
    feature('EXPERIMENTAL_SKILL_SEARCH') &&
    skillSearchModules?.featureCheck.isSkillSearchEnabled()
  ) {
    allCommands = filterToBundledAndMcp(allCommands)
  }
  
  const agentKey = toolUseContext.agentId ?? ''
  let sent = sentSkillNames.get(agentKey)
  if (!sent) {
    sent = new Set()
    sentSkillNames.set(agentKey, sent)
  }
  
  // Resume path: suppress if already sent
  if (suppressNext) {
    suppressNext = false
    for (const cmd of allCommands) {
      sent.add(cmd.name)
    }
    return []
  }
  
  // Find skills we haven't sent yet
  const newSkills = allCommands.filter(cmd => !sent.has(cmd.name))
  
  if (newSkills.length === 0) {
    return []
  }
  
  // Mark as sent
  for (const cmd of newSkills) {
    sent.add(cmd.name)
  }
  
  logForDebugging(
    `Sending ${newSkills.length} skills via attachment 
     (${isInitial ? 'initial' : 'dynamic'}, ${sent.size} total sent)`,
  )
  
  const contextWindowTokens = getContextWindowForModel(
    toolUseContext.options.mainLoopModel,
    getSdkBetas(),
  )
  const content = formatCommandsWithinBudget(newSkills, contextWindowTokens)
  
  return [
    {
      type: 'skill_listing',
      content,
      skillCount: newSkills.length,
      isInitial,
    },
  ]  // <-- Returns as Attachment
}
```

---

## VISUAL TIMELINE OF MESSAGE ORDERING

### **What gets sent to Claude in the FIRST API call:**

```
┌──────────────────────────────────────────────────┐
│ MESSAGES ARRAY SENT TO API (First Call)          │
├──────────────────────────────────────────────────┤
│ [0] User Message (type: 'user', isMeta: true)    │
│     Content: <system-reminder>                   │
│     "As you answer the user's questions,        │
│      you can use the following context:         │
│      # currentDate                              │
│      # gitStatus                                │
│      # claudeMd                                 │
│      (etc)"                                     │
│                                                  │
│ [1] Original User Message (type: 'user')         │
│     Content: "In D:\my_object\open-claude-code, │
│               I need to determine the EXACT     │
│               order of skill listing injection" │
│                                                  │
│ [2] Assistant Message [if continuation]         │
│ [3] Tool Results [if continuation]              │
│ ...                                              │
└──────────────────────────────────────────────────┘

SYSTEM PROMPT: Base system prompt + appended system context
              (git status, etc.)
              
SKILL LISTING: NOT in this call - comes LATER
```

### **What gets sent in the NEXT API call (after tool execution):**

```
┌──────────────────────────────────────────────────┐
│ MESSAGES ARRAY SENT TO API (Next Call)           │
├──────────────────────────────────────────────────┤
│ [0-N] Previous messages (all from above)         │
│ ...                                              │
│ [N+1] Tool Results from previous turn           │
│                                                  │
│ [N+2] Attachment Message                        │
│       (type: 'attachment')                      │
│       attachment: {                             │
│         type: 'skill_listing',                  │
│         content: "The following skills are...", │
│         skillCount: 42,                         │
│         isInitial: true                         │
│       }                                          │
│                                                  │
│ [N+3] Other attachments (if any)               │
│       - file changes                            │
│       - nested memory                           │
│       - dynamic skills                          │
│       - etc.                                    │
└──────────────────────────────────────────────────┘
```

---

## KEY TIMING POINTS

| Line | File | Event | When |
|------|------|-------|------|
| 365 | query.ts | `messagesForQuery` prepared | Before API call |
| 449-451 | query.ts | `fullSystemPrompt` assembled | Before API call |
| 659 | query.ts | `prependUserContext()` called | Immediately before API call |
| 661 | query.ts | `messages` passed to API | API call made HERE |
| 708-824 | query.ts | Model streams response | During API response |
| 1580 | query.ts | `getAttachmentMessages()` called | AFTER assistant response |
| 2937-2970 | attachments.ts | Attachments yielded | AFTER assistant response |
| 2661-2751 | attachments.ts | Skill listing assembled | Within getAttachmentMessages |

---

## SUMMARY: BEFORE vs AFTER API CALL

### **BEFORE First API Call:**
- ❌ Skill listing is NOT ready yet
- ✅ System context is appended to system prompt
- ✅ User context message is prepended to message array
- ✅ Original user message follows the context message

### **DURING API Call:**
- Model streams response while skill listing is being collected

### **AFTER API Call (tool execution loop):**
- ✅ Skill listing is assembled from `getAttachmentMessages()`
- ✅ Attachment messages (including skill listing) are yielded
- ✅ These attachments become part of the next API call

---

## ARCHITECTURAL REASON

The skill listing comes AFTER the user message because:

1. **Skill listing can be expensive** - It needs to discover and format all available skills
2. **User input takes priority** - The immediate response to the user should not wait for skill discovery
3. **Prefetch optimization** - Skills are prefetched during model streaming (line 331-335) so they're ready when tools complete
4. **Async generator pattern** - `getAttachmentMessages()` is an async generator that yields attachments as they're collected

This design ensures:
- Fast response to user input
- Skills available for follow-up tool calls
- No blocking on skill discovery for the initial user message

---

## Code Flow Summary

```
User sends message
  ↓
query() → queryLoop()
  ↓
getMessagesAfterCompactBoundary(messages)  [line 365]
  ↓
appendSystemContext(systemPrompt, systemContext)  [lines 449-451]
  ↓
prependUserContext(messagesForQuery, userContext)  [line 660]
  │ Creates message array with:
  │ [0] = <system-reminder> user context message
  │ [1...N] = original user messages
  ↓
deps.callModel({ messages: [above], systemPrompt, ... })  [lines 659-708]
  │ API CALL #1 - NO SKILL LISTING YET
  ↓
Model streams response + tools execute
  ↓
getAttachmentMessages(null, ...)  [lines 1580-1590]
  ↓
getAttachments()  [line 2947]
  ↓
getSkillListingAttachments()  [line 2661]
  │ Returns: [{ type: 'skill_listing', content, skillCount, isInitial }]
  ↓
createAttachmentMessage(attachment)  [line 2968]
  ↓
yield attachment  [line 1588]
  │ SKILL LISTING IS NOW AVAILABLE
  ↓
API CALL #2 - WITH ATTACHMENT MESSAGES (including skill listing)
```
