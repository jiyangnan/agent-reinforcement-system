#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from checkpoint_store import list_checkpoints, list_open_checkpoints
from sync_state import sync_status_report
from unified_memory_recall import recall

ROOT = Path(__file__).resolve().parent.parent


def collect_recent_repo_changes(limit: int = 5) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    try:
        out = subprocess.run(["git", "log", "--oneline", f"-{limit}"], cwd=ROOT, capture_output=True, text=True, timeout=10)
        for line in out.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split(" ", 1)
            changes.append({"commit": parts[0], "title": parts[1] if len(parts) > 1 else ""})
    except Exception:
        return []
    return changes


def collect_recent_memory_hits(open_checkpoints: list[dict[str, Any]], top_k: int = 5) -> list[dict[str, Any]]:
    queries = []
    for cp in open_checkpoints[:3]:
        q = " ".join(filter(None, [cp.get("goal_id"), cp.get("title"), cp.get("next_step"), cp.get("latest_decision")]))
        if q.strip():
            queries.append(q.strip())
    if not queries:
        return []
    seen = set()
    hits_out = []
    for q in queries:
        try:
            hits = recall(q, top_k=top_k)
        except Exception:
            hits = []
        for h in hits:
            if h.location in seen:
                continue
            seen.add(h.location)
            hits_out.append({
                "backend": h.backend,
                "location": h.location,
                "title": h.title,
                "snippet": h.snippet,
                "score": h.score,
            })
            if len(hits_out) >= top_k:
                return hits_out
    return hits_out


def build_rehydrate_snapshot() -> dict[str, Any]:
    open_checkpoints = list_open_checkpoints()
    all_checkpoints = list_checkpoints(include_done=True)
    memory_hits = collect_recent_memory_hits(open_checkpoints)
    repo_changes = collect_recent_repo_changes()
    sync = sync_status_report()
    suggested = None
    if sync.get("backfill_needed"):
        suggested = "Run xng sync backfill before relying on graph recall."
    elif open_checkpoints:
        cp = open_checkpoints[0]
        suggested = cp.get("next_step") or cp.get("next_iteration_hint") or cp.get("latest_decision")
    elif all_checkpoints:
        suggested = "No open checkpoints. Review the most recent completed checkpoint and choose the next goal."
    else:
        suggested = "No checkpoints found. Start a goal or ingest recent sessions first."
    return {
        "active_goals": [
            {
                "goal_id": cp.get("goal_id"),
                "title": cp.get("title"),
                "status": cp.get("status"),
                "current_phase": cp.get("current_phase"),
                "next_step": cp.get("next_step"),
                "blockers": cp.get("blockers", []),
                "updated_at": cp.get("updated_at"),
            }
            for cp in open_checkpoints
        ],
        "recent_checkpoints": [
            {
                "goal_id": cp.get("goal_id"),
                "loop_id": cp.get("loop_id"),
                "title": cp.get("title"),
                "status": cp.get("status"),
                "current_phase": cp.get("current_phase"),
                "next_step": cp.get("next_step"),
                "updated_at": cp.get("updated_at"),
            }
            for cp in all_checkpoints[:5]
        ],
        "open_loops": [
            {
                "loop_id": cp.get("loop_id"),
                "goal_id": cp.get("goal_id"),
                "status": cp.get("status"),
                "next_iteration_hint": cp.get("next_iteration_hint"),
            }
            for cp in open_checkpoints
        ],
        "recent_memory_hits": memory_hits,
        "recent_repo_changes": repo_changes,
        "sync_health": sync,
        "suggested_next_focus": suggested,
    }


def main() -> int:
    snapshot = build_rehydrate_snapshot()
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
