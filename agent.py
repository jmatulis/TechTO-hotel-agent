"""
Fast & Frugal Hotel Agent v2  -  TechTO Hackathon, Oct 17 2026

Pipeline (one process, one event loop):
  parse prompt (regex, LLM only as fallback)
    -> SerpApi google_hotels: one search, sorted by price (0 tokens)
    -> rank by price vs rating (0 tokens)
    -> look up phone + address for the top K (parallel SerpApi calls)
    -> verification calls to the top K IN PARALLEL (Twilio STT/TTS, LLM per turn)
         early stop: once the best-ranked hotel that can still win is verified,
         the remaining calls are hung up
    -> email a full confirmation record (winner, every option checked, transcript)

Run:  python agent.py "Find the best hotel deal in Toronto for 2026-10-18 to 2026-10-19, send to judge@x.com"
Test: set PHONE_OVERRIDE=<your cell> -> every call goes to you, one at a time, best first.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import smtplib
import sys
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from html import escape

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import Response

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

def E(key: str, default: str = "") -> str:
    """os.environ.get, but an empty value in .env falls back to the default rather
    than shadowing it (an empty GUEST_NAME used to put a hole in the opener)."""
    return os.environ.get(key) or default


# ---------------------------------------------------------------- config
PUBLIC_URL = E("PUBLIC_URL", "").rstrip("/")          # ngrok / cloudflared URL -> PORT
PORT = int(E("PORT", "8080"))

BB_BASE = E("BACKBOARD_BASE_URL", "https://app.backboard.io/api").rstrip("/")
BB_KEY = E("BACKBOARD_API_KEY", "")
BB_PROVIDER = E("BACKBOARD_PROVIDER", "cerebras")     # fastest of the 12; see models/providers
BB_MODEL = E("BACKBOARD_MODEL", "openai/gpt-oss-120b")  # 0.8s/turn measured; verify it still routes

SERPAPI_KEY = E("SERPAPI_KEY", "")
CURRENCY = E("CURRENCY", "CAD")
GL = E("SERP_GL", "ca")

TW_SID = E("TWILIO_ACCOUNT_SID", "")
TW_TOKEN = E("TWILIO_AUTH_TOKEN", "")
TW_FROM = E("TWILIO_FROM", "")
PHONE_OVERRIDE = E("PHONE_OVERRIDE", "")              # set -> all calls go here, sequentially (testing / judge line)
TOP_K = int(E("TOP_K", "3"))                          # how many hotels to verify by phone
VOICE = E("TTS_VOICE", "Polly.Joanna-Neural")          # fallback only, when ElevenLabs is off or fails

# ElevenLabs TTS (sponsor stack). Raw TTS over HTTP only - never their Agents product.
EL_KEY = E("ELEVEN_API_KEY", "")
EL_VOICE = E("ELEVEN_VOICE_ID", "NOpBlnGInO9m6vDvFkFC")   # Spuds Oxley; see GET /v1/voices
# Library voices carry their own (slow) saved settings; sending voice_settings overrides them.
# speed 1.2 cut the opener from 30.7s to 17.3s of speech = less billed call time per run.
# ElevenLabs rejects anything outside 0.7-1.2 with a 400 (then every line falls back to <Say>)
EL_SPEED = min(1.2, max(0.7, float(E("ELEVEN_SPEED", "1.2"))))
EL_STABILITY = float(E("ELEVEN_STABILITY", "0.5"))
EL_SIMILARITY = float(E("ELEVEN_SIMILARITY", "0.75"))
EL_MODEL = E("ELEVEN_MODEL", "eleven_flash_v2_5")         # flash = lowest latency tier
EL_FORMAT = E("ELEVEN_FORMAT", "mp3_22050_32")            # small file -> Twilio fetches it fast
EL_MODE = E("ELEVEN_MODE", "all").lower()                 # all | opener | off
EL_TIMEOUT = float(E("ELEVEN_TIMEOUT", "4"))              # past this we fall back rather than stall the call
SPEECH_TIMEOUT = E("SPEECH_TIMEOUT", "auto")          # "auto" or seconds, e.g. "1"
BARGE_IN = E("BARGE_IN", "0") == "1"                  # 1 = the hotel can cut the agent off mid-sentence
MAX_TURNS = int(E("MAX_TURNS", "6"))
CALL_TIMEOUT = float(E("CALL_TIMEOUT", "120"))        # per call attempt, incl. ringing (our side)
CALL_MAX_SECONDS = int(E("CALL_MAX_SECONDS", "60"))   # Twilio hangs up after this long connected
BYE = "Perfect, thanks. Goodbye."                     # fixed: its TTS is built once, reused every call
DIAL_RETRIES = int(E("DIAL_RETRIES", "1"))            # redials on busy / no answer
RECORD_CALLS = E("RECORD_CALLS", "1") == "1"          # Twilio records each call; the winner's mp3 is emailed
HOLD_ON_CALL = E("HOLD_ON_CALL", "0") == "1"          # 1 -> accept a hold/reservation if offered

GUEST_NAME = E("GUEST_NAME", "Jordan Fulawka")
JUDGE_EMAIL = E("JUDGE_EMAIL", "")
RESEND_KEY = E("RESEND_API_KEY", "")                  # fast path (HTTP); needs verified domain
MAIL_FROM = E("MAIL_FROM", "")
SMTP_HOST = E("SMTP_HOST", "smtp.gmail.com")
SMTP_USER = E("SMTP_USER", "")
SMTP_PASS = E("SMTP_PASS", "")                        # Gmail app password

RETRYABLE = {"busy", "no-answer"}
TERMINAL = {"verified", "unavailable", "unclear", "no_answer", "unreachable",
            "timeout", "no_phone", "cancelled", "error"}
FACT_KEYS = ("available", "price", "fees", "room_type", "cancellation", "check_in_time",
             "reference", "staff_name")


# ---------------------------------------------------------------- state
class Candidate:
    def __init__(self, idx: int, prop: dict, price: float, score: float):
        self.idx = idx
        self.score = score
        self.name = prop.get("name") or "Unknown hotel"
        self.price = round(float(price), 2)
        self.total = (prop.get("total_rate") or {}).get("extracted_lowest")
        self.rating = prop.get("overall_rating")
        self.reviews = prop.get("reviews")
        self.hotel_class = prop.get("hotel_class")
        self.link = prop.get("link")
        self.serp_link = prop.get("serpapi_property_details_link")
        self.token = prop.get("property_token")
        self.check_in_time = prop.get("check_in_time")
        self.check_out_time = prop.get("check_out_time")
        self.address = None
        self.phone = None
        self.km = None            # distance from the city centre, when the search knew it
        self.call = None          # schemas.CallQuery, filled once the transcript is read
        self.hotel = None         # schemas.Hotel this candidate was built from (pipeline.py)
        # call state
        self.status = "queued"
        self.call_status = None
        self.sid = None
        self.attempts = 0
        self.seconds = 0          # from Twilio's /status webhook, when it arrives in time
        self.local_seconds = 0    # our own dial-to-hangup measure, always available
        self.turns = 0
        self.silent = 0
        self.history: list = []
        self.facts: dict = {}
        self.verified_at = None
        self.done = asyncio.Event()

    @property
    def billed_seconds(self) -> int:
        """Twilio's own figure when the status callback made it back, else our measure."""
        return self.seconds or self.local_seconds

    def summary(self) -> dict:
        return {"name": self.name, "listed_price": self.price, "phone": self.phone,
                "distance_km": self.km, "status": self.status, "facts": self.facts, "attempts": self.attempts,
                "call_seconds": self.billed_seconds}


