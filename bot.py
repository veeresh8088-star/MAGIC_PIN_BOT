#!/usr/bin/env python3
"""
magicpin AI Challenge — Vera Bot Server
========================================
Powered by Google Gemini. Implements all 5 required endpoints.

SETUP:
  pip install fastapi uvicorn google-generativeai
  set GEMINI_API_KEY=your_key_here        (Windows)
  python bot.py

ENDPOINTS:
  GET  /v1/healthz
  GET  /v1/metadata
  POST /v1/context
  POST /v1/tick
  POST /v1/reply
"""

import os, json, time, re, hashlib
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import groq
import uvicorn

# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = "llama-3.3-70b-versatile"
TEAM_NAME      = "V Veeresh"
TEAM_MEMBERS   = ["V Veeresh"]
CONTACT_EMAIL  = "veereshv2004@gmail.com"
BOT_VERSION    = "1.0.0"
START_TIME     = time.time()

# ─────────────────────────────────────────────────────────────────
# GROQ SETUP
# ─────────────────────────────────────────────────────────────────
_groq_client = None

def get_client():
    global _groq_client
    if _groq_client is None and GROQ_API_KEY:
        _groq_client = groq.Groq(api_key=GROQ_API_KEY)
    return _groq_client

def call_llm(prompt: str, system: str = "") -> str:
    """Call Groq and return the text response."""
    client = get_client()
    if not client:
        return '{"error": "GROQ_API_KEY not set"}'
    
    for attempt in range(5):
        try:
            resp = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.0,
                max_tokens=800,
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            if "429" in str(e):
                wait_time = (attempt + 1) * 2
                print(f"Rate limited (429). Retrying in {wait_time}s...")
                time.sleep(wait_time)
                continue
            return f'{{"error": "{str(e)}"}}'
    return '{"error": "Max retries exceeded for Groq API"}'

# ─────────────────────────────────────────────────────────────────
# IN-MEMORY CONTEXT STORE
# ─────────────────────────────────────────────────────────────────
store: Dict[str, Dict] = {
    "category": {},    # slug -> {version, payload}
    "merchant": {},    # merchant_id -> {version, payload}
    "customer": {},    # customer_id -> {version, payload}
    "trigger":  {},    # trigger_id -> {version, payload}
}

# Conversation state: conv_id -> {merchant_id, customer_id, trigger_id, history, suppressed}
conversations: Dict[str, Dict] = {}

# Suppression set: keys that have been acted on this session
suppressed_keys: set = set()

def get_context(scope: str, cid: str) -> Optional[Dict]:
    return store.get(scope, {}).get(cid, {}).get("payload")

def get_version(scope: str, cid: str) -> int:
    return store.get(scope, {}).get(cid, {}).get("version", 0)

# ─────────────────────────────────────────────────────────────────
# AUTO-REPLY DETECTION
# ─────────────────────────────────────────────────────────────────
AUTO_REPLY_PATTERNS = [
    "thank you for contacting",
    "our team will respond",
    "we will get back to you",
    "automated message",
    "automated assistant",
    "this is an automated",
    "aapki madad ke liye shukriya",
    "main ek automated",
]

def is_auto_reply(msg: str) -> bool:
    m = msg.lower().strip()
    return any(p in m for p in AUTO_REPLY_PATTERNS)

# ─────────────────────────────────────────────────────────────────
# INTENT DETECTION
# ─────────────────────────────────────────────────────────────────
INTENT_COMMIT_PATTERNS = [
    "let's do it", "lets do it", "ok let's", "ok lets", "go ahead",
    "yes please", "sounds good", "confirm", "i want to join",
    "mujhe judrna hai", "haan", "bilkul", "zaroor", "chalo karte hain",
    "what's next", "whats next", "aage kya", "next step"
]

HOSTILE_PATTERNS = [
    "stop messaging", "not interested", "don't contact", "remove me",
    "useless", "spam", "irritating", "stop it", "band karo",
    "mat karo", "zaroorat nahi", "nahi chahiye"
]

def detect_intent(msg: str) -> str:
    """Returns: 'commit' | 'hostile' | 'auto_reply' | 'normal'"""
    if is_auto_reply(msg):
        return "auto_reply"
    m = msg.lower()
    if any(p in m for p in HOSTILE_PATTERNS):
        return "hostile"
    if any(p in m for p in INTENT_COMMIT_PATTERNS):
        return "commit"
    return "normal"

