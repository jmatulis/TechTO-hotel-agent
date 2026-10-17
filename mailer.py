"""
The confirmation email: build it from plain dicts, send it via Resend (HTTP) or Gmail SMTP.

  subject, text, html = build(request, winner, options, stats)
  await send(to, subject, text, html)

`winner` / each of `options` is a dict with any of: name, address, phone, km (distance from the
city centre), listed_night, quoted_night, quoted_total, fees, room_type, cancellation, reference,
status, verified_at, transcript (str or [{"role", "content"}]). Missing keys print as "n/a".

Subject follows the organizers' format when TEAM_NUMBER is set:
  "Team # <N> - Hotel Reservation Confirmed!"
"""
from __future__ import annotations

import asyncio
import base64
import os
import smtplib
from email.message import EmailMessage
from html import escape

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass


def E(key: str, default: str = "") -> str:
    return os.environ.get(key) or default


TEAM_NUMBER = E("TEAM_NUMBER")
CURRENCY = E("CURRENCY", "CAD")
GUEST_NAME = E("GUEST_NAME", "Jordan Fulawka")
RESEND_KEY = E("RESEND_API_KEY")
MAIL_FROM = E("MAIL_FROM")
SMTP_HOST = E("SMTP_HOST", "smtp.gmail.com")
SMTP_USER = E("SMTP_USER")
SMTP_PASS = E("SMTP_PASS")                         # Gmail app password

STATUS_TEXT = {"verified": "Confirmed available", "unavailable": "No availability",
               "unclear": "Could not confirm", "no_answer": "No answer / busy",
               "unreachable": "Call failed", "timeout": "Call timed out", "no_phone": "No phone listed",
               "cancelled": "Not needed", "error": "Error", "queued": "Not called"}


def configured() -> str | None:
    """None when sending can work, else what's missing."""
    if RESEND_KEY:
        return None if MAIL_FROM else "RESEND_API_KEY is set but MAIL_FROM is empty"
    if SMTP_USER and SMTP_PASS:
        return None
    return "set RESEND_API_KEY + MAIL_FROM, or SMTP_USER + SMTP_PASS (Gmail app password)"


# ---------------------------------------------------------------- formatting
def money(v) -> str:
    return f"${float(v):,.2f} {CURRENCY}" if isinstance(v, (int, float)) else "n/a"


def dist(v) -> str:
    return f"{float(v):.1f} km from city centre" if isinstance(v, (int, float)) else "n/a"


def lines(transcript) -> list[tuple[str, str]]:
    """[(speaker, text)] from either a 'Agent: ...\\nHotel: ...' string or chat-style dicts."""
    if not transcript:
        return []
    if isinstance(transcript, str):
        out = []
        for ln in transcript.splitlines():
            who, _, said = ln.partition(": ")
            if said:
                out.append((who.strip(), said.strip()))
        return out
    return [("Hotel" if m.get("role") == "user" else "Agent", m.get("content", "")) for m in transcript]


def stay_total(req: dict, h: dict):
    n = req.get("nights") or 1
    if isinstance(h.get("quoted_total"), (int, float)):
        return h["quoted_total"]
    if isinstance(h.get("quoted_night"), (int, float)):
        return h["quoted_night"] * n
    return None


