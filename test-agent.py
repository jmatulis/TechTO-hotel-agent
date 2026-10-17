"""Offline smoke tests: every outside service is faked. Run: python test-agent.py"""
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
    agent.RUN = agent.Run("x")
    agent.RUN.req = dict(REQ)
    agent.RUN.cands = agent.rank(PROPS)
    return agent.RUN


async def t_parse():
    agent.RUN = agent.Run("x")
    calls = []

    async def fake_llm(m, max_tokens=90):
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
    run = fresh()
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

    async def fake_llm(m, max_tokens=110):
        return next(nope) if other.name in m[0]["content"] else next(script)

    agent.llm = fake_llm
    cl = TestClient(agent.app)
    r = cl.post(f"/turn?c={w.idx}", data={"SpeechResult": "Yes we have a queen at 115 plus tax."})
    assert "<Gather" in r.text and f"/turn?c={w.idx}" in r.text
    r = cl.post(f"/turn?c={other.idx}", data={"SpeechResult": "Sorry, we're fully booked."})
    assert "<Hangup/>" in r.text and other.status == "unavailable"
    r = cl.post(f"/turn?c={w.idx}", data={"SpeechResult": "Free cancellation until 24 hours before. I'm Priya."})
    assert "<Hangup/>" in r.text and w.status == "verified" and w.facts["price"] == 115
    cl.post(f"/status?c={w.idx}", data={"CallStatus": "completed", "CallDuration": "48"})
    assert cl.post("/turn?c=99", data={}).text == "<Response><Hangup/></Response>"

    s = run.cands[2]
    a, b = cl.post(f"/turn?c={s.idx}", data={}), cl.post(f"/turn?c={s.idx}", data={})
    assert "<Gather" in a.text and "<Hangup/>" in b.text and s.status == "unclear"

    run.winner = w
    run.mark("calls")
    subj, text, html = agent.receipt(run)
    for must in ("Good Value Inn", "1 King St W", "+14165550101", "115.00 CAD", "230.00 CAD", "13% HST",
                 "Free until 24h before", "Priya", "Cheap Dump", "No availability", "CALL TRANSCRIPT"):
        assert must in text, must
    assert "<table" in html
    print("ok  webhooks (per-call state) + email\n\nSUBJECT:", subj, "\n" + text)

    run.winner = None
    subj, *_ = agent.receipt(run)
    assert subj.startswith("[NOT VERIFIED]")


class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


async def t_llm_backends():
    """Both wire formats: request shape, token accounting, lenient JSON."""
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

    # --- OpenAI-compatible (Groq) path
    agent.RUN = agent.Run("x")
    agent.LLM_BACKEND = "openai"
    agent.HTTP = FakeHTTP({"choices": [{"message": {"content": '{"say":"ok","done":true}'}}],
                           "usage": {"prompt_tokens": 40, "completion_tokens": 7}})
    out = await REAL_LLM(msgs)
    url, headers, body = sent[-1]
    assert out == {"say": "ok", "done": True}, out
    assert url.endswith("/chat/completions") and body["messages"] == msgs, body
    assert body["max_tokens"] == 110 and headers["Authorization"].startswith("Bearer ")
    assert (agent.RUN.tok_in, agent.RUN.tok_out, agent.RUN.llm_calls) == (40, 7, 1)

    # --- Backboard path
    agent.RUN = agent.Run("x")
    agent.LLM_BACKEND = "backboard"
    agent.BB_PROVIDER, agent.BB_MODEL, agent.BB_KEY = "openrouter", "fast/model", "k"
    agent.HTTP = FakeHTTP({"message": '```json\n{"say":"ok","available":true}\n```',
                           "input_tokens": 52, "output_tokens": 9, "thread_id": "th_1"})
    out = await REAL_LLM(msgs)
    url, headers, body = sent[-1]
    assert out == {"say": "ok", "available": True}, out                      # fenced JSON tolerated
    assert url.endswith("/threads/messages") and headers["X-API-Key"] == "k", (url, headers)
    assert body["system_prompt"] == "be brief", body                         # system hoisted out
    assert "thread_id" not in body and body["memory"] == "off", body          # stateless: no memory tax
    assert body["json_output"] is True and body["llm_provider"] == "openrouter"
    assert body["content"].splitlines() == ["Hotel: we have a room", "You: how much?", "Hotel: one forty"], body
    assert (agent.RUN.tok_in, agent.RUN.tok_out, agent.RUN.llm_calls) == (52, 9, 1)

    # single user message (parse_request) goes over bare, no role prefixes
    await REAL_LLM([{"role": "user", "content": "find me a hotel"}])
    assert sent[-1][2]["content"] == "find me a hotel" and "system_prompt" not in sent[-1][2]

    agent.LLM_BACKEND = "openai"
    print("ok  llm backends: openai-compatible + backboard, stateless, tokens counted")


async def main():
    await t_parse()
    await t_llm_backends()
    t_rank_phone()
    await t_shortlist()
    await t_verify_parallel()
    await t_verify_sequential()
    await t_call_cancel_hangs_up()
    await t_webhook_and_email()


if __name__ == "__main__":
    asyncio.run(main())
    print("\nALL TESTS PASSED")
