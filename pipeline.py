"""
Prompt -> Backboard parse -> hotel_search -> phone the cheapest CALL_BATCH hotels -> Backboard reads
every transcript -> cheapest hotel that actually confirmed. Nobody available -> next batch.

  python pipeline.py "King room in Toronto, Oct 18 to 19, 2 adults, send to judge@x.com"
  python pipeline.py --list "I'm looking for Toronto, for 3 people, Oct 18 to Oct 20"   # no calls

Reuses agent.py for the calls (/turn, /status, call_candidate) and the receipt. With
PHONE_OVERRIDE set, every call rings that one number, one after another.

LLM cost: 1 call to parse + the in-call turns + 1 call per batch to read all its transcripts.
"""
import asyncio
import json
import os
import sys
from datetime import date, datetime, timedelta

import httpx
import uvicorn

import agent
import hotel_search as hs

E = agent.E
BATCH = int(E("CALL_BATCH", "3"))              # hotels phoned per round
MAX_ROUNDS = int(E("MAX_ROUNDS", "3"))         # give up after this many rounds with nobody available
RADIUS_KM = float(E("RADIUS_KM", "20"))        # only hotels this close to the city centre; 0 = off
SEARCH_PAGES = int(E("SEARCH_PAGES", "1"))     # google_hotels pages (~20 hotels, 1 credit each)


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
    req.update(check_in=cin.isoformat(), check_out=cout.isoformat(), adults=int(req["adults"] or 1),
               email=req["email"] or agent.JUDGE_EMAIL)
    return req


# ---------------------------------------------------------------- 2. search
async def search(req: dict) -> list[dict]:
    """SerpApi Google Hotels, cheapest first, within RADIUS_KM of the city centre (one extra
    google_maps search to locate it). No room types: those are asked about on the call."""
    return await hs.search(req["city"], req["check_in"], agent.nights(req), req["adults"],
                           top=BATCH * MAX_ROUNDS * 2,      # spare rows for hotels with no phone
                           near=req["city"] if RADIUS_KM else None, radius_km=RADIUS_KM,
                           pages=SEARCH_PAGES)


async def phone(row: dict, req: dict):
    """Details page by token, then Google Places if keyed, then a Google Maps search."""
    if row["hotel_id"]:
        await hs.serp_phone(row, hs._serp_params(req["city"], req["check_in"], req["check_out"], req["adults"]))
    if not row["phone"] and hs.PLACES_KEY:
        await hs.phone_for(row, req["city"])
    if not row["phone"]:
        await hs.maps_phone(row, req["city"])


def candidate(i: int, row: dict) -> agent.Candidate:
    c = agent.Candidate(i, {"name": row["name"], "overall_rating": row.get("rating")},
                        row["per_night"], row["total"])
    c.total, c.address, c.phone = row["total"], row.get("address"), row.get("phone")
    return c


# ---------------------------------------------------------------- 3. calls
async def call_batch(run: agent.Run, batch: list[agent.Candidate]):
    """Every call runs to the end: the quoted price can reorder the list, so no early stop."""
    if agent.PHONE_OVERRIDE:                      # one line: one call at a time
        for c in batch:
            await agent.call_candidate(run, c)
    else:
        await asyncio.gather(*(agent.call_candidate(run, c) for c in batch))


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
                '"reference":str|null}]}. available=true only if the hotel confirmed a room for these dates.')},
            {"role": "user", "content": calls},
        ])
    except Exception as e:  # fall back to what the in-call LLM already collected
        print(f"[read] backboard failed: {e!r}", file=sys.stderr)
        return {}
    return {int(x["id"]): x for x in out.get("calls") or [] if str(x.get("id", "")).isdigit()}


def apply(run: agent.Run, c: agent.Candidate, x: dict | None):
    """Transcript reading overrides the live call's guess; keep live facts it didn't cover."""
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


def stay_cost(run: agent.Run, c: agent.Candidate) -> float:
    """Quoted total, else quoted nightly x nights, else the listing (unconfirmed price sorts last)."""
    f, n = c.facts, agent.nights(run.req)
    if isinstance(f.get("total"), (int, float)):
        return float(f["total"])
    if isinstance(f.get("price"), (int, float)):
        return float(f["price"]) * n
    return 1e9 + (c.total or c.price * n)


