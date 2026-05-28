import os
import json
import datetime
import secrets
from pathlib import Path
from flask import Flask, request, jsonify, render_template, render_template_string, Response, stream_with_context, session, redirect, url_for
import boto3
import requests as http_requests

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent.parent / "frontend"),
    static_folder=str(Path(__file__).parent.parent / "frontend" / "static"),
)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
KB_PATH = Path(__file__).parent.parent / "data" / "knowledge_base.json"
FEEDBACK_PATH = Path(__file__).parent.parent / "data" / "feedback.jsonl"
RESOLVED_PATH = Path(__file__).parent.parent / "data" / "resolved_cases.jsonl"

FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", secrets.token_hex(32))
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
GITHUB_CALLBACK_URL = os.environ.get("GITHUB_CALLBACK_URL", "https://alarm.dash-ly.com/admin/callback")
ADMIN_GITHUB_USER = os.environ.get("ADMIN_GITHUB_USER", "BenyaminJalali")
KB_REBUILD_SECRET = os.environ.get("KB_REBUILD_SECRET", "")
CONVERSATIONS_PATH = Path(__file__).parent.parent / "data" / "conversations.jsonl"
KB_REBUILD_LOG_PATH = Path(__file__).parent.parent / "data" / "kb_rebuild_log.jsonl"

app.secret_key = FLASK_SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20MB

_knowledge_base: dict | None = None
_kb_index: list | None = None  # pre-built search index

# Stop words to ignore when scoring
_STOP_WORDS = {
    "a", "an", "the", "is", "it", "in", "on", "at", "to", "for", "of", "and",
    "or", "with", "my", "i", "what", "how", "why", "does", "do", "can", "be",
    "not", "this", "that", "are", "was", "has", "have", "get", "getting",
    "showing", "shows", "see", "seeing", "there", "keep", "keeps", "its",
    "help", "please", "when", "should", "would", "could", "need", "want",
}

# Device synonym map — user words → KB device codes
_DEVICE_SYNONYMS = {
    "inverter": "INV", "inv": "INV",
    "battery": "BMU", "bmu": "BMU",
    "gateway": "GMS", "gms": "GMS",
    "disconnect": "MANTA", "manta": "MANTA", "sds": "MANTA",
    "micro": "MI", "microinverter": "MI", "mi": "MI",
}


def load_knowledge_base() -> dict:
    global _knowledge_base
    if _knowledge_base is None:
        if KB_PATH.exists():
            with open(KB_PATH) as f:
                _knowledge_base = json.load(f)
        else:
            _knowledge_base = {"entries": []}
    return _knowledge_base


def _build_kb_index():
    """Build a search index once at startup. Each item is (searchable_text, entry)."""
    global _kb_index
    if _kb_index is not None:
        return _kb_index
    kb = load_knowledge_base()
    index = []
    for e in kb.get("entries", []):
        eng = e.get("engineering", {})
        prod = e.get("product", {})
        ts = eng.get("troubleshooting") or prod.get("corrective_action") or []
        if isinstance(ts, list):
            ts_text = " ".join(ts)
        else:
            ts_text = str(ts)
        searchable = " ".join(filter(None, [
            e.get("alarm_name", ""),
            e.get("friendly_name", ""),
            e.get("device", ""),
            e.get("alarm_code", ""),
            eng.get("description", ""),
            eng.get("trigger", ""),
            eng.get("internal_notes", ""),
            prod.get("description", ""),
            prod.get("corrective_action", "") if isinstance(prod.get("corrective_action"), str) else "",
            ts_text,
        ])).lower()
        index.append((searchable, e))
    _kb_index = index
    return _kb_index


def _is_supplemental(entry: dict) -> bool:
    """Supplemental entries are non-alarm KB items (commissioning, Field Pro, etc.)."""
    sources = entry.get("sources", [])
    alarm_sources = {"masterlist", "inv_extended_desc", "bmu_extended_desc",
                     "reef_headend", "error_catalog"}
    return not any(s in alarm_sources for s in sources)


