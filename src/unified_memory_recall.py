#!/usr/bin/env python3
"""
Unified HA Memory Recall

Single entrypoint for local memory recall with automatic failover:
1) Neo4j episodic memory
2) SQLite FTS memory
3) File/session grep fallback

Goal: graceful degradation instead of memory collapse.
"""

from __future__ import annotations
import argparse, glob, json, os, re, sqlite3, subprocess, sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import List

ENTITY_TYPE_WEIGHT = {
    'person': 2.2,
    'agent': 2.0,
    'product': 1.8,
    'project': 1.7,
    'file': 1.5,
    'tool': 1.2,
    'technology': 1.0,
    'channel': 0.7,
    'concept': 0.9,
    'endpoint': 0.2,
    'command': 0.1,
}

WORKSPACE = "/Users/ferdinandji/.openclaw/workspace"
SQLITE_DB = "/Users/ferdinandji/.openclaw/memory/main.sqlite"
SESSION_DIRS = [
    "/Users/ferdinandji/.openclaw/agents/main/sessions",
    "/Users/ferdinandji/.openclaw/agents/growth/sessions",
    "/Users/ferdinandji/.openclaw/agents/invest/sessions",
]
NEO4J_URI = "bolt://localhost:7687"
NEO4J_USER = "neo4j"
NEO4J_PASSWORD = "password"


@dataclass
class Hit:
    backend: str
    score: float
    title: str
    location: str
    snippet: str
    meta: dict


def tokenize(query: str) -> list[str]:
    """Tokenize query into searchable tokens.
    English: split on word boundaries.
    Chinese: produce the full phrase + bigram (2-char) sliding windows for broader matching."""
    out = []
    seen = set()

    # English tokens
    for w in re.findall(r"[A-Za-z0-9_\-\.]+", query.lower()):
        if len(w) >= 2 and w not in seen:
            seen.add(w)
            out.append(w)

    # Chinese: extract continuous Chinese runs
    zh_runs = re.findall(r'[\u4e00-\u9fff]+', query)
    for run in zh_runs:
        # Add the full run as one token
        if len(run) >= 2 and run not in seen:
            seen.add(run)
            out.append(run)
        # Add bigrams for runs longer than 2 chars (broader matching)
        if len(run) > 2:
            for i in range(len(run) - 1):
                bg = run[i:i+2]
                if bg not in seen:
                    seen.add(bg)
                    out.append(bg)

    return out or [query.strip()]


def normalize_text(text: str) -> str:
    return re.sub(r'\s+', ' ', text or '').strip()


