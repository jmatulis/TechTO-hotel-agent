"""
Call-path test: dials one hotel you invent, with no SerpApi and no email.

Use it to tune the conversation without spending searches or sending mail. Needs a live
tunnel in PUBLIC_URL and a phone in PHONE_OVERRIDE; everything else comes from .env.

  python tests/call_test.py                          # "Test Inn" at 129
  python tests/call_test.py "Good Value Inn" 149     # name and nightly rate

Answer the phone as a front desk would: "yes, we have a queen at 129 plus tax".
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root, for agent.py etc.

import asyncio
import json

import httpx
import uvicorn

import agent


async def main(name: str, price: float):
    if not agent.PHONE_OVERRIDE:
        raise SystemExit("set PHONE_OVERRIDE in .env to the phone you want rung")
    if not agent.PUBLIC_URL:
        raise SystemExit("set PUBLIC_URL in .env to your live tunnel URL")
    if not (agent.TW_SID and agent.TW_TOKEN and agent.TW_FROM):
        raise SystemExit("Twilio credentials missing from .env")

    agent.HTTP = httpx.AsyncClient(http2=True, timeout=10)
    agent.RUN = run = agent.Run(f"call test: {name}")
    run.req = {"city": "Toronto", "check_in": "2026-10-18", "check_out": "2026-10-19",
               "adults": 2, "email": None}

    c = agent.Candidate(0, {"name": name, "overall_rating": 4.3}, price, price)
    c.phone = agent.PHONE_OVERRIDE
    run.cands = [c]

    server = uvicorn.Server(uvicorn.Config(agent.app, host="0.0.0.0", port=agent.PORT,
                                           log_level="warning"))
    srv = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.01)

    print(f"dialing {c.phone} as {name} @ {price:.0f} {agent.CURRENCY}/night")
    print(f"tunnel  {agent.PUBLIC_URL}")
    print(f"voice   mode={agent.EL_MODE} id={agent.EL_VOICE} speed={agent.EL_SPEED}")
    print(f"model   {agent.BB_PROVIDER}/{agent.BB_MODEL}")
    print(f"opener  {agent.opener(run, c)}\n")

    await agent.verify_all(run, [c])

    print("\n--- result ---")
    print("status :", c.status)
    print("facts  :", json.dumps(c.facts, indent=1))
    print(f"turns  : {c.turns} | seconds: {c.billed_seconds} | attempts: {c.attempts}")
    print(f"llm    : {run.llm_calls} calls, {run.tok_in}/{run.tok_out} tokens")
    print(f"tts    : {run.tts_calls} calls, {run.tts_chars} chars, {len(agent.AUDIO)} cached")
    print("\n--- transcript ---")
    for m in c.history:
        print(("Hotel: " if m["role"] == "user" else "Agent: ") + m["content"])

    # let Twilio's status callback land so seconds come from Twilio, not our estimate
    await asyncio.sleep(2.0)
    server.should_exit = True
    await srv
    await agent.HTTP.aclose()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "Test Inn",
                     float(sys.argv[2]) if len(sys.argv) > 2 else 129.0))