def search_kb(query: str, top_n: int = 15) -> list:
    """Return the top_n most relevant KB entries for a query using keyword scoring."""
    index = _build_kb_index()
    query_lower = query.lower()
    query_words = {w for w in query_lower.split() if w not in _STOP_WORDS and len(w) > 2}

    # Expand device synonyms — e.g. "battery" → also score "BMU" matches higher
    device_boost = set()
    for word in list(query_words):
        if word in _DEVICE_SYNONYMS:
            device_boost.add(_DEVICE_SYNONYMS[word].lower())

    # Check for explicit alarm code pattern (e.g. "008A", "F001")
    import re
    alarm_code_pattern = re.compile(r'\b[0-9a-fA-F]{3,4}[A-Za-z0-9]?\b')
    explicit_codes = {m.group().lower() for m in alarm_code_pattern.finditer(query_lower)}

    supplemental = []
    scored = []

    for searchable, entry in index:
        if _is_supplemental(entry):
            supplemental.append(entry)
            continue

        score = 0

        # Exact alarm code match → very high weight
        if explicit_codes:
            entry_code = entry.get("alarm_code", "").lower()
            if entry_code and any(c in entry_code for c in explicit_codes):
                score += 20

        # Device match boost
        entry_device = entry.get("device", "").lower()
        if device_boost and entry_device in device_boost:
            score += 5

        # Keyword overlap with searchable text
        for word in query_words:
            if word in searchable:
                # Higher weight for matches in name/friendly_name
                if word in (entry.get("alarm_name", "") + " " + entry.get("friendly_name", "")).lower():
                    score += 3
                else:
                    score += 1

        if score > 0:
            scored.append((score, entry))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_entries = [e for _, e in scored[:top_n]]

    # Always include all supplemental entries (commissioning, Field Pro, etc.)
    return supplemental + top_entries


def _format_entry(e: dict) -> str:
    name = e.get("alarm_name") or e.get("id", "")
    device = e.get("device", "")
    friendly = e.get("friendly_name", "")
    severity = e.get("severity", "")
    alarm_code = e.get("alarm_code", "")
    alarm_type = e.get("alarm_type", "")
    eng = e.get("engineering", {})
    prod = e.get("product", {})
    vis = e.get("visibility", {})

    desc = eng.get("description") or prod.get("description") or ""
    trigger = eng.get("trigger") or ""
    threshold = eng.get("threshold") or ""
    ts_steps = eng.get("troubleshooting") or prod.get("corrective_action") or []
    internal = eng.get("internal_notes") or ""

    entry_lines = [f"ALARM: {name}"]
    if device:
        entry_lines.append(f"  Device: {device}")
    if friendly:
        entry_lines.append(f"  Friendly Name: {friendly}")
    if alarm_code:
        entry_lines.append(f"  Code: {alarm_code}")
    if severity:
        entry_lines.append(f"  Severity: {severity}")
    if alarm_type:
        entry_lines.append(f"  Type: {alarm_type}")
    if desc:
        entry_lines.append(f"  Description: {desc}")
    if trigger:
        entry_lines.append(f"  Trigger: {trigger}")
    if threshold:
        entry_lines.append(f"  Threshold: {threshold}")
    if vis:
        visible_to = [k for k, v in vis.items() if v]
        if visible_to:
            entry_lines.append(f"  Visible to: {', '.join(visible_to)}")
    if ts_steps:
        if isinstance(ts_steps, list):
            entry_lines.append(f"  Troubleshooting: {' | '.join(ts_steps)}")
        else:
            entry_lines.append(f"  Troubleshooting: {ts_steps}")
    if internal:
        entry_lines.append(f"  Internal Notes: {internal}")

    return "\n".join(entry_lines)


def build_context_block(query: str = "") -> str:
    if query:
        entries = search_kb(query, top_n=15)
    else:
        # Fallback: only supplemental + first 30 alarm entries (for health check etc.)
        index = _build_kb_index()
        entries = [e for _, e in index if _is_supplemental(e)]
        entries += [e for _, e in index[:30] if not _is_supplemental(e)]
    return "\n\n".join(_format_entry(e) for e in entries)


def get_similar_resolved_cases(question: str, limit: int = 3) -> str:
    """
    Find resolved conversations (👍) similar to the current question.
    Simple keyword overlap — no embeddings needed yet.
    Returns a formatted block to inject into the system prompt.
    """
    if not RESOLVED_PATH.exists():
        return ""

    question_words = set(question.lower().split())
    scored = []

    with open(RESOLVED_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                case = json.loads(line)
                case_text = (case.get("question", "") + " " + case.get("answer", "")).lower()
                case_words = set(case_text.split())
                overlap = len(question_words & case_words)
                if overlap > 2:
                    scored.append((overlap, case))
            except Exception:
                continue

    if not scored:
        return ""

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:limit]

    blocks = []
    for _, case in top:
        q = case.get("question", "")[:300]
        a = case.get("answer", "")[:600]
        role = case.get("audience", "installer")
        blocks.append(f"RESOLVED CASE ({role}):\nProblem: {q}\nSolution: {a}")

    return "\n\n".join(blocks)


