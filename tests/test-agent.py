"""Offline smoke tests: every outside service is faked. Run: python tests/test-agent.py"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root, for agent.py etc.

import asyncio

from fastapi.testclient import TestClient

import agent

REAL_LLM = agent.llm          # t_parse et al. monkeypatch agent.llm; keep the real dispatcher

PROPS = [
    {"name": "Cheap Dump", "rate_per_night": {"extracted_lowest": 90}, "overall_rating": 2.9, "property_token": "t0"},
    {"name": "Good Value Inn", "rate_per_night": {"extracted_lowest": 110}, "overall_rating": 4.3, "property_token": "t1"},
    {"name": "Midtown Suites", "rate_per_night": {"extracted_lowest": 130}, "overall_rating": 4.4, "property_token": "t2"},
    {"name": "Lakeshore Hotel", "rate_per_night": {"extracted_lowest": 140}, "overall_rating": 4.5, "property_token": "t3"},
    {"name": "Fancy", "rate_per_night": {"extracted_lowest": 260}, "overall_rating": 4.8, "property_token": "t4"},
    {"name": "No price"},
]
PHONES = {"t1": "(416) 555-0101", "t2": None, "t3": "+1 416-555-0103", "t4": "416.555.0104", "t0": "416-555-0100"}
REQ = {"city": "Toronto", "check_in": "2026-10-18", "check_out": "2026-10-20", "adults": 2, "email": "judge@techto.ca"}


def fresh():
    agent.PHONE_OVERRIDE = ""      # tests must not inherit a real .env
    agent.RUN = agent.Run("x")
    agent.RUN.req = dict(REQ)
    agent.RUN.cands = agent.rank(PROPS)
    return agent.RUN


async def t_parse():
    agent.RUN = agent.Run("x")
    calls = []

    async def fake_llm(m):
        calls.append(m)
        return {"city": "Montreal", "check_in": "2026-10-23", "check_out": "2026-10-25", "adults": 2}

    agent.llm = fake_llm
    p = await agent.parse_request("Find the best hotel deal in Toronto for 2026-10-18 to 2026-10-20 for 2 adults. Send to judge@techto.ca.")
    assert p == REQ and not calls, p
    p = await agent.parse_request("Book a hotel in October in New York City, 2026-11-02")
    assert p["city"] == "New York City" and p["check_out"] == "2026-11-03" and not calls, p
    await agent.parse_request("Something in Montreal this weekend, 2 nights")
    assert len(calls) == 1
    print("ok  parse: regex fast path, month-name skip, LLM fallback")


def t_rank_phone():
    order = [c.name for c in agent.rank(PROPS)]
    assert order[:3] == ["Good Value Inn", "Cheap Dump", "Midtown Suites"] and "No price" not in order, order
    assert agent.e164("(416) 555-0101") == "+14165550101"
    assert agent.e164("+44 20 7946 0958") == "+442079460958"
    assert agent.e164("1-800-555-0199") == "+18005550199"
    assert agent.e164("555-0101") is None
    print("ok rank + phone normalisation:", order)


async def t_shortlist():
    run = fresh()                  # PHONE_OVERRIDE off: a phone-less hotel must be skipped
    serp_log = []

    async def fake_serp(params):
        serp_log.append(params.get("property_token") or params["engine"])
        if params["engine"] == "google_maps":
            return {"local_results": [{"phone": None}]}
        return {"phone": PHONES[params["property_token"]], "address": "1 King St W"}

    agent.serp = fake_serp
    short = await agent.pick_shortlist(run)
    names = [c.name for c in short]
    # Midtown has no phone anywhere -> skipped, backfilled with the next option
    assert names == ["Good Value Inn", "Cheap Dump", "Lakeshore Hotel"], names
    assert [c.name for c in run.cands if c.status == "no_phone"] == ["Midtown Suites"]
    print("ok  shortlist backfill:", names, "| serp calls:", serp_log)
    return short


async def t_verify_parallel():
    """Rank 0 slow + unavailable, rank 1 fast + verified, rank 2 slow -> wait for 0, pick 1, hang up 2."""
    run = fresh()
    short = run.cands[:3]
    plan = {0: (0.30, "unavailable"), 1: (0.05, "verified"), 2: (5.0, "verified")}

    async def fake_call(run_, c):
        c.status = "calling"
        c.attempts = 1
        try:
            await asyncio.sleep(plan[c.idx][0])
            c.status = plan[c.idx][1]
        except asyncio.CancelledError:
            c.status = "cancelled"
            raise

    real = agent.call_candidate
    agent.call_candidate = fake_call
    agent.PHONE_OVERRIDE = ""
    t = asyncio.get_running_loop().time()
    await agent.verify_all(run, short)
    took = asyncio.get_running_loop().time() - t
    agent.call_candidate = real
    assert run.winner is short[1], run.winner and run.winner.name
    assert [c.status for c in short] == ["unavailable", "verified", "cancelled"]
    assert took < 1.0, took
    print(f"ok  parallel early stop: winner={run.winner.name}, took {took:.2f}s, statuses={[c.status for c in short]}")


async def t_verify_sequential():
    run = fresh()
    short = run.cands[:3]
    order = []

    async def fake_call(run_, c):
        order.append(c.idx)
        c.status = "unavailable" if c.idx == 0 else "verified"

    real = agent.call_candidate
    agent.call_candidate = fake_call
    agent.PHONE_OVERRIDE = "+15195550000"
    await agent.verify_all(run, short)
    agent.call_candidate, agent.PHONE_OVERRIDE = real, ""
    assert order == [0, 1] and run.winner is short[1]
    print("ok  PHONE_OVERRIDE sequential: dialed", order, "stopped at first verified")


async def t_call_cancel_hangs_up():
    run = fresh()
    c = run.cands[0]
    hung = []

    async def fake_place(run_, cand):
        cand.attempts += 1
        return "CA123"

    async def fake_hang(sid):
        hung.append(sid)

    agent.place_call, agent.hang_up = fake_place, fake_hang
    task = asyncio.create_task(agent.call_candidate(run, c))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert hung == ["CA123"] and c.status == "cancelled"

    c2 = run.cands[1]
    agent.DIAL_RETRIES = 1
    task = asyncio.create_task(agent.call_candidate(run, c2))
    for n in (1, 2):  # two busy signals -> redial once, then give up
        while c2.attempts < n:
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.02)
        c2.call_status = "busy"
        c2.done.set()
    await asyncio.wait_for(task, 5)
    assert c2.attempts == 2 and c2.status == "no_answer", (c2.attempts, c2.status)
    print("ok  call lifecycle: cancel hangs up the line; busy -> redial -> no_answer")


async def t_webhook_and_email():
    run = fresh()
    short = run.cands[:3]
    w, other = short[0], short[1]
    w.phone, w.address = "+14165550101", "1 King St W, Toronto"
    print(" opener:", agent.opener(run, w))
    script = iter([
        {"say": "Great. And what's the cancellation policy?", "done": False, "available": True, "price": 115,
         "fees": "13% HST", "room_type": "Queen room"},
        {"say": "Perfect, thank you so much. Goodbye!", "done": True, "cancellation": "Free until 24h before",
         "staff_name": "Priya"},
    ])
    nope = iter([{"say": "Understood, thanks anyway. Goodbye!", "done": True, "available": False}])

    async def fake_llm(m):
        return next(nope) if other.name in m[0]["content"] else next(script)

    agent.llm = fake_llm
    cl = TestClient(agent.app)
    # available + price known -> hang up at once, even though the LLM wanted to ask about cancellation
    r = cl.post(f"/turn?c={w.idx}", data={"SpeechResult": "Yes we have a queen at 115 plus tax."})
    assert "<Hangup/>" in r.text and w.status == "verified" and w.facts["price"] == 115, r.text
    assert w.history[-1]["content"] == agent.BYE
    r = cl.post(f"/turn?c={other.idx}", data={"SpeechResult": "Sorry, we're fully booked."})
    assert "<Hangup/>" in r.text and other.status == "unavailable"
    cl.post(f"/status?c={w.idx}", data={"CallStatus": "completed", "CallDuration": "48"})
    assert cl.post("/turn?c=99", data={}).text == "<Response><Hangup/></Response>"

    s = run.cands[2]
    a, b = cl.post(f"/turn?c={s.idx}", data={}), cl.post(f"/turn?c={s.idx}", data={})
    assert "<Gather" in a.text and "<Hangup/>" in b.text and s.status == "unclear"

    run.winner = w
    run.mark("calls")
    subj, text, html = agent.receipt(run)
    for must in ("Good Value Inn", "1 King St W", "+14165550101", "115.00 CAD", "230.00 CAD", "13% HST",
                 "Cheap Dump", "No availability", "CALL TRANSCRIPT"):
        assert must in text, must
    assert "<table" in html
    print("ok  webhooks (per-call state) + email\n\nSUBJECT:", subj, "\n" + text)

    run.winner = None
    subj, *_ = agent.receipt(run)
    assert subj.startswith("[NOT VERIFIED]")


class FakeResp:
    def __init__(self, payload, content=b""):
        self._p = payload
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


async def t_llm():
    """Backboard wire format: request shape, token accounting, lenient JSON, 200-FAILED."""
    sent = []

    class FakeHTTP:
        def __init__(self, payload):
            self._p = payload

        async def post(self, url, headers=None, json=None, **kw):
            sent.append((url, headers, json))
            return FakeResp(self._p)

    msgs = [{"role": "system", "content": "be brief"},
            {"role": "user", "content": "we have a room"},
            {"role": "assistant", "content": "how much?"},
            {"role": "user", "content": "one forty"}]

    agent.RUN = agent.Run("x")
    agent.BB_PROVIDER, agent.BB_MODEL, agent.BB_KEY = "cerebras", "openai/gpt-oss-120b", "k"
    agent.HTTP = FakeHTTP({"message": "Message added successfully",
                           "content": '```json\n{"say":"ok","available":true}\n```',
                           "input_tokens": 52, "output_tokens": 9, "thread_id": "th_1"})
    out = await REAL_LLM(msgs)
    url, headers, body = sent[-1]
    assert out == {"say": "ok", "available": True}, out                      # fenced JSON tolerated
    assert url.endswith("/threads/messages") and headers["X-API-Key"] == "k", (url, headers)
    assert body["system_prompt"] == "be brief", body                         # system hoisted out
    assert "thread_id" not in body and body["memory"] == "off", body          # stateless: no memory tax
    assert body["json_output"] is True and body["llm_provider"] == "cerebras"
    assert body["model_name"] == "openai/gpt-oss-120b", body
    assert body["content"].splitlines() == ["Hotel: we have a room", "You: how much?", "Hotel: one forty"], body
    assert (agent.RUN.tok_in, agent.RUN.tok_out, agent.RUN.llm_calls) == (52, 9, 1)

    # single user message (parse_request) goes over bare, no role prefixes
    await REAL_LLM([{"role": "user", "content": "find me a hotel"}])
    assert sent[-1][2]["content"] == "find me a hotel" and "system_prompt" not in sent[-1][2]

    # upstream model failures arrive as HTTP 200 + status FAILED, not an HTTP error
    agent.HTTP = FakeHTTP({"message": "Message added successfully", "status": "FAILED",
                           "content": "LLM Error: LLM Invocation Error: Error code: 404"})
    try:
        await REAL_LLM(msgs)
        raise AssertionError("FAILED status should raise, not return junk")
    except RuntimeError as e:
        assert "backboard" in str(e), e

    print("ok  llm: backboard stateless call, tokens counted, 200-FAILED raises")


async def t_tts():
    """ElevenLabs TTS: <Play> when it works, <Say> fallback when it doesn't, caching, /audio route."""
    agent.RUN = agent.Run("x")
    agent.AUDIO.clear()
    agent.PUBLIC_URL, agent.EL_KEY, agent.EL_MODE = "https://tunnel.test", "el-key", "all"
    posts = []

    class FakeHTTP:
        def __init__(self, fail=False):
            self.fail = fail

        async def post(self, url, headers=None, params=None, json=None, timeout=None, **kw):
            posts.append((url, headers, params, json))
            if self.fail:
                raise RuntimeError("eleven down")
            return FakeResp(None, content=b"ID3-fake-mp3")

    agent.HTTP = FakeHTTP()
    cand = agent.rank(PROPS)[0]
    xml = await agent.gather(cand, "Do you have a room?")
    url, headers, params, body = posts[-1]
    assert "<Play>https://tunnel.test/audio/" in xml and "<Say" not in xml, xml
    assert url.endswith(f"/text-to-speech/{agent.EL_VOICE}") and headers["xi-api-key"] == "el-key"
    assert body["model_id"] == agent.EL_MODEL and params["output_format"] == agent.EL_FORMAT
    # voice_settings must be sent, or a library voice uses its own slow saved speed
    assert body["voice_settings"]["speed"] == agent.EL_SPEED, body
    assert '<Gather input="speech"' in xml and "/turn?c=" in xml
    # default: audio BEFORE <Gather>, so no sound from the hotel can cut the agent off
    assert xml.index("<Play>") < xml.index("<Gather"), xml
    agent.BARGE_IN = True
    xml = await agent.gather(cand, "Do you have a room?")
    assert xml.index("<Gather") < xml.index("<Play>"), "BARGE_IN=1 nests audio inside Gather"
    agent.BARGE_IN = False
    assert (agent.RUN.tts_calls, agent.RUN.tts_chars) == (1, len("Do you have a room?"))

    # same text again -> served from cache, no second API call
    before = len(posts)
    await agent.hangup_twiml("Do you have a room?")
    assert len(posts) == before and agent.RUN.tts_calls == 1

    # the mp3 is reachable on the tunnel for Twilio to <Play>
    key = agent._akey("Do you have a room?")
    assert agent.AUDIO[key] == b"ID3-fake-mp3"

    # TTS failure must never kill the line: fall back to <Say>
    agent.HTTP = FakeHTTP(fail=True)
    xml = await agent.gather(cand, "A line we have not generated")
    assert "<Say voice=" in xml and "<Play>" not in xml, xml

    # mode "opener" only plays pre-generated audio, everything else is <Say>
    agent.HTTP = FakeHTTP()
    agent.EL_MODE = "opener"
    xml = await agent.gather(cand, "Do you have a room?")          # cached -> Play
    assert "<Play>" in xml, xml
    xml = await agent.gather(cand, "An unplanned mid-call line")   # not cached -> Say
    assert "<Say voice=" in xml, xml

    agent.EL_MODE, agent.EL_KEY = "all", ""
    print("ok  tts: eleven <Play>, cache hit, failure falls back to <Say>, opener-only mode")