def build(req: dict, winner: dict | None, options: list[dict], stats: dict | None = None
          ) -> tuple[str, str, str]:
    """-> (subject, plain text, html). `req`: city, check_in, check_out, nights, adults, room_type."""
    w = winner
    if w:
        subject = (f"Team # {TEAM_NUMBER} - Hotel Reservation Confirmed!" if TEAM_NUMBER else
                   f"Hotel confirmed: {w.get('name')}, {req.get('city')}, "
                   f"{req.get('check_in')} to {req.get('check_out')}")
    else:
        subject = ((f"Team # {TEAM_NUMBER} - " if TEAM_NUMBER else "")
                   + f"No hotel could be confirmed in {req.get('city')}")

    summary = [("Guest", GUEST_NAME), ("City", req.get("city")),
               ("Reservation dates", f"{req.get('check_in')} to {req.get('check_out')} "
                                     f"({req.get('nights') or 1} night{'s' * ((req.get('nights') or 1) > 1)})"),
               ("Guests", req.get("adults")), ("Room requested", req.get("room_type") or "any")]
    hotel = []
    if w:
        hotel = [
            ("Hotel name", w.get("name")),
            ("Address", w.get("address") or "n/a"),
            ("Distance", dist(w.get("km"))),
            ("Phone", w.get("phone") or "n/a"),
            ("Reservation #", w.get("reference") or "none given on the call"),
            ("Nightly rate (quoted on the call)", money(w.get("quoted_night"))),
            ("Total for the stay", money(stay_total(req, w))),
            ("Nightly rate (listed online)", money(w.get("listed_night"))),
            ("Taxes / fees", w.get("fees") or "not stated"),
            ("Room type", w.get("room_type") or "not stated"),
            ("Cancellation", w.get("cancellation") or "not stated"),
            ("Confirmed at", w.get("verified_at") or "n/a"),
        ]
    opts = [(o.get("name"), dist(o.get("km")).replace(" from city centre", ""),
             money(o.get("listed_night")), money(o.get("quoted_night")),
             STATUS_TEXT.get(o.get("status"), o.get("status") or "n/a"), o.get("phone") or "n/a")
            for o in options]
    head = ("Hotel", "Distance", "Listed /night", "Quoted /night", "Result", "Phone")
    stat_rows = [(k, v) for k, v in (stats or {}).items()]

    # ---- plain text
    t = [subject, ""]
    t += [f"{k}: {v}" for k, v in summary + hotel]
    t += ["", "EVERY OPTION CHECKED"] + [" | ".join(map(str, r)) for r in [head] + opts]
    for o in [w] + [o for o in options if o is not w] if w else options:
        if o and lines(o.get("transcript")):
            t += ["", f"CALL TRANSCRIPT: {o.get('name')}"]
            t += [f"{who}: {said}" for who, said in lines(o.get("transcript"))]
    if stat_rows:
        t += ["", "RUN STATS"] + [f"{k}: {v}" for k, v in stat_rows]
    text = "\n".join(map(str, t))

    # ---- html
    def table(rows, header=None):
        cell = "padding:6px 10px;border:1px solid #ddd;text-align:left;vertical-align:top"
        h = ("<tr>" + "".join(f"<th style='{cell};background:#f4f4f4'>{escape(str(x))}</th>"
                              for x in header) + "</tr>") if header else ""
        body = "".join("<tr>" + "".join(f"<td style='{cell}'>{escape(str(x))}</td>" for x in r) + "</tr>"
                       for r in rows)
        return f"<table style='border-collapse:collapse;font:14px sans-serif'>{h}{body}</table>"

    html = [f"<h2 style='font-family:sans-serif'>{escape(subject)}</h2>", table(summary)]
    if hotel:
        html += ["<h3 style='font-family:sans-serif'>Confirmed hotel</h3>", table(hotel)]
    html += ["<h3 style='font-family:sans-serif'>Every option checked</h3>", table(opts, head)]
    for o in [w] + [o for o in options if o is not w] if w else options:
        if o and lines(o.get("transcript")):
            html.append(f"<h3 style='font-family:sans-serif'>Call transcript: {escape(str(o.get('name')))}</h3>")
            html += [f"<p style='font:14px sans-serif;margin:4px 0'><b>{escape(who)}:</b> {escape(said)}</p>"
                     for who, said in lines(o.get("transcript"))]
    if stat_rows:
        html += ["<h3 style='font-family:sans-serif'>Run stats</h3>", table(stat_rows)]
    return subject, text, "".join(html)


# ---------------------------------------------------------------- sending
def _smtp(to: str, subject: str, text: str, html: str, attachments: list | None = None):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = MAIL_FROM or SMTP_USER, to, subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    for name, data, mime in attachments or []:
        main, _, sub = mime.partition("/")
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    with smtplib.SMTP_SSL(SMTP_HOST, 465, timeout=10) as s:
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)


async def send(to: str, subject: str, text: str, html: str, client: httpx.AsyncClient | None = None,
               attachments: list[tuple[str, bytes, str]] | None = None):
    """Resend over HTTP when keyed (fast), else Gmail SMTP in a thread (never blocks the loop).
    attachments: [(filename, bytes, mime type)], e.g. the winning call's mp3."""
    if problem := configured():
        raise RuntimeError(problem)
    if RESEND_KEY:
        own = client is None
        client = client or httpx.AsyncClient(timeout=10)
        try:
            r = await client.post("https://api.resend.com/emails",
                                  headers={"Authorization": f"Bearer {RESEND_KEY}"},
                                  json={"from": MAIL_FROM, "to": [to], "subject": subject,
                                        "text": text, "html": html,
                                        **({"attachments": [{"filename": n, "content": base64.b64encode(d).decode()}
                                                            for n, d, _ in attachments]} if attachments else {})})
            r.raise_for_status()
        finally:
            if own:
                await client.aclose()
    else:
        await asyncio.to_thread(_smtp, to, subject, text, html, attachments)