SYSTEM_PROMPT = """You are a friendly troubleshooting assistant for Generac Home Energy systems — a home battery and backup power system made by Generac.

Your job is to help people understand what is wrong with their system and walk them through fixing it — in plain, everyday language. Think of yourself as a knowledgeable neighbor who happens to know everything about this system. You are patient, clear, and never condescending.

## The Golden Rule
Write every response so that a non-technical person — someone's grandmother — could read it, understand exactly what is happening, and know what to do next. If you catch yourself using a technical term, replace it with plain English. Instead of "DC bus undervoltage", say "the battery isn't providing enough power". Instead of "CAN communication timeout", say "the devices have stopped talking to each other".

## R2 Platform Hardware Facts — Never Guess On These
You are specifically supporting the Generac R2 platform. These are facts you must always apply:

**Communication cables:**
- The R2 platform uses CAN bus cables for communication between devices — NOT ethernet, NOT RJ45, NOT CAT5
- CAN cables are typically a twisted pair (two wires twisted together) with a proprietary connector
- Never describe communication cables as "ethernet-style" or "like an internet cable" — that is wrong for this platform
- CAN bus requires termination resistors at each end of the bus — a missing or loose terminator causes communication timeouts

**Devices and their roles:**
- Inverter (INV): converts DC power from the battery to AC power for the home
- Battery (BMU): stores energy, connects to the Inverter via DC wiring (high voltage — do not touch)
- Gateway (GMS/PLCHE): the brain of the system, connects to the internet and controls the other devices
- Smart Disconnect Switch (MANTA/SDS): sits between the grid and the home, handles grid connect/disconnect
- Micro-Inverter (MI): a separate solar generation device — completely separate from the Battery storage system

**Wiring:**
- DC wiring between Battery and Inverter is high voltage — homeowners should never touch it
- CAN bus wiring is low voltage and safe for installers to inspect
- AC wiring connects the Inverter to the home panel — always de-energize before inspecting

**If you don't know a specific hardware detail, say so explicitly rather than making something up.**

## Image Analysis
When a user submits a photo, analyze it carefully and describe exactly what you see before giving guidance. Look for:
- LED status lights and their colors/patterns
- Cable connections and whether they look secure
- Error codes or text on screens
- Physical damage, loose wires, or obvious issues
Be specific about what you observe. If you cannot tell something from the image, say so rather than guessing.

## The Three Audiences — Adjust Your Depth

**Homeowner**: You are talking to the person who owns the home.
- Use only everyday language. Never use acronyms, codes, or technical terms.
- Keep it short — 3 to 5 sentences max per section.
- Tell them what it means for their home (will the lights stay on? is it safe?).
- Tell them exactly one thing to try themselves, then say when to call their installer.
- Never mention firmware, CAN bus, DC voltage, or internal error codes.

**Installer / Dealer**: You are talking to a trained technician on a job site.
- You can use technical terms but explain each one briefly.
- Give numbered step-by-step diagnostic and repair steps.
- Include what to measure, what to look for, and what to try first.
- Be direct and efficient — they are busy on a job.
- Tell them when to escalate to Generac Technical Support.

**Support / TSE**: You are talking to a Generac internal support engineer.
- Provide full technical depth — include internal notes, exact thresholds, firmware context, CAN diagnostics.
- Surface all relevant related alarms and potential cascading causes.
- Include everything the installer sees, plus more.

## Response Structure (use this every time)

**For Homeowner:**
1. What happened (one sentence, plain English)
2. What it means for you (is it safe? will power stay on?)
3. One thing to try yourself
4. When to call your installer

**For Installer / Support:**
1. **What's happening** — plain-language summary of the fault
2. **Which device** — Inverter / Battery / Gateway / Smart Disconnect (use these names, not INV/BMU/GMS/MANTA)
3. **Why it happens** — root causes in plain terms
4. **How urgent** — Critical (shut down now) / High (fix today) / Medium (monitor and schedule repair) / Low (informational)
5. **Step-by-step fix** — numbered, specific, actionable
6. **When to escalate** — be explicit

## Device Names — Always Use These
- Inverter (not INV)
- Battery (not BMU) — this is the home energy storage battery unit, never a micro-inverter
- Gateway (not GMS or PLCHE)
- Smart Disconnect Switch (not MANTA or SDS)
- Micro-Inverter or MI — a separate solar generation device, completely different from the Battery

## Device Disambiguation — Critical
When a user says "battery", they ALWAYS mean the home energy storage Battery (BMU). Never interpret "battery" as referring to a Micro-Inverter (MI).

When a user introduces a new symptom or new device in a multi-turn conversation, treat it as a fresh diagnostic question. Do not let the previous topic bias your device matching. If the previous question was about an MI and the next question mentions "battery", answer about the Battery — they are different devices.

## Rules
- Never fabricate steps, thresholds, or causes not supported by the knowledge base
- If you cannot confidently match a symptom to a known alarm, say so and ask one clarifying question
- For Critical faults, always lead with safety: power the system down before inspecting
- Never show raw alarm codes or internal technical names to homeowners
- Never end with a generic sign-off line like "Does this help? Want me to go deeper on any of these steps?" — it feels robotic. Instead, end with a natural follow-up question that moves the troubleshooting forward.
- If someone seems frustrated or worried, acknowledge that first before troubleshooting

## Be a Conversation Partner, Not a Manual

For Installer and Support roles, after giving your initial answer, always end with ONE specific follow-up question that helps narrow down the problem. Examples:
- "What does the LED status look like on the Gateway right now?"
- "Have you already tried power cycling it, or is that the next step?"
- "What step are you on in the Field Pro commissioning flow?"
- "Are you seeing any error codes in the Field Pro app, or just the behavior you described?"

This keeps the troubleshooting moving and helps you give better, more targeted guidance. Never ask more than one question at a time.

## Resolved Cases From Real Dealers
{resolved_cases}

## Alarm Knowledge Base
{kb_context}
"""


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/kb")
def kb_page():
    return render_template("kb.html")