def t_env():
    """An empty value in .env must not shadow the code default."""
    import os
    os.environ["GUEST_NAME"] = ""
    assert agent.E("GUEST_NAME", "Jordan Fulawka") == "Jordan Fulawka"
    os.environ["GUEST_NAME"] = "Someone Else"
    assert agent.E("GUEST_NAME", "Jordan Fulawka") == "Someone Else"
    del os.environ["GUEST_NAME"]
    assert agent.E("GUEST_NAME", "Jordan Fulawka") == "Jordan Fulawka"
    print("ok  env: empty .env value falls back to the default")


def t_billed_seconds():
    """Cost reporting must survive a /status callback that never arrives."""
    c = agent.Candidate(0, {"name": "X"}, 100, 100.0)
    assert c.billed_seconds == 0
    c.local_seconds = 54                      # our dial-to-hangup measure
    assert c.billed_seconds == 54, "must fall back to the local measure"
    c.seconds = 60                            # Twilio's webhook landed in time
    assert c.billed_seconds == 60, "Twilio's own figure wins when present"
    assert c.summary()["call_seconds"] == 60
    print("ok  billed_seconds: Twilio figure preferred, local measure as fallback")


async def t_hotel_search():
    """SerpApi rates -> sorted by total; phone lookups only with --phones, top N only."""
    import hotel_search as hs

    assert hs.e164("+1 416-555-0101") == "+14165550101" and hs.e164("(416) 555-0101") == "+14165550101"
    assert hs.e164("555-0101") is None

    serp_log = []

    async def fake_serp(params):
        serp_log.append(params.get("property_token") or params["sort_by"])
        if "property_token" in params:
            return {"phone": "(416) 555-01" + params["property_token"][-2:], "address": "9 Front St"}
        return {"properties": [   # sort_by=3 comes back only roughly ascending
            {"name": "B", "rate_per_night": {"extracted_lowest": 95}, "property_token": "t02"},
            {"name": "A", "rate_per_night": {"extracted_lowest": 90}, "property_token": "t01"},
            {"name": "No price", "property_token": "t09"},
            {"name": "C", "rate_per_night": {"extracted_lowest": 99},
             "total_rate": {"extracted_lowest": 210, "extracted_before_taxes_fees": 180},
             "property_token": "t03"}]}

    hs.serp = fake_serp
    rows = await hs.search("Toronto", "2026-10-18", nights=2, top=2)
    assert [(r["name"], r["total"]) for r in rows] == [("A", 180.0), ("B", 190.0)], rows
    assert serp_log == ["3"] and rows[0]["phone"] is None, "no detail lookups without --phones"
    rows = await hs.search("Toronto", "2026-10-18", nights=2, top=3)
    assert rows[2]["pre_tax"] == 180 and rows[0]["pre_tax"] is None, rows
    serp_log.clear(); serp_log.append("3")
    rows = await hs.search("Toronto", "2026-10-18", top=2, phones=True)
    assert [r["phone"] for r in rows] == ["+14165550101", "+14165550102"], rows
    assert serp_log[1:] == ["3", "t01", "t02"], serp_log

    # radius: geocode the place once, page through results, keep only hotels inside the circle
    cn = (43.6426, -79.3871)                                   # CN Tower
    pages_log = []

    async def fake_serp_geo(params):
        if params["engine"] == "google_maps":
            pages_log.append("geo:" + params["q"])
            return {"place_results": {"gps_coordinates": {"latitude": cn[0], "longitude": cn[1]}}}
        pages_log.append(params.get("next_page_token") or params["q"])

        def prop(name, price, lat, lng):
            return {"name": name, "rate_per_night": {"extracted_lowest": price},
                    "gps_coordinates": {"latitude": lat, "longitude": lng}}
        if "next_page_token" not in params:
            return {"properties": [prop("Far Cheap", 60, 43.7735, -79.2577),       # Scarborough ~18 km
                                   prop("Near", 120, 43.6453, -79.3806)],           # ~0.6 km
                    "serpapi_pagination": {"next_page_token": "p2"}}
        return {"properties": [prop("City Hall-ish", 110, 43.6532, -79.3832),       # ~1.2 km
                               {"name": "No GPS", "rate_per_night": {"extracted_lowest": 50}}]}

    hs.serp = fake_serp_geo
    rows = await hs.search("Toronto", "2026-10-18", near="CN Tower", radius_km=2, pages=2)
    assert [r["name"] for r in rows] == ["City Hall-ish", "Near"], rows
    assert rows[0]["km"] == 1.22 and rows[1]["km"] < 1, rows
    assert pages_log == ["geo:CN Tower, Toronto", "hotels near CN Tower, Toronto", "p2"], pages_log
    pages_log.clear()
    rows = await hs.search("Toronto", "2026-10-18", near="43.6426, -79.3871", radius_km=1)
    assert [r["name"] for r in rows] == ["Near"] and pages_log == ["hotels in Toronto"], pages_log

    # namesake town: geocode says Whitby, England, hotels are in Whitby, Ontario -> use the hotels
    async def fake_serp_whitby(params):
        if params["engine"] == "google_maps":
            assert params.get("gl") == hs.GL, "geocode must be localized like the hotel search"
            return {"place_results": {"gps_coordinates": {"latitude": 54.4863, "longitude": -0.6133}}}
        return {"properties": [
            {"name": n, "rate_per_night": {"extracted_lowest": p}, "gps_coordinates": {"latitude": la, "longitude": lo}}
            for n, p, la, lo in (("Quality Suites Whitby", 127, 43.8724, -78.8961),
                                 ("Travelodge Oshawa Whitby", 115, 43.8738, -78.8905),
                                 ("Idlewood Inn", 97, 43.7603, -79.1972),       # Scarborough, ~25 km
                                 ("Spark Oshawa", 126, 43.8744, -78.8782))]}

    hs.serp = fake_serp_whitby
    rows = await hs.search("Whitby", "2026-10-18", near="Whitby", radius_km=20)
    assert [r["name"] for r in rows] == ["Travelodge Oshawa Whitby", "Spark Oshawa", "Quality Suites Whitby"], rows
    print("ok  hotel_search: serp cheapest sorted, pre-tax, phones only with --phones, radius + pages")


