import os
import json
import datetime
from pathlib import Path
from flask import Flask, request, jsonify, render_template, Response, stream_with_context
import boto3

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

_knowledge_base: dict | None = None


def load_knowledge_base() -> dict:
    global _knowledge_base
    if _knowledge_base is None:
        if KB_PATH.exists():
            with open(KB_PATH) as f:
                _knowledge_base = json.load(f)
        else:
            _knowledge_base = {"entries": []}
    return _knowledge_base


def build_context_block() -> str:
    kb = load_knowledge_base()
    entries = kb.get("entries", [])
    lines = []
    for e in entries:
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

        lines.append("\n".join(entry_lines))

    return "\n\n".join(lines)


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


@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.json or {}
    messages = data.get("messages", [])
    audience = data.get("audience", "installer")

    if not messages:
        return jsonify({"error": "No messages provided"}), 400

    kb_context = build_context_block()

    # Get first user text message for RAG lookup
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

    body = json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 4096,
        "system": system,
        "messages": messages,
    })

    def generate():
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8082, debug=False)
