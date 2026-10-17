"""Integration test: the whole pipeline on demo call transcripts, with the REAL Backboard LLM.

Search (SerpApi + LiteAPI), phone lookups and the phone calls are faked; the prompt parse and the
transcript reading are real, so this checks that messy human answers end up in the right CallQuery
fields and that the right hotel wins. Costs ~3 LLM calls. Skipped without BACKBOARD_API_KEY.

  python tests/integ_pipeline.py

Round 1 (3 hotels): fully booked / available but wrong dates / voicemail  -> nobody confirmed.
Round 2 (3 hotels): nightly + tax extra / all-in nightly / a total for both nights -> cheapest stay wins.
(The race stops early once nobody still ringing can beat the best quote by a price band, see decider.py.)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root

import asyncio

import httpx

import agent
import hotel_search as hs
import liteapi_search as lite
import obs
import pipeline as pl
from schemas import Hotel

PROMPT = "King room in Toronto, Oct 18 to Oct 20, 2 adults, send to judge@techto.ca"

H, A = "user", "assistant"        # the hotel speaks as "user"; the agent's opener is added by pipeline.transcript()
DEMO = {   # name: (listed nightly price, hotel phone, what the front desk says)
    "Lakeview Inn": (99, "416-555-0101", [
        (H, "Lakeview Inn front desk, this is Marcus."),
        (H, "Oh, for that weekend? Sorry, we're completely sold out, there's a conference in town."),
        (A, "Understood, thank you for your time. Goodbye."),
    ]),
    "Harbour Suites": (105, "416-555-0102", [
        (H, "Harbour Suites, how can I help?"),
        (H, "A king room, let me check. We do have one, but only from the nineteenth to the twenty-first of October. "
            "The eighteenth is taken."),
        (A, "So not the eighteenth. Thank you, goodbye."),
    ]),
    "Maple Motel": (110, "416-555-0103", [
        (H, "Thank you for calling Maple Motel. Our office is currently closed. Please leave a message after the tone."),
    ]),
    "Bay Street Hotel": (120, "416-555-0104", [
        (H, "Bay Street Hotel, Priya speaking."),
        (H, "Yes, we have a king room free both nights, two guests, no problem."),
        (A, "Great. What is the nightly rate?"),
        (H, "One thirty-five a night, plus thirteen percent tax and a twenty dollar resort fee."),
        (A, "And the cancellation policy?"),
        (H, "Free cancellation until forty-eight hours before arrival."),
    ]),
    "Union Lodge": (125, "416-555-0105", [
        (H, "Union Lodge."),
        (H, "King for the eighteenth and nineteenth? Yes, available. It's one twenty-nine a night, taxes included, "
            "no extra fees."),
        (A, "Thank you. Cancellation?"),
        (H, "Non-refundable at that rate, I'm afraid."),
    ]),
    "Queen West Inn": (125, "416-555-0106", [
        (H, "Queen West Inn, Dana here."),
        (H, "A king, October eighteenth to twentieth? We have it. I can do two hundred and forty dollars total "
            "for the two nights, all in."),
        (A, "Perfect, and cancellation?"),
        (H, "Free up to the day before."),
    ]),
}
EXPECT = {"Lakeview Inn": False, "Harbour Suites": False, "Maple Motel": None,   # round 1: nobody
          "Bay Street Hotel": True, "Union Lodge": True, "Queen West Inn": True}  # round 2
WINNER, WINNER_STAY = "Queen West Inn", 240       # 240 total beats Union 2 x 129 = 258 and Bay 2 x 135 = 270


async def main():
    if not agent.BB_KEY:
        print("skipped: BACKBOARD_API_KEY is empty")
        return
    pl.BATCH, pl.MAX_ROUNDS, pl.RADIUS_KM, pl.SEARCH_PAGES = 3, 3, 20, 1
    agent.PHONE_OVERRIDE = ""

    async def fake_serp(params):
        if "property_token" in params:
            return {"phone": DEMO[params["property_token"]][1]}
        if params["engine"] == "google_maps":
            return {"place_results": {"gps_coordinates": {"latitude": 43.6532, "longitude": -79.3832}}}
        return {"properties": [{"name": n, "rate_per_night": {"extracted_lowest": d[0]}, "property_token": n,
                                "gps_coordinates": {"latitude": 43.66, "longitude": -79.39}}
                               for n, d in DEMO.items()]}

    async def fake_lite(q):     # a duplicate of Union Lodge (pricier, must merge away) and a pricey extra
        return [Hotel(source="liteapi", id="lp1", name="Union Lodge", lat=43.66, lng=-79.39, distance_km=1.0,
                      price=300, currency=agent.CURRENCY, phone="+14165550105"),
                Hotel(source="liteapi", id="lp2", name="Palace Grand", lat=43.65, lng=-79.38, distance_km=0.2,
                      price=900, currency=agent.CURRENCY, phone="+14165550199")]

    async def fake_call(run, c):    # "the phone call": replay the demo transcript
        c.attempts += 1
        c.history = [{"role": r, "content": t} for r, t in DEMO[c.name][2]]
        c.status = "unclear"        # the live guess; transcript reading decides

    async def no_tts(text):
        return None

    hs.serp, lite.find_hotels, agent.call_candidate, agent.tts = fake_serp, fake_lite, fake_call, no_tts
    agent.HTTP = hs.HTTP = httpx.AsyncClient(http2=True, timeout=30)
    agent.RUN = run = agent.Run(PROMPT)
    obs.setup(service="hotel-agent-test")            # spans go to Logfire when a token is set
    try:
        with obs.span("hotel-agent run", test="integ_pipeline") as run.span:
            winner = await pl.run_pipeline(run)
    finally:
        await agent.HTTP.aclose()
        obs.flush()

    print(f"request: {run.req}\nllm: {run.llm_calls} calls, {run.tok_in}/{run.tok_out} tokens\n")
    for c in run.cands:
        cq = c.call
        print(f"{c.name:<18} {c.status:<12} available={getattr(cq, 'available', '-')!s:<5} "
              f"stay={getattr(cq, 'price', '-')!s:<7} | {getattr(cq, 'summary', '')}")
    called = [c for c in run.cands if c.call]

    assert (run.req["city"], run.req["adults"]) == ("Toronto", 2), run.req
    assert run.req["check_in"][5:] == "10-18" and run.req["check_out"][5:] == "10-20", run.req
    assert [c.name for c in run.cands].count("Union Lodge") == 1 and "Palace Grand" in [c.name for c in run.cands]
    for c in called:
        assert c.call.available is EXPECT[c.name] or (EXPECT[c.name] is None and not c.call.available), \
            f"{c.name}: expected {EXPECT[c.name]}, got {c.call.available} ({c.call.summary})"
        assert c.call.transcript.startswith("Agent: Hi, automated call."), c.name
    assert {"Lakeview Inn", "Harbour Suites", "Maple Motel", "Queen West Inn"} <= {c.name for c in called}, \
        "round 1 is read in full; in round 2 the race may cut calls once nobody left can win a price band"
    assert winner is not None and winner.name == WINNER, f"winner {winner and winner.name}, wanted {WINNER}"
    assert abs(winner.call.price - WINNER_STAY) < 1, winner.call
    assert run.winner is winner and agent.receipt(run)[0].startswith("[VERIFIED] Queen West Inn")
    assert 1 < run.llm_calls <= 7, f"1 parse + 1 read per call that talked, got {run.llm_calls}"
    print("\nOK  integ pipeline: 2 rounds, wrong dates / voicemail / sold out rejected, cheapest total wins, race cut the rest")


if __name__ == "__main__":
    asyncio.run(main())
