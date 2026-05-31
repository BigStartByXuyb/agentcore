# Claude Code TypeScript Architecture - Analysis for Python Implementation

## Executive Summary
Claude Code uses a modular tool-based architecture where agents process API responses containing tool calls, execute tools, and feed results back into the conversation loop. Below are the key architectural patterns to replicate in Python.

---

## 1. TOOL RESULT INTERFACE (Tool.ts)

### Key TypeScript Type: `ToolResult`
```typescript
interface ToolResult {
  type: string;           // Result type identifier
  content?: string;       // Main content/output
  output?: string;        // Alias for content
  error?: string;         // Error message if failed
  exitCode?: number;      // Process exit code
  state?: any;           // Optional state management
  [key: string]: any;    // Extensible for custom fields
}
```

### Tool Interface Structure
```typescript
interface Tool {
  name: string;
  description: string;
  schema: JSONSchema;    // Anthropic-style JSON schema
  execute(params: any): Promise<ToolResult>;
  mapResult?(result: ToolResult): string;  // Format result for Claude
}
```

### Python Equivalent Pattern
```python
from dataclasses import dataclass
from typing import Any, Optional, Dict

@dataclass
class ToolResult:
    type: str
    content: Optional[str] = None
    error: Optional[str] = None
    exit_code: Optional[int] = None
    state: Optional[Dict[str, Any]] = None
    # Allow arbitrary fields via __dict__

class Tool:
    name: str
    description: str
    schema: Dict[str, Any]  # JSON Schema
    
    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        pass
    
    def map_result(self, result: ToolResult) -> str:
        return result.content or ""
```

---

## 2. TOOL REGISTRATION & AGGREGATION (tools.ts)

### TypeScript Pattern
- **Registry Pattern**: Tools are imported individually and collected into a registry
- **Export Pattern**: All tools exported as a named export (e.g., `TOOLS: Tool[]`)
- **Dynamic Loading**: Tools can be conditionally loaded based on platform/environment

### Typical Structure
```typescript
import { BashTool } from './tools/BashTool';
import { FileReadTool } from './tools/FileReadTool';
import { GrepTool } from './tools/GrepTool';
import { SkillTool } from './tools/SkillTool';
import { AgentTool } from './tools/AgentTool';

export const TOOLS = [
  BashTool.instance,
  FileReadTool.instance,
  GrepTool.instance,
  SkillTool.instance,
  AgentTool.instance,
  // ... more tools
];
```

### Key Patterns
1. **Singleton Pattern**: Tools often have `.instance` static property
2. **Lazy Registration**: Not all tools loaded for all scenarios
3. **Tool Discovery**: Tools can self-register or be manually registered
4. **Namespace Isolation**: Tools don't interfere with each other

### Python Equivalent
```python
# tools/__init__.py
from .bash_tool import BashTool
from .file_read_tool import FileReadTool
from .grep_tool import GrepTool
from .skill_tool import SkillTool
from .agent_tool import AgentTool

TOOLS = [
    BashTool.instance,
    FileReadTool.instance,
    GrepTool.instance,
    SkillTool.instance,
    AgentTool.instance,
]

# Enable conditional loading
def get_tools(platform: str = "all") -> List[Tool]:
    if platform == "windows":
        return [tool for tool in TOOLS if not tool.unix_only]
    return TOOLS
```

---

## 3. INDIVIDUAL TOOL STRUCTURE (BashTool / FileReadTool / GrepTool)

### Typical Tool Components

#### A. Schema Definition
```typescript
const SCHEMA = {
  type: "object",
  properties: {
    command: {
      type: "string",
      description: "Bash command to execute"
    },
    timeout: {
      type: "number",
      description: "Timeout in milliseconds"
    },
    // ... more properties
  },
  required: ["command"]
};
```

