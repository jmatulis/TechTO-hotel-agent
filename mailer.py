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

FOOTER = "Rate and availability confirmed by an automated phone call. No payment was taken."

STATUS_TEXT = {"verified": "Confirmed available", "unavailable": "No availability",
               "unclear": "Could not confirm", "no_answer": "No answer / busy",
               "unreachable": "Call failed", "timeout": "Call timed out", "no_phone": "No phone listed", "duplicate": "Same number as another hotel",
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
    """-> (subject, plain text, html). `req`: city, check_in, check_out, nights, adults, room_type.

    With a `winner`, the email is about that one hotel: price, details, what it said on the call,
    and the call transcript as a chat. `options` is only listed (briefly) when nobody confirmed.
    `stats` ({label: value}) is shown as a small "Run timing" block at the bottom."""
    w = winner
    n = req.get("nights") or 1
    if w:
        subject = (f"Team # {TEAM_NUMBER} - Hotel Reservation Confirmed!" if TEAM_NUMBER else
                   f"Hotel confirmed: {w.get('name')}, {req.get('city')}, "
                   f"{req.get('check_in')} to {req.get('check_out')}")
    else:
        subject = ((f"Team # {TEAM_NUMBER} - " if TEAM_NUMBER else "")
                   + f"No hotel could be confirmed in {req.get('city')}")

    stay = [("Guest", GUEST_NAME),
            ("Reservation dates", f"{req.get('check_in')} to {req.get('check_out')} "
                                  f"({n} night{'s' * (n > 1)})"),
            ("Guests", req.get("adults")), ("Room requested", req.get("room_type") or "any")]
    total = stay_total(req, w) if w else None
    hotel = [] if not w else [
        ("Hotel name", w.get("name")),
        ("Address", w.get("address") or "n/a"),
        ("Phone", w.get("phone") or "n/a"),
        ("Distance", dist(w.get("km"))),
        ("Reservation #", w.get("reference") or "none given on the call"),
        ("Total for the stay", money(total)),
        ("Nightly rate (quoted on the call)", money(w.get("quoted_night"))),
        ("Nightly rate (listed online)", money(w.get("listed_night"))),
        ("Taxes / fees", w.get("fees") or "not stated"),
        ("Room type", w.get("room_type") or "not stated"),
        ("Cancellation", w.get("cancellation") or "not stated"),
        ("Confirmed at", w.get("verified_at") or "n/a"),
    ]
    chat = lines(w.get("transcript")) if w else []
    said = (w or {}).get("summary") or ""
    tried = [(o.get("name"), STATUS_TEXT.get(o.get("status"), o.get("status") or "n/a"), o.get("phone") or "n/a")
             for o in options] if not w else []

    # ---- plain text
    t = [subject, ""]
    t += [f"{k}: {v}" for k, v in stay + hotel]
    if said:
        t += ["", "WHAT THE HOTEL SAID", said]
    if chat:
        t += ["", "CALL TRANSCRIPT"] + [f"{who}: {txt}" for who, txt in chat]
    if tried:
        t += ["", "HOTELS TRIED"] + [" | ".join(map(str, r)) for r in tried]
    if stats:
        t += ["", "RUN TIMING"] + [f"{k}: {v}" for k, v in stats.items()]
    t += ["", FOOTER]
    text = "\n".join(map(str, t))

    # ---- html (inline styles, one 600px column: renders the same in Gmail, Outlook, Apple Mail)
    font = "font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif"
    cell = f"padding:7px 0;border-bottom:1px solid #eee;{font};font-size:14px;vertical-align:top"

    def rows(items):
        return "<table width='100%' style='border-collapse:collapse'>" + "".join(
            f"<tr><td style='{cell};color:#666;width:42%'>{escape(str(k))}</td>"
            f"<td style='{cell};color:#111'>{v}</td></tr>" for k, v in items) + "</table>"

    def h3(label):
        return (f"<div style='{font};font-size:12px;letter-spacing:.06em;text-transform:uppercase;"
                f"color:#666;margin:26px 0 8px'>{label}</div>")

    if w:
        phone = w.get("phone")
        tel = (f"<a href='tel:{escape(str(phone))}' style='color:#0b57d0;text-decoration:none'>"
               f"{escape(str(phone))}</a>") if phone else "n/a"
        nightly = w.get("quoted_night")
        sub = (f"{money(nightly)} per night &times; {n} night{'s' * (n > 1)}" if isinstance(nightly, (int, float))
               and total is not None and not isinstance(w.get("quoted_total"), (int, float))
               else "quoted for the whole stay")
        facts = [("Address", escape(str(w.get("address") or "n/a"))), ("Phone", tel),
                 ("Distance", escape(dist(w.get("km")))),
                 ("Room", escape(str(w.get("room_type") or req.get("room_type") or "not stated"))),
                 ("Taxes / fees", escape(str(w.get("fees") or "not stated"))),
                 ("Cancellation", escape(str(w.get("cancellation") or "not stated"))),
                 ("Reservation #", escape(str(w.get("reference") or "none given on the call"))),
                 ("Listed online", escape(money(w.get("listed_night")) + " per night")),
                 ("Confirmed at", escape(str(w.get("verified_at") or "n/a")))]
        banner = ("<div style='background:#e6f4ea;color:#137333;padding:10px 20px;font-weight:600;"
                  f"{font};font-size:14px'>&#10003; Room confirmed by phone</div>")
        head = (f"<div style='padding:22px 20px 6px'><div style='{font};font-size:22px;font-weight:700;"
                f"color:#111'>{escape(str(w.get('name')))}</div>"
                f"<div style='{font};font-size:14px;color:#555;margin-top:4px'>{escape(str(req.get('city')))}"
                f" &middot; {escape(str(req.get('check_in')))} to {escape(str(req.get('check_out')))}"
                f" &middot; {req.get('adults')} guest{'s' * ((req.get('adults') or 1) > 1)}</div></div>"
                f"<div style='padding:6px 20px 0'><div style='{font};font-size:34px;font-weight:700;color:#111'>"
                f"{escape(money(total))}</div>"
                f"<div style='{font};font-size:13px;color:#666'>{sub}</div></div>")
        body = [rows(facts)]
        if said:
            body += [h3("What the hotel said"),
                     f"<div style='{font};font-size:14px;color:#222;background:#f6f8fa;border-left:3px solid #137333;"
                     f"padding:10px 14px'>{escape(said)}</div>"]
        if chat:
            body.append(h3("Call transcript"))
            for who, txt in chat:
                hotel_side = who.lower().startswith("hotel")
                body.append(
                    f"<div style='text-align:{'right' if hotel_side else 'left'};margin:6px 0'>"
                    f"<div style='{font};font-size:11px;color:#888;margin-bottom:2px'>{escape(who)}</div>"
                    f"<span style='display:inline-block;max-width:80%;text-align:left;{font};font-size:14px;"
                    f"padding:8px 12px;border-radius:14px;"
                    f"background:{'#d2e3fc' if hotel_side else '#eceff1'};color:#111'>{escape(txt)}</span></div>")
        inner = banner + head + "<div style='padding:14px 20px 4px'>" + "".join(body) + "</div>"
    else:
        banner = ("<div style='background:#fef7e0;color:#b06000;padding:10px 20px;font-weight:600;"
                  f"{font};font-size:14px'>No room could be confirmed</div>")
        listing = rows([(escape(str(a)), f"{escape(str(b))} &middot; {escape(str(c))}") for a, b, c in tried])
        inner = (banner + f"<div style='padding:20px'><div style='{font};font-size:14px;color:#333'>"
                 f"Nobody we phoned in {escape(str(req.get('city')))} confirmed a room for "
                 f"{escape(str(req.get('check_in')))} to {escape(str(req.get('check_out')))}.</div>"
                 + (h3("Hotels tried") + listing if tried else "") + "</div>")
    html = (f"<div style='background:#f1f3f4;padding:20px 8px'><div style='max-width:600px;margin:0 auto;"
            f"background:#fff;border-radius:10px;overflow:hidden'>{inner}"
            + (f"<div style='padding:0 20px'>{h3('Run timing')}"
               + rows([(k, escape(str(v))) for k, v in stats.items()]) + "</div>" if stats else "") +
            f"<div style='{font};font-size:12px;color:#888;padding:14px 20px 20px'>{escape(FOOTER)}</div>"
            "</div></div>")
    return subject, text, html


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