class Run:
    def __init__(self, prompt: str):
        self.prompt = prompt
        self.req: dict = {}
        self.cands: list[Candidate] = []
        self.winner: Candidate | None = None
        self.tok_in = self.tok_out = self.llm_calls = self.serp_calls = 0
        self.tts_calls = self.tts_chars = 0
        self.t0 = time.perf_counter()
        self.marks: dict = {}

    def mark(self, k: str):
        self.marks[k] = round(time.perf_counter() - self.t0, 3)


RUN: Run = None  # type: ignore
HTTP: httpx.AsyncClient = None  # type: ignore
app = FastAPI()


# ---------------------------------------------------------------- LLM
async def llm(messages: list) -> dict:
    """Backboard POST /threads/messages. No thread_id + memory off = stateless, so we
    send our own capped window and pay for nothing we didn't send.
    Their API has no max_tokens: brevity is enforced by the prompt alone."""
    sys_p = " ".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    content = turns[0]["content"] if len(turns) == 1 else "\n".join(
        f"{'Hotel' if m['role'] == 'user' else 'You'}: {m['content']}" for m in turns)
    body = {"content": content, "json_output": True, "memory": "off", "stream": False}
    if sys_p:
        body["system_prompt"] = sys_p
    if BB_PROVIDER:
        body["llm_provider"] = BB_PROVIDER
    if BB_MODEL:
        body["model_name"] = BB_MODEL

    r = await HTTP.post(f"{BB_BASE}/threads/messages", headers={"X-API-Key": BB_KEY}, json=body)
    r.raise_for_status()
    j = r.json()
    RUN.tok_in += j.get("input_tokens") or 0
    RUN.tok_out += j.get("output_tokens") or 0
    RUN.llm_calls += 1
    text = j.get("content") or ""
    # Backboard answers 200 even when the upstream model fails; the error is in the body.
    if j.get("status") == "FAILED" or text.startswith("LLM Error"):
        raise RuntimeError(f"backboard: {text[:200]}")
    return _jparse(text or j.get("message") or "")