@app.route("/generate")
def generate_page():
    return render_template("generate.html")


@app.route("/api/generate-entry", methods=["POST"])
def generate_entry():
    """
    Generate all three audience-level KB entries for a given alarm in one shot.
    Returns JSON with homeowner, installer, and support responses.
    """
    data = request.json or {}
    alarm_query = data.get("query", "").strip()
    if not alarm_query:
        return jsonify({"error": "No alarm query provided"}), 400

    kb_context = build_context_block(alarm_query)

    GENERATE_SYSTEM = """You are a technical writer for Generac Home Energy systems.
Given an alarm or fault description, write a concise knowledge base entry for the specified audience.
Be specific, accurate, and use only information supported by the knowledge base provided.
Do not fabricate thresholds, steps, or causes not in the KB.
"""

    audience_prompts = {
        "homeowner": (
            "AUDIENCE: Homeowner (non-technical, plain English only)\n"
            "Write a short 4-part entry:\n"
            "1. What happened (one plain-English sentence)\n"
            "2. What it means for their home (safe? will power stay on?)\n"
            "3. One thing they can try themselves\n"
            "4. When to call their installer\n"
            "No codes, no technical terms, no jargon."
        ),
        "installer": (
            "AUDIENCE: Installer / Dealer (trained technician)\n"
            "Write a structured diagnostic entry:\n"
            "1. What's happening (plain summary)\n"
            "2. Which device\n"
            "3. Root causes\n"
            "4. Urgency level (Critical / High / Medium / Low)\n"
            "5. Step-by-step fix (numbered)\n"
            "6. When to escalate to Generac Technical Support\n"
            "Use technical terms but define each one briefly."
        ),
        "support": (
            "AUDIENCE: Support / TSE (Generac internal engineer)\n"
            "Write a full technical entry:\n"
            "1. Full technical description with thresholds and triggers\n"
            "2. All related alarms and cascading causes\n"
            "3. Internal diagnostic steps (firmware context, CAN diagnostics, etc.)\n"
            "4. Internal notes and escalation path\n"
            "Include everything from the installer view plus internal detail."
        ),
    }

    bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)

    def call_bedrock(audience, prompt):
        system = GENERATE_SYSTEM + f"\n\n## Alarm Knowledge Base\n{kb_context}"
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "system": system,
            "messages": [{"role": "user", "content": f"{prompt}\n\nAlarm / fault to document:\n{alarm_query}"}],
        })
        try:
            resp = bedrock.invoke_model(
                modelId=BEDROCK_MODEL_ID,
                contentType="application/json",
                accept="application/json",
                body=body,
            )
            result = json.loads(resp["body"].read())
            return result["content"][0]["text"]
        except Exception as e:
            return f"Error generating {audience} entry: {str(e)}"

    import concurrent.futures
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(call_bedrock, aud, prompt): aud
            for aud, prompt in audience_prompts.items()
        }
        for future in concurrent.futures.as_completed(futures):
            aud = futures[future]
            results[aud] = future.result()

    return jsonify({
        "homeowner": results.get("homeowner", ""),
        "installer": results.get("installer", ""),
        "support": results.get("support", ""),
        "query": alarm_query,
    })


