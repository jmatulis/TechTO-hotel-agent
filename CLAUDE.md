# Hotel Agent: TechTO Fast & Frugal Hackathon (Oct 17, 2026)

## What we're building

A lightweight AI agent that, from **one one-shot prompt** from the judges, finds the best-value
hotel in a city (revealed live), **phones hotels to confirm the room is real and available**, and
**emails a confirmation** to an address the judges give us.

The bake-off is at **5:15pm**: every team runs the same prompt at the same time, and the **fastest and
cheapest** successful run wins. Cost means tokens, API calls and call minutes, so keep every one of them low.

The organizers clarified that **the phone call only confirms the reservation**: availability, rate, and
details. The agent does not negotiate, and by default it doesn't take payment or make a booking by phone.

## Hard rules (the event disqualifies you for breaking them)

- **No ready-made agents** or agent frameworks: no LangChain agents, CrewAI, AutoGen, Vapi/Retell/Bland
  agent products, etc. Plain Python + direct HTTP calls only.
- **No browser automation**: no Selenium, Playwright, Puppeteer, headless Chrome.
- Must make a **real phone call** and must deliver an **email** to the provided address.
- Public foundation models via API only.
- **Backboard is the model layer**, used as the Unified API over plain HTTP (`httpx`). Their Studio,
  SDK (`backboard-sdk`) and R-CLI are dev-time tools at most and must never be in the shipped path:
  R-CLI is itself a coding agent, which is exactly what the no-frameworks rule bans.
- **ElevenLabs is the voice layer** (hackathon sponsor, credits provided): their raw TTS HTTP API
  only. **Never ElevenLabs Agents** - that product runs the whole call loop and is the same category
  as Vapi/Retell/Bland, i.e. a disqualification. STT stays Twilio `<Gather input="speech">`.

## Files

| File               | What it is                                                                           |
| ------------------ | ------------------------------------------------------------------------------------ |
| `agent.py`         | The whole agent: config, search, ranking, Twilio webhooks, call orchestration, email |
| `tests/test-agent.py`    | Offline tests; every outside service is faked. **Run after every change.**           |
| `pipeline.py`      | Prompt → Backboard parse → SerpApi + LiteAPI in parallel → `merge` + `rank` → call best `CALL_BATCH` → Backboard reads transcripts into `CallQuery` → `ranker.cheapest` (confirmed rooms only); next batch if none |
| `mailer.py`        | The email: `build(req, winner, options, stats)` → subject/text/html (hotel, address, km from centre, quoted vs listed price, every option, transcripts); `send()` via Resend or Gmail SMTP |
| `hotel_search.py`  | SerpApi Google Hotels: cheapest rates, distance from centre, phones. CLI, `search()` (dicts) or `find_hotels()` (`Hotel` objects) |
| `liteapi_search.py`| LiteAPI rates + Google Places phones/reviews → `find_hotels()` returns `Hotel` objects |
| `schemas.py`       | pydantic models: `HotelQuery`, `Hotel`, `CallQuery` (transcript, summary, quoted price, available), `RankWeights` |
| `ranker.py`        | `merge` (de-dupe across sources), `rank` (price × distance × review penalty), `final_rank`/`cheapest` (after the calls: `available is True` only, by quoted price). No API calls |
| `decider.py`       | Post-call stop rule: price table + band segments + latency cost; says when to stop waiting for the other calls (no I/O) |
| `tests/integ_pipeline.py` | Pipeline on six demo call transcripts with the real Backboard LLM; search and calls faked |
| `tests/call_test.py`     | Dials one invented hotel: real call, no SerpApi, no email. Tune the call here         |
| `README.md`        | Human-facing blueprint, API payloads, setup, checklist                               |
| `.env.example`     | All config keys; copy to `.env` (never commit `.env`)                                |
| `requirements.txt` | httpx[http2], fastapi, uvicorn, python-dotenv, python-multipart, pydantic>=2         |

## How it runs (`main()` in agent.py)

One process, one asyncio loop. A FastAPI/uvicorn server (`/turn`, `/status`) runs in the background
of the same loop so Twilio can call back into it through a public tunnel.