def recency_boost(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
        days = max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
        if days < 1:
            return 0.18
        if days < 7:
            return 0.16
        if days < 30:
            return 0.08
    except Exception:
        pass
    return 0.0


def source_adjustment(location: str, title: str) -> float:
    score = 0.0
    if '.checkpoint.' in location:
        score += 0.25
    if location.startswith('episode:26e731ba-64b2-4f40-b34f-f58ab8a03987'):
        score -= 1.2
    if title.startswith('前天我让你做了一个系统大升级'):
        score -= 0.8
    if 'Conversation info (untrusted metadata)' in title:
        score -= 0.2
    return score


def score_text_match(query: str, tokens: list[str], summary: str, text: str, topics=None, entities=None) -> float:
    summary_n = normalize_text(summary).lower()
    text_n = normalize_text(text).lower()
    query_n = normalize_text(query).lower()
    topics_n = [str(x).lower() for x in (topics or [])]
    entity_pairs = []
    for x in (entities or []):
        if isinstance(x, dict):
            entity_pairs.append((str(x.get('name','')).lower(), str(x.get('entity_type','concept'))))
        else:
            entity_pairs.append((str(x).lower(), 'concept'))
    score = 0.0
    if query_n and query_n in summary_n:
        score += 4.0
    if query_n and query_n in text_n:
        score += 2.5
    for t in tokens:
        if t.lower() in summary_n:
            score += 1.4
        if t.lower() in text_n:
            score += 0.7
        if any(t.lower() in x for x in topics_n):
            score += 1.2
        for ename, etype in entity_pairs:
            if t.lower() in ename:
                score += ENTITY_TYPE_WEIGHT.get(etype, 0.8)
                break
    # exact entity match bonus
    for ename, etype in entity_pairs:
        if query_n and query_n == ename:
            score += ENTITY_TYPE_WEIGHT.get(etype, 0.8) + 1.2
    return score


def extract_snippet(text: str, tokens: list[str], span: int = 140) -> str:
    lower = text.lower()
    for t in tokens:
        idx = lower.find(t.lower())
        if idx >= 0:
            start = max(0, idx - span)
            end = min(len(text), idx + len(t) + span)
            s = text[start:end].replace("\n", " ")
            return ("..." if start > 0 else "") + s + ("..." if end < len(text) else "")
    return text[:span*2].replace("\n", " ")


def recall_neo4j(query: str, top_k: int) -> list[Hit]:
    try:
        from neo4j import GraphDatabase
    except Exception:
        return []
    tokens = tokenize(query)
    clauses = []
    params = {}
    for i, tok in enumerate(tokens):
        params[f"t{i}"] = tok.lower()
        clauses.append(f"toLower(coalesce(e.summary, '')) CONTAINS $t{i}")
        clauses.append(f"toLower(coalesce(e.full_text, '')) CONTAINS $t{i}")
        clauses.append(f"ANY(x IN coalesce(e.topics, []) WHERE toLower(x) CONTAINS $t{i})")
        clauses.append(f"ANY(x IN coalesce(e.entity_names, []) WHERE toLower(x) CONTAINS $t{i})")
    cypher = f"""
    MATCH (e:Episode)
    WHERE {' OR '.join(clauses)}
    OPTIONAL MATCH (e)-[:MENTIONS]->(n:Entity)
    WITH e, collect(DISTINCT {{name:n.name, entity_type:n.entity_type}}) AS entity_rows
    RETURN e.session_id AS sid,
           e.summary AS summary,
           e.full_text AS full_text,
           e.topics AS topics,
           e.first_timestamp AS ts,
           e.channel AS channel,
           e.message_count AS cnt,
           entity_rows AS entity_rows
    ORDER BY e.first_timestamp DESC
    LIMIT $limit
    """
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    hits: list[Hit] = []
    with driver.session() as s:
        for r in s.run(cypher, **params, limit=top_k):
            text = (r["full_text"] or "")
            entities = [x for x in (r["entity_rows"] or []) if x and x.get('name')]
            score = 2.0 + score_text_match(query, tokens, r["summary"] or "", text, r["topics"] or [], entities) + recency_boost(r["ts"]) + source_adjustment(f"episode:{r['sid']}", r["summary"] or r["sid"])
            hits.append(Hit(
                backend="neo4j",
                score=score,
                title=r["summary"] or r["sid"],
                location=f"episode:{r['sid']}",
                snippet=extract_snippet(text or (r["summary"] or ""), tokens),
                meta={"topics": r["topics"] or [], "entities": entities, "ts": r["ts"], "channel": r["channel"], "message_count": r["cnt"]},
            ))
    driver.close()
    return hits


def recall_sqlite(query: str, top_k: int) -> list[Hit]:
    if not os.path.exists(SQLITE_DB):
        return []
    tokens = tokenize(query)
    fts_query = " OR ".join(f'"{t}"' if ' ' in t else t for t in tokens)
    conn = sqlite3.connect(SQLITE_DB)
    conn.row_factory = sqlite3.Row
    hits = []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT c.path, c.source, c.text,
                   snippet(chunks_fts, 0, '[', ']', ' … ', 20) AS snip,
                   bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.rowid = chunks_fts.rowid
            WHERE chunks_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (fts_query, top_k),
        )
        for row in cur.fetchall():
            text = row["text"] or ""
            rank = float(row["rank"])
            score = 1.5 + (5.0 - rank) + score_text_match(query, tokens, row["path"], text) + source_adjustment(row["path"], row["path"])
            hits.append(Hit(
                backend="sqlite_fts",
                score=score,
                title=row["path"],
                location=row["path"],
                snippet=row["snip"] or extract_snippet(text, tokens),
                meta={"source": row["source"]},
            ))
    except Exception:
        pass
    finally:
        conn.close()
    return hits


