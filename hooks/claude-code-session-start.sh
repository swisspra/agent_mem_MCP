#!/bin/bash
# ─────────────────────────────────────────────────────────
# Claude Code Hook: SessionStart
# Injects agent memory context on session start
# Works with Claude Code's hook system (settings.json)
# ─────────────────────────────────────────────────────────

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
AGENT_MEM_DIR="$PROJECT_DIR/.agent-mem"

# Read stdin (Claude Code sends JSON)
INPUT=$(cat)

if [ ! -d "$AGENT_MEM_DIR" ]; then
    echo "⚠️ No .agent-mem/ found. Run memory_init to set up shared agent memory."
    exit 0
fi

# Build summary
python3 -c "
import json
from pathlib import Path

mem_dir = Path('$AGENT_MEM_DIR')
lines = ['🧠 AGENT SHARED MEMORY LOADED']

# Project
pf = mem_dir / 'project.json'
if pf.exists():
    with open(pf) as f:
        p = json.load(f)
    lines.append(f\"Project: {p.get('description','')} | Tech: {p.get('tech_stack','')}\")

# Agents
af = mem_dir / 'agents.json'
if af.exists():
    with open(af) as f:
        agents = json.load(f)
    lines.append(f'\\n👥 {len(agents)} previous agent(s):')
    for a in agents.values():
        e = {'active':'🟢','kia':'💀','completed':'✅','handed_off':'🤝'}.get(a.get('status',''),'❓')
        lines.append(f\"  {e} {a.get('agent_name','?')} ({a.get('agent_platform','?')}) — {a.get('status','?')}\")

# Last handoff
mf = mem_dir / 'memories.json'
if mf.exists():
    with open(mf) as f:
        entries = json.load(f).get('entries',[])
    handoffs = [e for e in entries if e.get('memory_type')=='handoff']
    if handoffs:
        h = handoffs[-1]
        lines.append(f\"\\n🤝 LAST HANDOFF from {h['agent_name']}:\")
        lines.append(h.get('content','')[:800])
    lines.append(f'\\n📚 {len(entries)} total memories')

lines.append('\\n⚡ PROTOCOL: memory_agent_join → memory_get_briefing → work → memory_write → memory_checkpoint → memory_handoff')
print('\\n'.join(lines))
" 2>/dev/null

exit 0
