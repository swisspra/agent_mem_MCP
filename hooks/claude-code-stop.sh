#!/bin/bash
# ─────────────────────────────────────────────────────────
# Claude Code Hook: Stop / SessionEnd
# Auto-save emergency checkpoint when agent stops
# ─────────────────────────────────────────────────────────

PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(pwd)}"
AGENT_MEM_DIR="$PROJECT_DIR/.agent-mem"

INPUT=$(cat)

if [ ! -d "$AGENT_MEM_DIR" ]; then
    exit 0
fi

python3 -c "
import json, time, sys
from datetime import datetime
from pathlib import Path

def local_now():
    return datetime.now().astimezone().isoformat()

mem_dir = Path('$AGENT_MEM_DIR')

try:
    input_data = json.loads('''$INPUT''')
except:
    input_data = {}

reason = input_data.get('reason', 'unknown')

agents_file = mem_dir / 'agents.json'
if not agents_file.exists():
    sys.exit(0)

with open(agents_file) as f:
    agents = json.load(f)

active_name = 'unknown'
for aid, a in agents.items():
    if a.get('status') == 'active':
        active_name = a.get('agent_name', 'unknown')
        a['status'] = 'kia'
        a['kia_at'] = local_now()
        a['kia_reason'] = f'session_stop_{reason}'
        break

with open(agents_file, 'w') as f:
    json.dump(agents, f, indent=2)

mem_file = mem_dir / 'memories.json'
if mem_file.exists():
    with open(mem_file) as f:
        memories = json.load(f)
    if memories.get('entries'):
        memories['entries'].append({
            'id': f'auto-{int(time.time())}',
            'agent_name': active_name,
            'memory_type': 'checkpoint',
            'title': f'⚡ Auto-checkpoint on stop ({reason})',
            'content': json.dumps({'reason': reason, 'auto': True}),
            'tags': ['emergency','auto'],
            'related_files': [], 'priority': 2, 'pinned': True,
            'created_at': local_now(),
            'timestamp': time.time()
        })
        with open(mem_file, 'w') as f:
            json.dump(memories, f, indent=2)
" 2>/dev/null

exit 0
