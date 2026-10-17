"""LIVE mailer test: builds the confirmation email for a demo hotel and really sends it.

No search, no calls, no LLM. Uses RESEND_API_KEY (+ MAIL_FROM) or SMTP_USER/SMTP_PASS from .env.

  python tests/integ_mailer.py                  # to JUDGE_EMAIL
  python tests/integ_mailer.py you@example.com  # to this address
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root

import asyncio

import httpx

import mailer

REQ = {"city": "Toronto", "check_in": "2026-10-18", "check_out": "2026-10-19", "nights": 1,
       "adults": 2, "room_type": "king"}
HOTEL = {
    "name": "Jawahar Hotel (demo)", "address": "102 Test St, Toronto, ON", "phone": "+13439879162",
    "km": 3.8, "listed_night": 160, "quoted_night": 135, "quoted_total": None, "fees": "taxes included",
    "room_type": "King", "cancellation": "Free cancellation until 24 hours before", "reference": "JW-4471",
    "verified_at": "2026-10-18 09:04:12 EDT", "status": "verified",
    "summary": "King room available Oct 18, $135 a night taxes included, free cancellation.",
    "transcript": ("Agent: Hi, this is an automated assistant calling for the guest. Do you have a king room "
                   "available October 18 for 2 guests, around 160 a night?\n"
                   "Hotel: Yes we do, 135 a night, taxes included.\n"
                   "Agent: And the cancellation policy?\n"
                   "Hotel: Free until 24 hours before. Reference J W 4471.\n"
                   "Agent: Perfect, thank you. Goodbye!"),
}


async def main(argv: list[str]):
    to = next((a for a in argv if "@" in a), "") or mailer.E("JUDGE_EMAIL")
    if not to:
        raise SystemExit("no address: pass one, or set JUDGE_EMAIL in .env")
    if problem := mailer.configured():
        raise SystemExit(f"mailer not configured: {problem}")
    subject, text, html = mailer.build(REQ, HOTEL, [HOTEL])
    async with httpx.AsyncClient(timeout=15) as client:
        await mailer.send(to, f"[TEST] {subject}", text, html, client=client)
    print(f"sent via {'Resend' if mailer.RESEND_KEY else 'SMTP'} to {to}: [TEST] {subject}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