1. **`parse_request()`**: regex pulls city, ISO dates, guest count and email out of the prompt.
   It calls the LLM only if regex can't (fuzzy dates like "this weekend"). Skips month and day
   names when finding the city.
2. **`search_hotels()`**: ONE SerpApi `google_hotels` request, `sort_by=3` (cheapest), `rating=7` (3.5+).
3. **`rank()`**: score = nightly price × (1 + 0.3 × max(0, 4.2 − rating)); lowest first. Uses no tokens.
4. **`pick_shortlist()`**: gets phone + address for the top `TOP_K` (default 3) in parallel:
   SerpApi property details by `property_token`, with Google Maps (`engine=google_maps`) as fallback.
   A hotel with no phone gets status `no_phone` and the next one is pulled in.
5. **`verify_all()`**: calls the shortlist **in parallel**.
   - Each call: Twilio REST create-call with the opening line as **inline TwiML** (`Twiml` param), so
     there's no webhook hop before it speaks. Twilio `<Gather input="speech">` does speech-to-text and
     POSTs to `/turn?c=<idx>`. We ask the LLM for the next line and speak it back.
   - **Voice**: `voice_el()` returns `<Play>{PUBLIC_URL}/audio/<key}></Play>` for ElevenLabs audio,
     falling back to Twilio `<Say voice=TTS_VOICE>` whenever ElevenLabs is off, uncached in
     `opener` mode, or erroring. MP3s live in the in-process `AUDIO` dict and are served by
     `GET /audio/{key}` through the same tunnel, keyed by a hash of the text so repeated lines
     ("Sorry, I didn't catch that") are synthesized once. **Openers are pre-generated inside
     `pick_shortlist()`, concurrently with the SerpApi phone lookups**, so the first and most
     important line costs no latency at call time. `ELEVEN_MODE=all|opener|off` trades voice
     quality against per-turn latency; `ELEVEN_TIMEOUT` bounds the stall before falling back.
   - `?c=<idx>` on `/turn` and `/status` identifies which hotel's call a webhook belongs to
     (`run.cands[idx]`). Each `Candidate` holds its own history, facts and status.
   - **`decide()`**: walk the shortlist in rank order. The first `verified` hotel wins once every
     better-ranked one has finished. The remaining calls are cancelled, and `call_candidate`
     hangs up via the Twilio API (`Status=completed`) on cancellation.
   - With `PHONE_OVERRIDE` set, all calls go to that one number **sequentially**, best first, and
     the run stops at the first verified one. Use this for testing on your own phone or for a judge-run line.
6. **`send_receipt()`**: email via Resend (HTTP) if `RESEND_API_KEY` is set, else Gmail SMTP.
   Contains the winner's details, every option checked with its result, and the call transcript.
7. Prints a JSON report: timings per stage, LLM calls, SerpApi calls, tokens in/out, call seconds.

### LLM contract (per conversation turn)

Backboard Unified API: `POST https://app.backboard.io/api/threads/messages`, auth header
`X-API-Key`, with `json_output: true` and `memory: "off"`. No `thread_id` is sent, so every request
is stateless and we keep paying only for our own window: history capped at the last 6 messages.
`llm()` hoists the system message into `system_prompt` and flattens the rest to `Hotel:` / `You:`
lines, since the API takes a single `content` string. Tokens come back as `input_tokens` /
`output_tokens`. The model must reply only with:

```json
{"say":"<=25 words","done":bool,"available":bool|null,"price":number|null,"fees":str|null,
 "room_type":str|null,"cancellation":str|null,"check_in_time":str|null,
 "reference":str|null,"staff_name":str|null}
```

Fields that aren't null get merged into `c.facts`. `available=false` ends the call at once.
`done=true` → `finish()` sets status `verified` / `unavailable` / `unclear`.

### Call statuses

`verified`, `unavailable`, `unclear` (silence, hang-up, turn limit), `no_answer` (busy/no answer after
redial), `unreachable`, `timeout`, `cancelled` (a better option won), `no_phone`, `error`.

## Config (`.env`)

