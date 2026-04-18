---
name: agent-memory
description: "PROACTIVELY use this skill on EVERY session. Shared memory system for multi-agent projects. Call memory_get_briefing FIRST, then memory_agent_join. Write memories after EVERY action. Checkpoint every 10-15 min. Handoff before leaving."
disable-model-invocation: false
---

# Agent Shared Memory — Mandatory Protocol

You have access to an **Agent Shared Memory MCP** server on this project.
Other agents (Claude, Cursor, Codex, AntiGravity) have worked here before you.
Their context, decisions, and warnings are stored in `.agent-mem/`.

## ⛔ MANDATORY — Do these IN ORDER, do NOT skip

### Step 1: Read context (BEFORE doing anything)
```
memory_get_briefing()
```
This gives you: project overview, last handoff, decisions, warnings, open tickets, agent history.

### Step 2: Register yourself
```
memory_agent_join(
  agent_name="<your-stable-name>",   ← SAME name every session, no dates/model names
  agent_platform="<platform>",        ← claude|cursor|codex|antigravity|claude-code
  agent_role="<role>",                 ← main|reviewer|utility|planner
  task_focus="<what you'll do>"
)
```
⛔ You CANNOT write anything until you join. The MCP will block you.

### Step 3: Check tickets
```
memory_list_tickets()
```
Other agents may have created tickets assigned to you. Claim with `memory_claim_ticket`.

### Step 4: Work — write after EVERY action
After every code change, decision, or discovery:
```
memory_write(
  agent_name="<same name>",
  memory_type="progress",     ← or: decision, blocker, warning, discovery, file_change, todo
  title="Short summary",
  content="Detailed description with file names and reasoning",
  priority=0                  ← 0=normal, 1=important, 2=high, 3=critical (auto-pins)
)
```
**Do NOT batch writes.** Write immediately after each action. If you die, this is all the next agent has.

### Step 5: Checkpoint every 10-15 minutes
```
memory_checkpoint(
  agent_name="<same name>",
  summary="Current state...",
  remaining_tasks=["task1", "task2"],
  blockers=["blocker1"]
)
```

### Step 6: Before finishing — handoff
```
memory_handoff(
  agent_name="<same name>",
  summary="What I accomplished...",
  next_steps=["step1", "step2"],
  warnings=["gotcha1"],
  files_modified=["file1.ts"],
  files_created=["file2.ts"]
)
```

## Ticketing — request help from other agents
```
memory_create_ticket(title="...", description="...", assigned_to="cursor", priority="high")
```
When you finish a ticket: `memory_submit_ticket` → auto-handoff.

## Token management
When memories grow large:
```
memory_prepare_compaction()     ← shows cold entries for you to summarize
memory_write(type="context", title="Digest: ...", content="<your summary>")
memory_compact(use_llm=False)   ← archives old entries
```

## Rules
- **Your name is stamped on everything** — you are accountable
- **Do NOT edit code without user confirmation first**
- **Do NOT skip memory_write** — if you die, context is lost forever
- Previous agents may have hallucinated — verify decisions with code, not just memory