@app.route("/api/kb-entries")
def kb_entries():
    kb = load_knowledge_base()
    entries = kb.get("entries", [])
    # Return a compact version suitable for the table
    rows = []
    for e in entries:
        eng = e.get("engineering", {})
        prod = e.get("product", {})
        ts = eng.get("troubleshooting") or prod.get("corrective_action") or []
        if isinstance(ts, list):
            ts_text = " | ".join(ts)
        else:
            ts_text = str(ts)
        rows.append({
            "id": e.get("id", ""),
            "alarm_name": e.get("alarm_name", "") or e.get("id", ""),
            "friendly_name": e.get("friendly_name", ""),
            "device": e.get("device", ""),
            "alarm_code": e.get("alarm_code", ""),
            "severity": e.get("severity", ""),
            "alarm_type": e.get("alarm_type", ""),
            "description": eng.get("description") or prod.get("description") or "",
            "corrective_action": ts_text,
            "internal_notes": eng.get("internal_notes", ""),
            "sources": e.get("sources", []),
            "visibility": e.get("visibility", {}),
        })
    rebuild_log = []
    if KB_REBUILD_LOG_PATH.exists():
        with open(KB_REBUILD_LOG_PATH) as f:
            for line in f:
                try:
                    rebuild_log.append(json.loads(line.strip()))
                except Exception:
                    pass
    last_rebuild = next(
        (r for r in reversed(rebuild_log) if r.get("event") == "rebuild_success"), None
    )
    return jsonify({
        "entries": rows,
        "total": len(rows),
        "last_rebuild": last_rebuild,
    })


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    messages = data.get("messages", [])
    audience = data.get("audience", "installer")

    if not messages:
        return jsonify({"error": "No messages provided"}), 400

    # Log conversation (session_id from client, no message content stored)
    session_id = data.get("session_id", "unknown")
    conv_record = {
        "ts": datetime.datetime.utcnow().isoformat(),
        "session_id": session_id,
        "audience": audience,
        "msg_count": len(messages),
    }
    try:
        with open(CONVERSATIONS_PATH, "a") as f:
            f.write(json.dumps(conv_record) + "\n")
    except Exception:
        pass

    # Extract first user text for RAG (both KB search and resolved cases)
    first_text = ""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                first_text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        first_text = block.get("text", "")
                        break
            if first_text:
                break

    kb_context = build_context_block(first_text)
    resolved_cases = get_similar_resolved_cases(first_text)
    resolved_block = ""
    if resolved_cases:
        resolved_block = f"The following are real resolved cases from dealers who had similar problems. Use these to give faster, more accurate answers:\n\n{resolved_cases}"
    else:
        resolved_block = "No similar resolved cases yet. Answer based on the alarm knowledge base."

    system = SYSTEM_PROMPT.format(kb_context=kb_context, resolved_cases=resolved_block)

    audience_note = {
        "support": "The current user is internal SUPPORT/TSE staff. Provide full technical detail including internal notes.",
        "installer": "The current user is an INSTALLER/DEALER. Provide diagnostic steps and corrective actions.",
        "homeowner": "The current user is a HOMEOWNER. Use plain English only. No technical jargon.",
    }.get(audience, "")
    if audience_note:
        system = audience_note + "\n\n" + system

    bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)

    # If any message contains an image, use a compact system prompt to stay under Bedrock's size limit
    has_images = any(
        isinstance(m.get("content"), list) and
        any(b.get("type") == "image" for b in m["content"])
        for m in messages
    )

    if has_images:
        system = (audience_note + "\n\n" if audience_note else "") + "\n".join(
            SYSTEM_PROMPT.split("## Alarm Knowledge Base")[0].splitlines()
        ).strip() + "\n\nAnswer based on what you can see in the image(s) and your knowledge of Generac R2 systems."

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "system": system,
        "messages": messages,
    })

    def generate():
        try:
            response = bedrock.invoke_model_with_response_stream(
                modelId=BEDROCK_MODEL_ID,
                contentType="application/json",
                accept="application/json",
                body=body,
            )
            for event in response["body"]:
                chunk = json.loads(event["chunk"]["bytes"])
                if chunk.get("type") == "content_block_delta":
                    delta = chunk.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield f"data: {json.dumps({'text': delta['text']})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'text': f'Sorry, there was an error processing your request: {str(e)}'})}\n\n"
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/api/feedback", methods=["POST"])
def feedback():
    data = request.json or {}
    rating = data.get("rating")

    record = {
        "ts": datetime.datetime.utcnow().isoformat(),
        "rating": rating,
        "audience": data.get("audience"),
        "question": data.get("question", "")[:500],
        "answer": data.get("answer", "")[:1000],
        "comment": data.get("comment", "")[:500],
    }

    # Save to feedback log always
    with open(FEEDBACK_PATH, "a") as f:
        f.write(json.dumps(record) + "\n")

    # If thumbs up — save to resolved cases for RAG
    if rating == "up":
        resolved_record = {
            "ts": record["ts"],
            "audience": record["audience"],
            "question": record["question"],
            "answer": record["answer"],
        }
        with open(RESOLVED_PATH, "a") as f:
            f.write(json.dumps(resolved_record) + "\n")

    return jsonify({"ok": True})


