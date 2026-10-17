"""
Prompt -> Backboard parse -> search SerpApi + LiteAPI -> merge and rank -> phone the best CALL_BATCH
hotels -> Backboard reads every transcript into a CallQuery -> cheapest hotel that confirmed a room.
Nobody available -> next batch.

  python pipeline.py "King room in Toronto, Oct 18 to 19, 2 adults, send to judge@x.com"
  python pipeline.py --list "I'm looking for Toronto, for 3 people, Oct 18 to Oct 20"   # no calls

Reuses agent.py for the calls (/turn, /status, call_candidate) and the receipt. With
PHONE_OVERRIDE set, every call rings that one number, one after another.

LLM cost: 1 call to parse + the in-call turns + 1 call per batch to read all its transcripts.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import date, datetime, timedelta

import httpx
import uvicorn

import agent
import decider
import hotel_search as hs
import liteapi_search as lite
import ranker
from schemas import CallQuery, Hotel, HotelQuery
import mailer

E = agent.E
BATCH = int(E("CALL_BATCH", "15"))             # hotels phoned in parallel per round
MAX_ROUNDS = int(E("MAX_ROUNDS", "3"))         # give up after this many rounds with nobody available
RADIUS_KM = float(E("RADIUS_KM", "20"))        # only hotels this close to the city centre; 0 = off
SEARCH_PAGES = int(E("SEARCH_PAGES", "1"))     # google_hotels pages (~20 hotels, 1 credit each)
POLICY = decider.Policy(latency_cost=float(E("LATENCY_COST", "0.5")),    # currency we'd pay to save 1 s
                        segment_pct=float(E("SEGMENT_PCT", "0.10")),     # price band width, share of cheapest
                        deadline_s=agent.CALL_TIMEOUT,
                        call_s=agent.CALL_TIMEOUT * 2 / 3)               # typical call, until measured
COUNTRY = E("SERP_GL", "ca").upper()           # ISO-2 for LiteAPI nationality and the Places lookups


# ---------------------------------------------------------------- 1. parse
async def parse(prompt: str) -> dict:
    """Backboard first (handles 'this weekend', room types); regex parser if it fails."""
    today = date.today()
    try:
        out = await agent.llm([
            {"role": "system", "content": (
                f"Today is {today.isoformat()} ({today:%A}). Extract the hotel booking request as JSON: "
                '{"city":str,"check_in":"YYYY-MM-DD","check_out":"YYYY-MM-DD",'
                '"adults":int,"room_type":str|null,"email":str|null}. room_type is the bed/room asked '
                'for, e.g. "king", "two queen", "suite"; null if none. Default: 1 night from tomorrow, '
                "1 adult.")},
            {"role": "user", "content": prompt},
        ])
    except Exception as e:
        print(f"[parse] backboard failed, using regex: {e!r}", file=sys.stderr)
        out = {}
    req = {}
    if not out.get("city"):
        try:
            req = await agent.parse_request(prompt)   # regex; may itself try the LLM once
        except Exception as e:
            print(f"[parse] regex fallback failed: {e!r}", file=sys.stderr)
    req = {"city": None, "check_in": None, "check_out": None, "adults": 1,
           "room_type": None, "email": None, **req, **{k: v for k, v in out.items() if v}}
    try:
        cin = date.fromisoformat(req["check_in"])
    except (TypeError, ValueError):
        cin = today + timedelta(days=1)
    try:
        cout = date.fromisoformat(req["check_out"])
    except (TypeError, ValueError):
        cout = cin
    if cout <= cin:
        cout = cin + timedelta(days=1)
    # the address typed in the prompt wins, exactly as written; the LLM's copy and JUDGE_EMAIL are fallbacks
    m = agent.EMAIL_RE.search(prompt)
    prompt_email = m.group(0).rstrip(".") if m else None
    req.update(check_in=cin.isoformat(), check_out=cout.isoformat(), adults=int(req["adults"] or 1),
               email=prompt_email or req["email"] or agent.JUDGE_EMAIL)
    return req


# ---------------------------------------------------------------- 2. search
async def search(req: dict) -> list[Hotel]:
    """SerpApi and LiteAPI in parallel, merged (a hotel found by both keeps its cheaper price) and
    ranked: which to phone first. One source failing is fine; both failing raises. No room types:
    those are asked about on the call. LiteAPI brings phones; SerpApi phones are looked up lazily."""
    q = HotelQuery(city=req["city"], country=COUNTRY, check_in=req["check_in"], check_out=req["check_out"],
                   adults=req["adults"], currency=agent.CURRENCY,
                   radius_m=int(RADIUS_KM * 1000) if RADIUS_KM else 100_000,
                   limit=BATCH * MAX_ROUNDS * 2)            # spare hotels for ones with no phone
    sources = {"serpapi": hs.find_hotels(q, phones=False, pages=SEARCH_PAGES), "liteapi": lite.find_hotels(q)}
    results = await asyncio.gather(*sources.values(), return_exceptions=True)
    lists = []
    for name, r in zip(sources, results):
        if isinstance(r, BaseException):
            print(f"[search] {name} failed: {r!r}", file=sys.stderr)
        else:
            lists.append(r)
    if not lists:
        raise next(r for r in results if isinstance(r, BaseException))
    return ranker.rank(ranker.merge(*lists))


async def phone(h: Hotel, req: dict):
    """Fill `h.phone` if missing: SerpApi details page (SerpApi hotels only), then Google Places
    if keyed, then a Google Maps search."""
    if h.phone:
        return
    row = {"hotel_id": h.id if h.source == "serpapi" else None, "name": h.name,
           "address": h.address, "phone": None}
    if row["hotel_id"]:
        await hs.serp_phone(row, hs._serp_params(req["city"], req["check_in"], req["check_out"], req["adults"]))
    if not row["phone"] and hs.PLACES_KEY:
        await hs.phone_for(row, req["city"])
    if not row["phone"]:
        await hs.maps_phone(row, req["city"])
    h.phone, h.address = row["phone"], row["address"] or h.address


def candidate(i: int, h: Hotel, nights: int) -> agent.Candidate:
    c = agent.Candidate(i, {"name": h.name, "overall_rating": h.review_rating, "reviews": h.review_count},
                        h.price / nights, h.score or h.price)
    c.total, c.address, c.phone, c.km, c.hotel = h.price, h.address, h.phone, h.distance_km, h
    return c


# ---------------------------------------------------------------- 3. calls
async def race(run: agent.Run, batch: list[agent.Candidate]) -> agent.Candidate | None:
    """Phone the whole batch at once and decide as the calls finish, not when the last one does.

    Each call is its own task: it dials (dropped after CALL_TIMEOUT), reads its own transcript, then
    posts itself on a queue. We take events one at a time, update the price table and ask
    `decider.Board.decide()` whether another call is still worth waiting for. When it says stop, the
    calls still ringing are hung up. Returns the winning candidate, or None if nobody confirmed."""
    key = lambda c: str(c.idx)
    n = agent.nights(run.req)
    board = decider.Board([decider.Entry(key(c), c.name, c.total or c.price * n) for c in batch], POLICY)
    events: asyncio.Queue = asyncio.Queue()
    gate = asyncio.Semaphore(1 if agent.PHONE_OVERRIDE else len(batch))   # one test line: one call at a time
    t0 = time.perf_counter()
    clock = lambda: time.perf_counter() - t0

    async def one(c: agent.Candidate):
        try:
            async with gate:
                board.start(key(c), clock())
                try:
                    await asyncio.wait_for(agent.call_candidate(run, c), agent.CALL_TIMEOUT)
                except asyncio.TimeoutError:
                    c.status = "timeout"                 # too late: whatever was said is ignored
            if c.status != "timeout":
                talked = c.history and c.status != "unavailable"      # a clear "no" needs no second reading
                facts = await read_calls(run, [c]) if talked else {}
                apply(run, c, facts.get(c.idx))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[call] {c.name}: {e!r}", file=sys.stderr)
            c.status = "error"
        await events.put(c)

    tasks = [asyncio.create_task(one(c)) for c in batch]
    by_key = {key(c): c for c in batch}
    decision = decider.Decision(False, None, "")
    try:
        while True:
            try:
                c = await asyncio.wait_for(events.get(), 1.0)         # a quiet second still re-runs the maths
                ok = bool(c.call and c.call.available)
                board.answer(key(c), clock(), ok, c.call.price if ok else None)
                print(f"[{clock():5.1f}s] {c.name}: {c.status}"
                      + (f", {c.call.price:.0f} for the stay" if ok and c.call.price is not None else ""),
                      file=sys.stderr)
            except asyncio.TimeoutError:
                pass
            decision = board.decide(clock())
            if decision.stop:
                break
    finally:
        for t in tasks:
            t.cancel()                                    # hangs up whoever is still ringing
        await asyncio.gather(*tasks, return_exceptions=True)
    print(board.render(clock()) + f"\n=> {decision.reason}", file=sys.stderr)
    return by_key.get(decision.winner)


# ---------------------------------------------------------------- 4. read the transcripts
def transcript(run: agent.Run, c: agent.Candidate) -> str:
    lines = [f"Agent: {agent.opener(run, c)}"]       # the opener isn't kept in c.history
    lines += [f"{'Hotel' if m['role'] == 'user' else 'Agent'}: {m['content']}" for m in c.history]
    return "\n".join(lines)


async def read_calls(run: agent.Run, batch: list[agent.Candidate]) -> dict[int, dict]:
    """ONE Backboard call for the whole batch. Returns {cand idx: extracted facts}."""
    talked = [c for c in batch if c.history]
    if not talked:
        return {}
    q = run.req
    calls = "\n\n".join(f"CALL {c.idx}: {c.name} (listed {c.price:.0f} {agent.CURRENCY}/night)\n"
                        f"{transcript(run, c)}" for c in talked)
    try:
        out = await agent.llm([
            {"role": "system", "content": (
                f"You read phone transcripts where an assistant asked hotels about a stay: "
                f"{q['check_in']} to {q['check_out']} ({agent.nights(q)} nights), {q['adults']} adult(s)"
                f"{', room type ' + q['room_type'] if q.get('room_type') else ''}, prices in {agent.CURRENCY}. "
                "Use only what the HOTEL said; null when not stated. Never guess. "
                'Reply JSON: {"calls":[{"id":int,"available":bool|null,"nightly_price":number|null,'
                '"total_price":number|null,"taxes_included":bool|null,"fees":str|null,"room_type":str|null,'
                '"check_in":"YYYY-MM-DD"|null,"check_out":"YYYY-MM-DD"|null,"cancellation":str|null,'
                '"reference":str|null,"summary":str}]}. available=true only if the hotel confirmed a room for these '
                'dates. summary: what the hotel said, in at most 25 words.')},
            {"role": "user", "content": calls},
        ])
    except Exception as e:  # fall back to what the in-call LLM already collected
        print(f"[read] backboard failed: {e!r}", file=sys.stderr)
        return {}
    return {int(x["id"]): x for x in out.get("calls") or [] if str(x.get("id", "")).isdigit()}


def apply(run: agent.Run, c: agent.Candidate, x: dict | None):
    """Transcript reading overrides the live call's guess; keep live facts it didn't cover.
    Also fills `c.call`, the CallQuery that the final sort reads."""
    if x:
        for k, v in (("available", x.get("available")), ("price", x.get("nightly_price")),
                     ("total", x.get("total_price")), ("taxes_included", x.get("taxes_included")),
                     ("fees", x.get("fees")), ("room_type", x.get("room_type")),
                     ("cancellation", x.get("cancellation")), ("reference", x.get("reference")),
                     ("check_in", x.get("check_in")), ("check_out", x.get("check_out"))):
            if v is not None:
                c.facts[k] = v
    f, q = c.facts, run.req
    wrong_dates = (f.get("check_in") not in (None, q["check_in"])
                   or f.get("check_out") not in (None, q["check_out"]))
    if c.history:          # only reclassify calls that reached a person
        c.status = ("verified" if f.get("available") is True and not wrong_dates
                    else "unavailable" if f.get("available") is False or wrong_dates else "unclear")
    if c.status == "verified" and not c.verified_at:
        c.verified_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    if c.history:
        c.call = CallQuery(transcript=transcript(run, c), call_url=call_url(c.sid), summary=(x or {}).get("summary") or "",
                           price=quoted_total(run, c), available=False if wrong_dates else f.get("available"))
        c.hotel.call_query = c.call


def quoted_total(run: agent.Run, c: agent.Candidate) -> float | None:
    """What the hotel quoted for the whole stay: its total, else its nightly rate x nights."""
    f = c.facts
    if isinstance(f.get("total"), (int, float)):
        return float(f["total"])
    if isinstance(f.get("price"), (int, float)):
        return float(f["price"]) * agent.nights(run.req)
    return None


def call_url(sid: str | None) -> str | None:
    return f"https://console.twilio.com/us1/monitor/logs/calls/{sid}" if sid else None


def stay_cost(run: agent.Run, c: agent.Candidate) -> float:
    """Quoted stay price, else the listing (an unconfirmed price sorts last)."""
    quoted = c.call.price if c.call else quoted_total(run, c)
    return quoted if quoted is not None else 1e9 + (c.total or c.price * agent.nights(run.req))


# ---------------------------------------------------------------- orchestration
async def run_pipeline(run: agent.Run) -> agent.Candidate | None:
    run.req = await parse(run.prompt)
    run.mark("parse")
    if not run.req.get("city"):
        raise SystemExit("Could not find a city in the prompt.")
    print("request:", json.dumps(run.req), file=sys.stderr)

    hotels = await search(run.req)
    run.cands = [candidate(i, h, agent.nights(run.req)) for i, h in enumerate(hotels)]
    run.mark("search")
    print(f"search:  {len(hotels)} priced options", file=sys.stderr)

    pool = list(run.cands)
    for rnd in range(1, MAX_ROUNDS + 1):
        batch = []
        while len(batch) < BATCH and pool:          # backfill past hotels with no phone
            take, pool = pool[:BATCH - len(batch)], pool[BATCH - len(batch):]
            await asyncio.gather(*(phone(c.hotel, run.req) for c in take))
            for c in take:
                c.phone, c.address = c.hotel.phone, c.hotel.address or c.address
                if c.phone or agent.PHONE_OVERRIDE:
                    batch.append(c)
                else:
                    c.status = "no_phone"
        if not batch:
            break
        print(f"round {rnd}: calling " + ", ".join(f"{c.name} ({c.price:.0f})" for c in batch),
              file=sys.stderr)
        await asyncio.gather(agent.tts(agent.BYE),                          # pre-build openers + goodbye
                             *(agent.tts(agent.opener(run, c)) for c in batch))
        winner = await race(run, batch)
        run.mark(f"round{rnd}")
        if winner:
            run.winner = winner
            return winner
    return None


def report(run: agent.Run) -> dict:
    n = agent.nights(run.req) if run.req.get("check_in") else 1    # empty if we stopped before parsing
    called = [c for c in run.cands if c.attempts or c.status == "no_phone"]
    return {
        "request": run.req,
        "winner": ({**run.winner.summary(), "stay_cost": stay_cost(run, run.winner)}
                   if run.winner else None),
        "called": [{"name": c.name, "listed_night": c.price, "status": c.status, "phone": c.phone,
                    "distance_km": c.km, "summary": c.call.summary if c.call else None,
                    "quoted_night": c.facts.get("price"),
                    "quoted_total": c.facts.get("total") or (c.facts.get("price") or 0) * n or None,
                    "room_type": c.facts.get("room_type"), "fees": c.facts.get("fees"),
                    "transcript": c.call.transcript if c.call else None} for c in called],
        "timings_s": run.marks,
        "llm": {"calls": run.llm_calls, "tokens_in": run.tok_in, "tokens_out": run.tok_out},
        "requests": dict(hs.COUNT),
        "tts": {"calls": run.tts_calls, "chars": run.tts_chars},
        "call_seconds": sum(c.billed_seconds for c in run.cands),
    }


# ---------------------------------------------------------------- preflight
def preflight(calling: bool = True):
    """Fail loudly on a missing key instead of a 401 traceback mid-run."""
    if not hs.SERPAPI_KEY:
        raise SystemExit("SERPAPI_KEY is empty in .env - no search, no phone numbers.")
    if not agent.BB_KEY:
        raise SystemExit("BACKBOARD_API_KEY is empty in .env - cannot parse or read transcripts.")
    if calling and not (agent.TW_SID and agent.TW_TOKEN and agent.TW_FROM):
        raise SystemExit("Twilio is not configured (TWILIO_ACCOUNT_SID/AUTH_TOKEN/FROM).")
    if calling and not agent.PUBLIC_URL:
        print("[preflight] PUBLIC_URL is empty: no /turn webhooks, calls go nowhere.", file=sys.stderr)


def hotel_table(hotels: list[Hotel], req: dict) -> str:
    if not hotels:
        return "No matching rates."
    out = [f"{'#':>2}  {'hotel':<40} {'total':>9} {'/night':>8} {'km':>6} {'src':<8} phone"]
    n = agent.nights(req)
    for i, h in enumerate(hotels, 1):
        km = f"{h.distance_km:.1f}" if h.distance_km is not None else "-"
        out.append(f"{i:>2}  {h.name[:40]:<40} {h.price:>9.2f} {h.price / n:>8.2f} {km:>6} {h.source:<8} {h.phone or '-'}")
    out.append(f"prices in {agent.CURRENCY}; total for {n} night(s)")
    return "\n".join(out)


def email_items(run: agent.Run) -> tuple[dict, dict | None, list[dict], dict]:
    """Run state -> the plain dicts mailer.build() takes."""
    n = agent.nights(run.req)

    def item(c: agent.Candidate) -> dict:
        f = c.facts
        return {"name": c.name, "address": c.address, "phone": c.phone, "km": getattr(c, "km", None),
                "listed_night": c.price, "quoted_night": f.get("price"), "quoted_total": f.get("total"),
                "fees": f.get("fees"), "room_type": f.get("room_type"), "cancellation": f.get("cancellation"),
                "reference": f.get("reference"), "status": c.status, "verified_at": c.verified_at,
                "summary": c.call.summary if c.call else "",
                "transcript": transcript(run, c) if c.history else None}

    called = [c for c in run.cands if c.attempts or c.status == "no_phone"]
    items = {c.idx: item(c) for c in called}
    winner = items.get(run.winner.idx) if run.winner else None
    stats = {"Total run time (s)": round(time.perf_counter() - run.t0, 1),
             "Hotels phoned": sum(1 for c in called if c.attempts),
             "Call minutes": round(sum(c.billed_seconds for c in run.cands) / 60, 2),
             "LLM calls / tokens in / out": f"{run.llm_calls} / {run.tok_in} / {run.tok_out}",
             "SerpApi requests": hs.COUNT["serpapi"]}
    return {**run.req, "nights": n}, winner, list(items.values()), stats


async def winner_recording(run: agent.Run) -> list[tuple[str, bytes, str]]:
    """The winning call's Twilio recording as an email attachment; [] if off or unavailable."""
    if not (agent.RECORD_CALLS and run.winner):
        return []
    mp3 = await agent.recording_mp3(run.winner.sid)
    if not mp3:
        print("[email] winner's recording not available; sending without it", file=sys.stderr)
        return []
    safe = "".join(ch if ch.isalnum() else "-" for ch in run.winner.name).strip("-")[:40]
    return [(f"call-{safe}-{run.req['check_in']}.mp3", mp3, "audio/mpeg")]


