#!/usr/bin/env python3
"""
Episode Ingest — writes session episodes to Neo4j.
Designed for three trigger modes:
  1. cron idle (5min no activity)
  2. ending phrase detection (user says "就这样" etc.)
  3. session close detection (compaction triggered)
"""

import sys, json, os, re, sqlite3, hashlib, time
from datetime import datetime, timezone

NEO4J_URI = os.getenv("ARS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("ARS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("ARS_NEO4J_PASSWORD", "password")

SESSION_BASE = os.getenv("ARS_SESSION_BASE", os.path.expanduser("~/.openclaw/agents"))
MEMORY_DB = os.getenv("ARS_MEMORY_DB", os.path.expanduser("~/.openclaw/memory/main.sqlite"))

END_PHRASES = ["就这样", "先这样", "好了", "没了", "谢谢", "行", "好", "OK", "ok", "好的", "知道了", "明白了", "拜拜", "再见"]
SKIP_MARKERS = ["HEARTBEAT_OK", "NO_REPLY", "System (untrusted)", "Exec completed", "Cron job", "queued messages"]

TOPIC_KEYWORDS = {
    "neo4j": "neo4j", "向量": "vector-index", "memory": "memory",
    "episodic": "episodic-memory", "episode": "episodic-memory",
    "discord": "discord", "telegram": "telegram", "feishu": "feishu",
    "cron": "cron", "skill": "skill", "写作": "writing",
    "article": "writing", "公众号": "wechat", "twitter": "twitter",
    "x.com": "twitter", "产品": "product", "独立开发": "indie-dev",
    "openclaw": "openclaw", "agent": "agent", "claude": "claude",
    "gemini": "gemini", "ollama": "ollama", "embedding": "embedding",
    "blue": "blue-music", "音乐": "music",
}

KNOWN_ENTITIES = {
    "白羊武士": ("白羊武士", "person"),
    "Aries Warrior": ("白羊武士", "person"),
    "ferdinand6205": ("白羊武士", "person"),
    "小南瓜": ("小南瓜", "agent"),
    "小瓜": ("小南瓜", "agent"),
    "Samantha": ("Samantha", "person"),
    "盖伦": ("盖伦", "person"),
    "Neo4j": ("Neo4j", "technology"),
    "OpenClaw": ("OpenClaw", "technology"),
    "BotLearn": ("BotLearn", "product"),
    "Ollama": ("Ollama", "technology"),
    "Claude Code": ("Claude Code", "tool"),
    "Codex": ("Codex", "tool"),
    "SOUL.md": ("SOUL.md", "file"),
    "MEMORY.md": ("MEMORY.md", "file"),
    "AGENTS.md": ("AGENTS.md", "file"),
    "Telegram": ("Telegram", "channel"),
    "Discord": ("Discord", "channel"),
    "Feishu": ("Feishu", "channel"),
    "First-Principles-Only": ("First-Principles-Only", "concept"),
    "Hybrid-Vector-Graph": ("Hybrid-Vector-Graph", "concept"),
}

ENTITY_STOPWORDS = {
    'Now', 'None', 'Avoid', 'Current', 'Conversation', 'Sender', 'Message',
    'Description', 'Community', 'Apple', 'API', 'CPU', 'Browser', 'Memory',
    'Episode', 'Entity', 'Benchmark', 'Aries', 'Code', 'CLI', 'Bolt',
    'Users', 'True', 'Retest', 'OCR', 'Keep', 'Image', 'HTTP', 'General',
    'Gateway', 'Desktop', 'JSONL', 'MEDIA', 'MERGE', 'MENTIONS', 'TAGGED',
    'CONTAINS', 'SQLite', 'OpenAI', 'FTS', 'HTTP', 'HTTPS', 'JSON', 'SKILL',
    'py_compile', 'full_text', 'entity_names', 'entities', 'password'
}

ENTITY_TYPE_PRIORITY = {
    'person': 1,
    'agent': 2,
    'product': 3,
    'project': 4,
    'file': 5,
    'tool': 6,
    'technology': 7,
    'channel': 8,
    'concept': 9,
    'endpoint': 10,
    'command': 11,
}


def _clean_text(text):
    text = re.sub(r'Conversation info \(untrusted metadata\):\s*```json.*?```', ' ', text, flags=re.S)
    text = re.sub(r'Sender \(untrusted metadata\):\s*```json.*?```', ' ', text, flags=re.S)
    text = re.sub(r'Replied message \(untrusted.*?```', ' ', text, flags=re.S)
    text = re.sub(r'\{\s*"message_id".*?\}', ' ', text, flags=re.S)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def neo4j_write(session_id, summary, full_text, channel, topics, entities, first_ts, last_ts, msg_count):
    """Write episode node to Neo4j. Return True/False instead of hard failing."""
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        with driver.session() as sess:
            sess.run("""
                MERGE (e:Episode {session_id: $sid})
                SET e.channel = $ch,
                    e.summary = $sum,
                    e.topics = $topics,
                    e.entity_names = $entity_names,
                    e.first_timestamp = $fts,
                    e.last_timestamp = $lts,
                    e.message_count = $cnt,
                    e.full_text = $ft,
                    e.updated_at = datetime()
            """, sid=session_id, ch=channel, sum=summary, topics=topics,
               entity_names=[e['name'] for e in entities],
               fts=first_ts, lts=last_ts, cnt=msg_count, ft=full_text[:30000])

            sess.run("""
                MATCH (e:Episode {session_id: $sid})-[r:TAGGED|MENTIONS]->()
                DELETE r
            """, sid=session_id)

            for t in topics:
                sess.run("""
                    MERGE (t:Topic {name: $name})
                    WITH t
                    MATCH (e:Episode {session_id: $sid})
                    MERGE (e)-[:TAGGED]->(t)
                """, name=t, sid=session_id)

            for ent in entities:
                sess.run("""
                    MERGE (n:Entity {name: $name, entity_type: $etype})
                    WITH n
                    MATCH (e:Episode {session_id: $sid})
                    MERGE (e)-[:MENTIONS]->(n)
                """, name=ent['name'], etype=ent['entity_type'], sid=session_id)

            # Link to previous episode (FOLLOWED_BY)
            sess.run("""
                MATCH (prev:Episode)
                WHERE prev.last_timestamp < $fts
                  AND prev.session_id <> $sid
                WITH prev
                ORDER BY prev.last_timestamp DESC
                LIMIT 1
                MATCH (e:Episode {session_id: $sid})
                MERGE (prev)-[:FOLLOWED_BY]->(e)
            """, sid=session_id, fts=first_ts)
        driver.close()
        return True
    except Exception:
        try:
            driver.close()
        except Exception:
            pass
        return False


def sqlite_write(session_id, summary, full_text, channel, topics, first_ts, last_ts, msg_count):
    """Write episode to OpenClaw's local SQLite memory (for FTS search). Return True/False."""
    conn = sqlite3.connect(MEMORY_DB)
    cur = conn.cursor()
    chunk_id = "ep_" + hashlib.sha1(session_id.encode()).hexdigest()
    path = f"episode:{session_id}"
    emb = json.dumps([0.0] * 768)
    now_ms = int(time.time() * 1000)
    h = hashlib.sha256(full_text[:500].encode()).hexdigest()
    try:
        cur.execute("DELETE FROM chunks WHERE path = ?", (path,))
        cur.execute("""
            INSERT OR REPLACE INTO chunks (id, path, source, start_line, end_line, hash, model, text, embedding, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (chunk_id, path, channel, 0, 0, h, "none", full_text[:30000], emb, now_ms))
        cur.execute("""
            INSERT OR REPLACE INTO files (path, source, hash, mtime, size)
            VALUES (?, ?, ?, ?, ?)
        """, (path, channel, h, now_ms, len(full_text)))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


def parse_messages(fpath):
    """Parse JSONL session file, return messages + detected end phrase."""
    entries = []
    ended_by_phrase = None
    with open(fpath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("type") != "message":
                    continue
                msg = obj.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, list):
                    texts = [c.get("text", "") for c in content if c.get("type") in ("text", "output")]
                    content = " ".join(texts)
                role = msg.get("role", "?")
                ts = obj.get("timestamp", "")
                if not content or role not in ("user", "assistant"):
                    continue
                if any(m in content[:30] for m in SKIP_MARKERS):
                    continue
                if role == "user":
                    for phrase in END_PHRASES:
                        if phrase in content:
                            ended_by_phrase = phrase
                entries.append({"role": role, "text": content, "timestamp": ts})
            except:
                pass
    # Skip cron-initiated sessions (detected by first user message pattern)
    CRON_PATTERNS = [
        re.compile(r'^\[cron:'),
        re.compile(r'^\[[A-Z][a-z]{2} \d{4}-\d{2}-\d{2} \d{2}:\d{2} GMT[+\-]\d+\]'),
        re.compile(r'^Read HEARTBEAT\.md'),
        re.compile(r'^Write a dream diary entry'),
        re.compile(r'^Continue where you left off'),
        re.compile(r'^System: \[\d{4}-\d{2}-\d{2}'),
        re.compile(r'^\[Subagent Context\]'),
    ]
    first_user = next((e for e in entries if e["role"] == "user"), None)
    if first_user:
        txt = first_user["text"]
        if any(p.search(txt) for p in CRON_PATTERNS):
            return [], ended_by_phrase
    return entries, ended_by_phrase


def normalize_entity(name, entity_type):
    key = name.strip()
    if key in KNOWN_ENTITIES:
        canon, canon_type = KNOWN_ENTITIES[key]
        return canon, canon_type
    return key, entity_type


def is_noise_entity(token: str) -> bool:
    token = token.strip()
    if not token or token in ENTITY_STOPWORDS:
        return True
    if len(token) < 2:
        return True
    if token.lower() in {x.lower() for x in ENTITY_STOPWORDS}:
        return True
    if token.startswith(('http://', 'https://', 'bolt://')):
        return False
    if re.fullmatch(r'[0-9\-_:/.]+', token):
        return True
    if re.fullmatch(r'[A-Z]{1,4}', token):
        return True
    return False


def extract_entities(messages, full_text):
    found = {}
    for name, pair in KNOWN_ENTITIES.items():
        if name.lower() in full_text.lower():
            canon, etype = pair
            found[canon] = etype

    # backticked files / commands
    for token in re.findall(r'`([^`]{2,80})`', full_text):
        token = token.strip()
        if token.endswith(('.md', '.py', '.json', '.jsonl')):
            canon, etype = normalize_entity(token, 'file')
            found[canon] = etype
        elif token.startswith(('http://', 'https://', 'bolt://')):
            canon, etype = normalize_entity(token, 'endpoint')
            found.setdefault(canon, etype)
        elif token.startswith('--') and len(token) <= 48:
            canon, etype = normalize_entity(token, 'command')
            found.setdefault(canon, etype)
        elif ' ' not in token and token.isascii() and 4 <= len(token) <= 32 and re.search(r'[A-Za-z]', token):
            canon, etype = normalize_entity(token, 'command')
            found.setdefault(canon, etype)

    # English product/tool style entities
    for token in re.findall(r'\b[A-Z][A-Za-z0-9]+(?:[\-\.][A-Za-z0-9]+)*\b', full_text):
        if token in ENTITY_STOPWORDS:
            continue
        if token.isdigit():
            continue
        if re.fullmatch(r'[A-Z]{1,3}', token):
            continue
        if token.lower() in {'true','false','none','null','json','http','https'}:
            continue
        if len(token) >= 4:
            canon, etype = normalize_entity(token, 'concept')
            found.setdefault(canon, etype)

    # Chinese proper-noun hints
    for token in ['第一性原理', '图数据库', '向量索引', '情景记忆', '语义知识', '强制规则', '混合索引']:
        if token in full_text:
            canon, etype = normalize_entity(token, 'concept')
            found.setdefault(canon, etype)

    entities = []
    for k, v in found.items():
        if is_noise_entity(k):
            continue
        entities.append({"name": k, "entity_type": v})
    entities.sort(key=lambda x: (ENTITY_TYPE_PRIORITY.get(x['entity_type'], 99), x['name']))
    return entities[:16]


def extract_meta(messages):
    """Extract metadata from messages without external LLM."""
    if not messages:
        return None
    msgs = sorted(messages, key=lambda x: x.get("timestamp", ""))
    first_ts = msgs[0].get("timestamp", "")
    last_ts = msgs[-1].get("timestamp", "")

    cleaned_candidates = []
    for m in msgs:
        cleaned = _clean_text(m["text"])
        if cleaned and len(cleaned) >= 6:
            cleaned_candidates.append((m["role"], cleaned))

    summary_src = ""
    for role, cleaned in cleaned_candidates:
        if role == "user":
            summary_src = cleaned
            break
    if not summary_src and cleaned_candidates:
        summary_src = cleaned_candidates[0][1]
    summary = summary_src[:160] + ("..." if len(summary_src) > 160 else "")

    full_text = "\n".join(f"[{m['role']}] {_clean_text(m['text'])[:800]}" for m in msgs)
    full_text = full_text[:30000]
    combined = full_text.lower()
    topics = []
    for kw, topic in TOPIC_KEYWORDS.items():
        if kw in combined and topic not in topics:
            topics.append(topic)
    entities = extract_entities(msgs, full_text)
    return {
        "summary": summary or (msgs[0]['text'][:80] if msgs else ''),
        "topics": topics,
        "entities": entities,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "msg_count": len(msgs),
        "full_text": full_text,
    }


def ingest_event(session_id, summary, full_text, channel="runtime", first_ts=None, last_ts=None, msg_count=1):
    """Ingest a synthetic event/memory item into both SQLite and Neo4j."""
    first_ts = first_ts or datetime.now(timezone.utc).isoformat()
    last_ts = last_ts or first_ts
    cleaned_summary = _clean_text(summary)
    cleaned_full_text = _clean_text(full_text)
    combined = cleaned_full_text.lower()
    topics = []
    for kw, topic in TOPIC_KEYWORDS.items():
        if kw in combined and topic not in topics:
            topics.append(topic)
    entities = extract_entities([{"role": "assistant", "text": cleaned_full_text, "timestamp": first_ts}], cleaned_full_text)
    sqlite_ok = sqlite_write(session_id, cleaned_summary, cleaned_full_text, channel, topics, first_ts, last_ts, msg_count)
    neo4j_ok = neo4j_write(session_id, cleaned_summary, cleaned_full_text, channel, topics, entities, first_ts, last_ts, msg_count)
    return {
        "summary": cleaned_summary,
        "topics": topics,
        "entities": entities,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "msg_count": msg_count,
        "full_text": cleaned_full_text,
        "sqlite_ok": sqlite_ok,
        "neo4j_ok": neo4j_ok,
    }


def ingest_session(session_id, fpath, channel="discord"):
    """Ingest a session file to both Neo4j and SQLite with graceful degradation."""
    messages, ended = parse_messages(fpath)
    if not messages:
        return None
    meta = extract_meta(messages)
    if not meta:
        return None
    sqlite_ok = sqlite_write(session_id, meta["summary"], meta["full_text"], channel,
                             meta["topics"], meta["first_ts"], meta["last_ts"], meta["msg_count"])
    neo4j_ok = neo4j_write(session_id, meta["summary"], meta["full_text"], channel,
                           meta["topics"], meta["entities"], meta["first_ts"], meta["last_ts"], meta["msg_count"])
    return {"ended_by": ended, "sqlite_ok": sqlite_ok, "neo4j_ok": neo4j_ok, **meta}


# ────────────────────────────────────────────────────────────────────────────
# Trigger 1: Idle detection (called by cron)
# ────────────────────────────────────────────────────────────────────────────
def trigger_idle_check(idle_minutes=5, current_session_id=None):
    """Scan all sessions, ingest those idle for > idle_minutes."""
    now_ts = time.time()
    ingested = []
    for agent in ["main", "growth", "invest"]:
        sessions_dir = f"{SESSION_BASE}/{agent}/sessions"
        if not os.path.exists(sessions_dir):
            continue
        for fname in os.listdir(sessions_dir):
            if not fname.endswith(".jsonl"):
                continue
            session_id = fname.replace(".jsonl", "")
            # Skip current active session
            if current_session_id and session_id == current_session_id:
                continue
            fpath = os.path.join(sessions_dir, fname)
            mtime = os.path.getmtime(fpath)
            # Must be idle for at least idle_minutes
            if now_ts - mtime < idle_minutes * 60:
                continue
            messages, _ = parse_messages(fpath)
            if not messages:
                continue
            # Skip sessions with < 3 real user messages (likely cron/system)
            real_user_msgs = [m for m in messages if m["role"] == "user"]
            if len(real_user_msgs) < 3:
                continue
            last_msg_ts = messages[-1].get("timestamp", "")
            try:
                last_ts_epoch = datetime.fromisoformat(last_msg_ts.replace("Z", "+00:00")).timestamp()
            except:
                last_ts_epoch = mtime
            if now_ts - last_ts_epoch < idle_minutes * 60:
                continue
            channel = _detect_channel(session_id, agent)
            result = ingest_session(session_id, fpath, channel)
            if result:
                ingested.append((session_id, result["summary"][:60]))
    return ingested


def _detect_channel(session_id, agent):
    """Heuristic: detect channel from session_id parts."""
    s = session_id.lower()
    for ch in ["discord", "telegram", "feishu", "signal"]:
        if ch in s:
            return ch
    return "discord"


# ────────────────────────────────────────────────────────────────────────────
# Trigger 2: Ending phrase detection (called immediately when phrase detected)
# ────────────────────────────────────────────────────────────────────────────
def trigger_ending_phrase(session_key):
    """Called when user says an ending phrase. Ingest that session immediately."""
    # session_key format: agent:main:discord:default:direct:1024267179727794207
    parts = session_key.split(":")
    agent = "main"
    for p in parts:
        if p in ("main", "growth", "invest"):
            agent = p
    channel = "discord"
    for p in parts:
        if p in ("discord", "telegram", "feishu", "signal"):
            channel = p
    # Find the latest session file for this agent
    sessions_dir = f"{SESSION_BASE}/{agent}/sessions"
    if not os.path.exists(sessions_dir):
        return None
    files = [(f, os.path.getmtime(os.path.join(sessions_dir, f)))
             for f in os.listdir(sessions_dir) if f.endswith(".jsonl")]
    if not files:
        return None
    latest = max(files, key=lambda x: x[1])[0]
    session_id = latest.replace(".jsonl", "")
    fpath = os.path.join(sessions_dir, latest)
    return ingest_session(session_id, fpath, channel)


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "idle":
        idle_minutes = 5
        current_session_id = None
        if len(sys.argv) > 2:
            if str(sys.argv[2]).isdigit():
                idle_minutes = int(sys.argv[2])
                current_session_id = sys.argv[3] if len(sys.argv) > 3 else None
            else:
                current_session_id = sys.argv[2]
        results = trigger_idle_check(idle_minutes=idle_minutes, current_session_id=current_session_id)
        if not results:
            print("No idle sessions to ingest.")
        else:
            for sid, summary in results:
                print(f"✅ {sid[:20]}... | {summary}")
    elif cmd == "ending":
        session_key = sys.argv[2] if len(sys.argv) > 2 else "agent:main:discord:default:direct:example-user"
        result = trigger_ending_phrase(session_key)
        if result:
            print(f"✅ Ingested (ended by '{result['ended_by']}'): {result['summary'][:80]} | sqlite={result['sqlite_ok']} neo4j={result['neo4j_ok']}")
        else:
            print("Nothing to ingest.")
    elif cmd == "ingest":
        session_id = sys.argv[2]
        channel = sys.argv[3] if len(sys.argv) > 3 else "discord"
        # Find file
        for agent in ["main", "growth", "invest"]:
            fpath = f"{SESSION_BASE}/{agent}/sessions/{session_id}.jsonl"
            if os.path.exists(fpath):
                result = ingest_session(session_id, fpath, channel)
                if result:
                    print(f"✅ {result['summary'][:80]} | sqlite={result['sqlite_ok']} neo4j={result['neo4j_ok']}")
                break
        else:
            print(f"Session not found: {session_id}")
    elif cmd == "ingest-file":
        fpath = sys.argv[2]
        channel = sys.argv[3] if len(sys.argv) > 3 else "discord"
        session_id = os.path.basename(fpath).replace('.jsonl','')
        result = ingest_session(session_id, fpath, channel)
        if result:
            print(f"✅ {result['summary'][:80]} | sqlite={result['sqlite_ok']} neo4j={result['neo4j_ok']}")
        else:
            print("Nothing to ingest.")
    else:
        print("Usage:")
        print("  episode_ingest.py idle [idle_minutes] [current_session_id]  # scan and ingest idle sessions")
        print("  episode_ingest.py ending <key>  # trigger on ending phrase")
        print("  episode_ingest.py ingest <session_id> [channel]")
        print("  episode_ingest.py ingest-file <file_path> [channel]")