async def t_pipeline():
    """Backboard parse -> search -> call batch of 3 -> transcripts read -> cheapest *quoted* wins.
    Round 1: nobody available -> round 2. A phone-less hotel is skipped and backfilled."""
    import hotel_search as hs
    import liteapi_search as lite
    import pipeline as pl
    from schemas import Hotel

    saved = (agent.llm, agent.call_candidate, agent.tts, hs.serp, hs.PLACES_KEY, lite.find_hotels)
    agent.PHONE_OVERRIDE, hs.PLACES_KEY = "", ""
    pl.BATCH, pl.MAX_ROUNDS, pl.RADIUS_KM, pl.SEARCH_PAGES = 3, 3, 20, 1
    prices = {"H0": 90, "H1": 95, "H2": 99, "H3": 100, "H4": 104, "H5": 110, "H6": 120}
    # what each hotel "says" on the phone: (available, quoted nightly)
    said = {"H0": (False, None), "H1": (False, None), "H2": (False, None),
            "H4": (True, 170), "H5": (True, 130), "H6": (True, 125)}   # H3 has no phone
    llm_log, dialed = [], []

    async def fake_llm(m):
        llm_log.append(m)
        if "Extract the hotel booking" in m[0]["content"]:
            return {"city": "Toronto", "check_in": "2026-10-18", "check_out": "2026-10-20",
                    "adults": 2, "room_type": "king", "email": "judge@techto.ca"}
        assert "transcripts" in m[0]["content"] and "room type king" in m[0]["content"]
        out = []
        for block in m[1]["content"].split("\n\n"):
            idx = int(block.split()[1].rstrip(":"))
            name = block.split(": ", 1)[1].split(" (")[0]
            a, p = said[name]
            out.append({"id": idx, "available": a, "nightly_price": p, "total_price": None,
                        "room_type": "King" if a else None, "check_in": None, "check_out": None,
                        "summary": f"{name} said " + ("yes" if a else "no")})
        return {"calls": out}

    async def fake_serp(params):
        if "property_token" in params:
            name = params["property_token"]
            return {"phone": None if name == "H3" else "416-555-01" + name[1:].zfill(2)}
        if params["engine"] == "google_maps":
            if params["q"] == "Toronto":                       # geocoding the city centre
                return {"place_results": {"gps_coordinates": {"latitude": 43.6532, "longitude": -79.3832}}}
            return {"local_results": [{"phone": None}]}
        assert params["q"] == "hotels in Toronto", params      # centre = city: query unchanged
        props = [{"name": n, "rate_per_night": {"extracted_lowest": p}, "property_token": n,
                  "gps_coordinates": {"latitude": 43.66, "longitude": -79.39}} for n, p in prices.items()]
        # cheapest of all, but ~50 km away in Hamilton: outside RADIUS_KM, never called
        props.append({"name": "Far", "rate_per_night": {"extracted_lowest": 40}, "property_token": "Far",
                      "gps_coordinates": {"latitude": 43.2557, "longitude": -79.8711}})
        return {"properties": props}

    async def fake_call(run_, c):
        dialed.append(c.name)
        c.attempts += 1
        c.sid = "CA" + c.name.lower()
        c.history = [{"role": "user", "content": "Yes" if said[c.name][0] else "Sorry, we're full"}]
        c.facts["available"] = said[c.name][0]
        c.status = "verified" if said[c.name][0] else "unavailable"   # live guess; transcript read wins

    async def no_tts(text):
        return None

    async def fake_lite(q):
        # the same H4, pricier than on SerpApi (2 nights x 104 = 208): the merge keeps the SerpApi price
        # and takes this phone + reviews. LiteAPI-only hotels would simply be extra rows.
        assert (q.city, q.check_in.isoformat(), q.currency) == ("Toronto", "2026-10-18", agent.CURRENCY)
        return [Hotel(source="liteapi", id="lp4", name="H4", lat=43.66, lng=-79.39, distance_km=1.0,
                      price=268, currency=agent.CURRENCY, phone="+14165559999", review_rating=4.3,
                      review_count=900)]

    agent.llm, agent.call_candidate, agent.tts, hs.serp = fake_llm, fake_call, no_tts, fake_serp
    lite.find_hotels = fake_lite
    try:
        agent.RUN = run = agent.Run("King room in Toronto Oct 18-20 for 2, send to judge@techto.ca")
        w = await pl.run_pipeline(run)
        assert run.req["room_type"] == "king" and run.req["check_out"] == "2026-10-20", run.req
        assert "a king room" in agent.opener(run, run.cands[0])
        assert dialed == ["H0", "H1", "H2", "H4", "H5", "H6"], dialed          # H3 backfilled, Far out of radius
        assert "Far" not in [c.name for c in run.cands]
        assert [c.name for c in run.cands].count("H4") == 1, "found by both searches: merged to one"
        h4 = next(c for c in run.cands if c.name == "H4")
        assert (h4.total, h4.phone, h4.rating, h4.reviews) == (208, "+14165559999", 4.3, 900), h4.summary()
        assert [c.status for c in run.cands if c.name == "H3"] == ["no_phone"]
        # H6 was listed highest but quoted lowest (125/night) -> it wins, total 250 for 2 nights
        assert w.name == "H6" and pl.stay_cost(run, w) == 250 and run.winner is w, w.name
        assert len(llm_log) == 4, "1 parse + 1 read per call that talked (a clear 'no' is not re-read)"
        # the transcript read fills the CallQuery: summary, quoted stay price, availability
        cq = w.call
        assert (cq.summary, cq.price, cq.available) == ("H6 said yes", 250.0, True), cq
        assert cq.call_url == "https://console.twilio.com/us1/monitor/logs/calls/CAh6", cq.call_url
        assert "Hotel: Yes" in cq.transcript and cq.transcript.startswith("Agent:")
        assert run.cands[0].call.available is False and run.cands[0].call.price is None
        assert next(c for c in run.cands if c.name == "H3").call is None      # never reached a person
        assert 0.5 < w.km < 1.5, w.km                                         # distance carried from search
        assert w.summary()["distance_km"] == w.km
        rep = pl.report(run)
        assert rep["winner"]["name"] == "H6" and len(rep["called"]) == 7
        h6 = next(x for x in rep["called"] if x["name"] == "H6")
        assert h6["distance_km"] == w.km and h6["summary"] == "H6 said yes" and "Hotel: Yes" in h6["transcript"]
        # the email gets the winner's distance, quoted price and transcript
        import mailer
        req, win, opts, stats = pl.email_items(run)
        assert win["name"] == "H6" and win["quoted_night"] == 125 and win["km"] is not None, win
        assert req["nights"] == 2 and len(opts) == 7 and stats["Hotels phoned"] == 6
        subject, text, html = mailer.build(req, win, opts, stats)
        assert "H6" in subject or "Reservation Confirmed" in subject
        assert "$250.00 CAD" in text and "km from city centre" in text and "Hotel: Yes" in text, text
    finally:
        agent.llm, agent.call_candidate, agent.tts, hs.serp, hs.PLACES_KEY, lite.find_hotels = saved
    print("ok  pipeline: parse, search, batch of 3, next batch when none free, cheapest quote wins")

    # one search source down: carry on with the other; both down: fail loudly
    async def boom(q, **kw):
        raise RuntimeError("liteapi down")

    async def ok_serp(q, **kw):
        return [Hotel(source="serpapi", id="s", name="Only", price=100, currency="CAD", phone="+14165550000")]

    saved = (hs.find_hotels, lite.find_hotels)
    try:
        hs.find_hotels, lite.find_hotels = ok_serp, boom
        got = await pl.search({"city": "Toronto", "check_in": "2026-10-18", "check_out": "2026-10-19", "adults": 2})
        assert [h.name for h in got] == ["Only"], got
        hs.find_hotels = boom
        try:
            await pl.search({"city": "Toronto", "check_in": "2026-10-18", "check_out": "2026-10-19", "adults": 2})
            raise AssertionError("both sources down should raise")
        except RuntimeError:
            pass
    finally:
        hs.find_hotels, lite.find_hotels = saved
    print("ok  pipeline search: survives one source failing")

    # preflight: a dead tunnel must stop the run before anyone is dialed
    class FakeHTTP:
        def __init__(self, result):
            self.result = result

        async def get(self, url, timeout=None):
            if isinstance(self.result, Exception):
                raise self.result
            return type("R", (), {"status_code": self.result})()

    saved_url, saved_http = agent.PUBLIC_URL, agent.HTTP
    try:
        agent.PUBLIC_URL = ""
        assert "empty" in await pl.tunnel_ok()
        agent.PUBLIC_URL = "https://dead.trycloudflare.com"
        agent.HTTP = FakeHTTP(502)
        assert "502" in await pl.tunnel_ok()
        agent.HTTP = FakeHTTP(OSError("nxdomain"))
        assert "unreachable" in await pl.tunnel_ok()
        agent.HTTP = FakeHTTP(200)
        assert await pl.tunnel_ok() is None
        assert pl.report(agent.Run("stopped before parse"))["called"] == []
    finally:
        agent.PUBLIC_URL, agent.HTTP = saved_url, saved_http
    print("ok  pipeline preflight: empty/dead/5xx tunnel caught before dialing")