`PUBLIC_URL` (tunnel URL, must be current), `PORT`, `BACKBOARD_API_KEY`, `BACKBOARD_BASE_URL`,
`BACKBOARD_PROVIDER` (default `cerebras`), `BACKBOARD_MODEL` (default `openai/gpt-oss-120b`),
`SERPAPI_KEY`, `MAX_ROUNDS` (3), `RADIUS_KM` (20, from city centre), `SEARCH_PAGES` (1), `GOOGLE_PLACES_KEY` (optional), `CURRENCY=CAD`,
`SERP_GL=ca`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`, `PHONE_OVERRIDE`, `TOP_K`,
`RECORD_CALLS` (1 = record every call, attach the winner's mp3 to the email), `HOLD_ON_CALL` (1 = accept a hold or reservation if the hotel offers one), `TTS_VOICE`,
`SPEECH_TIMEOUT`, `BARGE_IN` (0 = the agent is never interrupted), `MAX_TURNS`, `CALL_TIMEOUT` (45 s: later calls are cut off and never win; their partial transcript is kept), `CALL_MAX_SECONDS` (60, Twilio hard cap per call), `CALL_BATCH` (15 calls at once), `LATENCY_COST` (currency per second of waiting, 0.5), `SEGMENT_PCT` (price band width, 0.10),
`DIAL_RETRIES`, `GUEST_NAME`, `JUDGE_EMAIL`,
`ELEVEN_API_KEY`, `ELEVEN_VOICE_ID` (default `NOpBlnGInO9m6vDvFkFC`, Spuds Oxley),
`ELEVEN_MODEL` (default `eleven_flash_v2_5`), `ELEVEN_FORMAT`, `ELEVEN_MODE`, `ELEVEN_TIMEOUT`,
`ELEVEN_SPEED` (default 1.2), `ELEVEN_STABILITY`, `ELEVEN_SIMILARITY`,
`TEAM_NUMBER` (email subject `Team # N - Hotel Reservation Confirmed!`), `RESEND_API_KEY`, `MAIL_FROM`, `SMTP_HOST`, `SMTP_USER`, `SMTP_PASS`.

## Commands

```bash
pip install -r requirements.txt
.venv/bin/python tests/test-agent.py                         # offline tests, no keys needed
.venv/bin/python tests/test_decider.py                     # the stop rule, pure arithmetic
.venv/bin/python tests/integ_pipeline.py                  # demo transcripts through the real LLM (~3 calls, needs BACKBOARD_API_KEY)
cloudflared tunnel --url http://localhost:8080         # or: ngrok http 8080 → PUBLIC_URL
.venv/bin/python tests/call_test.py "Good Value Inn" 129      # ONE real call, no search, no email
.venv/bin/python pipeline.py "King room in Toronto Oct 18-19 for 2, send to judge@x.com"   # full flow
.venv/bin/python pipeline.py --list "I'm looking for Toronto, for 3 people, Oct 18 to Oct 20"   # parse + search + phones, no calls
.venv/bin/python hotel_search.py Toronto 2026-10-18 --top 10 --phones    # rates + phones
.venv/bin/python agent.py "Find the best hotel deal in Toronto for 2026-10-18 to 2026-10-19, send to judge@x.com"
```

Test the multi-call flow on your own phone: set `PHONE_OVERRIDE=<cell>`, answer the first call with
"sorry, we're fully booked", then confirm the second.

## Working rules for Claude Code

- **Speed and cost are the score.** Don't add LLM calls, SerpApi calls or call turns without a reason.
  Prefer regex, templates and arithmetic over the LLM. Keep prompts short.
- Keep it in plain `asyncio` + `httpx`. Add no new dependencies unless clearly needed.
- **Never block the event loop**: blocking work goes through `asyncio.to_thread` (like SMTP).
- A `/turn` handler must always return valid TwiML, even when the LLM fails, so the line never goes dead.
- The agent says it's an automated assistant, never shares payment details, and never invents facts.
- Run `.venv/bin/python tests/test-agent.py` after every change (plain `python` is not on PATH, and
  system `python3` has no fastapi); add a test for each new behaviour, with
  services faked as the existing tests do.
- Don't print or log API keys or the full `.env`.

## Team split (two people, so you don't edit the same code)

- **Person A, the phone call:** `opener`, `system_prompt`, `/turn`, `/status`, `call_candidate`,
  `verify_all`, `decide`. Tune the conversation on a real phone.