def _jparse(s: str) -> dict:
    """Lenient JSON: strip code fences / stray prose some models add."""
    s = s.strip()
    i, j = s.find("{"), s.rfind("}")
    return json.loads(s[i:j + 1] if 0 <= i < j else s)


# ---------------------------------------------------------------- step 1: parse
CITY_RE = re.compile(r"\b(?:in|at|to|near)\s+([A-Z][\w.'-]*(?:,?\s+[A-Z][\w.'-]*){0,3})")
ISO_RE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
NOT_CITY = {"jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
            "mon", "tue", "wed", "thu", "fri", "sat", "sun", "the", "a", "an"}
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
ADULTS_RE = re.compile(r"\b(\d)\s*(?:adults?|guests?|people|persons?)\b", re.I)
DATEISH_RE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d|\b(?:tonight|tomorrow|weekend|\d+\s+nights?)\b",
    re.I,
)


async def parse_request(prompt: str) -> dict:
    tomorrow = date.today() + timedelta(days=1)
    req = {"city": None, "check_in": tomorrow.isoformat(),
           "check_out": (tomorrow + timedelta(days=1)).isoformat(), "adults": 1, "email": JUDGE_EMAIL}
    if m := EMAIL_RE.search(prompt):
        req["email"] = m.group(0).rstrip(".")
    if m := ADULTS_RE.search(prompt):
        req["adults"] = int(m.group(1))
    isos = ISO_RE.findall(prompt)
    if isos:
        req["check_in"] = isos[0]
        req["check_out"] = isos[1] if len(isos) > 1 else (date.fromisoformat(isos[0]) + timedelta(days=1)).isoformat()
    for m in CITY_RE.finditer(prompt):
        cand = m.group(1).strip(" .,")
        if cand.split()[0].lower()[:3] not in NOT_CITY:
            req["city"] = cand
            break

    if req["city"] and (isos or not DATEISH_RE.search(prompt)):
        return req  # fast path: zero tokens

    out = await llm([
        {"role": "system", "content": (
            f"Today is {date.today().isoformat()}. Extract hotel request as JSON: "
            '{"city":str,"check_in":"YYYY-MM-DD","check_out":"YYYY-MM-DD","adults":int}. '
            "Default 1 night starting tomorrow, 1 adult.")},
        {"role": "user", "content": prompt},
    ])
    req.update({k: v for k, v in out.items() if v and k in req})
    return req


# ---------------------------------------------------------------- step 2: search, rank, enrich
def _hotel_params(req: dict) -> dict:
    return {"engine": "google_hotels", "q": f"hotels in {req['city']}",
            "check_in_date": req["check_in"], "check_out_date": req["check_out"],
            "adults": req["adults"], "currency": CURRENCY, "gl": GL, "hl": "en",
            "api_key": SERPAPI_KEY}


async def serp(params: dict) -> dict:
    RUN.serp_calls += 1
    r = await HTTP.get("https://serpapi.com/search.json", params=params, timeout=12)
    r.raise_for_status()
    return r.json()