async def t_race():
    """Calls run at once; the table is re-read as each finishes. Early stop, wait-for-a-cheaper-one,
    and the 1-minute (here: 0.4 s) cut-off. Times are scaled down; latency_cost scaled up to match."""
    import decider
    import pipeline as pl
    import ranker
    from schemas import Hotel

    # name: (listed stay price, seconds the call takes, does the hotel confirm, quoted stay price)
    def setup(spec):
        agent.RUN = run = agent.Run("x")
        run.req = {"city": "T", "check_in": "2026-10-18", "check_out": "2026-10-19", "adults": 2}
        run.cands = [pl.candidate(i, Hotel(id=n, name=n, price=v[0], currency="CAD", phone=f"+1416555010{i}"), 1)
                     for i, (n, v) in enumerate(spec.items())]
        return run

    async def run_race(spec, **policy):
        run = setup(spec)
        seen, hung = [], []

        async def fake_call(run_, c):
            delay, ok, quote = spec[c.name][1:]
            c.attempts += 1
            c.status = "calling"
            c.history = [{"role": "user", "content": f"{c.name} picks up"}]       # what is said first
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                hung.append(c.name)                 # call_candidate hangs up on cancellation
                c.status = "cancelled"
                raise
            c.history = [{"role": "user", "content": f"{c.name} says {'yes' if ok else 'no'}"}]
            c.facts["available"] = ok
            c.status = "verified" if ok else "unavailable"

        async def fake_llm(m):
            seen.append(m[1]["content"])
            name = m[1]["content"].split(": ", 1)[1].split(" (")[0]
            return {"calls": [{"id": int(m[1]["content"].split()[1].rstrip(":")), "available": spec[name][2],
                               "nightly_price": spec[name][3], "summary": f"{name}"}]}

        saved = (agent.call_candidate, agent.llm, pl.POLICY, agent.CALL_TIMEOUT)
        agent.call_candidate, agent.llm = fake_call, fake_llm
        pl.POLICY = decider.Policy(latency_cost=policy.get("latency_cost", 100.0), call_s=0.2, min_wait_s=0.02,
                                   deadline_s=0.4)
        agent.CALL_TIMEOUT = 0.4
        t0 = time.perf_counter()
        try:
            w = await pl.race(run, run.cands)
        finally:
            agent.call_candidate, agent.llm, pl.POLICY, agent.CALL_TIMEOUT = saved
        return run, w, time.perf_counter() - t0, hung

    import time
    # A. the cheapest listing confirms first: go now, the slow ones are hung up
    run, w, took, hung = await run_race({"Cheap": (100, 0.05, True, 100), "Mid": (110, 3, True, 110),
                                         "Dear": (150, 3, True, 150)})
    assert w.name == "Cheap" and took < 0.6, (w and w.name, took)
    assert sorted(hung) == ["Dear", "Mid"], hung
    # B. an expensive hotel confirms first, a much cheaper one is still ringing: wait for it
    run, w, took, hung = await run_race({"Cheap": (100, 0.2, True, 100), "Dear": (200, 0.02, True, 200)})
    assert w.name == "Cheap" and not hung, (w.name, hung)
    # B2. ... unless waiting is dear: with a huge price on latency the quick confirmation is taken
    run, w, took, hung = await run_race({"Cheap": (100, 0.3, True, 100), "Dear": (200, 0.02, True, 200)},
                                        latency_cost=1e6)
    assert w.name == "Dear" and hung == ["Cheap"], (w.name, hung)
    # C. a cheaper hotel that picks up too late (past the cut-off) is ignored, even if it would say yes
    run, w, took, hung = await run_race({"Late": (100, 0.8, True, 90), "Dear": (200, 0.05, True, 200)})
    late = run.cands[0]
    assert w.name == "Dear" and late.status == "timeout", (w.name, late.status)
    # ... but its partial transcript is kept, flagged as cut off, and it can never win
    assert late.call.available is None and late.call.price is None, late.call
    assert "Late picks up" in late.call.transcript and "cut off after 0.4 s" in late.call.summary, late.call
    assert late.hotel.call_query is late.call
    assert ranker.cheapest([c.hotel for c in run.cands]).name == "Dear"
    # D. nobody confirms: None, and every call was read or skipped without a crash
    run, w, took, hung = await run_race({"A": (100, 0.02, False, None), "B": (110, 0.03, False, None)})
    assert w is None
    print("ok  race: early stop, waits for a much cheaper hotel only while it pays, cut-off ignores late calls")