async def list_only(prompt: str, top: int = 10):
    """Parse + search + phones, print the table, phone nobody."""
    preflight(calling=False)
    agent.HTTP = hs.HTTP = httpx.AsyncClient(http2=True, timeout=15)
    agent.RUN = run = agent.Run(prompt)
    try:
        run.req = await parse(prompt)
        print("request:", json.dumps(run.req))
        hotels = (await search(run.req))[:top]
        await asyncio.gather(*(phone(h, run.req) for h in hotels))
        print(hotel_table(hotels, run.req))
        print(f"radius {RADIUS_KM:g} km from {run.req['city']} centre | llm {run.llm_calls} call(s), "
              f"{run.tok_in}/{run.tok_out} tokens | requests {hs.COUNT}")
    finally:
        await agent.HTTP.aclose()


async def tunnel_ok() -> str | None:
    """Fetch our own server through PUBLIC_URL. A dead tunnel otherwise shows up only as Twilio
    saying 'an application error has occurred' on a live call. Returns the problem, or None."""
    if not agent.PUBLIC_URL:
        return "PUBLIC_URL is empty"
    try:
        r = await agent.HTTP.get(f"{agent.PUBLIC_URL}/audio/preflight", timeout=5)
    except Exception as e:
        return f"{agent.PUBLIC_URL} unreachable ({type(e).__name__})"
    return None if r.status_code == 200 else f"{agent.PUBLIC_URL} answered HTTP {r.status_code}"