async def search_hotels(req: dict) -> list:
    j = await serp({**_hotel_params(req), "sort_by": "3", "rating": "7"})  # cheapest, 3.5+ stars
    return j.get("properties", [])


def rank(props: list) -> list[Candidate]:
    """Score = nightly price, raised for ratings under 4.2. Lowest score first. Zero tokens."""
    scored = []
    for p in props:
        price = (p.get("rate_per_night") or {}).get("extracted_lowest")
        if not price:
            continue
        rating = p.get("overall_rating") or 3.5
        scored.append((price * (1 + max(0.0, 4.2 - rating) * 0.3), p, price))
    scored.sort(key=lambda t: t[0])
    return [Candidate(i, p, price, round(s, 2)) for i, (s, p, price) in enumerate(scored)]


def e164(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+") and len(digits) >= 10:
        return "+" + digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits[0] == "1":
        return "+" + digits
    return None


async def enrich(c: Candidate, req: dict):
    """Phone + address: hotel details page first, Google Maps as fallback."""
    try:
        if c.token:
            d = await serp({**_hotel_params(req), "property_token": c.token})
            c.phone = e164(d.get("phone"))
            c.address = d.get("address") or c.address
            c.link = d.get("link") or c.link
            c.check_in_time = d.get("check_in_time") or c.check_in_time
            c.check_out_time = d.get("check_out_time") or c.check_out_time
        if not c.phone:
            m = await serp({"engine": "google_maps", "type": "search", "q": f"{c.name}, {req['city']}",
                            "hl": "en", "api_key": SERPAPI_KEY})
            place = m.get("place_results") or (m.get("local_results") or [{}])[0]
            c.phone = e164(place.get("phone"))
            c.address = c.address or place.get("address")
    except Exception as e:
        print(f"[enrich] {c.name}: {e!r}", file=sys.stderr)
    if not c.phone and not PHONE_OVERRIDE:
        c.status = "no_phone"


async def pick_shortlist(run: Run) -> list[Candidate]:
    """Enrich in parallel; backfill from the ranked list until TOP_K have a phone."""
    pool, short, i = run.cands, [], 0
    while len(short) < TOP_K and i < len(pool):
        batch = pool[i:i + (TOP_K - len(short))]
        i += len(batch)
        # opener text needs only name/price/req, so its audio can be built
        # concurrently with the phone-number lookups - free latency.
        await asyncio.gather(*(enrich(c, run.req) for c in batch),
                             *(tts(opener(run, c)) for c in batch))
        short += [c for c in batch if c.status != "no_phone"]
    return short


# ---------------------------------------------------------------- step 3: verification calls
AUDIO: dict[str, bytes] = {}                          # key -> mp3, served from /audio/<key>


def _akey(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()[:16]


async def tts(text: str) -> str | None:
    """ElevenLabs -> cached mp3. Returns the audio key, or None to fall back to <Say>."""
    if not (EL_KEY and PUBLIC_URL) or EL_MODE == "off":
        return None
    key = _akey(text)
    if key in AUDIO:                                   # repeated lines cost nothing
        return key
    try:
        r = await HTTP.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{EL_VOICE}",
            headers={"xi-api-key": EL_KEY},
            params={"output_format": EL_FORMAT},
            json={"text": text, "model_id": EL_MODEL,
                  "voice_settings": {"speed": EL_SPEED, "stability": EL_STABILITY,
                                     "similarity_boost": EL_SIMILARITY}},
            timeout=EL_TIMEOUT,
        )
        r.raise_for_status()
    except Exception as e:                             # never stall a live call for TTS
        print(f"[tts] {e!r}", file=sys.stderr)
        return None
    AUDIO[key] = r.content
    RUN.tts_calls += 1
    RUN.tts_chars += len(text)
    return key


async def voice_el(text: str) -> str:
    """<Play> ElevenLabs audio when we have it, else Twilio's own <Say>."""
    if EL_MODE == "all" or (EL_MODE == "opener" and _akey(text) in AUDIO):
        key = await tts(text)
        if key:
            return f"<Play>{PUBLIC_URL}/audio/{key}</Play>"
    return f'<Say voice="{VOICE}">{escape(text)}</Say>'


@app.get("/audio/{key}")
async def audio(key: str):
    return Response(content=AUDIO.get(key, b""), media_type="audio/mpeg")


async def gather(c: Candidate, say: str) -> str:
    """Twilio has no bargeIn attribute, so placement is the control: audio nested inside
    <Gather> is cut off by the first sound the hotel makes, audio before <Gather> always
    finishes and the Gather then only listens."""
    g = (f'<Gather input="speech" action="{PUBLIC_URL}/turn?c={c.idx}" method="POST" '
         f'speechTimeout="{SPEECH_TIMEOUT}" speechModel="phone_call" enhanced="true" '
         f'actionOnEmptyResult="true" language="en-US">')
    v = await voice_el(say)
    body = f"{g}{v}</Gather>" if BARGE_IN else f"{v}{g}</Gather>"
    return f"<Response>{body}</Response>"


async def hangup_twiml(say: str) -> str:
    return f"<Response>{await voice_el(say)}<Hangup/></Response>"


def xml(body: str) -> Response:
    return Response(content=body, media_type="application/xml")


def nights(q: dict) -> int:
    return (date.fromisoformat(q["check_out"]) - date.fromisoformat(q["check_in"])).days


def opener(run: Run, c: Candidate) -> str:
    """One question, straight to the point: no guest name, no preamble beyond the two words
    that say it's automated. With BARGE_IN=0 this plays uninterrupted, so every extra clause is
    billed dead air. A 'yes' to this confirms availability AND the rate in one turn."""
    q, n = run.req, nights(run.req)
    when = f"{date.fromisoformat(q['check_in']):%B %-d}"
    stay = when if n == 1 else f"{n} nights from {when}"
    room = f"{q['room_type']} room" if q.get("room_type") else "room"
    return (
        f"Hi, automated call. Do you have a {room} available {stay} for "
        f"{q['adults']} guest{'s' * (q['adults'] > 1)}, at {c.price:.0f} a night?"
    )


def system_prompt(run: Run, c: Candidate) -> str:
    q = run.req
    hold = ("If they offer to hold or reserve the room, accept under the guest name and ask for the "
            "reference number. " if HOLD_ON_CALL else
            "Do not reserve or commit; you are only verifying. ")
    return (
        f"You are an automated assistant on a live phone call with {c.name}. Calls are billed by the "
        f"second: be as brief as possible. You asked if a room is available {q['check_in']} to "
        f"{q['check_out']}, {q['adults']} adult(s)"
        f"{', room type: ' + q['room_type'] if q.get('room_type') else ''}, at {c.price:.0f} {CURRENCY}/night. "
        "You need ONLY two facts: is a room available, and the nightly price. If they say yes to your "
        f"rate, price={c.price:.0f}. If one fact is missing, ask only for it in under 12 words. "
        "Never ask about fees, cancellation or anything else; no small talk, no restating. "
        f"{hold}Never share payment details. Never invent facts. "
        'Reply ONLY JSON: {"say":str (<=25 words, natural speech),"done":bool,"available":bool|null,'
        '"price":number|null,"fees":str|null,"room_type":str|null,"cancellation":str|null,'
        '"check_in_time":str|null,"reference":str|null,"staff_name":str|null}. '
        "Set done=true the moment availability and price are both known, or as soon as they say "
        "nothing is available; then say only a short goodbye."
    )


def finish(c: Candidate):
    if c.status not in TERMINAL:
        a = c.facts.get("available")
        c.status = "verified" if a is True else "unavailable" if a is False else "unclear"
        if c.status == "verified":
            c.verified_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    c.done.set()


def _cand(request: Request) -> Candidate | None:
    try:
        return RUN.cands[int(request.query_params.get("c", "-1"))]
    except (ValueError, IndexError):
        return None


@app.post("/turn")
async def turn(request: Request):
    c = _cand(request)
    if c is None:
        return xml("<Response><Hangup/></Response>")
    form = await request.form()
    heard = (form.get("SpeechResult") or "").strip()
    c.turns += 1

    if not heard:
        c.silent += 1
        if c.silent >= 2:
            finish(c)
            return xml(await hangup_twiml("Sorry, I'm having trouble hearing you. Thank you, goodbye."))
        return xml(await gather(c, "Sorry, I didn't catch that. Could you say that again?"))

    c.silent = 0
    c.history.append({"role": "user", "content": heard})
    try:
        out = await llm([{"role": "system", "content": system_prompt(RUN, c)}] + c.history[-6:])
    except Exception as e:  # never leave the line dead
        print(f"[llm] {c.name}: {e!r}", file=sys.stderr)
        out = {"say": "Sorry, could you repeat that?", "done": False}

    say = out.get("say") or "Thank you."
    c.history.append({"role": "assistant", "content": say})
    for k in FACT_KEYS:
        if out.get(k) is not None:
            c.facts[k] = out[k]

    # Confirmed room + price is all we came for: hang up now, whatever the LLM wanted to ask next.
    if c.facts.get("available") is True and c.facts.get("price") is not None:
        c.history[-1]["content"] = say = BYE
        finish(c)
        return xml(await hangup_twiml(say))
    if out.get("done") or c.facts.get("available") is False or c.turns >= MAX_TURNS:
        finish(c)
        return xml(await hangup_twiml(say))
    return xml(await gather(c, say))


@app.post("/status")
async def status(request: Request):
    c = _cand(request)
    if c is not None:
        form = await request.form()
        c.call_status = form.get("CallStatus")
        c.seconds += int(form.get("CallDuration") or 0)
        c.done.set()  # wakes call_candidate; it decides whether to redial
    return Response(status_code=204)


TW_CALLS = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Calls"


async def place_call(run: Run, c: Candidate) -> str:
    c.attempts += 1
    r = await HTTP.post(
        TW_CALLS.format(sid=TW_SID) + ".json", auth=(TW_SID, TW_TOKEN),
        data={"To": PHONE_OVERRIDE or c.phone, "From": TW_FROM,
              "Twiml": await gather(c, opener(run, c)),   # inline: no webhook hop before speaking
              "StatusCallback": f"{PUBLIC_URL}/status?c={c.idx}",
              "StatusCallbackEvent": "completed", "Timeout": "20",
              "TimeLimit": str(CALL_MAX_SECONDS),         # hard cap, enforced by Twilio
              **({"Record": "true"} if RECORD_CALLS else {})},
    )
    r.raise_for_status()
    return r.json()["sid"]


async def recording_mp3(call_sid: str | None, wait: float = 20.0) -> bytes | None:
    """The call's recording as mp3 bytes, or None. Twilio finishes processing a few seconds after
    hangup, so poll until it's 'completed'. Media needs our auth: email the bytes, not the URL."""
    if not call_sid:
        return None
    base = f"https://api.twilio.com/2010-04-01/Accounts/{TW_SID}/Recordings"
    deadline = time.perf_counter() + wait
    try:
        while True:
            r = await HTTP.get(base + ".json", params={"CallSid": call_sid}, auth=(TW_SID, TW_TOKEN))
            r.raise_for_status()
            recs = r.json().get("recordings") or []
            done = [x for x in recs if x.get("status") == "completed"]
            if done:
                m = await HTTP.get(f"{base}/{done[0]['sid']}.mp3", auth=(TW_SID, TW_TOKEN),
                                   follow_redirects=True, timeout=20)
                m.raise_for_status()
                return m.content
            if (recs and all(x.get("status") in ("failed", "absent") for x in recs)) \
                    or time.perf_counter() > deadline:
                return None
            await asyncio.sleep(1.0)
    except Exception as e:
        print(f"[recording] {call_sid}: {e!r}", file=sys.stderr)
        return None


async def hang_up(sid: str | None):
    if not sid:
        return
    try:
        await HTTP.post(TW_CALLS.format(sid=TW_SID) + f"/{sid}.json",
                        auth=(TW_SID, TW_TOKEN), data={"Status": "completed"})
    except Exception as e:
        print(f"[hangup] {sid}: {e!r}", file=sys.stderr)


async def call_candidate(run: Run, c: Candidate):
    c.status = "calling"
    try:
        for attempt in range(DIAL_RETRIES + 1):
            c.done.clear()
            c.call_status = None
            dialed = time.perf_counter()
            c.sid = await place_call(run, c)
            try:
                await asyncio.wait_for(c.done.wait(), CALL_TIMEOUT)
            except asyncio.TimeoutError:
                await hang_up(c.sid)
                c.status = "timeout"
                c.local_seconds += int(time.perf_counter() - dialed)
                return
            finally:
                # Twilio's /status callback carries CallDuration but lands a beat after
                # hangup, often past the end of the run. Keep our own measure so the
                # cost report is never 0; billed_seconds prefers Twilio's when it arrives.
                c.local_seconds = max(c.local_seconds, int(time.perf_counter() - dialed))
            if c.status in TERMINAL:
                return
            if c.call_status not in RETRYABLE:
                break
            await asyncio.sleep(1.0 * (attempt + 1))
        c.status = {"busy": "no_answer", "no-answer": "no_answer",
                    "failed": "unreachable", "canceled": "unreachable"}.get(c.call_status, "unclear")
    except asyncio.CancelledError:
        await hang_up(c.sid)
        if c.status not in TERMINAL:
            c.status = "cancelled"
        raise
    except Exception as e:
        print(f"[call] {c.name}: {e!r}", file=sys.stderr)
        c.status = "error"


def decide(short: list[Candidate]) -> tuple[bool, Candidate | None]:
    """Walk in rank order. A verified hotel wins once every better-ranked one has failed."""
    for c in short:
        if c.status == "verified":
            return True, c
        if c.status not in TERMINAL:
            return False, None  # a better option is still on the phone
    return True, None           # all finished, none verified


async def verify_all(run: Run, short: list[Candidate]):
    if PHONE_OVERRIDE:  # one line: dial one at a time, best first
        for c in short:
            await call_candidate(run, c)
            if c.status == "verified":
                break
    else:
        pending = {asyncio.create_task(call_candidate(run, c)) for c in short}
        while pending:
            _, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            settled, _w = decide(short)
            if settled:
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                break
    run.winner = decide(short)[1]


# ---------------------------------------------------------------- step 4: confirmation email
STATUS_TEXT = {"verified": "Verified available", "unavailable": "No availability",
               "unclear": "Could not confirm", "no_answer": "No answer / busy",
               "unreachable": "Call failed", "timeout": "Call timed out", "no_phone": "No phone listed",
               "cancelled": "Not needed (better option verified)", "error": "Error",
               "queued": "Not called", "calling": "Not called"}


def money(v) -> str:
    return f"{float(v):.2f} {CURRENCY}" if isinstance(v, (int, float)) else (v or "n/a")


def receipt(run: Run) -> tuple[str, str, str]:
    q, w = run.req, run.winner
    n = nights(q)
    head = "VERIFIED" if w else "NOT VERIFIED"
    subject = (f"[{head}] {w.name}, {q['city']}, {q['check_in']} to {q['check_out']}" if w
               else f"[{head}] Hotel search in {q.get('city')}: no option could be confirmed")

    rows = [("Status", head), ("Guest", GUEST_NAME), ("City", q.get("city")),
            ("Check-in", q.get("check_in")), ("Check-out", q.get("check_out")),
            ("Nights", n), ("Guests", q.get("adults"))]
    if w:
        f = w.facts
        rate = f.get("price") or w.price
        rows += [
            ("Hotel", w.name), ("Address", w.address or "n/a"), ("Phone", w.phone or "n/a"),
            ("Distance from city centre", f"{w.km:g} km" if w.km is not None else "n/a"),
            ("Nightly rate (confirmed by phone)", money(f.get("price")) if f.get("price") else "not stated"),
            ("Nightly rate (listed online)", money(w.price)),
            ("Estimated total before fees", money(round(float(rate) * n, 2))),
            ("Listed total", money(w.total)),
            ("Taxes / fees", f.get("fees") or "not stated"),
            ("Room type", f.get("room_type") or "not stated"),
            ("Cancellation policy", f.get("cancellation") or "not stated"),
            ("Check-in time", f.get("check_in_time") or w.check_in_time or "n/a"),
            ("Check-out time", w.check_out_time or "n/a"),
            ("Reference number", f.get("reference") or "none given"),
            ("Confirmed by", f.get("staff_name") or "front desk"),
            ("Verified at", w.verified_at),
            ("Rating", (f"{w.rating}" + (f" ({w.reviews} reviews)" if w.reviews else "")) if w.rating else "n/a"),
            ("Book online", w.link or "n/a"),
        ]
    rows += [("Total agent time (s)", run.marks.get("calls")),
             ("LLM tokens in/out", f"{run.tok_in}/{run.tok_out}"),
             ("Call minutes", round(sum(c.billed_seconds for c in run.cands) / 60, 2))]

    checked = [c for c in run.cands if c.status not in ("queued",) or c.attempts]
    opt_rows = [(c.name, money(c.facts.get("price") or c.price), STATUS_TEXT.get(c.status, c.status),
                 f"{c.km:g} km" if c.km is not None else "n/a", c.phone or "n/a", c.link or "n/a")
                for c in checked]
    transcript = [(m["role"], m["content"]) for m in (w.history if w else [])]

    def t(items):
        return "<table cellpadding='4' style='border-collapse:collapse'>" + "".join(
            "<tr>" + "".join(f"<td style='border:1px solid #ddd'>{escape(str(x))}</td>" for x in r) + "</tr>"
            for r in items) + "</table>"

    html = (f"<h2>{escape(subject)}</h2>{t(rows)}"
            f"<h3>Every option checked</h3>{t([('Hotel', 'Rate', 'Result', 'Distance', 'Phone', 'Link')] + opt_rows)}")
    if transcript:
        html += "<h3>Call transcript</h3>" + "".join(
            f"<p><b>{'Hotel' if r == 'user' else 'Agent'}:</b> {escape(s)}</p>" for r, s in transcript)

    text = "\n".join(f"{k}: {v}" for k, v in rows)
    text += "\n\nEVERY OPTION CHECKED\n" + "\n".join(" | ".join(map(str, r)) for r in opt_rows)
    if transcript:
        text += "\n\nCALL TRANSCRIPT\n" + "\n".join(
            f"{'Hotel' if r == 'user' else 'Agent'}: {s}" for r, s in transcript)
    return subject, text, html


def _smtp_send(to: str, subject: str, text: str, html: str):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = MAIL_FROM or SMTP_USER, to, subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=10) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