async def t_observability():
    """The waterfall: run > race > call (one per hotel, with latency + transcript), the decision events,
    and run > email > build/send with the timing block in the mail. Exported to memory, not to Logfire."""
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    import mailer
    import obs
    import pipeline as pl
    from schemas import Hotel

    exporter = InMemorySpanExporter()
    assert obs.setup(processors=[SimpleSpanProcessor(exporter)])
    agent.RUN = run = agent.Run("x")
    run.req = {"city": "T", "check_in": "2026-10-18", "check_out": "2026-10-19", "adults": 2, "email": "j@x.ca"}
    run.cands = [pl.candidate(i, Hotel(id=n, name=n, price=p, currency="CAD", phone=f"+1416555010{i}"), 1)
                 for i, (n, p) in enumerate([("Yes Inn", 100), ("No Inn", 110)])]
    sent = {}

    async def fake_call(run_, c):
        c.attempts += 1
        await asyncio.sleep(0.02)
        ok = c.name == "Yes Inn"
        c.history = [{"role": "user", "content": "Yes" if ok else "Full"}]
        c.facts.update(available=ok, price=100 if ok else None)
        c.status = "verified" if ok else "unavailable"

    async def fake_llm(m):
        return {"calls": [{"id": 0, "available": True, "nightly_price": 100, "summary": "Yes Inn said yes"}]}

    async def fake_send(to, subject, text, html, client=None, attachments=None):
        sent.update(to=to, text=text, html=html)

    saved = (agent.call_candidate, agent.llm, mailer.send, mailer.configured, agent.RECORD_CALLS)
    agent.call_candidate, agent.llm, mailer.send, mailer.configured, agent.RECORD_CALLS = (
        fake_call, fake_llm, fake_send, lambda: None, False)
    try:
        with obs.span("hotel-agent run") as rsp:
            run.span = rsp
            run.mark("parse")
            run.mark("search")
            run.winner = await pl.race(run, run.cands)
            run.mark("round1")
            await pl.send_email(run)
    finally:
        agent.call_candidate, agent.llm, mailer.send, mailer.configured, agent.RECORD_CALLS = saved
        obs.ENABLED = False

    spans = exporter.get_finished_spans()
    by_id = {s.context.span_id: s for s in spans}
    parent = lambda s: by_id[s.parent.span_id].name if s.parent and s.parent.span_id in by_id else None
    names = [s.name for s in spans]
    calls = [s for s in spans if s.name == "call"]
    assert len(calls) == 2 and all(parent(s) == "race" for s in calls), [(s.name, parent(s)) for s in spans]
    assert parent(next(s for s in spans if s.name == "race")) == "hotel-agent run"
    yes = next(s for s in calls if s.attributes["hotel"] == "Yes Inn")
    assert yes.attributes["available"] is True and yes.attributes["quoted_stay"] == 100.0, dict(yes.attributes)
    assert "Yes" in yes.attributes["transcript"] and yes.attributes["latency_s"] >= 0.0
    assert yes.attributes["transcript"].startswith("Agent: ") and yes.attributes["status"] == "verified"
    assert all(parent(next(s for s in spans if s.name == n)) == "email" for n in ("build email", "send email"))
    assert any(s.name == "decision" for s in spans), names
    assert sent["to"] == "j@x.ca" and "RUN TIMING" in sent["text"] and "Total until sent" in sent["text"], sent["text"]
    assert "Run timing" in sent["html"] and "Phone calls, until the decision" in sent["html"]
    print("ok  observability: run > race > call spans with latency + transcript, email spans, timing in the mail")