#### B. Tool Class Structure
```typescript
class BashTool implements Tool {
  static instance = new BashTool();
  
  name = "bash";
  description = "Execute bash commands";
  schema = SCHEMA;
  
  async execute(params: {
    command: string;
    timeout?: number;
    // ... other params
  }): Promise<ToolResult> {
    try {
      const result = await this.runCommand(params.command);
      return {
        type: "success",
        content: result.stdout,
        exitCode: result.code
      };
    } catch (error) {
      return {
        type: "error",
        error: error.message
      };
    }
  }
  
  mapResult(result: ToolResult): string {
    // Format for Claude's consumption
    if (result.error) return `Error: ${result.error}`;
    return result.content || "";
  }
}
```

#### C. Key Implementation Details

**For BashTool:**
- Spawns child processes
- Handles stdout/stderr capture
- Manages timeouts
- Tracks exit codes
- Supports streaming output (line buffering)
- Handles process signals

**For FileReadTool:**
- File system access with permission checks
- Large file handling (head/tail parameters)
- Line number tracking
- Character encoding handling
- Error handling for non-existent files

**For GrepTool:**
- Regex pattern matching
- File type filtering (glob patterns)
- Multiline matching support
- Context lines (-B, -A, -C)
- Line number output option
- Head limit for large result sets

### Python Tool Skeleton
```python
from dataclasses import dataclass
from typing import Any, Dict, Optional

class MyTool(Tool):
    """Tool implementation"""
    
    instance: Optional['MyTool'] = None
    
    def __init__(self):
        self.name = "my_tool"
        self.description = "What this tool does"
        self.schema = {
            "type": "object",
            "properties": { ... },
            "required": [ ... ]
        }
    
    @classmethod
    def get_instance(cls) -> 'MyTool':
        if cls.instance is None:
            cls.instance = cls()
        return cls.instance
    
    async def execute(self, params: Dict[str, Any]) -> ToolResult:
        """Execute the tool with given parameters"""
        try:
            # Validate inputs
            self.validate_params(params)
            
            # Execute
            result = await self._do_work(params)
            
            # Return success result
            return ToolResult(
                type="success",
                content=result
            )
        except Exception as e:
            return ToolResult(
                type="error",
                error=str(e)
            )
    
    def map_result(self, result: ToolResult) -> str:
        """Convert ToolResult to string for Claude"""
        if result.error:
            return f"Error: {result.error}"
        return result.content or ""
```

---

## 4. AGENT LOOP & QUERY PROCESSING (query.ts)

### Message Flow Architecture

```
┌─────────────────────────────────────────────┐
│         Initial User Message                │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
        ┌─────────────────────┐
        │  Build Message Set  │
        │  (Include Context)  │
        └──────────┬──────────┘
                   │
                   ▼
   ┌──────────────────────────────────────┐
   │  Call Claude API with Tool Schemas   │
   │  (model, messages, tools, max_tokens)│
   └──────────────┬───────────────────────┘
                  │
        ┌─────────┴─────────┐
        │                   │
        ▼                   ▼
  ┌──────────┐      ┌─────────────────┐
  │ Text     │      │ Tool Use Block  │
  │ Response │      │ (stop_reason)   │
  └──────────┘      └────────┬────────┘
        │                    │
        │              ┌─────▼──────────┐
        │              │ Execute Tool   │
        │              │ (with params)  │
        │              └────────┬───────┘
        │                       │
        │              ┌────────▼──────────┐
        │              │ Get ToolResult    │
        │              │ map_result()      │
        │              └────────┬──────────┘
        │                       │
        │         ┌─────────────┴──────────┐
        │         │ Add to Messages:       │
        │         │ - assistant message    │
        │         │ - tool result message  │
        │         └───────────┬────────────┘
        │                     │
        └─────────────────────┘
                   │
                   ▼
        ┌─────────────────────┐
        │ stop_reason ==      │
        │ "end_turn"?         │
        └────────┬────────────┘
             Yes │  No
                 │   └──────────────────┐
                 │                      │
                 ▼                      ▼
            Return                   Loop back to
            Final Text              API Call
```

