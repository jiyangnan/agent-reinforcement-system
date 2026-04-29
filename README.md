# agent-reinforcement-system

A three-part agent runtime reinforcement project:

1. **First-Principles Runtime** — forces the agent to reason from axioms instead of habits.
2. **HA Episodic Memory** — a high-availability local memory stack with automatic failover across Neo4j, SQLite FTS, and raw session/files.
3. **Autonomous-Loop** — a bounded execution loop that lets the agent keep pushing a goal until it is done, blocked, waiting for human input, or aborted.

This repository extracts the first two reinforcement modules from a production OpenClaw assistant setup and packages them so other agents can reproduce the same capabilities.

---

## What this project gives you

### Module 1 — First-Principles Runtime
Turns “first-principles-only” from a vague prompt idea into a runtime discipline.

**Effects**
- reduces hand-wavy answers
- forces explicit assumptions
- improves debugging and root-cause analysis
- makes output more explainable and auditable

### Module 2 — High-Availability Episodic Memory
Builds a local memory layer that does **not collapse when one backend fails**.

### Module 3 — Autonomous-Loop
Builds the execution reinforcement layer:
- Observe
- Orient
- Decide
- Act
- Verify
- Record
- Loop / Exit

This is what turns the agent from a smart responder into a bounded autonomous worker.

Core runtime artifacts:
- `docs/module-3-autonomous-loop.md`
- `schemas/goal_frame.schema.json`
- `schemas/loop_state.schema.json`
- `src/autonomous_loop.py`

**Recall order**
1. Neo4j episode graph
2. SQLite FTS
3. raw memory/session grep

**Effects**
- avoids single-provider amnesia
- enables durable recall of prior sessions, decisions, and upgrades
- keeps memory usable even when embeddings or graph services fail

---

## Repo structure

```text
agent-reinforcement-system/
├── README.md
├── requirements.txt
├── docs/
│   ├── architecture.md
│   ├── module-1-first-principles.md
│   ├── module-2-ha-episodic-memory.md
│   ├── module-3-autonomous-loop.md
│   └── quickstart.md
├── src/
│   ├── episode_ingest.py
│   ├── unified_memory_recall.py
│   └── neo4j_recall.py
├── examples/
│   ├── first_principles_system_prompt.md
│   ├── goal_frame.example.json
│   └── env.example
└── docker/
    └── docker-compose.neo4j.yml
```

---

## Quick start

### 1. Start Neo4j
```bash
cd docker
cp ../examples/env.example .env
docker compose -f docker-compose.neo4j.yml up -d
```

### 2. Install Python deps
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Set environment
```bash
export ARS_NEO4J_URI=bolt://localhost:7687
export ARS_NEO4J_USER=neo4j
export ARS_NEO4J_PASSWORD=password
export ARS_SESSION_BASE=~/.openclaw/agents
export ARS_MEMORY_DB=~/.openclaw/memory/main.sqlite
export ARS_WORKSPACE=$PWD
```

### 4. Ingest sessions
```bash
python3 src/episode_ingest.py ingest-file /path/to/session.jsonl discord
```

### 5. Recall memory
```bash
python3 src/unified_memory_recall.py "First-Principles-Only"
python3 src/unified_memory_recall.py "Hybrid-Vector-Graph neo4j ollama"
```

### 6. Run the autonomous loop demo
```bash
python3 src/autonomous_loop.py examples/goal_frame.example.json --mode run
```

---

## Key design principle

> Memory must degrade gracefully, not disappear.

If Neo4j fails, SQLite still works.
If SQLite fails, raw file recall still works.
If embeddings fail, keyword + graph recall still works.

---

## Core scripts

### `src/episode_ingest.py`
- ingests session transcripts into SQLite + Neo4j
- writes SQLite first, then Neo4j
- supports `ingest`, `ingest-file`, `idle`, and `ending`
- extracts topics and entities
- rebuild-safe: stable IDs + relationship reset before rebuild

### `src/unified_memory_recall.py`
- single recall entrypoint
- ranks results across backends
- down-ranks current-session echo
- prefers meaningful entity hits over technical noise

### `src/neo4j_recall.py`
- lightweight Neo4j-only recall script
- useful for debugging the graph layer directly

### `src/autonomous_loop.py`
- executable bounded autonomy skeleton
- enforces loop state transitions
- supports `step` and `run`
- serializes loop state to JSON

---

## Reproducibility goal

This repo is meant to let another operator reproduce the same reinforcement capabilities used in the original assistant:
- a first-principles reasoning discipline
- a robust local episodic memory system
- a bounded autonomous execution loop

---

## License

MIT