async def t_rate_limits():
    """429s: retried with backoff, bounded in flight, never leaking the SerpApi key; TTS gives up on a busy slot."""
    import httpx
    import hotel_search as hs
    import net

    def resp(code, url="https://x.test/", **headers):
        return httpx.Response(code, headers=headers, request=httpx.Request("GET", url))

    # retry: two 429s (Retry-After: 0) then success
    seq = [resp(429, **{"retry-after": "0"}), resp(429, **{"retry-after": "0"}), resp(200)]
    n = []

    async def send():
        n.append(1)
        return seq[len(n) - 1]
    r = await net.retry_429(send, tries=3, label="t")
    assert r.status_code == 200 and len(n) == 3
    # a 429 that never clears is handed back after `tries` attempts
    n.clear()

    async def always():
        n.append(1)
        return resp(429, **{"retry-after": "0"})
    assert (await net.retry_429(always, tries=3)).status_code == 429 and len(n) == 3
    # other errors are not retried
    n.clear()

    async def boom():
        n.append(1)
        return resp(500)
    assert (await net.retry_429(boom, tries=3)).status_code == 500 and len(n) == 1

    # gate: never more than 3 in flight
    net.reset()
    live = peak = 0

    async def work():
        nonlocal live, peak
        async with net.gate("t", 3):
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.01)
            live -= 1
    await asyncio.gather(*(work() for _ in range(12)))
    assert peak == 3, peak

    # SerpApi: 429 is retried, and the error text carries no URL (the api_key rides in it)
    calls = []

    class FakeHTTP:
        async def get(self, url, params=None, **kw):
            calls.append(1)
            return resp(429, url + "?api_key=SECRET123&q=x", **{"retry-after": "0"})
    saved = (hs.HTTP, hs.SERPAPI_KEY)
    hs.HTTP, hs.SERPAPI_KEY = FakeHTTP(), "SECRET123"
    net.reset()
    try:
        await hs.serp({"engine": "google_hotels"})
        raise AssertionError("a 429 that never clears should raise")
    except RuntimeError as e:
        assert "429" in str(e) and "SECRET123" not in str(e) and "api_key" not in str(e), str(e)
    finally:
        hs.HTTP, hs.SERPAPI_KEY = saved
    assert len(calls) == 3, calls

    # TTS: every ElevenLabs slot busy -> no request, the line goes out in Twilio's voice
    net.reset()
    sent = []

    class FakeEL:
        async def post(self, *a, **k):
            sent.append(1)
            return resp(200)
    saved = (agent.HTTP, agent.EL_KEY, agent.PUBLIC_URL, agent.EL_MODE, agent.EL_SLOT_WAIT, agent.ELEVEN_CONCURRENCY)
    agent.HTTP, agent.EL_KEY, agent.PUBLIC_URL, agent.EL_MODE = FakeEL(), "k", "https://p.test", "all"
    agent.EL_SLOT_WAIT, agent.ELEVEN_CONCURRENCY = 0.05, 1
    agent.RUN = agent.Run("x")
    try:
        slot = net.gate("eleven", 1)
        await slot.acquire()
        assert await agent.tts("a line nobody has cached") is None and not sent
        slot.release()
    finally:
        (agent.HTTP, agent.EL_KEY, agent.PUBLIC_URL, agent.EL_MODE, agent.EL_SLOT_WAIT,
         agent.ELEVEN_CONCURRENCY) = saved
        net.reset()
    print("ok  rate limits: 429 retried with backoff, requests capped, no key in errors, busy TTS slot -> Say")


async def t_one_call_each():
    """Each hotel is dialed once: a phone number shared by two listings is taken once, and the
    default is no redial on busy / no answer."""
    import pipeline as pl
    from schemas import Hotel

    hotels = [Hotel(id=i, name=n, price=p, currency="CAD", phone=ph) for i, n, p, ph in
              [("a", "Alpha", 100, "+14165550001"), ("b", "Alpha Annex", 105, "+14165550001"),
               ("c", "Charlie", 110, "+14165550003")]]
    dialed = []

    async def fake_search(req):
        return hotels

    async def fake_call(run_, c):
        dialed.append(c.name)
        c.attempts += 1
        c.history = [{"role": "user", "content": "Sorry, full"}]
        c.facts["available"] = False
        c.status = "unavailable"

    async def fake_llm(m):
        return {"city": "Toronto", "check_in": "2026-10-18", "check_out": "2026-10-19", "adults": 2}

    async def no_tts(text):
        return None
    saved = (pl.search, agent.call_candidate, agent.llm, agent.tts, agent.PHONE_OVERRIDE)
    pl.search, agent.call_candidate, agent.llm, agent.tts, agent.PHONE_OVERRIDE = (
        fake_search, fake_call, fake_llm, no_tts, "")
    agent.RUN = run = agent.Run("Toronto")
    try:
        await pl.run_pipeline(run)
    finally:
        pl.search, agent.call_candidate, agent.llm, agent.tts, agent.PHONE_OVERRIDE = saved
    assert dialed == ["Alpha", "Charlie"], dialed
    assert next(c for c in run.cands if c.name == "Alpha Annex").status == "duplicate"
    assert "DIAL_RETRIES=0" in open(Path(__file__).resolve().parent.parent / ".env.example").read()
    print("ok  one call per hotel: shared phone number dialed once, DIAL_RETRIES defaults to 0")


async def t_prompt_email():
    """The address typed in the prompt wins over the LLM's copy and JUDGE_EMAIL; no address -> JUDGE_EMAIL."""
    import pipeline as pl

    async def fake_llm(m):      # the model "misreads" the address
        return {"city": "Toronto", "check_in": "2026-10-18", "check_out": "2026-10-19", "adults": 2,
                "email": "wrong@llm.example"}

    saved = (agent.llm, agent.JUDGE_EMAIL)
    agent.llm, agent.JUDGE_EMAIL = fake_llm, "fallback@techto.ca"
    agent.RUN = agent.Run("x")
    try:
        req = await pl.parse("King room in Toronto Oct 18, send it to Judge.Name+1@Example.co.uk.")
        assert req["email"] == "Judge.Name+1@Example.co.uk", req["email"]
        req = await pl.parse("King room in Toronto Oct 18")
        assert req["email"] in ("wrong@llm.example", "fallback@techto.ca"), req["email"]
        agent.llm = lambda m: _empty()
        req = await pl.parse("King room in Toronto Oct 18")
        assert req["email"] == "fallback@techto.ca", req["email"]
    finally:
        agent.llm, agent.JUDGE_EMAIL = saved
    print("ok  email address comes from the prompt (regex), JUDGE_EMAIL only as the fallback")


async def _empty():
    return {}