# ─────────────────────────────────────────────────────────────────
# TRIGGER PRIORITY SCORER
# ─────────────────────────────────────────────────────────────────
def score_trigger_priority(trigger: Dict, merchant: Dict) -> float:
    """Score a trigger for sending priority (higher = send first)."""
    urgency = trigger.get("urgency", 1)
    kind = trigger.get("kind", "")

    # Urgency is the main driver
    score = urgency * 10.0

    # Boost by kind importance
    kind_boost = {
        "regulation_change": 5,
        "supply_alert": 5,
        "renewal_due": 4,
        "perf_dip": 3,
        "active_planning_intent": 3,
        "recall_due": 3,
        "winback_eligible": 2,
        "competitor_opened": 2,
        "festival_upcoming": 1,
        "research_digest": 1,
        "curious_ask_due": 0,
    }
    score += kind_boost.get(kind, 0)

    # Merchant signals
    signals = merchant.get("signals", [])
    if "engaged_in_last_24h" in signals or "engaged_in_last_48h" in signals:
        score += 3

    return score

# ─────────────────────────────────────────────────────────────────
# MESSAGE COMPOSER (the core intelligence)
# ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Vera, magicpin's elite merchant AI assistant messaging Indian merchants over WhatsApp.

STRICT RULES TO MAXIMIZE QUALITY:
1. EXTREME SPECIFICITY (CRITICAL): You must weave in exact numbers, exact offer prices (e.g., "₹299 cleaning", not "discount"), exact metrics (e.g., "50% drop", "124 high-risk adults"), and exact dates. Do not generalize. If a specific stat or payload exists, YOU MUST USE IT.
2. ENGAGEMENT COMPULSION (CRITICAL): Force engagement by framing the message using ONE distinct psychological lever:
   - Loss Aversion: "You are missing out on X bookings compared to your peers..."
   - Social Proof: "Top merchants in your area are seeing Y..."
   - Effort Externalization: "I have already analyzed this and drafted the campaign. Just reply YES to activate."
   - Curiosity: "Do you know why your views dropped yesterday?"
3. CATEGORY FIT (CRITICAL): Embody the persona completely:
   - dentists: clinical, peer-reviewed, scientific, use "Dr."
   - salons: warm, practical, trend-focused
   - restaurants: fast-paced, operator-to-operator, focus on table turns
   - gyms: coaching, energetic, results-driven
   - pharmacies: precise, trustworthy, fast
4. MERCHANT FIT (CRITICAL): Prove you know them. Mention their exact locality, their past review themes (e.g., "customers love your ambiance"), or their specific active offers.
5. NO PREAMBLE: Never say "I hope you're doing well" or re-introduce yourself. Start with the hook immediately.
6. SINGLE STRONG CTA: End with one clear action: a binary YES/NO, or an open-ended question.
7. LANGUAGE: Use Hindi-English mix (code-mix) only if the merchant's languages include "hi".
8. NEVER fabricate data: use only provided context.
9. Return ONLY valid JSON, no markdown.

OUTPUT FORMAT (strict JSON):
{
  "body": "<the WhatsApp message text>",
  "cta": "<binary_yes_no | open_ended | none | multi_choice_slot>",
  "send_as": "<vera | merchant_on_behalf>",
  "suppression_key": "<unique key to prevent duplicate sends>",
  "rationale": "<1-2 sentences: why this message, what lever it uses>"
}"""

def build_compose_prompt(category: Dict, merchant: Dict, trigger: Dict, customer: Optional[Dict] = None) -> str:
    identity   = merchant.get("identity", {})
    perf       = merchant.get("performance", {})
    signals    = merchant.get("signals", [])
    offers     = [o for o in merchant.get("offers", []) if o.get("status") == "active"]
    cust_agg   = merchant.get("customer_aggregate", {})
    conv_hist  = merchant.get("conversation_history", [])
    reviews    = merchant.get("review_themes", [])
    voice      = category.get("voice", {})
    digest     = category.get("digest", [])
    peer_stats = category.get("peer_stats", {})

    # Build last conversation context
    last_turns = ""
    if conv_hist:
        last_turns = "\n".join([f"  [{t['from'].upper()}] {t['body'][:120]}" for t in conv_hist[-3:]])

    # Build trigger summary
    payload = trigger.get("payload", {}).copy()
    if trigger.get("kind") == "perf_dip" and "delta_pct" in payload:
        try:
            val = float(payload["delta_pct"])
            payload["delta_pct"] = f"{abs(val) * 100:.1f}%"
        except (ValueError, TypeError):
            pass

    trig_summary = f"Kind: {trigger.get('kind')} | Urgency: {trigger.get('urgency')}/5 | Payload: {json.dumps(payload)[:400]}"

    # Build customer section
    cust_section = "None (merchant-facing message)"
    if customer:
        ci = customer.get("identity", {})
        rel = customer.get("relationship", {})
        cust_section = (
            f"Name: {ci.get('name')}, Language: {ci.get('language_pref', 'en')}, "
            f"State: {customer.get('state')}, Last visit: {rel.get('last_visit')}, "
            f"Visits: {rel.get('visits_total')}, Services: {rel.get('services_received', [])}"
        )

    return f"""COMPOSE A MERCHANT MESSAGE:

=== CATEGORY ({category.get('slug', '?')}) ===
Voice/tone: {voice.get('tone', 'professional')}
Taboo words: {voice.get('vocab_taboo', [])}
Peer stats: avg_rating={peer_stats.get('avg_rating')}, avg_ctr={peer_stats.get('avg_ctr')}
Digest items: {json.dumps(digest[:3])[:500]}

=== MERCHANT ===
Name: {identity.get('name')} | Owner: {identity.get('owner_first_name')}
City: {identity.get('city')}, Locality: {identity.get('locality')}
Verified: {identity.get('verified')} | Languages: {identity.get('languages', ['en'])}
Subscription: {merchant.get('subscription', {}).get('status')} ({merchant.get('subscription', {}).get('days_remaining')}d remaining)
Performance (30d): views={perf.get('views')}, calls={perf.get('calls')}, ctr={perf.get('ctr')} | 7d delta: {perf.get('delta_7d', {})}
Active offers: {[o.get('title') for o in offers]}
Signals: {signals}
Customer aggregate: {json.dumps(cust_agg)[:200]}
Review themes: {[r.get('theme')+'('+r.get('sentiment')+')' for r in reviews]}
Recent conversation:
{last_turns or '  (no prior conversation)'}

=== TRIGGER ===
{trig_summary}

=== CUSTOMER (if any) ===
{cust_section}

Now compose the best possible message. 
- You MUST use the compulsion levers: specificity, loss aversion, social proof, effort externalization, or curiosity. 
- You MUST include exact numbers from the trigger payload or performance stats.
- You MUST adopt the category voice perfectly.
- Do NOT fabricate data."""


def compose_message(category: Dict, merchant: Dict, trigger: Dict, customer: Optional[Dict] = None) -> Dict:
    """Core compose function — calls Gemini and returns structured message."""
    prompt = build_compose_prompt(category, merchant, trigger, customer)
    raw = call_llm(prompt, SYSTEM_PROMPT)

    # Extract JSON from response
    try:
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            result = json.loads(match.group())
            # Validate required fields
            for field in ["body", "cta", "send_as", "suppression_key", "rationale"]:
                if field not in result:
                    result[field] = _fallback_field(field, merchant, trigger)
            return result
    except Exception:
        pass

    # Fallback if Gemini fails or returns bad JSON
    return _fallback_compose(merchant, trigger)


def _fallback_field(field: str, merchant: Dict, trigger: Dict) -> str:
    defaults = {
        "cta": "open_ended",
        "send_as": "vera",
        "suppression_key": f"fallback:{trigger.get('id','?')}",
        "rationale": "Fallback compose used.",
        "body": f"Hi {merchant.get('identity',{}).get('owner_first_name','')}, wanted to share a quick update. Can we connect?"
    }
    return defaults.get(field, "")


def _fallback_compose(merchant: Dict, trigger: Dict) -> Dict:
    name = merchant.get("identity", {}).get("owner_first_name", "")
    kind = trigger.get("kind", "update")
    return {
        "body": f"Hi {name}, wanted to share something about your {kind.replace('_',' ')}. Can we talk for a minute?",
        "cta": "open_ended",
        "send_as": "vera",
        "suppression_key": f"fallback:{trigger.get('id', hashlib.md5(str(time.time()).encode()).hexdigest()[:8])}",
        "rationale": "Fallback compose: Gemini unavailable or returned invalid JSON."
    }

# ─────────────────────────────────────────────────────────────────
# REPLY HANDLER
# ─────────────────────────────────────────────────────────────────

CUSTOMER_REPLY_SYSTEM = """You are an AI assistant replying on behalf of a local merchant directly to a customer on WhatsApp.

