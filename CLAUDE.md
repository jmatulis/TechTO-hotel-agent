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

## Files

| File               | What it is                                                                           |
| ------------------ | ------------------------------------------------------------------------------------ |
| `agent.py`         | The whole agent: config, search, ranking, Twilio webhooks, call orchestration, email |
| `test_agent.py`    | Offline tests; every outside service is faked. **Run after every change.**           |
| `README.md`        | Human-facing blueprint, API payloads, setup, checklist                               |
| `.env.example`     | All config keys; copy to `.env` (never commit `.env`)                                |
| `requirements.txt` | httpx[http2], fastapi, uvicorn, python-dotenv, python-multipart                      |

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
     POSTs to `/turn?c=<idx>`. We ask the LLM for the next line and answer with `<Say>` (Twilio TTS).
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

Groq (OpenAI-compatible) `/chat/completions`, `response_format: json_object`, `max_tokens=110`,
history capped at the last 6 messages. The model must reply only with:

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

`PUBLIC_URL` (tunnel URL, must be current), `PORT`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`
(default `llama-3.1-8b-instant`; **check it's still listed on Groq**), `SERPAPI_KEY`, `CURRENCY=CAD`,
`SERP_GL=ca`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM`, `PHONE_OVERRIDE`, `TOP_K`,
`HOLD_ON_CALL` (1 = accept a hold or reservation if the hotel offers one), `TTS_VOICE`,
`SPEECH_TIMEOUT`, `MAX_TURNS`, `CALL_TIMEOUT`, `DIAL_RETRIES`, `GUEST_NAME`, `JUDGE_EMAIL`,
`RESEND_API_KEY`, `MAIL_FROM`, `SMTP_HOST`, `SMTP_USER`, `SMTP_PASS`.

## Commands

```bash
pip install -r requirements.txt
python test_agent.py                                   # offline tests, no keys needed
cloudflared tunnel --url http://localhost:8080         # or: ngrok http 8080 → PUBLIC_URL
python agent.py "Find the best hotel deal in Toronto for 2026-10-18 to 2026-10-19, send to judge@x.com"
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
- Run `python test_agent.py` after every change; add a test for each new behaviour, with
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
- Groq model IDs get retired; a 404 from the LLM usually means a bad `LLM_MODEL`.
- If Twilio rejects `<Gather>` options, try `SPEECH_TIMEOUT=1` instead of `auto`.
- STT mangles spelled-out reference numbers ("T X 4 4 7 1"); have the agent read them back.
- `RUN` is global: one run per process, which is all the bake-off needs.
