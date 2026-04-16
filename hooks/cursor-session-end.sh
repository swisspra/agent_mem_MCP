#!/bin/bash
# ─────────────────────────────────────────────────────────
# Cursor Hook: stop (session end)
# Auto-saves emergency checkpoint when agent dies/stops
# ─────────────────────────────────────────────────────────

AGENT_MEM_DIR="${CURSOR_PROJECT_DIR:-.}/.agent-mem"

# Read stdin
INPUT=$(cat)
REASON=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('reason','unknown'))" 2>/dev/null || echo "unknown")

# Only act if .agent-mem exists
if [ ! -d "$AGENT_MEM_DIR" ]; then
    exit 0
fi

# Save emergency checkpoint
python3 -c "
import json, time
from datetime import datetime
from pathlib import Path

def local_now():
    return datetime.now().astimezone().isoformat()

mem_dir = Path('$AGENT_MEM_DIR')
reason = '$REASON'

# Find active agent
agents_file = mem_dir / 'agents.json'
if not agents_file.exists():
    exit()

with open(agents_file) as f:
    agents = json.load(f)

active_name = 'unknown'
for aid, a in agents.items():
    if a.get('status') == 'active':
        active_name = a.get('agent_name', 'unknown')
        # Mark as KIA if not graceful
        if reason not in ('completed', 'user_close'):
            a['status'] = 'kia'
            a['kia_at'] = local_now()
            a['kia_reason'] = f'session_end_{reason}'
        break

with open(agents_file, 'w') as f:
    json.dump(agents, f, indent=2)

# Add emergency checkpoint to memories
mem_file = mem_dir / 'memories.json'
memories = {'entries': []}
if mem_file.exists():
    with open(mem_file) as f:
        memories = json.load(f)

# Only add if there are existing memories (project is active)
if memories.get('entries'):
    emergency = {
        'id': f'emergency-{int(time.time())}',
        'agent_name': active_name,
        'memory_type': 'checkpoint',
        'title': f'⚡ Emergency checkpoint — session ended ({reason})',
        'content': json.dumps({
            'reason': reason,
            'note': 'Auto-saved by stop hook. Agent may not have had time to handoff.',
            'timestamp': local_now()
        }),
        'tags': ['emergency', 'auto-checkpoint'],
        'related_files': [],
        'priority': 2,
        'pinned': True,
        'created_at': local_now(),
        'timestamp': time.time()
    }
    memories['entries'].append(emergency)
    with open(mem_file, 'w') as f:
        json.dump(memories, f, indent=2)

print(f'Emergency checkpoint saved for {active_name} (reason: {reason})')
" 2>/dev/null

exit 0