RULES:
1. Speak as the merchant (or their helpful assistant) answering the customer.
2. Acknowledge their message (e.g., confirm booking, answer question).
3. Be polite, warm, and extremely concise (under 50 words).
4. Do NOT address the merchant. Address the customer.
5. Return ONLY valid JSON, no markdown.

OUTPUT FORMAT:
{
  "action": "<send | wait | end>",
  "body": "<message text — only if action=send>",
  "cta": "<none | open_ended>",
  "wait_seconds": <number — only if action=wait>,
  "rationale": "<1 sentence why>"
}"""

def _handle_customer_reply(conv: Dict, merchant_id: str, customer_id: Optional[str], message: str, turn: int) -> Dict:
    merchant = get_context("merchant", merchant_id) or {}
    customer = get_context("customer", customer_id) if customer_id else {}
    
    merchant_name = merchant.get("identity", {}).get("name", "the merchant")
    customer_name = customer.get("identity", {}).get("name", "Customer")
    
    history_text = "\n".join([
        f"  [{h['role'].upper()} T{h['turn']}] {h['body'][:100]}"
        for h in conv.get("history", [])[-5:]
    ])

    prompt = f"""CUSTOMER REPLY — decide how to respond:

Merchant: {merchant_name}
Customer: {customer_name}
Turn: {turn}

Conversation so far:
{history_text or '  (start of conversation)'}

LATEST CUSTOMER MESSAGE (turn {turn}):
"{message}"

Compose a reply to the customer on behalf of {merchant_name}. Address the customer directly."""

    raw = call_llm(prompt, CUSTOMER_REPLY_SYSTEM)

    try:
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            result = json.loads(match.group())
            if result.get("action") == "send" and result.get("body"):
                conv["history"].append({"role": "merchant_on_behalf", "turn": turn + 1, "body": result["body"]})
            return result
    except Exception:
        pass

    fallback_body = "Thanks for reaching out! We've received your message and will get back to you shortly."
    conv["history"].append({"role": "merchant_on_behalf", "turn": turn + 1, "body": fallback_body})
    return {
        "action": "send",
        "body": fallback_body,
        "cta": "none",
        "rationale": "Fallback customer reply."
    }

REPLY_SYSTEM = """You are Vera, magicpin's merchant AI assistant responding to a WhatsApp reply.

RULES:
1. If merchant committed ("yes let's do it", "go ahead") → switch immediately to ACTION mode, execute the next step, no more qualifying.
2. If auto-reply detected → first time: one prompt for owner; second time: wait 4h; third time: end.
3. If hostile/opt-out → end the conversation gracefully with one sentence max.
4. If off-topic question → decline politely in one line, redirect to the original topic.
5. Be concise (under 80 words). No preamble. Use their language.
6. Return ONLY valid JSON, no markdown.

OUTPUT FORMAT:
{
  "action": "<send | wait | end>",
  "body": "<message text — only if action=send>",
  "cta": "<binary_yes_no | open_ended | none>",
  "wait_seconds": <number — only if action=wait>,
  "rationale": "<1 sentence why>"
}"""

def build_reply_prompt(conv: Dict, merchant: Dict, category: Dict, message: str, turn: int) -> str:
    identity = merchant.get("identity", {})
    history_text = "\n".join([
        f"  [{h['role'].upper()} T{h['turn']}] {h['body'][:100]}"
        for h in conv.get("history", [])[-5:]
    ])
    auto_count = conv.get("auto_reply_count", 0)

    return f"""MERCHANT REPLY — decide how to respond:

Merchant: {identity.get('name')} ({identity.get('city')})
Category: {category.get('slug', '?')} | Voice: {category.get('voice', {}).get('tone', 'professional')}
Turn: {turn} | Auto-reply count so far: {auto_count}

Conversation so far:
{history_text or '  (start of conversation)'}

LATEST MERCHANT MESSAGE (turn {turn}):
"{message}"