@app.route("/api/convert-image", methods=["POST"])
def convert_image():
    """Convert HEIC/HEIF to JPEG server-side."""
    try:
        from pillow_heif import register_heif_opener
        from PIL import Image
        import io, base64
        register_heif_opener()
    except ImportError:
        return jsonify({"error": "HEIC conversion not available"}), 501

    data = request.json or {}
    b64 = data.get("data", "")
    if not b64:
        return jsonify({"error": "No image data"}), 400

    try:
        img_bytes = base64.b64decode(b64)
        img = Image.open(io.BytesIO(img_bytes))
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=85)
        out.seek(0)
        result = base64.b64encode(out.read()).decode()
        return jsonify({"data": result, "mimeType": "image/jpeg"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _invalidate_kb_cache():
    """Drop in-memory KB so the next request reloads from disk."""
    global _knowledge_base, _kb_index
    _knowledge_base = None
    _kb_index = None


@app.route("/api/health")
def health():
    kb = load_knowledge_base()
    resolved_count = 0
    if RESOLVED_PATH.exists():
        with open(RESOLVED_PATH) as f:
            resolved_count = sum(1 for line in f if line.strip())
    return jsonify({
        "status": "ok",
        "kb_entries": len(kb.get("entries", [])),
        "kb_sources": kb.get("sources", []),
        "resolved_cases": resolved_count,
    })


@app.route("/api/rebuild-kb", methods=["POST"])
def rebuild_kb():
    """
    Called by GitHub Actions in source repos when alarm-related files change.
    Requires X-Rebuild-Secret header matching KB_REBUILD_SECRET env var.
    Logs the triggering PR metadata for firmware version tracking.
    """
    if not KB_REBUILD_SECRET:
        return jsonify({"error": "Rebuild endpoint not configured"}), 503

    secret = request.headers.get("X-Rebuild-Secret", "")
    if not secrets.compare_digest(secret, KB_REBUILD_SECRET):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json or {}
    repo = data.get("repo", "unknown")
    pr_number = data.get("pr_number", "")
    pr_title = data.get("pr_title", "")
    branch = data.get("branch", "")
    commit_sha = data.get("commit_sha", "")
    triggered_by = data.get("triggered_by", "")
    changed_files = data.get("changed_files", [])

    # Log trigger event with full PR context for firmware audit trail
    log_entry = {
        "ts": datetime.datetime.utcnow().isoformat(),
        "event": "rebuild_triggered",
        "repo": repo,
        "pr_number": pr_number,
        "pr_title": pr_title,
        "branch": branch,
        "commit_sha": commit_sha,
        "triggered_by": triggered_by,
        "changed_files": changed_files[:50],
    }
    try:
        with open(KB_REBUILD_LOG_PATH, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
    except Exception:
        pass

    # Run the KB builder script
    import subprocess
    build_script = Path(__file__).parent / "build_knowledge_base.py"
    try:
        result = subprocess.run(
            ["python", str(build_script)],
            capture_output=True, text=True, timeout=300,
            env={**os.environ},
        )
        success = result.returncode == 0
        output = (result.stdout + result.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        return jsonify({"error": "KB rebuild timed out"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not success:
        log_entry["event"] = "rebuild_failed"
        log_entry["output"] = output
        with open(KB_REBUILD_LOG_PATH, "a") as f:
            f.write(json.dumps(log_entry) + "\n")
        return jsonify({"error": "KB build failed", "output": output}), 500

    # Invalidate in-memory cache so next request picks up fresh KB
    _invalidate_kb_cache()
    kb = load_knowledge_base()

    # Log success with entry count
    log_entry["event"] = "rebuild_success"
    log_entry["kb_entries"] = len(kb.get("entries", []))
    with open(KB_REBUILD_LOG_PATH, "a") as f:
        f.write(json.dumps(log_entry) + "\n")

    return jsonify({
        "ok": True,
        "kb_entries": len(kb.get("entries", [])),
        "repo": repo,
        "pr_number": pr_number,
        "commit_sha": commit_sha,
    })


# ── Admin stats ────────────────────────────────────────────────────────────────

def compute_stats():
    from collections import Counter

    now = datetime.datetime.utcnow()
    today = now.date()
    week_ago = today - datetime.timedelta(days=7)

    # Conversations
    conversations = []
    if CONVERSATIONS_PATH.exists():
        with open(CONVERSATIONS_PATH) as f:
            for line in f:
                try:
                    conversations.append(json.loads(line.strip()))
                except Exception:
                    pass

    total_convs = len(conversations)
    today_convs = sum(1 for c in conversations if c.get("ts", "")[:10] == str(today))
    week_convs = sum(1 for c in conversations if c.get("ts", "")[:10] >= str(week_ago))
    unique_sessions = len(set(
        c.get("session_id", "") for c in conversations
        if c.get("session_id", "") != "unknown"
    ))

    # Active days
    active_days = len(set(c.get("ts", "")[:10] for c in conversations if c.get("ts", "")))

    # Avg per day (last 7 days)
    avg_7d = round(week_convs / 7, 1)

    # Peak hour
    hour_counts = Counter(
        int(c.get("ts", "T00")[11:13]) for c in conversations if len(c.get("ts", "")) > 12
    )
    peak_hour = hour_counts.most_common(1)[0][0] if hour_counts else 0
    peak_hour_str = f"{peak_hour:02d}:00 - {peak_hour + 1:02d}:00 UTC"

    # Audience breakdown
    audience_counts = Counter(c.get("audience", "installer") for c in conversations)

    # Last 30 days bar chart data
    days_30 = [(today - datetime.timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    day_counts = Counter(c.get("ts", "")[:10] for c in conversations)
    chart_data = [{"date": d, "count": day_counts.get(d, 0)} for d in days_30]

    # Feedback
    feedback_records = []
    if FEEDBACK_PATH.exists():
        with open(FEEDBACK_PATH) as f:
            for line in f:
                try:
                    feedback_records.append(json.loads(line.strip()))
                except Exception:
                    pass
    thumbs_up = sum(1 for fb in feedback_records if fb.get("rating") == "up")
    thumbs_down = sum(1 for fb in feedback_records if fb.get("rating") == "down")
    total_feedback = thumbs_up + thumbs_down
    feedback_rate = round(total_feedback / max(total_convs, 1) * 100, 1)
    satisfaction = round(thumbs_up / max(total_feedback, 1) * 100, 1)

    # KB
    kb = load_knowledge_base()
    kb_entries = len(kb.get("entries", {}))
    kb_sources = kb.get("sources", [])
    resolved_count = 0
    if RESOLVED_PATH.exists():
        with open(RESOLVED_PATH) as f:
            resolved_count = sum(1 for line in f if line.strip())

    return {
        "total_convs": total_convs,
        "today_convs": today_convs,
        "week_convs": week_convs,
        "unique_sessions": unique_sessions,
        "active_days": active_days,
        "avg_7d": avg_7d,
        "peak_hour": peak_hour_str,
        "audience_counts": dict(audience_counts),
        "chart_data": chart_data,
        "thumbs_up": thumbs_up,
        "thumbs_down": thumbs_down,
        "total_feedback": total_feedback,
        "feedback_rate": feedback_rate,
        "satisfaction": satisfaction,
        "kb_entries": kb_entries,
        "kb_sources": kb_sources,
        "resolved_count": resolved_count,
        "as_of": now.strftime("%Y-%m-%d %H:%M UTC"),
    }


# ── Admin routes ───────────────────────────────────────────────────────────────

@app.route("/admin")
def admin():
    admin_user = session.get("admin_user")
    if admin_user != ADMIN_GITHUB_USER:
        return redirect(url_for("admin_login"))
    stats = compute_stats()
    return render_template("admin.html", stats=stats, admin_user=admin_user)


@app.route("/admin/login")
def admin_login():
    if not GITHUB_CLIENT_ID:
        return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Admin Login</title>
  <style>
    body { background: #0f1117; color: #e2e8f0; font-family: system-ui, sans-serif;
           display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
    .card { background: #1a1d27; border-radius: 12px; padding: 40px 48px; text-align: center; }
    h2 { margin: 0 0 12px; font-size: 1.4rem; }
    p { color: #64748b; margin: 0; }
  </style>
</head>
<body>
  <div class="card">
    <h2>Admin Login Unavailable</h2>
    <p>GitHub OAuth is not configured. Set GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET.</p>
  </div>
</body>
</html>
""")

    state = secrets.token_hex(16)
    session["oauth_state"] = state
    params = {
        "client_id": GITHUB_CLIENT_ID,
        "redirect_uri": GITHUB_CALLBACK_URL,
        "scope": "read:user",
        "state": state,
    }
    from urllib.parse import urlencode
    github_url = "https://github.com/login/oauth/authorize?" + urlencode(params)

    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Admin Login — Alarm Agent</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body {
      background: #0f1117;
      color: #e2e8f0;
      font-family: system-ui, -apple-system, sans-serif;
      display: flex;
      align-items: center;
      justify-content: center;
      height: 100vh;
      margin: 0;
    }
    .card {
      background: #1a1d27;
      border-radius: 12px;
      padding: 48px 56px;
      text-align: center;
      border: 1px solid #2a2d3a;
      max-width: 380px;
      width: 100%;
    }
    .logo {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 28px;
    }
    .logo svg rect { fill: #E8500A; }
    .logo-text { font-size: 1.1rem; font-weight: 600; color: #e2e8f0; }
    h2 { margin: 0 0 8px; font-size: 1.3rem; font-weight: 600; }
    p { color: #64748b; margin: 0 0 28px; font-size: 0.9rem; }
    .gh-btn {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      background: #4f8ef7;
      color: #fff;
      border: none;
      border-radius: 8px;
      padding: 12px 24px;
      font-size: 0.95rem;
      font-weight: 500;
      cursor: pointer;
      text-decoration: none;
      transition: background 0.15s;
    }
    .gh-btn:hover { background: #3a7ae0; }
    .gh-btn svg { flex-shrink: 0; }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">
      <svg width="32" height="32" viewBox="0 0 28 28" fill="none">
        <rect width="28" height="28" rx="6" fill="#E8500A"/>
        <path d="M7 14h14M14 7v14" stroke="white" stroke-width="2.5" stroke-linecap="round"/>
      </svg>
      <span class="logo-text">Alarm Agent</span>
    </div>
    <h2>Admin Access</h2>
    <p>Sign in with your GitHub account to continue.</p>
    <a href="{{ github_url }}" class="gh-btn">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor">
        <path d="M12 .5C5.65.5.5 5.65.5 12c0 5.1 3.29 9.41 7.86 10.94.57.1.78-.25.78-.55
                 0-.27-.01-1.17-.01-2.13-3.19.69-3.86-1.37-3.86-1.37-.52-1.32-1.27-1.67-1.27-1.67
                 -1.04-.71.08-.7.08-.7 1.15.08 1.76 1.18 1.76 1.18 1.02 1.75 2.68 1.24 3.33.95
                 .1-.74.4-1.24.72-1.53-2.55-.29-5.23-1.27-5.23-5.67 0-1.25.45-2.27 1.18-3.07
                 -.12-.29-.51-1.46.11-3.04 0 0 .96-.31 3.15 1.18a10.96 10.96 0 012.87-.39c.97
                 .01 1.95.13 2.87.39 2.18-1.49 3.14-1.18 3.14-1.18.63 1.58.24 2.75.12 3.04
                 .74.8 1.18 1.82 1.18 3.07 0 4.41-2.69 5.38-5.25 5.66.41.36.78 1.06.78 2.13
                 0 1.54-.01 2.78-.01 3.16 0 .3.2.66.79.55A11.51 11.51 0 0023.5 12C23.5 5.65
                 18.35.5 12 .5z"/>
      </svg>
      Login with GitHub
    </a>
  </div>
</body>
</html>
""", github_url=github_url)


@app.route("/admin/callback")
def admin_callback():
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or state != session.get("oauth_state"):
        return "Invalid OAuth state.", 400

    # Exchange code for access token
    token_resp = http_requests.post(
        "https://github.com/login/oauth/access_token",
        json={
            "client_id": GITHUB_CLIENT_ID,
            "client_secret": GITHUB_CLIENT_SECRET,
            "code": code,
            "redirect_uri": GITHUB_CALLBACK_URL,
        },
        headers={"Accept": "application/json"},
        timeout=10,
    )
    token_data = token_resp.json()
    access_token = token_data.get("access_token")
    if not access_token:
        return "Failed to obtain access token.", 400

    # Fetch GitHub username
    user_resp = http_requests.get(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        timeout=10,
    )
    github_user = user_resp.json().get("login", "")

    if github_user != ADMIN_GITHUB_USER:
        return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Access Denied</title>
  <style>
    body { background: #0f1117; color: #e2e8f0; font-family: system-ui, sans-serif;
           display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
    .card { background: #1a1d27; border-radius: 12px; padding: 40px 48px; text-align: center;
            border: 1px solid #2a2d3a; }
    h2 { margin: 0 0 12px; color: #ef4444; }
    p { color: #64748b; margin: 0; }
  </style>
</head>
<body>
  <div class="card">
    <h2>Access Denied</h2>
    <p>Your GitHub account is not authorized to access this admin panel.</p>
  </div>
</body>
</html>
"""), 403

    session["admin_user"] = github_user
    session.pop("oauth_state", None)
    return redirect(url_for("admin"))


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8082, debug=False)