### TypeScript Query Loop Pseudocode
```typescript
async function query(
  systemPrompt: string,
  userMessage: string,
  tools: Tool[] = [],
  options: QueryOptions = {}
): Promise<string> {
  
  const messages: MessageParam[] = [
    { role: "user", content: userMessage }
  ];
  
  while (true) {
    // Build tool definitions for API
    const toolDefinitions = tools.map(tool => ({
      name: tool.name,
      description: tool.description,
      input_schema: tool.schema
    }));
    
    // Call Claude API
    const response = await client.messages.create({
      model: "claude-3-5-sonnet-20241022",
      max_tokens: options.maxTokens || 4096,
      system: systemPrompt,
      messages: messages,
      tools: toolDefinitions
    });
    
    // Process response
    if (response.stop_reason === "end_turn") {
      // Extract final text
      const finalText = response.content
        .filter(block => block.type === "text")
        .map(block => block.text)
        .join("\n");
      return finalText;
    }
    
    if (response.stop_reason === "tool_use") {
      // Add assistant message to history
      messages.push({
        role: "assistant",
        content: response.content
      });
      
      // Process each tool use block
      for (const block of response.content) {
        if (block.type === "tool_use") {
          // Find tool
          const tool = tools.find(t => t.name === block.name);
          if (!tool) {
            throw new Error(`Tool not found: ${block.name}`);
          }
          
          // Execute tool
          const result = await tool.execute(block.input);
          const resultText = tool.mapResult(result);
          
          // Add tool result to messages
          messages.push({
            role: "user",
            content: [
              {
                type: "tool_result",
                tool_use_id: block.id,
                content: resultText
              }
            ]
          });
        }
      }
    } else {
      // Unexpected stop reason
      throw new Error(`Unexpected stop reason: ${response.stop_reason}`);
    }
  }
}
```

### Key Message Structure Patterns

#### Message Types
```typescript
type MessageParam = 
  | { role: "user"; content: string | ContentBlockParam[] }
  | { role: "assistant"; content: ContentBlockParam[] };

type ContentBlockParam = 
  | { type: "text"; text: string }
  | { type: "tool_use"; id: string; name: string; input: any }
  | { type: "tool_result"; tool_use_id: string; content: string };
```

#### Critical Details
1. **Message History**: Complete history sent with each API call
2. **Tool Result Format**: Must match assistant's tool_use.id
3. **Stop Reason Handling**: 
   - `"end_turn"`: Agent is done, extract text
   - `"tool_use"`: Execute tools and loop
   - Other: Error handling
4. **Content Block Arrays**: Can mix text and tool_use in single message
5. **Tool Input Validation**: Schema validation before execution

### Python Equivalent
```python
from anthropic import Anthropic
from typing import List, Optional

async def query(
    system_prompt: str,
    user_message: str,
    tools: List[Tool] = None,
    options: Optional[Dict[str, Any]] = None
) -> str:
    
    client = Anthropic()
    options = options or {}
    messages = [{"role": "user", "content": user_message}]
    tools_list = tools or []
    
    while True:
        # Build tool definitions
        tool_definitions = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.schema
            }
            for tool in tools_list
        ]
        
        # Call API
        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=options.get("max_tokens", 4096),
            system=system_prompt,
            messages=messages,
            tools=tool_definitions if tool_definitions else None
        )
        
        # Handle response
        if response.stop_reason == "end_turn":
            return "\n".join(
                block.text for block in response.content 
                if hasattr(block, 'text')
            )
        
        if response.stop_reason == "tool_use":
            # Add assistant response
            messages.append({
                "role": "assistant",
                "content": response.content
            })
            
            # Process tool calls
            for block in response.content:
                if block.type == "tool_use":
                    tool = next((t for t in tools_list if t.name == block.name), None)
                    if not tool:
                        raise ValueError(f"Tool not found: {block.name}")
                    
                    result = await tool.execute(block.input)
                    result_text = tool.map_result(result)
                    
                    messages.append({
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": result_text
                            }
                        ]
                    })
        else:
            raise ValueError(f"Unexpected stop reason: {response.stop_reason}")
```

---

## 5. SPECIAL TOOLS: SkillTool & AgentTool

### SkillTool - Running Subprocesses

**Key Concept**: Execute Claude Code's "/skill" commands