async def t_mailer():
    """Format from plain dicts (organizers' subject with TEAM_NUMBER); Resend vs SMTP routing."""
    import mailer

    req = {"city": "Whitby", "check_in": "2026-10-18", "check_out": "2026-10-20", "nights": 2,
           "adults": 2, "room_type": "king"}
    win = {"name": "Quality Suites Whitby", "summary": "King room free, 119 a night plus HST.", "address": "1700 Champlain Ave, Whitby, ON", "km": 4.66,
           "phone": "+19054328800", "listed_night": 127, "quoted_night": 119, "fees": "13% HST",
           "room_type": "King", "reference": "QS4471", "status": "verified",
           "transcript": "Agent: Do you have a king room?\nHotel: Yes, 119 a night.<script>"}
    other = {"name": "Spark Oshawa", "km": 5.71, "listed_night": 126, "status": "unavailable",
             "transcript": [{"role": "user", "content": "We're full."}]}
    saved = (mailer.TEAM_NUMBER, mailer.RESEND_KEY, mailer.MAIL_FROM, mailer.SMTP_USER,
             mailer.SMTP_PASS, mailer._smtp)
    try:
        mailer.TEAM_NUMBER = "7"
        subject, text, html = mailer.build(req, win, [win, other], {"Call minutes": 1.2})
        assert subject == "Team # 7 - Hotel Reservation Confirmed!", subject
        for want in ("Hotel name: Quality Suites Whitby", "Address: 1700 Champlain Ave", "4.7 km from city centre",
                     "Reservation #: QS4471", "$119.00 CAD", "Total for the stay: $238.00 CAD",
                     "Reservation dates: 2026-10-18 to 2026-10-20 (2 nights)", "Hotel: Yes, 119 a night.",
                     "WHAT THE HOTEL SAID", "CALL TRANSCRIPT"):
            assert want in text, (want, text)
        assert "Spark Oshawa" not in text and "Spark Oshawa" not in html, "only the cheapest hotel is listed"
        assert "We're full" not in text and "HOTELS TRIED" not in text
        assert "<script>" not in html and "&lt;script&gt;" in html, "transcript must be escaped"
        assert "tel:+19054328800" in html and "Room confirmed by phone" in html and "$238.00 CAD" in html
        mailer.TEAM_NUMBER = ""
        subject, text, html = mailer.build(req, None, [other])
        assert subject == "No hotel could be confirmed in Whitby", subject
        assert "HOTELS TRIED" in text and "Spark Oshawa | No availability" in text, text
        assert "No room could be confirmed" in html

        sent = []

        class FakeClient:
            async def post(self, url, headers, json):
                sent.append(("resend", json["to"], json["subject"]))
                return type("R", (), {"raise_for_status": lambda self: None})()

        mailer.RESEND_KEY, mailer.MAIL_FROM, mailer.SMTP_USER, mailer.SMTP_PASS = "re_x", "a@b.c", "", ""
        await mailer.send("judge@techto.ca", "S", "T", "<p>H</p>", client=FakeClient())
        mailer.RESEND_KEY, mailer.MAIL_FROM, mailer.SMTP_USER, mailer.SMTP_PASS = "", "", "me@gmail.com", "pw"
        mailer._smtp = lambda to, s, t, h, att=None: sent.append(("smtp", to, s))
        await mailer.send("judge@techto.ca", "S2", "T", "<p>H</p>")
        assert sent == [("resend", ["judge@techto.ca"], "S"), ("smtp", "judge@techto.ca", "S2")], sent
        mailer.SMTP_PASS = ""
        assert "SMTP_PASS" in mailer.configured()
    finally:
        (mailer.TEAM_NUMBER, mailer.RESEND_KEY, mailer.MAIL_FROM, mailer.SMTP_USER,
         mailer.SMTP_PASS, mailer._smtp) = saved
    print("ok  mailer: organizers' subject, hotel/price/km/transcripts, escaped html, resend + smtp")


async def t_recording():
    """Every call is recorded; only the winner's mp3 is fetched (after Twilio finishes processing)
    and attached to the email, via Resend (base64) or SMTP (MIME part)."""
    import base64
    import mailer
    import pipeline as pl

    sent_data, gets = [], []

    class R:
        def __init__(self, j=None, content=b""):
            self._j, self.content, self.status_code = j, content, 200

        def json(self):
            return self._j

        def raise_for_status(self):
            pass

    class FakeHTTP:
        def __init__(self):
            self.polls = 0

        async def post(self, url, auth=None, data=None):
            sent_data.append(data)
            return R({"sid": "CA1"})

        async def get(self, url, params=None, auth=None, follow_redirects=False, timeout=None):
            gets.append(url.rsplit("/", 1)[-1])
            if url.endswith(".mp3"):
                return R(content=b"ID3fake-mp3")
            self.polls += 1   # first poll: still processing
            return R({"recordings": [{"sid": "RE9", "status": "processing" if self.polls == 1 else "completed"}]})

    async def no_tts(text):
        return None

    saved = (agent.HTTP, agent.RECORD_CALLS, agent.recording_mp3, agent.tts)
    try:
        agent.HTTP, agent.tts = FakeHTTP(), no_tts
        run = fresh()
        agent.RECORD_CALLS = True
        await agent.place_call(run, run.cands[0])
        agent.RECORD_CALLS = False
        await agent.place_call(run, run.cands[0])
        assert sent_data[0].get("Record") == "true" and "Record" not in sent_data[1], sent_data

        t0 = asyncio.get_running_loop().time()
        assert await agent.recording_mp3("CA1") == b"ID3fake-mp3"
        assert gets == ["Recordings.json", "Recordings.json", "RE9.mp3"], gets
        assert asyncio.get_running_loop().time() - t0 < 3

        class Empty(FakeHTTP):
            async def get(self, url, **kw):
                return R({"recordings": []})
        agent.HTTP = Empty()
        assert await agent.recording_mp3("CA2", wait=0.1) is None, "gives up, never blocks the email"
        assert await agent.recording_mp3(None) is None

        # pipeline: only the winner's call, named after the hotel
        fetched = []

        async def fake_rec(sid, wait=20.0):
            fetched.append(sid)
            return b"mp3!"
        agent.recording_mp3, agent.RECORD_CALLS = fake_rec, True
        run.winner = run.cands[1]
        run.winner.sid = "CAwin"
        att = await pl.winner_recording(run)
        assert fetched == ["CAwin"] and att == [("call-Cheap-Dump-2026-10-18.mp3", b"mp3!", "audio/mpeg")], att
        agent.RECORD_CALLS = False
        assert await pl.winner_recording(run) == []
    finally:
        agent.HTTP, agent.RECORD_CALLS, agent.recording_mp3, agent.tts = saved

    # mailer: attachment reaches both transports
    saved = (mailer.RESEND_KEY, mailer.MAIL_FROM, mailer.SMTP_USER, mailer.SMTP_PASS, mailer.smtplib.SMTP_SSL)
    try:
        posted = []

        class FakeClient:
            async def post(self, url, headers, json):
                posted.append(json)
                return type("R", (), {"raise_for_status": lambda self: None})()

        mailer.RESEND_KEY, mailer.MAIL_FROM = "re_x", "a@b.c"
        await mailer.send("j@x.ca", "S", "T", "<p>H</p>", client=FakeClient(),
                          attachments=[("call.mp3", b"mp3!", "audio/mpeg")])
        assert posted[0]["attachments"] == [{"filename": "call.mp3", "content": base64.b64encode(b"mp3!").decode()}]

        msgs = []

        class FakeSMTP:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, u, p):
                pass

            def send_message(self, m):
                msgs.append(m)

        mailer.smtplib.SMTP_SSL = FakeSMTP
        mailer.RESEND_KEY, mailer.SMTP_USER, mailer.SMTP_PASS = "", "me@gmail.com", "pw"
        await mailer.send("j@x.ca", "S", "T", "<p>H</p>", attachments=[("call.mp3", b"mp3!", "audio/mpeg")])
        parts = [p for p in msgs[0].iter_attachments()]
        assert [(p.get_filename(), p.get_content_type(), p.get_content()) for p in parts] == \
            [("call.mp3", "audio/mpeg", b"mp3!")], parts
    finally:
        mailer.RESEND_KEY, mailer.MAIL_FROM, mailer.SMTP_USER, mailer.SMTP_PASS, mailer.smtplib.SMTP_SSL = saved
    print("ok  recording: calls recorded, winner's mp3 polled + fetched, attached via resend and smtp")