def recall_files(query: str, top_k: int) -> list[Hit]:
    tokens = tokenize(query)
    patterns = []
    for t in tokens[:6]:
        patterns.extend(["-e", t])
    search_paths = [os.path.join(WORKSPACE, "memory"), "/Users/ferdinandji/.openclaw/workspace/MEMORY.md", *SESSION_DIRS]
    cmd = [
        "rg", "-n", "-i", "--no-heading", "--max-count", str(top_k),
        "-g", "*.md",
        "-g", "*.jsonl",
        "-g", "!*.checkpoint.*.jsonl",
        "-g", "!*.trajectory.jsonl",
        "-g", "!*.trajectory-path.json",
        *patterns,
        *search_paths,
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
    except Exception:
        return []
    hits = []
    for line in out.stdout.splitlines()[:top_k]:
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        path, line_no, text = parts[0], parts[1], parts[2]
        score = 0.6 + score_text_match(query, tokens, os.path.basename(path), text) + source_adjustment(path, os.path.basename(path))
        hits.append(Hit(
            backend="ripgrep",
            score=score,
            title=os.path.basename(path),
            location=f"{path}#{line_no}",
            snippet=text.strip(),
            meta={},
        ))
    return hits


def canonical_episode_id(value: str) -> str:
    return re.sub(r'\.checkpoint\.[A-Za-z0-9\-]+$', '', value)


def canonical_key(hit: Hit) -> str:
    if hit.location.startswith('episode:'):
        return 'episode:' + canonical_episode_id(hit.location[len('episode:'):])
    m = re.search(r'episode:([A-Za-z0-9\-\.]+)', hit.snippet)
    if m:
        return 'episode:' + canonical_episode_id(m.group(1))
    return hit.location


def dedupe_and_rank(hits: list[Hit], top_k: int) -> list[Hit]:
    best = {}
    backend_bias = {'neo4j': 0.3, 'sqlite_fts': 0.15, 'ripgrep': 0.0}
    for h in hits:
        h.score += backend_bias.get(h.backend, 0.0)
        key = canonical_key(h)
        if key not in best or h.score > best[key].score:
            best[key] = h
    ranked = sorted(best.values(), key=lambda x: x.score, reverse=True)
    return ranked[:top_k]


def recall(query: str, top_k: int = 8, use_neo4j: bool = True, use_sqlite: bool = True, use_files: bool = True) -> list[Hit]:
    hits = []
    if use_neo4j:
        hits.extend(recall_neo4j(query, top_k))
    if use_sqlite:
        hits.extend(recall_sqlite(query, top_k))
    if use_files and len(hits) < max(3, top_k // 2):
        hits.extend(recall_files(query, top_k))
    return dedupe_and_rank(hits, top_k)


def format_text(query: str, hits: list[Hit]) -> str:
    if not hits:
        return f"No memory hits for: {query}"
    lines = [f"Unified Memory Recall · query={query} · hits={len(hits)}\n"]
    for i, h in enumerate(hits, 1):
        lines.append(f"{i}. [{h.backend}] {h.title}")
        lines.append(f"   at: {h.location}")
        if h.meta:
            lines.append(f"   meta: {json.dumps(h.meta, ensure_ascii=False)}")
        lines.append(f"   {h.snippet[:260]}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--top-k", type=int, default=8)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-neo4j", action="store_true")
    ap.add_argument("--no-sqlite", action="store_true")
    ap.add_argument("--no-files", action="store_true")
    args = ap.parse_args()
    hits = recall(
        args.query,
        args.top_k,
        use_neo4j=not args.no_neo4j,
        use_sqlite=not args.no_sqlite,
        use_files=not args.no_files,
    )
    if args.json:
        print(json.dumps([asdict(h) for h in hits], ensure_ascii=False, indent=2))
    else:
        print(format_text(args.query, hits))


if __name__ == "__main__":
    main()