#### TypeScript Structure
```typescript
class SkillTool implements Tool {
  name = "skill";
  description = "Execute a Claude Code skill/command";
  schema = {
    properties: {
      skill: { type: "string", description: "Skill name or command" },
      args: { type: "string", description: "Optional arguments" }
    }
  };
  
  async execute(params: { skill: string; args?: string }): Promise<ToolResult> {
    const mode = this.determineMode(params.skill);
    
    if (mode === "inline") {
      // Execute within same process context
      return await this.executeInline(params.skill, params.args);
    } else if (mode === "fork") {
      // Spawn new process
      return await this.executeFork(params.skill, params.args);
    }
  }
}
```

**Execution Modes:**
- **Inline**: Skills that augment current environment (e.g., config updates)
- **Fork**: Skills that need isolated environment (e.g., building projects)

**State Management**: 
- Skill execution can modify agent state
- Results propagate back to parent agent
- Supports nested skill calls

### AgentTool - Launching Subagents

**Key Concept**: Delegate work to child agents with independent contexts

#### TypeScript Structure
```typescript
class AgentTool implements Tool {
  name = "agent";
  description = "Launch a subagent with a specific task";
  schema = {
    properties: {
      task: { type: "string", description: "Task description" },
      context: { type: "object", description: "Context to pass" },
      tools: { type: "array", description: "Available tools" }
    }
  };
  
  async execute(params: {
    task: string;
    context?: any;
    tools?: string[];
  }): Promise<ToolResult> {
    // Create new agent instance
    const agent = new Agent({
      tools: this.getTools(params.tools),
      systemPrompt: `You are a specialized agent. Task: ${params.task}`
    });
    
    // Run agent with context
    const result = await agent.query(params.task);
    
    return {
      type: "success",
      content: result
    };
  }
}
```

**Key Patterns:**
- Tool filtering: Subagent gets subset of tools
- Context passing: Parent state available to child
- Sandboxing: Independent message history
- Result bubbling: Success/failure propagates up

---

## 6. DATA FLOW & INTEGRATION PATTERNS

### Complete Flow Diagram
```
User Input
    │
    ▼
System Prompt + Messages → Claude API
    │
    ├─► Text Output? → Return to User
    │
    └─► Tool Use? → Find Tool
         │
         ▼
    Execute Tool.execute()
         │
         ▼
    ToolResult object
         │
         ├─► map_result() → String
         │
         └─► Add to Message History
              │
              ▼
         Loop: Call API again
```

### Error Handling Patterns

```typescript
// Tool execution should never throw
async execute(params: any): Promise<ToolResult> {
  try {
    const result = await this.doWork(params);
    return { type: "success", content: result };
  } catch (error) {
    // Always return ToolResult, never throw
    return {
      type: "error",
      error: error.message,
      exitCode: -1
    };
  }
}

// Query loop handles API errors
try {
  const response = await client.messages.create(...);
} catch (error) {
  if (error.status === 429) {
    // Rate limit - retry with backoff
  } else if (error.status === 401) {
    // Auth error - fail immediately
  } else {
    // Other errors - retry or fail
  }
}
```

### Tool Parameter Validation

```typescript
private validateParams(params: any): void {
  // Schema validation before execution
  const ajv = new Ajv();
  const validate = ajv.compile(this.schema);
  
  if (!validate(params)) {
    throw new Error(`Invalid params: ${JSON.stringify(validate.errors)}`);
  }
}
```

### Python Equivalent
```python
from jsonschema import validate, ValidationError
from pydantic import BaseModel, ValidationError as PydanticError

async def execute(self, params: Dict[str, Any]) -> ToolResult:
    try:
        # Validate against schema
        validate(instance=params, schema=self.schema)
        
        # Execute
        result = await self._do_work(params)
        
        return ToolResult(type="success", content=result)
    except ValidationError as e:
        return ToolResult(type="error", error=f"Invalid params: {e.message}")
    except Exception as e:
        return ToolResult(type="error", error=str(e))
```

---

## 7. CONFIGURATION & SYSTEM PROMPT PATTERNS

### System Prompt Structure
```typescript
const SYSTEM_PROMPT = `You are Claude, an AI assistant built by Anthropic.