Choose: send (reply with content), wait (back off N seconds), or end (close conversation)."""


def handle_reply(conv_id: str, merchant_id: str, customer_id: Optional[str],
                 message: str, turn: int, from_role: str = "merchant") -> Dict:
    """Decide how to respond to a merchant/customer reply."""

    # Get or create conversation
    if conv_id not in conversations:
        conversations[conv_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "history": [],
            "auto_reply_count": 0,
            "suppressed": False,
        }
    conv = conversations[conv_id]

    # Suppressed conversation → always end
    if conv.get("suppressed"):
        return {"action": "end", "rationale": "Conversation was previously suppressed (opt-out or ended)."}

    conv["history"].append({"role": from_role, "turn": turn, "body": message})

    if from_role == "customer":
        return _handle_customer_reply(conv, merchant_id, customer_id, message, turn)

    # Detect intent
    intent = detect_intent(message)

    # ── Hostile → end immediately
    if intent == "hostile":
        conv["suppressed"] = True
        return {
            "action": "send",
            "body": "Apologies — won't message again. If anything changes, just reply 'Hi Vera'. 🙏",
            "cta": "none",
            "rationale": "Merchant opted out explicitly. Closing and suppressing."
        }

    # ── Auto-reply handling
    if intent == "auto_reply":
        conv["auto_reply_count"] = conv.get("auto_reply_count", 0) + 1
        auto_count = conv["auto_reply_count"]

        if auto_count == 1:
            return {
                "action": "send",
                "body": "Looks like an auto-reply 😊 When the owner sees this, just reply 'Yes' to continue.",
                "cta": "binary_yes_no",
                "rationale": "First auto-reply detected; one prompt to reach the owner."
            }
        elif auto_count == 2:
            return {
                "action": "wait",
                "wait_seconds": 14400,
                "rationale": "Same auto-reply twice; owner not at phone. Backing off 4h."
            }
        else:
            conv["suppressed"] = True
            return {
                "action": "end",
                "rationale": f"Auto-reply {auto_count}x in a row with no real response. Closing conversation."
            }

    # ── Committed intent → action mode
    if intent == "commit":
        merchant = get_context("merchant", merchant_id) or {}
        category = get_context("category", merchant.get("category_slug", "")) or {}
        identity = merchant.get("identity", {})
        name = identity.get("owner_first_name", "")
        active_offers = [o.get("title") for o in merchant.get("offers", []) if o.get("status") == "active"]

        if not active_offers:
            body = "Great! Moving to action now. I'll draft the GBP post right away — give me 60 seconds."
        else:
            body = f"Great! Moving to action now. Proceeding with your {active_offers[0]} campaign. Reply CONFIRM to activate."
        result = {
            "action": "send",
            "body": body,
            "cta": "binary_yes_no",
            "rationale": "Merchant committed; switched from qualifying to action mode immediately."
        }
        conv["history"].append({"role": "vera", "turn": turn + 1, "body": body})
        return result

    # ── Normal reply → call Gemini
    merchant = get_context("merchant", merchant_id) or {}
    category = get_context("category", merchant.get("category_slug", "")) or {}

    prompt = build_reply_prompt(conv, merchant, category, message, turn)
    raw = call_llm(prompt, REPLY_SYSTEM)

    try:
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            result = json.loads(match.group())
            if result.get("action") == "send" and result.get("body"):
                conv["history"].append({"role": "vera", "turn": turn + 1, "body": result["body"]})
            return result
    except Exception:
        pass

    # Fallback reply
    return {
        "action": "send",
        "body": "Thanks for your message! Let me pull that up and get back to you in a moment.",
        "cta": "none",
        "rationale": "Fallback reply: Gemini unavailable."
    }

# ─────────────────────────────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────────────────────────────
app = FastAPI(title="Vera Bot", version=BOT_VERSION)


@app.get("/v1/healthz")
def healthz():
    return {
        "status": "ok",
        "uptime_seconds": round(time.time() - START_TIME),
        "contexts_loaded": {
            "category": len(store["category"]),
            "merchant":  len(store["merchant"]),
            "customer":  len(store["customer"]),
            "trigger":   len(store["trigger"]),
        }
    }


@app.get("/v1/metadata")
def metadata():
    return {
        "team_name":    TEAM_NAME,
        "team_members": TEAM_MEMBERS,
        "model":        f"groq/{GROQ_MODEL}",
        "approach":     "Gemini-powered single-prompt composer with trigger-kind routing, auto-reply detection, and intent-transition handling.",
        "contact_email": CONTACT_EMAIL,
        "version":      BOT_VERSION,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/v1/context")
async def push_context(request: Request):
    body = await request.json()
    scope      = body.get("scope")
    context_id = body.get("context_id")
    version    = body.get("version", 1)
    payload    = body.get("payload", {})
    delivered  = body.get("delivered_at", datetime.now(timezone.utc).isoformat())

    if scope not in store:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": f"unknown_scope:{scope}"})

    current_ver = get_version(scope, context_id)

    # Idempotency: reject same version
    if current_ver >= version:
        return JSONResponse(status_code=409, content={
            "accepted": False, "reason": "stale_version", "current_version": current_ver
        })

    store[scope][context_id] = {"version": version, "payload": payload}
    ack_id = f"ack_{context_id}_v{version}"

    return {
        "accepted": True,
        "ack_id": ack_id,
        "stored_at": datetime.now(timezone.utc).isoformat()
    }


@app.post("/v1/tick")
async def tick(request: Request):
    body = await request.json()
    now_str  = body.get("now", datetime.now(timezone.utc).isoformat())
    avail    = body.get("available_triggers", [])

    actions = []

    # Score and sort triggers by priority
    scored = []
    for tid in avail:
        trigger = get_context("trigger", tid)
        if not trigger:
            continue

        supp_key = trigger.get("suppression_key", tid)
        if supp_key in suppressed_keys:
            continue

        merchant_id = trigger.get("merchant_id")
        merchant = get_context("merchant", merchant_id) if merchant_id else None
        if not merchant:
            continue

        priority = score_trigger_priority(trigger, merchant)
        scored.append((priority, tid, trigger, merchant_id, merchant))

    # Sort descending by priority — process top triggers
    scored.sort(key=lambda x: x[0], reverse=True)

    for priority, tid, trigger, merchant_id, merchant in scored[:5]:  # max 5 actions per tick
        category_slug = merchant.get("category_slug", "")
        category = get_context("category", category_slug) or {}

        customer_id = trigger.get("customer_id")
        customer = get_context("customer", customer_id) if customer_id else None

        # Compose the message
        composed = compose_message(category, merchant, trigger, customer)

        # Build conversation ID
        conv_id = f"conv_{merchant_id}_{tid}"

        # Track suppression
        suppressed_keys.add(trigger.get("suppression_key", tid))

        # Store in conversation history
        conversations[conv_id] = {
            "merchant_id": merchant_id,
            "customer_id": customer_id,
            "history": [{"role": "vera", "turn": 1, "body": composed.get("body", "")}],
            "auto_reply_count": 0,
            "suppressed": False,
        }

        action = {
            "conversation_id":  conv_id,
            "merchant_id":      merchant_id,
            "customer_id":      customer_id,
            "send_as":          composed.get("send_as", "vera"),
            "trigger_id":       tid,
            "template_name":    f"vera_{trigger.get('kind','msg')}_v1",
            "template_params":  [
                merchant.get("identity", {}).get("owner_first_name", ""),
                composed.get("body", "")[:200],
            ],
            "body":             composed.get("body", ""),
            "cta":              composed.get("cta", "open_ended"),
            "suppression_key":  trigger.get("suppression_key", tid),
            "rationale":        composed.get("rationale", ""),
        }
        actions.append(action)

    return {"actions": actions}


@app.post("/v1/reply")
async def reply(request: Request):
    body = await request.json()
    conv_id     = body.get("conversation_id", "")
    merchant_id = body.get("merchant_id", "")
    customer_id = body.get("customer_id")
    message     = body.get("message", "")
    turn        = body.get("turn_number", 2)
    from_role   = body.get("from_role", "merchant")

    result = handle_reply(conv_id, merchant_id, customer_id, message, turn, from_role)
    return result


# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if not GROQ_API_KEY:
        print("\nWARNING: GROQ_API_KEY is not set!")
        print("  Set it with: set GROQ_API_KEY=your_key_here\n")
    else:
        print(f"\nGroq configured ({GROQ_MODEL})")

    print(f"Starting Vera Bot v{BOT_VERSION}")
    print(f"Listening on http://localhost:8080\n")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
