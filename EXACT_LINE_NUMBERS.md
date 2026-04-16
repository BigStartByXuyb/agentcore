# Code Flow: Exact Line Numbers for Skill Listing Injection

## Complete Execution Trace

### PHASE 1: User Input Received
```
User sends: "In D:\my_object\open-claude-code, I need to determine..."

ENTRY POINT:
  src/query.ts:219-228
  export async function* query(params: QueryParams): AsyncGenerator {
    const terminal = yield* queryLoop(params, consumedCommandUuids)
    ...
  }
```

### PHASE 2: Query Loop Initialization
```
src/query.ts:241-307
async function* queryLoop(params: QueryParams, consumedCommandUuids: string[]): AsyncGenerator {
  let state: State = {
    messages: params.messages,  // User's messages passed in
    toolUseContext: params.toolUseContext,
    maxOutputTokensOverride: params.maxOutputTokensOverride,
    autoCompactTracking: undefined,
    stopHookActive: undefined,
    maxOutputTokensRecoveryCount: 0,
    hasAttemptedReactiveCompact: false,
    turnCount: 1,
    pendingToolUseSummary: undefined,
    transition: undefined,
  }
  
  while (true) {
    // Main query loop begins here
```

### PHASE 3: Message Preparation
```
src/query.ts:365
let messagesForQuery = [...getMessagesAfterCompactBoundary(messages)]

Note: messagesForQuery now contains:
  [0] = Original user message(s)
  No skill listing yet
```

### PHASE 4: System Prompt Assembly
```
src/query.ts:449-451
const fullSystemPrompt = asSystemPrompt(
  appendSystemContext(systemPrompt, systemContext)
)

Where appendSystemContext (api.ts:437-447):
  export function appendSystemContext(
    systemPrompt: SystemPrompt,
    context: { [k: string]: string },
  ): string[] {
    return [
      ...systemPrompt,
      Object.entries(context)
        .map(([key, value]) => `${key}: ${value}`)
        .join('\n'),
    ].filter(Boolean)
  }

Result:
  System prompt with appended context (git status, etc.)
  NO skill listing in system prompt
```

### PHASE 5: Skill Prefetch Initiated
```
src/query.ts:331-335
const pendingSkillPrefetch = skillPrefetch?.startSkillDiscoveryPrefetch(
  null,
  messages,
  toolUseContext,
)

Note: This starts async skill discovery that runs during model streaming
```

### PHASE 6: Stream Request Start
```
src/query.ts:337
yield { type: 'stream_request_start' }

Note: Signal that API request is about to be made
```

### PHASE 7: FIRST API CALL
```
src/query.ts:659-708
for await (const message of deps.callModel({
  messages: prependUserContext(messagesForQuery, userContext),  // LINE 660
  systemPrompt: fullSystemPrompt,
  thinkingConfig: toolUseContext.options.thinkingConfig,
  tools: toolUseContext.options.tools,
  signal: toolUseContext.abortController.signal,
  options: {
    async getToolPermissionContext() {
      const appState = toolUseContext.getAppState()
      return appState.toolPermissionContext
    },
    model: currentModel,
    ...(config.gates.fastModeEnabled && {
      fastMode: appState.fastMode,
    }),
    toolChoice: undefined,
    isNonInteractiveSession:
      toolUseContext.options.isNonInteractiveSession,
    fallbackModel,
    onStreamingFallback: () => {
      streamingFallbackOccured = true
    },
    querySource,
    agents: toolUseContext.options.agentDefinitions.activeAgents,
    allowedAgentTypes:
      toolUseContext.options.agentDefinitions.allowedAgentTypes,
    hasAppendSystemPrompt:
      !!toolUseContext.options.appendSystemPrompt,
    maxOutputTokensOverride,
    fetchOverride: dumpPromptsFetch,
    mcpTools: appState.mcp.tools,
    hasPendingMcpServers: appState.mcp.clients.some(
      c => c.type === 'pending',
    ),
    queryTracking,
    effortValue: appState.effortValue,
    advisorModel: appState.advisorModel,
    skipCacheWrite,
    agentId: toolUseContext.agentId,
    addNotification: toolUseContext.addNotification,
    ...(params.taskBudget && {
      taskBudget: {
        total: params.taskBudget.total,
        ...(taskBudgetRemaining !== undefined && {
          remaining: taskBudgetRemaining,
        }),
      },
    }),
  },
})) {
  // Process streaming response
  // Messages property contains FIRST API CALL MESSAGE ARRAY
}

MESSAGES ARRAY IN FIRST API CALL:
  Result of prependUserContext(messagesForQuery, userContext) from api.ts:449-474

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
      ...messages,  // Original messages appended
    ]
  }

FINAL FIRST API CALL MESSAGE ARRAY:
  [0] = User message with <system-reminder> (isMeta: true)
  [1...N] = Original user messages
  [N+1...M] = Prior assistant messages (if any)
  [M+1...K] = Prior tool results (if any)
  
  NO SKILL LISTING YET
```