# ---------------------------------------------------------------- orchestration
async def run_pipeline(run: agent.Run) -> agent.Candidate | None:
    run.req = await parse(run.prompt)
    run.mark("parse")
    if not run.req.get("city"):
        raise SystemExit("Could not find a city in the prompt.")
    print("request:", json.dumps(run.req), file=sys.stderr)

    rows = await search(run.req)
    run.cands = [candidate(i, r) for i, r in enumerate(rows)]
    run.mark("search")
    print(f"search:  {len(rows)} priced options", file=sys.stderr)

    pool = list(run.cands)
    for rnd in range(1, MAX_ROUNDS + 1):
        batch = []
        while len(batch) < BATCH and pool:          # backfill past hotels with no phone
            take, pool = pool[:BATCH - len(batch)], pool[BATCH - len(batch):]
            await asyncio.gather(*(phone(rows[c.idx], run.req) for c in take))
            for c in take:
                c.phone, c.address = rows[c.idx]["phone"], rows[c.idx]["address"] or c.address
                if c.phone or agent.PHONE_OVERRIDE:
                    batch.append(c)
                else:
                    c.status = "no_phone"
        if not batch:
            break
        print(f"round {rnd}: calling " + ", ".join(f"{c.name} ({c.price:.0f})" for c in batch),
              file=sys.stderr)
        await asyncio.gather(*(agent.tts(agent.opener(run, c)) for c in batch))   # pre-build openers
        await call_batch(run, batch)
        facts = await read_calls(run, batch)
        for c in batch:
            apply(run, c, facts.get(c.idx))
        ok = sorted((c for c in batch if c.status == "verified"), key=lambda c: stay_cost(run, c))
        run.mark(f"round{rnd}")
        if ok:
            run.winner = ok[0]
            return ok[0]
    return None


def report(run: agent.Run) -> dict:
    n = agent.nights(run.req)
    called = [c for c in run.cands if c.attempts or c.status == "no_phone"]
    return {
        "request": run.req,
        "winner": ({**run.winner.summary(), "stay_cost": stay_cost(run, run.winner)}
                   if run.winner else None),
        "called": [{"name": c.name, "listed_night": c.price, "status": c.status, "phone": c.phone,
                    "quoted_night": c.facts.get("price"),
                    "quoted_total": c.facts.get("total") or (c.facts.get("price") or 0) * n or None,
                    "room_type": c.facts.get("room_type"), "fees": c.facts.get("fees"),
                    "transcript": transcript(run, c) if c.history else None} for c in called],
        "timings_s": run.marks,
        "llm": {"calls": run.llm_calls, "tokens_in": run.tok_in, "tokens_out": run.tok_out},
        "requests": dict(hs.COUNT),
        "tts": {"calls": run.tts_calls, "chars": run.tts_chars},
        "call_seconds": sum(c.billed_seconds for c in run.cands),
    }


async def list_only(prompt: str, top: int = 10):
    """Parse + search + phones, print the table, phone nobody."""
    agent.HTTP = hs.HTTP = httpx.AsyncClient(http2=True, timeout=15)
    agent.RUN = run = agent.Run(prompt)
    try:
        run.req = await parse(prompt)
        print("request:", json.dumps(run.req))
        rows = (await search(run.req))[:top]
        await asyncio.gather(*(phone(r, run.req) for r in rows))
        print(hs.table(rows))
        print(f"radius {RADIUS_KM:g} km from {run.req['city']} centre | llm {run.llm_calls} call(s), "
              f"{run.tok_in}/{run.tok_out} tokens | requests {hs.COUNT}")
    finally:
        await agent.HTTP.aclose()


async def main(prompt: str):
    agent.HTTP = hs.HTTP = httpx.AsyncClient(http2=True, timeout=15)
    agent.RUN = run = agent.Run(prompt)
    server = uvicorn.Server(uvicorn.Config(agent.app, host="0.0.0.0", port=agent.PORT, log_level="warning"))
    srv = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        await run_pipeline(run)
        if run.req.get("email") and (agent.RESEND_KEY or agent.SMTP_USER):
            await agent.send_receipt(run)
            run.mark("email")
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
