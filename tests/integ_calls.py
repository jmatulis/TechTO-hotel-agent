"""LIVE test: three dummy Toronto hotels whose "front desks" are real phones (ours).

No search is run (no SerpApi / LiteAPI spend). The pipeline gets three made-up hotels, rings all
three phones at once through Twilio, the real LLM talks to whoever picks up, reads the transcripts
into CallQuery objects, picks the cheapest confirmed room and emails the receipt with the
transcript. Real Twilio minutes, real LLM calls, a real email.

Before you run it
  1. cloudflared tunnel --url http://localhost:8080   -> put the URL in PUBLIC_URL in .env
  2. Twilio, Backboard and email (RESEND_API_KEY or SMTP_*) keys set in .env
  3. Whoever answers plays the front desk. Suggested script:
       Jonas Hotel   (226-820-8441): "Sorry, we're sold out."
       Jordan Hotel  (519-702-6611): "Yes, king room, $150 a night plus tax."
       Jawahar Hotel (343-987-9162): "Yes, king room, $135 a night, taxes included."  <- should win

  python tests/integ_calls.py                       # email goes to JUDGE_EMAIL
  python tests/integ_calls.py you@example.com       # or to this address
  python tests/integ_calls.py --dry                 # show the plan, dial nobody
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root

import asyncio
from datetime import date, timedelta

import agent
import hotel_search as hs
import pipeline as pl
import ranker
from schemas import Hotel

# name, phone, listed price for the one night, distance from the centre (km)
DUMMY = [("Jonas Hotel", "+12268208441", 140, 1.2),
         ("Jordan Hotel", "+15197026611", 150, 2.5),
         ("Jawahar Hotel", "+13439879162", 160, 3.8)]


def dummy_hotels(currency: str) -> list[Hotel]:
    return ranker.rank([
        Hotel(source="dummy", id=f"dummy-{i}", name=name, address=f"{100 + i} Test St, Toronto, ON",
              lat=43.65 + i / 100, lng=-79.38, distance_km=km, price=price, currency=currency, phone=phone)
        for i, (name, phone, price, km) in enumerate(DUMMY)])


async def main(argv: list[str]):
    dry = "--dry" in argv
    email = next((a for a in argv if "@" in a), agent.JUDGE_EMAIL)
    cin = date.today() + timedelta(days=1)
    prompt = (f"King room in Toronto, {cin:%B} {cin.day} to {cin + timedelta(days=1):%B} "
              f"{(cin + timedelta(days=1)).day}, 2 adults" + (f", send to {email}" if email else ""))

    async def fake_search(req):
        return dummy_hotels(agent.CURRENCY)

    async def no_lookup(*a, **k):      # phones are already on the dummy hotels
        raise AssertionError("no lookups expected")

    pl.search = fake_search
    hs.serp = no_lookup
    hs.SERPAPI_KEY = hs.SERPAPI_KEY or "unused"      # preflight wants it; this test never searches
    agent.PHONE_OVERRIDE = ""                        # each call must ring its own hotel, not one override line

    print("prompt:", prompt)
    for h in dummy_hotels(agent.CURRENCY):
        print(f"  {h.name:<14} {h.phone}  {h.price:.0f} {h.currency}/night  {h.distance_km} km")
    print("email to:", email or "(none: receipt skipped)")
    if dry:
        return
    await pl.main(prompt)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
