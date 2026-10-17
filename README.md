# Fast & Frugal Hotel Agent v2 — TechTO Hackathon (Oct 17, 2026)

One Python process, one asyncio event loop, no agent frameworks, no browser automation.
The agent finds the best-value hotels, **phones the top few at the same time to verify the room
is real and available at the listed price**, picks the best verified one, and emails a full
confirmation record. The LLM is only used for the phone conversations (and for prompts that regex
can't read); everything else is deterministic.

## Blueprint

```
judge prompt
   │
   ▼
[1] parse_request()     regex fast path, 0 tokens; LLM fallback only for fuzzy dates
   ▼
[2] search_hotels()     ONE SerpApi google_hotels call: cheapest first, rating ≥3.5
   ▼
[3] rank()              price × rating penalty, 0 tokens
   ▼
[4] pick_shortlist()    phone + address for the top K (parallel SerpApi lookups);
                        a hotel with no phone is skipped and the next one is pulled in
   ▼
[5] verify_all()        calls the top K AT THE SAME TIME
   │   ┌─ call 1 ─┐ ┌─ call 2 ─┐ ┌─ call 3 ─┐   each: Twilio <Gather> STT → POST /turn?c=i
   │   │ verify   │ │ verify   │ │ verify   │         → LLM (JSON) → Twilio <Say> TTS
   │   └──────────┘ └──────────┘ └──────────┘
   │   decide(): walk in rank order; the first VERIFIED hotel wins as soon as every
   │   better-ranked one has failed → remaining calls are hung up immediately
   ▼
[6] send_receipt()      winner details + every option checked + call transcript
```

### What each call verifies

Availability for the exact dates and guest count, the nightly rate, taxes/fees, room type and
cancellation policy, plus the staff member's name and any reference number they give. The agent
introduces itself as an automated assistant, never shares payment details, and by default does not
reserve anything (`HOLD_ON_CALL=1` lets it accept a hold under the guest's name).

### Call outcomes

| Status                  | Meaning                                                              |
| ----------------------- | -------------------------------------------------------------------- |
| `verified`              | Hotel confirmed a room is available                                  |
| `unavailable`           | Hotel said no rooms; call ends right away                            |
| `unclear`               | Call finished without a clear answer (silence, hang-up, turn limit)  |
| `no_answer`             | Busy or no answer, even after a redial                               |
| `unreachable` / `error` | Twilio couldn't place the call                                       |
| `timeout`               | Ran past `CALL_TIMEOUT`; the agent hangs up                          |
| `cancelled`             | A better-ranked hotel was verified first, so this call was cut short |
| `no_phone`              | No phone number found; never called                                  |

### The confirmation email

Everything the guest might need later, in both HTML and plain text:

- **The booking:** hotel, address, phone, dates, nights, guests, guest name
- **Price:** the rate confirmed by phone _and_ the listed rate, estimated total, taxes/fees
- **Terms:** room type, cancellation policy, check-in/check-out times
- **Proof:** who confirmed it, any reference number, when it was verified, and the booking link
- **Every option checked,** with its result and phone number, so the user has backups
- **The full call transcript**
- **Cost stats:** agent time, tokens, call minutes

If nothing could be verified, the email still goes out marked `[NOT VERIFIED]`, with every option checked.

### Why it's fast and cheap

| Choice                                               | Saves                                                           |
| ---------------------------------------------------- | --------------------------------------------------------------- |
| Parallel verification calls                          | Total call time ≈ the slowest needed call, not the sum          |
| Early stop + Twilio hang-up                          | Stops paying for call minutes the moment the outcome is decided |
| "No availability" ends the call at once              | No wasted turns on dead options                                 |
| Regex parse, deterministic ranking, templated opener | Zero tokens outside the conversations                           |
| Inline `Twiml` on call create                        | No webhook round trip before the hotel hears anything           |
| History capped at 6 messages, stateless `memory: "off"` | We pay only for the window we send, never Backboard-side state |
| HTTP/2 + pre-warmed connections                      | TLS handshakes off the critical path                            |

## API payloads

**SerpApi search** (`GET https://serpapi.com/search.json`)

```
engine=google_hotels q="hotels in Toronto" check_in_date check_out_date adults currency=CAD gl=ca
sort_by=3 rating=7 api_key
```

**SerpApi property details** (same params + `property_token`) → `phone`, `address`, `link`,
`check_in_time`, `check_out_time`. Fallback: `engine=google_maps type=search q="<hotel>, <city>"` →
`place_results.phone` or `local_results[0].phone`.

**Twilio create call** (`POST /2010-04-01/Accounts/{SID}/Calls.json`)

```
To=<hotel phone or PHONE_OVERRIDE>  From=<your Twilio number>
Twiml=<Response><Gather input="speech" action="{PUBLIC_URL}/turn?c=2" …><Say>opener</Say></Gather></Response>
StatusCallback={PUBLIC_URL}/status?c=2  StatusCallbackEvent=completed  Timeout=20
```

The `?c=<index>` on both URLs is how the server knows which hotel's conversation a webhook belongs to.

**Twilio hang-up** (`POST …/Calls/{CallSid}.json`, `Status=completed`) is used for early stop and timeouts.

**Backboard message** (`POST https://app.backboard.io/api/threads/messages`, header `X-API-Key`)

```json
{
  "content": "Hotel: we have a queen\nYou: how much?\nHotel: one twenty nine",
  "system_prompt": "<system_prompt(run, c)>",
  "json_output": true,
  "memory": "off",
  "stream": false,
  "llm_provider": "cerebras",
  "model_name": "openai/gpt-oss-120b"
}
```

No `thread_id`, so each turn is stateless and we send our own 6-message window. Reply fields used:
`content` (the JSON below), `input_tokens`, `output_tokens`, and `status` — which is `"FAILED"`,
on an HTTP **200**, when the upstream model errors. Measured ~0.8s/turn, ~$0.0003/turn.
Spend to date: `GET /api/billing/usage/summary`.

**ElevenLabs TTS** (`POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}`, header `xi-api-key`)

```
?output_format=mp3_22050_32
{"text": "<the line>", "model_id": "eleven_flash_v2_5",
 "voice_settings": {"speed": 1.2, "stability": 0.5, "similarity_boost": 0.75}}
```

Returns raw MP3, cached in `AUDIO` under a hash of the text and served by `GET /audio/{key}` so
Twilio can `<Play>` it off the tunnel. Openers are generated during `pick_shortlist()`, in parallel
with the phone lookups, so they add no latency at call time. Any failure or timeout falls back to
`<Say voice=TTS_VOICE>` - the line never goes dead. `ELEVEN_MODE=opener` restricts `<Play>` to
pre-generated openers; `off` disables it entirely.

**LLM reply contract**

```json
{
  "say": "And what's the cancellation policy?",
  "done": false,
  "available": true,
  "price": 115,
  "fees": "13% HST",
  "room_type": "Queen",
  "cancellation": null,
  "check_in_time": null,
  "reference": null,
  "staff_name": null
}
```

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env              # fill in keys
cloudflared tunnel --url http://localhost:8080   # or: ngrok http 8080 → put URL in PUBLIC_URL
.venv/bin/python test-agent.py    # offline tests, no keys needed
```

## Commands

Run from the repo folder. Anything that phones needs the tunnel up and its URL in `PUBLIC_URL`.
Any `.env` value can be overridden for one run by prefixing it, e.g.
`PUBLIC_URL=https://x.trycloudflare.com PHONE_OVERRIDE=+1XXXXXXXXXX .venv/bin/python ...`.

**Tests (free, no keys, no network)**
```bash
.venv/bin/python test-agent.py
```

**Hotel search: prices + phones, no calls** (`hotel_search.py`, SerpApi)
```bash
.venv/bin/python hotel_search.py Toronto 2026-10-18                      # 10 cheapest, 1 night, 2 adults (1 credit)
.venv/bin/python hotel_search.py Toronto 2026-10-18 --phones --top 5     # + phone numbers (+1 credit per hotel)
.venv/bin/python hotel_search.py "New York" 2026-11-02 --nights 2 --adults 1
.venv/bin/python hotel_search.py Toronto 2026-10-18 --near "CN Tower" --radius 2     # radius around a place
.venv/bin/python hotel_search.py Toronto 2026-10-18 --near 43.6426,-79.3871 --radius 1.5 --pages 2
.venv/bin/python hotel_search.py Halifax 2026-10-18 --fresh              # skip SerpApi's ~1h cache
.venv/bin/python hotel_search.py Toronto 2026-10-18 --json               # raw rows instead of a table
.venv/bin/python hotel_search.py -h                                      # all options
```

**Prompt → list, no calls** (`pipeline.py --list`: Backboard parse, 20 km radius, phones)
```bash
.venv/bin/python pipeline.py --list "I'm looking for Whitby, for 2 people, at October 18 to October 19"
```

**Full pipeline: parse → search → phone the 3 cheapest → read transcripts → cheapest confirmed**
```bash
.venv/bin/python pipeline.py "King room in Toronto, Oct 18 to 19, 2 adults, send to judge@x.com"
PHONE_OVERRIDE=+1XXXXXXXXXX .venv/bin/python pipeline.py "..."   # every call rings your cell, one at a time
```
Tuning: `CALL_BATCH` (3 per round), `MAX_ROUNDS` (3), `RADIUS_KM` (20, 0 = off), `SEARCH_PAGES` (1).

**One test call, no search, no email** (`call_test.py`; needs `PHONE_OVERRIDE`)
```bash
.venv/bin/python call_test.py                         # "Test Inn" at 129
.venv/bin/python call_test.py "Good Value Inn" 149    # your own hotel name and nightly rate
```

**Original single-file agent** (`agent.py`: SerpApi search, parallel calls, first verified wins, email)
```bash
.venv/bin/python agent.py "Find the best hotel deal in Toronto for 2026-10-18 to 2026-10-19, send to judge@x.com"
```

## Testing with your own phone

Set `PHONE_OVERRIDE=<your cell>`. Every call goes to you, **one at a time, best-ranked first**, and
the run stops at the first one you verify. To demo the multi-call flow, answer the first call
with "sorry, we're fully booked", then confirm the second one.

## Pre-bake-off checklist

- [ ] Twilio account **upgraded** (trial accounts can only dial verified numbers, and they play a preamble)
- [ ] Twilio Geo Permissions allow Canada/US
- [ ] Confirm `BACKBOARD_MODEL` still routes: one live call, not just a catalogue listing
      (`GET /api/models/provider/cerebras`). A 200 with `status: "FAILED"` is the failure mode.
- [ ] SerpApi credits: each run uses about 1 + K lookups (a few more if hotels lack phone numbers)
- [ ] Tunnel up, and `PUBLIC_URL` matches it
- [ ] Test receipt lands in the inbox, not spam
- [ ] `call_test.py` against your own cell, then one real end-to-end `agent.py` run with `PHONE_OVERRIDE` - listen for the ElevenLabs voice on the
      opener; if you hear Polly instead, `<Play>` failed and it fell back
- [ ] Ask the organizers: do agents call the real hotels, or a judge-run line? If it's one line,
      set `PHONE_OVERRIDE` to it (calls then go out one at a time so they don't collide)

## Known risks

- **Calling real hotels:** the agent says it's automated and doesn't reserve anything by default,
  but you're still placing AI calls to real businesses. Confirm with the organizers that this is expected.
- **Phone menus and voicemail** ("press 1 for reservations") aren't handled; those calls end as `unclear`.
- **Missing phone numbers:** if SerpApi has no phone for a hotel, it's skipped and backfilled.
- **Ranking is by listed price:** if the confirmed rate differs, the email shows both,
  but the winner is still chosen by listing rank.
- **One booking per process** (global `RUN`), which is all the bake-off needs.