### PHASE 8: Model Streaming Response
```
src/query.ts:708-824
Processing messages from model streaming...
Collecting assistant messages
Detecting tool_use blocks
```

### PHASE 9: Tool Execution
```
src/query.ts:1366-1408
if (streamingToolExecutor) {
  // Tools execute here
} else {
  // Traditional tool execution
}

const toolUpdates = streamingToolExecutor
  ? streamingToolExecutor.getRemainingResults()
  : runTools(toolUseBlocks, assistantMessages, canUseTool, toolUseContext)

for await (const update of toolUpdates) {
  if (update.message) {
    yield update.message
    toolResults.push(...)
  }
  if (update.newContext) {
    updatedToolUseContext = {
      ...update.newContext,
      queryTracking,
    }
  }
}
```

### PHASE 10: ATTACHMENT COLLECTION - SKILL LISTING ASSEMBLED
```
src/query.ts:1580-1590
for await (const attachment of getAttachmentMessages(
  null,                                    // No fresh user input
  updatedToolUseContext,
  null,
  queuedCommandsSnapshot,
  [...messagesForQuery, ...assistantMessages, ...toolResults],
  querySource,
)) {
  yield attachment                         // Yield each attachment
  toolResults.push(attachment)             // Add to message chain
}

CALLS:
  src/utils/attachments.ts:2937-2970
  export async function* getAttachmentMessages(
    input: string | null,
    toolUseContext: ToolUseContext,
    ideSelection: IDESelection | null,
    queuedCommands: QueuedCommand[],
    messages?: Message[],
    querySource?: QuerySource,
    options?: { skipSkillDiscovery?: boolean },
  ): AsyncGenerator<AttachmentMessage, void> {
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
      yield createAttachmentMessage(attachment)  // YIELDS EACH
    }
  }
```

### PHASE 11: GET ATTACHMENTS DETAILS
```
src/utils/attachments.ts:743-942
export async function getAttachments(
  input: string | null,
  toolUseContext: ToolUseContext,
  ideSelection: IDESelection | null,
  queuedCommands: QueuedCommand[],
  messages?: Message[],
  querySource?: QuerySource,
  options?: { skipSkillDiscovery?: boolean },
): Promise<Attachment[]> {
  
  // User input attachments (lines 773-815)
  const userInputAttachments = input
    ? [
        maybe('at_mentioned_files', () =>
          processAtMentionedFiles(input, context),
        ),
        maybe('mcp_resources', () =>
          processMcpResourceAttachments(input, context),
        ),
        maybe('agent_mentions', () =>
          Promise.resolve(
            processAgentMentions(
              input,
              toolUseContext.options.agentDefinitions.activeAgents,
            ),
          ),
        ),
        ...(feature('EXPERIMENTAL_SKILL_SEARCH') &&
        skillSearchModules &&
        !options?.skipSkillDiscovery
          ? [
              maybe('skill_discovery', () =>
                skillSearchModules.prefetch.getTurnZeroSkillDiscovery(
                  input,
                  messages ?? [],
                  context,
                ),
              ),
            ]
          : []),
      ]
    : []

  const userAttachmentResults = await Promise.all(userInputAttachments)

  // All thread attachments (lines 824-941)
  const allThreadAttachments = [
    maybe('queued_commands', () => getQueuedCommandAttachments(queuedCommands)),
    maybe('date_change', () =>
      Promise.resolve(getDateChangeAttachments(messages)),
    ),
    maybe('ultrathink_effort', () =>
      Promise.resolve(getUltrathinkEffortAttachment(input)),
    ),
    maybe('deferred_tools_delta', () =>
      Promise.resolve(
        getDeferredToolsDeltaAttachment(
          toolUseContext.options.tools,
          toolUseContext.options.mainLoopModel,
          messages,
          {
            callSite: isMainThread
              ? 'attachments_main'
              : 'attachments_subagent',
            querySource,
          },
        ),
      ),
    ),
    maybe('agent_listing_delta', () =>
      Promise.resolve(getAgentListingDeltaAttachment(toolUseContext, messages)),
    ),
    maybe('mcp_instructions_delta', () =>
      Promise.resolve(
        getMcpInstructionsDeltaAttachment(
          toolUseContext.options.mcpClients,
          toolUseContext.options.tools,
          toolUseContext.options.mainLoopModel,
          messages,
        ),
      ),
    ),
    ...(feature('BUDDY')
      ? [
          maybe('companion_intro', () =>
            Promise.resolve(getCompanionIntroAttachment(messages)),
          ),
        ]
      : []),
    maybe('changed_files', () => getChangedFiles(context)),
    maybe('nested_memory', () => getNestedMemoryAttachments(context)),
    
    // LINE 875 - SKILL LISTING IS HERE
    maybe('skill_listing', () => getSkillListingAttachments(context)),
    
    // ... more attachments ...
  ]
  
  // Process all attachments in parallel
  const results = await Promise.all([
    ...userAttachmentResults,
    ...allThreadAttachments,
  ])
  
  // Return flattened and filtered results
  return results.filter(Boolean).flat()
}
```