You have access to the following tools:
${tools.map(t => `- ${t.name}: ${t.description}`).join('\n')}

Instructions:
1. You can use tools to help accomplish tasks
2. Always explain your reasoning
3. Ask for clarification when needed
4. ...
`;
```

### Configuration Management
```typescript
interface QueryOptions {
  maxTokens?: number;
  temperature?: number;
  systemPrompt?: string;
  tools?: Tool[];
  timeout?: number;
  retryCount?: number;
}

// Defaults
const DEFAULT_OPTIONS: QueryOptions = {
  maxTokens: 4096,
  temperature: 1,
  tools: [],
  timeout: 300000, // 5 minutes
  retryCount: 3
};
```

---

## 8. KEY ARCHITECTURAL PRINCIPLES

### 1. **Stateless Tools**
- Tools are stateless; state managed by message history
- Each tool call is independent
- Results don't persist between calls

### 2. **Fail-Safe Execution**
- Tools never throw; always return ToolResult
- Errors returned in result, not as exceptions
- Query loop continues even on tool failures

### 3. **Message-Based State**
- Agent state is the message history
- No external state storage needed
- Complete context available for next decision

### 4. **Composable Architecture**
- Tools can be easily added/removed
- Agents can be nested (AgentTool)
- Skills can extend functionality (SkillTool)

### 5. **Schema-Driven Interface**
- Tool capabilities defined via JSON Schema
- Claude validates before calling
- Type-safe tool invocation

### 6. **Result Formatting**
- `mapResult()` converts ToolResult to human-readable string
- Allows tools to format complex results
- Claude sees formatted output, not raw ToolResult

### 7. **Loop-Based Processing**
- Single agent loop handles all message processing
- Deterministic: same input → same output
- Loops until Claude says it's done (end_turn)

---

## 9. PYTHON IMPLEMENTATION CHECKLIST

```python
# Core Classes
[ ] ToolResult dataclass with flexible fields
[ ] Tool abstract base class
[ ] QueryOptions/Config dataclass
[ ] Agent/Query executor class

# Tool Registry
[ ] Tool discovery/registration system
[ ] Singleton pattern for tool instances
[ ] Dynamic tool loading

# Individual Tools
[ ] BashTool (process execution)
[ ] FileReadTool (file operations)
[ ] GrepTool (pattern matching)
[ ] Optional: SkillTool, AgentTool

# API Integration
[ ] Anthropic SDK initialization
[ ] Message building
[ ] Response handling (tool_use, end_turn)
[ ] Error handling & retries

# Message Handling
[ ] Message history management
[ ] Tool result formatting
[ ] Content block array handling
[ ] Stop reason interpretation

# Testing
[ ] Unit tests per tool
[ ] Integration tests for agent loop
[ ] Mock API responses
[ ] Error scenarios
```

---

## 10. EXAMPLE INTEGRATION TEST FLOW

```python
async def test_agent_with_bash_tool():
    # Setup
    bash_tool = BashTool.get_instance()
    tools = [bash_tool]
    system_prompt = "You are a helpful assistant with bash access"
    
    # Execute
    result = await query(
        system_prompt=system_prompt,
        user_message="List files in /tmp",
        tools=tools,
        options={"max_tokens": 1024}
    )
    
    # Assert
    assert "tmp" in result or "No such file" in result
    # Result should contain either file listing or error message
```

---

## REFERENCES & PATTERNS TO REPLICATE

1. **Singleton Tool Pattern**: Reuse instance across queries
2. **Stateless Execution**: No persistent tool state
3. **Message History as State**: Complete context in messages
4. **Schema-Driven Design**: JSON Schema defines tool interface
5. **Fail-Safe Execution**: Errors never throw, always return Result
6. **Nested Agents**: Support agent composition via AgentTool
7. **Configurable System Prompt**: Allow customization per query
8. **Tool Result Mapping**: Format complex results for Claude
9. **Streaming Support**: (Optional) Line-buffered output for long-running tools
10. **Timeout Handling**: Graceful handling of tool timeouts

