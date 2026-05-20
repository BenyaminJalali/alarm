# CLAUDE.md — R2 Fault Advisor Project Context

## What This Is
AI troubleshooting agent for Generac Home Energy R2 platform. Live at alarm.dash-ly.com. Internal fleet team testing now, Field Pro dealer rollout is Phase 2.

## Stack
- Flask + AWS Bedrock (boto3) — Claude Sonnet 4.6 via `us.anthropic.claude-sonnet-4-6`
- SSE streaming responses
- Docker on Lightsail 3.143.89.105 (port 8083)
- Nginx reverse proxy with `proxy_buffering off` for SSE
- GitHub Actions auto-deploys on every push to master

## Key Files
- `backend/app.py` — Flask app, system prompt, Bedrock streaming, feedback endpoint
- `backend/build_knowledge_base.py` — Builds KB from Alarms.xlsx + GitHub repos at startup
- `data/Alarms.xlsx` — Theresa's masterlist (637 alarms + 63 MI alarms = 700 rows)
- `data/knowledge_base.json` — Built at container startup, stored in Docker named volume `alarm_data`
- `frontend/index.html` — Chat UI
- `frontend/static/js/chat.js` — SSE streaming, feedback buttons
- `.github/workflows/deploy.yml` — Auto-deploy to Lightsail on push

## Knowledge Base Sources (1,020 total entries)
1. `Alarms.xlsx` — Theresa's masterlist, sheets: "Alarms Masterlist" + "MI List"
2. `generacclean/generac-home-error-catalog` — 240 YAML files
3. `neurio/pwrinverter` ExtendedDescriptions — 112 .md files
4. `neurio/pwrbmu` ExtendedDescriptions — 102 .md files
5. `neurio/reef` headend-events.json — 43 GMS/Manta events

## IAM / Credentials
- IAM user: `alarm-agent-prod` (policy: `alarm-agent-bedrock-only` — Resource: * for Bedrock invoke)
- Root keys have been replaced with scoped keys on the server
- `.env` on server at `/home/ubuntu/alarm/.env` — never committed to git
- GitHub token — stored in `.env` on server only, SAML-authorized for Generac enterprise

## SSH Access
- Key: `C:\Users\BJalali\.ssh\LightsailDefaultKey-us-east-2.pem`
- Host: `ubuntu@3.143.89.105`

## Three Audience Tiers
- Homeowner — plain English, no jargon, 3-5 sentences
- Installer/Dealer — step-by-step technical, numbered, direct
- Support/TSE — full depth, internal notes, thresholds, cascading alarms (most technical)

## Pending / Next Steps
1. **RAG feedback loop** — 👍 clicks save resolved conversations, future similar questions pull from them automatically (like Zendesk AI). This is the "training" the user wants — no model fine-tuning, just growing resolved case library.
2. **Image upload** — Bedrock supports vision natively. Installer photos error screen or wiring → agent diagnoses. Manuals needed as reference.
3. **Power Automate flow** — Theresa's SharePoint Excel auto-pushes to GitHub when she saves → KB always current
4. **Auth** — Lock to @generac.com before wide rollout (same GitHub OAuth as PR dashboard)
5. **Langfuse** — Observability, traces, cost per conversation (mentioned in Phil Swanson memo)

## Important Context
- Pod 3 of Phase 1 AI roadmap (Phil Swanson → Gaurang Kavaiya memo, May 11 2026)
- Success metrics: ECDC (Error Code Documentation Completeness), FTD (FW Time to Documentation)
- Theresa Nguyen = Domain SME, Jacob Hagler = Eval/QA
- R2 uses CAN bus cables — NOT RJ45/ethernet. Never confuse these.
- "Battery" always means BMU energy storage, never MI (micro-inverter)
- Port 8082 is taken by cost-estimator, alarm uses 8083