async def t_short_call():
    """Straight to the point: no guest name in the opener, only availability + price asked for,
    Twilio caps the call at CALL_MAX_SECONDS, and a missing price keeps the call going."""
    run = fresh()
    c = run.cands[0]
    op = agent.opener(run, c)
    assert op.startswith("Hi, this is an automated assistant with a quick availability question. Do you have a room"), op
    assert agent.GUEST_NAME not in op and len(op.split()) <= 30, f"opener: {len(op.split())} words"
    sp = agent.system_prompt(run, c)
    assert "ONLY two facts" in sp and "Never ask about fees" in sp

    sent = []

    class FakeHTTP:
        async def post(self, url, auth=None, data=None):
            sent.append(data)
            return type("R", (), {"raise_for_status": lambda s: None, "json": lambda s: {"sid": "CA1"}})()

    async def no_tts(text):
        return None

    saved = (agent.HTTP, agent.tts, agent.llm)
    try:
        agent.HTTP, agent.tts = FakeHTTP(), no_tts
        await agent.place_call(run, c)
        assert sent[0]["TimeLimit"] == str(agent.CALL_MAX_SECONDS) == "60", sent[0]

        async def yes_no_price(m):     # hotel said yes but no number yet -> must ask, not hang up
            return {"say": "What's the nightly rate?", "done": False, "available": True, "price": None}
        agent.llm = yes_no_price
        r = TestClient(agent.app).post(f"/turn?c={c.idx}", data={"SpeechResult": "Yes we do."})
        assert "<Gather" in r.text and c.status != "verified", r.text
    finally:
        agent.HTTP, agent.tts, agent.llm = saved
    print("ok  short call: no guest name, 2 facts only, 60s Twilio cap, hang up on confirmed price")


async def t_repeat():
    """'Sorry, can you repeat that?' replays our last line with no LLM call (max 2 times);
    a real question ('what dates?') goes to the LLM, which answers then re-asks."""
    run = fresh()
    c = run.cands[0]
    llm_calls = []

    async def fake_llm(m):
        llm_calls.append(m[-1]["content"])
        return {"say": "October 18 to 20. Do you have a room at 110?", "done": False}

    async def no_tts(text):
        return None

    for yes in ("Sorry, can you repeat that?", "Sorry?", "Hello?", "What?", "Pardon me", "I didn't catch that",
                "Say that again please", "Come again?"):
        assert agent.wants_repeat(yes), yes
    for no in ("What dates was that for?", "Yes we have one at 110", "Sorry, we're fully booked",
               "What we have is a king at 140 plus tax tonight only"):
        assert not agent.wants_repeat(no), no

    saved = (agent.llm, agent.tts)
    try:
        agent.llm, agent.tts = fake_llm, no_tts
        cl = TestClient(agent.app)
        op = agent.opener(run, c)
        r = cl.post(f"/turn?c={c.idx}", data={"SpeechResult": "Hello?"})          # cut off mid-opener
        assert op in r.text and "<Gather" in r.text and not llm_calls, r.text
        r = cl.post(f"/turn?c={c.idx}", data={"SpeechResult": "What dates was that for?"})
        assert llm_calls == ["What dates was that for?"] and "October 18" in r.text
        r = cl.post(f"/turn?c={c.idx}", data={"SpeechResult": "Sorry, can you repeat that?"})
        assert "October 18 to 20" in r.text and len(llm_calls) == 1, "replays the LAST line, no LLM"
        r = cl.post(f"/turn?c={c.idx}", data={"SpeechResult": "Sorry?"})        # 3rd ask: LLM takes over
        assert len(llm_calls) == 2 and c.repeats == agent.MAX_REPEATS
        assert "re-ask your question" in agent.system_prompt(run, c)
    finally:
        agent.llm, agent.tts = saved
    print("ok  repeat: replay last line on 'sorry?/repeat', no LLM, max 2; questions answered then re-asked")


async def t_hold():
    """'Hold on, let me check' -> 'Sure, take your time.' + a long silent listen. Silence while on
    hold is not a failure and doesn't use up MAX_TURNS; after HOLD_CHECKS windows, a nudge that
    asks for the single answer. Any speech ends the hold."""
    run = fresh()
    c = run.cands[0]
    llm_calls = []

    async def fake_llm(m):
        llm_calls.append(m[-1]["content"])
        return {"say": "Is that the lowest nightly price?", "done": False, "available": True}

    async def no_tts(text):
        return None

    for yes in ("Hold on", "One moment please", "Let me check", "Can you hang on a sec?", "Just a minute",
                "Bear with me", "Wait, let me look"):
        assert agent.wants_wait(yes), yes
    for no in ("Yes we have one at 110", "We checked and we're full", "Sorry?"):
        assert not agent.wants_wait(no), no

    saved = (agent.llm, agent.tts, agent.MAX_TURNS)
    try:
        agent.llm, agent.tts, agent.MAX_TURNS = fake_llm, no_tts, 4
        cl = TestClient(agent.app)
        r = cl.post(f"/turn?c={c.idx}", data={"SpeechResult": "Hold on, let me check."})
        assert agent.WAIT_OK in r.text and f'timeout="{agent.HOLD_SECONDS}"' in r.text and c.holding
        assert not llm_calls, "no LLM for a hold"
        for _ in range(agent.HOLD_CHECKS):              # silence on hold: keep listening, say nothing
            r = cl.post(f"/turn?c={c.idx}", data={})
            assert "<Say" not in r.text and "<Play" not in r.text and f'timeout="{agent.HOLD_SECONDS}"' in r.text
        assert c.silent == 0 and c.status != "unclear" and c.turns == 1, (c.silent, c.status, c.turns)
        r = cl.post(f"/turn?c={c.idx}", data={})        # still nothing: polite nudge for the one answer
        assert "Are you still there?" in r.text and "110 a night" in r.text and not c.holding, r.text
        # the hold resumes on another "one moment", and real speech ends it and goes to the LLM
        cl.post(f"/turn?c={c.idx}", data={"SpeechResult": "One moment."})
        assert c.holding
        r = cl.post(f"/turn?c={c.idx}", data={"SpeechResult": "OK we have a few rooms, it depends on the type."})
        assert not c.holding and llm_calls and "<Gather" in r.text
        sp = agent.system_prompt(run, c)
        assert "patient" in sp and "ONE clear answer" in sp and "lowest nightly price" in sp
    finally:
        agent.llm, agent.tts, agent.MAX_TURNS = saved
    print("ok  hold: 'hold on' -> wait quietly (no LLM, no turn cost), nudge after silence, prompt pushes one answer")


async def main():
    await t_hold()
    await t_repeat()
    await t_short_call()
    await t_recording()
    await t_race()
    await t_rate_limits()
    await t_one_call_each()
    await t_observability()
    await t_prompt_email()
    await t_mailer()
    await t_hotel_search()
    await t_pipeline()
    await t_parse()
    await t_llm()
    await t_tts()
    t_env()
    t_rank_phone()
    t_billed_seconds()
    await t_shortlist()
    await t_verify_parallel()
    await t_verify_sequential()
    await t_call_cancel_hangs_up()
    await t_webhook_and_email()


if __name__ == "__main__":
    asyncio.run(main())
    print("\nALL TESTS PASSED")
