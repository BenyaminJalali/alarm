import os
import json
from pathlib import Path
from flask import Flask, request, jsonify, render_template, Response, stream_with_context
import boto3

app = Flask(
    __name__,
    template_folder=str(Path(__file__).parent.parent / "frontend"),
    static_folder=str(Path(__file__).parent.parent / "frontend" / "static"),
)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
# Claude Sonnet 4.6 on Bedrock
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
KB_PATH = Path(__file__).parent.parent / "data" / "knowledge_base.json"

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


SYSTEM_PROMPT = """You are the Generac R2 Fault Advisor — an internal diagnostic assistant for Generac Home Energy systems (R2 platform: Inverter, Battery Management Unit, Grid Management Switch, and Manta/PLCHE controller).

Your purpose is to help users understand alarms and faults, diagnose root causes, and walk through corrective actions step by step.

## Audience Tiers
Adapt your response depth based on who is asking. The user will identify themselves or you can infer from context:
- **Support/TSE**: Full technical detail — include internal notes, CAN diagnostics, firmware context, exact thresholds
- **Installer/Dealer**: Diagnostic steps + corrective actions — practical, field-serviceable guidance
- **Homeowner**: Plain English only — what it means, what to do, when to call for help (no technical jargon)

If the user does not identify their role, default to **Installer** depth and ask if they need more or less detail.

## Knowledge Base
You have access to the Generac R2 alarm catalog below. Use it as your primary source of truth. When a user describes a symptom or enters a fault code, search the catalog for matching alarms and use the data to explain and guide.

## Response Format
1. **Alarm Identified**: Name and device (INV / BMU / GMS / MANTA)
2. **What this means**: Plain-language explanation of what happened
3. **Why it happens**: Root causes (from engineering trigger/description)
4. **Severity**: Critical / High / Medium / Low and what that means operationally
5. **Corrective Steps**: Numbered, actionable steps
6. **When to escalate**: Be explicit about when field repair is insufficient

## Rules
- Never fabricate alarm codes, thresholds, or steps not in your knowledge base
- If you cannot find a match, say so clearly and ask for more detail
- For Critical (Severity 1) faults, always emphasize safety — power down first
- Always end with: "Does this help? Would you like more detail on any step?"
- Do not expose internal notes to homeowner-tier users

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
    system = SYSTEM_PROMPT.format(kb_context=kb_context)

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
        "max_tokens": 1024,
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


@app.route("/api/health")
def health():
    kb = load_knowledge_base()
    return jsonify({
        "status": "ok",
        "kb_entries": len(kb.get("entries", [])),
        "kb_sources": kb.get("sources", []),
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8082, debug=False)
