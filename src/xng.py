#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def run_py(script: str, args: list[str]) -> int:
    cmd = [sys.executable, str(SRC / script), *args]
    return subprocess.call(cmd, cwd=ROOT)


def cmd_memory(ns: argparse.Namespace) -> int:
    if ns.memory_cmd == "recall":
        args = [ns.query, "--top-k", str(ns.top_k)]
        if ns.json:
            args.append("--json")
        if ns.no_neo4j:
            args.append("--no-neo4j")
        if ns.no_sqlite:
            args.append("--no-sqlite")
        if ns.no_files:
            args.append("--no-files")
        return run_py("unified_memory_recall.py", args)
    if ns.memory_cmd == "ingest-file":
        return run_py("episode_ingest.py", ["ingest-file", ns.path, ns.channel])
    if ns.memory_cmd == "ingest-session":
        return run_py("episode_ingest.py", ["ingest", ns.session_id, ns.channel])
    print("unknown memory command", file=sys.stderr)
    return 2


def cmd_loop(ns: argparse.Namespace) -> int:
    args = [ns.goal]
    if ns.state:
        args += ["--state", ns.state]
    if ns.out:
        args += ["--out", ns.out]
    args += ["--mode", ns.loop_cmd]
    return run_py("autonomous_loop.py", args)


def cmd_doctor(_: argparse.Namespace) -> int:
    report = {
        "cwd": str(ROOT),
        "neo4j_uri": os.getenv("ARS_NEO4J_URI", "bolt://localhost:7687"),
        "session_base": os.getenv("ARS_SESSION_BASE", os.path.expanduser("~/.openclaw/agents")),
        "memory_db": os.getenv("ARS_MEMORY_DB", os.path.expanduser("~/.openclaw/memory/main.sqlite")),
        "workspace": os.getenv("ARS_WORKSPACE", str(ROOT)),
        "checks": {},
    }
    import socket, sqlite3
    host, port = "localhost", 7687
    try:
        with socket.create_connection((host, port), timeout=2):
            report["checks"]["neo4j_port_7687"] = "ok"
    except Exception as e:
        report["checks"]["neo4j_port_7687"] = f"fail: {e}"
    db = Path(report["memory_db"]).expanduser()
    if db.exists():
        try:
            conn = sqlite3.connect(db)
            cur = conn.cursor()
            cur.execute("select count(*) from sqlite_master where type='table'")
            report["checks"]["sqlite"] = f"ok: {cur.fetchone()[0]} tables"
            conn.close()
        except Exception as e:
            report["checks"]["sqlite"] = f"fail: {e}"
    else:
        report["checks"]["sqlite"] = "missing"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


def cmd_demo(_: argparse.Namespace) -> int:
    goal = str(ROOT / "examples" / "goal_frame.example.json")
    return run_py("autonomous_loop.py", [goal, "--mode", "run"])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xng", description="xiaonangua CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("memory", help="memory operations")
    msub = m.add_subparsers(dest="memory_cmd", required=True)

    mr = msub.add_parser("recall", help="recall memory")
    mr.add_argument("query")
    mr.add_argument("--top-k", type=int, default=8)
    mr.add_argument("--json", action="store_true")
    mr.add_argument("--no-neo4j", action="store_true")
    mr.add_argument("--no-sqlite", action="store_true")
    mr.add_argument("--no-files", action="store_true")
    mr.set_defaults(func=cmd_memory)

    mif = msub.add_parser("ingest-file", help="ingest a transcript file")
    mif.add_argument("path")
    mif.add_argument("channel", nargs="?", default="discord")
    mif.set_defaults(func=cmd_memory)

    mis = msub.add_parser("ingest-session", help="ingest a session by id")
    mis.add_argument("session_id")
    mis.add_argument("channel", nargs="?", default="discord")
    mis.set_defaults(func=cmd_memory)

    l = sub.add_parser("loop", help="autonomous loop")
    lsub = l.add_subparsers(dest="loop_cmd", required=True)
    for mode in ("run", "step"):
        lp = lsub.add_parser(mode, help=f"{mode} autonomous loop")
        lp.add_argument("goal")
        lp.add_argument("--state")
        lp.add_argument("--out")
        lp.set_defaults(func=cmd_loop)

    d = sub.add_parser("doctor", help="environment checks")
    d.set_defaults(func=cmd_doctor)

    demo = sub.add_parser("demo", help="run demo goal")
    demo.set_defaults(func=cmd_demo)
    return p


def main() -> int:
    parser = build_parser()
    ns = parser.parse_args()
    return ns.func(ns)


if __name__ == "__main__":
    raise SystemExit(main())