async def send_receipt(run: Run):
    subject, text, html = receipt(run)
    to = run.req.get("email") or JUDGE_EMAIL
    if RESEND_KEY:
        r = await HTTP.post("https://api.resend.com/emails",
                            headers={"Authorization": f"Bearer {RESEND_KEY}"},
                            json={"from": MAIL_FROM, "to": [to], "subject": subject,
                                  "text": text, "html": html})
        r.raise_for_status()
    else:
        await asyncio.to_thread(_smtp_send, to, subject, text, html)


# ---------------------------------------------------------------- orchestrator
async def warm():
    urls = ["https://serpapi.com/", f"{BB_BASE}/models/providers", "https://api.twilio.com/"]
    if RESEND_KEY:
        urls.append("https://api.resend.com/")
    await asyncio.gather(*(HTTP.head(u, timeout=5) for u in urls), return_exceptions=True)


async def main(prompt: str):
    global RUN, HTTP
    HTTP = httpx.AsyncClient(http2=True, timeout=10)
    RUN = run = Run(prompt)
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="warning"))
    srv = asyncio.create_task(server.serve())
    warmer = asyncio.create_task(warm())

    try:
        run.req = await parse_request(prompt)
        run.mark("parse")
        if not run.req.get("city"):
            raise SystemExit("Could not find a city in the prompt.")

        await warmer
        run.cands = rank(await search_hotels(run.req))
        run.mark("search")

        short = await pick_shortlist(run)
        run.mark("enrich")

        if short:
            while not server.started:
                await asyncio.sleep(0.01)
            await verify_all(run, short)
        run.mark("calls")

        await send_receipt(run)
        run.mark("email")
    finally:
        print(json.dumps({
            "request": run.req,
            "winner": run.winner.summary() if run.winner else None,
            "checked": [c.summary() for c in run.cands if c.attempts or c.status == "no_phone"],
            "timings_s": run.marks,
            "llm_calls": run.llm_calls, "serpapi_calls": run.serp_calls,
            "tokens": {"in": run.tok_in, "out": run.tok_out},
            "tts": {"calls": run.tts_calls, "chars": run.tts_chars, "cached": len(AUDIO)},
            "call_seconds": sum(c.billed_seconds for c in run.cands),
        }, indent=2, default=str))
        await asyncio.sleep(0.5)  # let Twilio deliver the last status callbacks
        server.should_exit = True
        await srv
        await HTTP.aclose()


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or sys.stdin.read()))