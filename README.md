# Vera Bot — magicpin AI Challenge Submission

**Team**: V Veeresh  
**Model**: Google Gemini 1.5 Flash  
**Version**: 1.0.0

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your Gemini API key
set GEMINI_API_KEY=your_key_here        # Windows
export GEMINI_API_KEY=your_key_here     # Linux/Mac

# 3. Run the bot
python bot.py
# → Listening on http://localhost:8080

# 4. Run the judge simulator (in another terminal)
python judge_simulator.py
```

---

## Approach

### Architecture
Single-prompt composer with trigger-kind awareness, powered by Gemini 1.5 Flash at `temperature=0` for determinism.

### Context Handling
- All 4 context types (category, merchant, trigger, customer) stored in-memory with version tracking
- Idempotency: same version re-push returns `409 stale_version`
- Atomic version replacement on version bump

### Message Composition
Each message is composed by feeding the full 4-context payload to Gemini with a strict system prompt that enforces:
- **Specificity**: use real numbers from context, never generic "% off"
- **Category voice**: dentist=clinical-peer, salon=warm, restaurant=operator, gym=coaching, pharmacy=precise
- **Hindi-English code-mix**: auto-detected from merchant's `languages` field
- **Single CTA**: binary YES/STOP for action triggers, open-ended for info
- **No URLs** (Meta policy)

### Trigger Prioritization
Triggers are scored by urgency (1-5) × kind importance before composing. Max 5 actions per tick to avoid spam.

### Compulsion Levers Used
1. Specificity / verifiability (real numbers, source citations)
2. Loss aversion ("you're missing X")
3. Effort externalization ("I've drafted it — just say go")
4. Curiosity hooks
5. Single binary CTA

### Reply Handling
State machine with 4 paths:
- **Auto-reply**: turn 1 → prompt owner; turn 2 → wait 4h; turn 3+ → end
- **Hostile/opt-out**: graceful one-line exit, suppress conversation
- **Committed intent** ("let's do it"): immediately switch to action mode, no more qualifying
- **Normal**: Gemini generates contextual follow-up

### What would have helped
- Real-time merchant WhatsApp session state (to know if window is active)
- Offer catalog with pricing from magicpin's actual inventory
- Historical send performance per trigger kind per category
- Customer-level conversation history (not just merchant-level)

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/v1/healthz` | Health check + context counts |
| GET | `/v1/metadata` | Team info + model |
| POST | `/v1/context` | Accept category/merchant/customer/trigger context |
| POST | `/v1/tick` | Decide which triggers to act on and compose messages |
| POST | `/v1/reply` | Handle merchant/customer replies |