### PHASE 12: GET SKILL LISTING ATTACHMENTS
```
src/utils/attachments.ts:2661-2751
async function getSkillListingAttachments(
  toolUseContext: ToolUseContext,
): Promise<Attachment[]> {
  
  // Skip in test
  if (process.env.NODE_ENV === 'test') {
    return []
  }
  
  // Skip if no Skill tool available
  if (
    !toolUseContext.options.tools.some(t => toolMatchesName(t, SKILL_TOOL_NAME))
  ) {
    return []
  }
  
  // Get project root and commands
  const cwd = getProjectRoot()
  const localCommands = await getSkillToolCommands(cwd)
  const mcpSkills = getMcpSkillCommands(
    toolUseContext.getAppState().mcp.commands,
  )
  let allCommands =
    mcpSkills.length > 0
      ? uniqBy([...localCommands, ...mcpSkills], 'name')
      : localCommands
  
  // Filter if skill search is enabled
  if (
    feature('EXPERIMENTAL_SKILL_SEARCH') &&
    skillSearchModules?.featureCheck.isSkillSearchEnabled()
  ) {
    allCommands = filterToBundledAndMcp(allCommands)
  }
  
  // Track sent skills per agent
  const agentKey = toolUseContext.agentId ?? ''
  let sent = sentSkillNames.get(agentKey)
  if (!sent) {
    sent = new Set()
    sentSkillNames.set(agentKey, sent)
  }
  
  // Handle resume suppression
  if (suppressNext) {
    suppressNext = false
    for (const cmd of allCommands) {
      sent.add(cmd.name)
    }
    return []
  }
  
  // Find new skills
  const newSkills = allCommands.filter(cmd => !sent.has(cmd.name))
  
  if (newSkills.length === 0) {
    return []
  }
  
  // Mark as sent
  for (const cmd of newSkills) {
    sent.add(cmd.name)
  }
  
  // Log and format
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
  ]  // RETURNS ATTACHMENT OBJECT HERE
}
```

### PHASE 13: CREATE ATTACHMENT MESSAGE
```
src/utils/attachments.ts:3201-3210
export function createAttachmentMessage(
  attachment: Attachment,
): AttachmentMessage {
  return {
    attachment,
    type: 'attachment',
    uuid: randomUUID(),
    timestamp: new Date().toISOString(),
  }
}

Returns:
  {
    attachment: {
      type: 'skill_listing',
      content: "The following skills are available: /update-config, ...",
      skillCount: 42,
      isInitial: true
    },
    type: 'attachment',
    uuid: '...',
    timestamp: '2026-04-13T...'
  }
```

### PHASE 14: YIELD ATTACHMENT MESSAGE
```
src/query.ts:1588
yield attachment

Result: Attachment message is now available for:
  1. Display in UI
  2. Inclusion in next API call

toolResults.push(attachment)
Result: Added to message chain for next iteration
```

### PHASE 15: SECOND API CALL (with skill listing)
```
The skill listing attachment is now part of toolResults

When next API call is made (same pattern as first call at line 659):
  messages: prependUserContext(
    [...messagesForQuery, ...assistantMessages, ...toolResults],
    userContext
  )

MESSAGES ARRAY IN SECOND API CALL:
  [0] = User context reminder (isMeta: true)
  [1] = Original user message
  [2] = Assistant message (Claude's response to first message)
  [3...N] = Tool results (if any tools were called)
  [N+1] = ATTACHMENT MESSAGE (type: 'attachment')
          WITH skill_listing inside
  [N+2...M] = Other attachments
  
  SKILL LISTING IS NOW PRESENT
```

---

## Summary of Line Numbers

| Event | File | Lines | Description |
|-------|------|-------|-------------|
| Query entry | query.ts | 219-228 | query() function |
| Loop init | query.ts | 241-307 | queryLoop() start |
| Message prep | query.ts | 365 | messagesForQuery created |
| System prompt | query.ts | 449-451 | fullSystemPrompt assembled |
| Skill prefetch start | query.ts | 331-335 | startSkillDiscoveryPrefetch |
| First API call | query.ts | 659-708 | deps.callModel() |
| prependUserContext | api.ts | 449-474 | Wraps messages with reminder |
| Tool execution | query.ts | 1366-1408 | runTools() |
| Attachment collection | query.ts | 1580-1590 | getAttachmentMessages() loop |
| getAttachmentMessages | attachments.ts | 2937-2970 | Async generator |
| getAttachments | attachments.ts | 743-942 | Collects all attachments |
| getSkillListingAttachments | attachments.ts | 2661-2751 | Skill discovery |
| Skill listing line | attachments.ts | 875 | maybe('skill_listing', ...) |
| createAttachmentMessage | attachments.ts | 3201-3210 | Wraps attachment |
| Yield attachment | query.ts | 1588 | yield attachment |