- **Person B, everything else:** setup (Twilio upgrade + Geo Permissions, tunnel, `.env`, Gmail app
  password), `parse_request`, `search_hotels`, `rank`, `pick_shortlist`/`enrich`, `receipt`/`send_receipt`,
  the JSON report and learnings, slides.
- Interface: `run.cands` (ranked `Candidate` list) → `verify_all(run, shortlist)` sets `run.winner` →
  `send_receipt(run)`.

## Open questions (ask the organizers, then update this file)

1. What exact email format do they want? An earlier brief asked for the subject
   `Team # X - Hotel Reservation Confirmed!` with Hotel Name, Address, Phone, Reservation #,
   Reservation Date (and price). **Check whether that still applies**, and get our team number.
   The current `receipt()` doesn't use that format yet.
2. If the call only confirms, where does the "reservation #" come from? Is a reference given on the
   call enough, or is one generated some other way?
3. Do agents call the **real hotels**, or a judge-run phone line? If it's one line, set `PHONE_OVERRIDE`.
4. The email address for the receipt (set `JUDGE_EMAIL`, or include it in the prompt).

## Backlog, most valuable first

1. Match the required email format once confirmed (see open question 1).
2. **Phone menus (IVR):** listen before speaking. Let the LLM return a `press` field → `<Play digits>`.
   Today the opener plays straight away and can talk over an automated menu.
3. **Holds:** don't hang up after 2 silences while on hold; wait silently up to ~2 minutes.
4. **Voicemail:** detect it and end that call early so the agent moves on.
5. Learnings report: estimated $ cost (Twilio minutes, SerpApi searches, tokens) for the presentation.

## Known gotchas

- Twilio **trial accounts** can only dial verified numbers and play a preamble. Upgrade first.
- A stale `PUBLIC_URL` means the opener plays and then the call goes nowhere (no `/turn` webhooks arrive).
  With ElevenLabs on, a stale `PUBLIC_URL` also breaks `<Play>`, so the hotel hears **silence**
  instead of a wrong-but-audible voice. Check the tunnel first when a call goes quiet.
- `AUDIO` is in-process, so audio only survives for the life of the run - which is all we need.
- **Twilio's `/status` callback (with `CallDuration`) lands a beat after hangup**, usually after the
  run has printed its report, so `c.seconds` stays 0. `call_candidate` measures dial-to-hangup
  itself into `c.local_seconds`; read totals through `c.billed_seconds`, which prefers Twilio's
  figure when it arrived. A measured 54s call was reporting 0 before this.
- **Voice Library voices carry their own saved settings, and they are slow.** Spuds Oxley read the
  opener in 30.7s on its defaults vs 17.3s at `speed=1.2` - 1.76x the billed call time for the same
  words. Always send `voice_settings`; measure speech duration as `bytes * 8 / 32000` at
  `mp3_22050_32` (constant bitrate) rather than trusting how it sounds.
- A voice ID from someone else's workspace 404s as `voice_not_found`: it must be added to **your**
  account from the Voice Library first.
- **Backboard returns HTTP 200 even when the upstream model fails**: `status: "FAILED"` with the
  error text in `content`, so `raise_for_status()` misses it. `llm()` checks for this and raises.
- Model IDs must *route*, not just exist in the catalogue. `meta-llama/llama-3.1-8b-instruct` is
  listed under `cerebras` but 404s ("every candidate endpoint was removed during routing").
  Verify with `GET /api/models/provider/cerebras` and one live call before the bake-off.
- The chat API has **no `max_tokens`**. Brevity comes only from the prompt's `<=25 words`;
  gpt-oss-120b spends ~330 output tokens/turn on hidden reasoning.
- If Twilio rejects `<Gather>` options, try `SPEECH_TIMEOUT=1` instead of `auto`.
- **Twilio has no `bargeIn` attribute; placement is the control.** Audio nested *inside*
  `<Gather>` is cut off by the first sound the hotel makes - a cough or "hello?" kills the opener
  mid-sentence. `BARGE_IN=0` (default) emits the audio *before* an empty `<Gather>`, so the agent
  always finishes, at the cost that anything said during the audio is not captured.
- STT mangles spelled-out reference numbers ("T X 4 4 7 1"); have the agent read them back.
- `RUN` is global: one run per process, which is all the bake-off needs.
