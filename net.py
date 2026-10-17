"""Rate-limit handling for the outside APIs: limit how many requests are in flight, retry a 429.

  async with net.gate("eleven", 4):           # at most 4 at once, the rest wait their turn
      r = await net.retry_429(lambda: client.post(...), label="eleven")

We fire up to 15 calls at once, and each one talks to the LLM, ElevenLabs and Twilio. Without a
cap that is a burst, and the providers answer 429. `retry_429` waits (Retry-After if the server
sends one, else a short exponential backoff) and tries again; a 429 that never clears is returned
to the caller, whose own error handling (TTS falls back to Twilio's voice, etc.) takes over.
"""
from __future__ import annotations

import asyncio
import random

import httpx

import obs

_gates: dict[str, asyncio.Semaphore] = {}


def gate(name: str, limit: int) -> asyncio.Semaphore:
    """One semaphore per name, created inside the running loop on first use."""
    if name not in _gates:
        _gates[name] = asyncio.Semaphore(max(1, limit))
    return _gates[name]


def reset():
    """Forget the gates (tests, or a new event loop)."""
    _gates.clear()


def retry_after(r: httpx.Response) -> float | None:
    try:
        return float(r.headers.get("retry-after", ""))
    except ValueError:
        return None


async def retry_429(send, tries: int = 3, base: float = 0.5, cap: float = 5.0, label: str = "") -> httpx.Response:
    """await send() until it stops answering 429 (at most `tries` attempts). Returns the last response."""
    for i in range(tries):
        r = await send()
        if getattr(r, "status_code", 200) != 429 or i == tries - 1:
            return r
        wait = min(cap, retry_after(r) or base * 2 ** i) + random.random() * 0.2
        obs.event("rate limited", service=label, attempt=i + 1, wait_s=round(wait, 2))
        await asyncio.sleep(wait)
    return r  # pragma: no cover


def fail(r: httpx.Response, label: str) -> None:
    """Raise for an error status WITHOUT the request URL (SerpApi puts the api_key in it)."""
    if r.is_error:
        raise RuntimeError(f"{label}: HTTP {r.status_code} {r.reason_phrase}: {r.text[:200]}")