async def main(prompt: str):
    preflight()
    agent.HTTP = hs.HTTP = httpx.AsyncClient(http2=True, timeout=15)
    agent.RUN = run = agent.Run(prompt)
    server = uvicorn.Server(uvicorn.Config(agent.app, host="0.0.0.0", port=agent.PORT, log_level="warning"))
    srv = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        if problem := await tunnel_ok():
            raise SystemExit(f"tunnel check failed: {problem}. Start cloudflared "
                             f"(--url http://localhost:{agent.PORT}) and put its URL in PUBLIC_URL.")
        await run_pipeline(run)
        if not run.req.get("email"):
            print("[email] skipped: no address in the prompt and JUDGE_EMAIL is empty", file=sys.stderr)
        elif problem := mailer.configured():
            print(f"[email] skipped: {problem}", file=sys.stderr)
        else:
            try:
                await mailer.send(run.req["email"], *mailer.build(*email_items(run)), client=agent.HTTP,
                                  attachments=await winner_recording(run))
                run.mark("email")
                print(f"[email] sent to {run.req['email']}", file=sys.stderr)
            except Exception as e:      # keep the report: the calls already cost money
                print(f"[email] FAILED: {e!r}", file=sys.stderr)
    finally:
        print(json.dumps(report(run), indent=2, default=str))
        await asyncio.sleep(1.0)                    # let Twilio's last /status callbacks land
        server.should_exit = True
        await srv
        await agent.HTTP.aclose()


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--list":
        asyncio.run(list_only(" ".join(args[1:]) or sys.stdin.read()))
    else:
        asyncio.run(main(" ".join(args) or sys.stdin.read()))
