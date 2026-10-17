"""Observability: Logfire spans for the whole run, or nothing at all.

  obs.setup()                                  # once, in main(); no token -> everything below is a no-op
  with obs.span("search", city="Toronto") as s:
      ...
      s.set_attribute("hotels", 12)
  obs.event("decision", reason="...")          # a point-in-time log line inside the current span

Spans nest through asyncio contextvars, so a task started inside a span reports under it. The call
webhooks (/turn) run in other tasks: `ctx = obs.capture()` when the call starts, then
`with obs.activate(ctx):` in the handler puts the turn under that call in the waterfall.

Never put API keys, URLs with keys, or headers in attributes: log names, counts, timings, transcripts.
Offline tests never call setup(), so they stay silent even with a token in the environment.
LOGFIRE_CONSOLE=1 also prints the span tree to the terminal (and the project link).
"""
from __future__ import annotations

import contextlib
import os

try:
    import logfire
except ImportError:                               # optional dependency
    logfire = None

ENABLED = False
KEEP_UNSCRUBBED = {"transcript", "summary"}       # call text may contain words Logfire's scrubber redacts


class _Null:
    def set_attribute(self, *a, **k): ...
    def set_attributes(self, *a, **k): ...
    def record_exception(self, *a, **k): ...


def setup(service: str = "hotel-agent", token: str | None = None, processors: list | None = None) -> bool:
    """Start exporting to Logfire. `token` defaults to LOGFIRE_WRITE_TOKEN / LOGFIRE_TOKEN.
    `processors` is for tests: extra OpenTelemetry span processors, with nothing sent to Logfire."""
    global ENABLED
    token = token or os.environ.get("LOGFIRE_WRITE_TOKEN") or os.environ.get("LOGFIRE_TOKEN")
    if logfire is None or not (token or processors):
        return False

    def keep(match):                              # Logfire redacts values that look sensitive
        return match.value if match.path and match.path[-1] in KEEP_UNSCRUBBED else None

    logfire.configure(service_name=service, token=token, send_to_logfire=bool(token and not processors),
                      console=logfire.ConsoleOptions(min_log_level="info") if os.environ.get("LOGFIRE_CONSOLE") == "1" else False,
                      additional_span_processors=processors,
                      scrubbing=logfire.ScrubbingOptions(callback=keep), inspect_arguments=False)
    ENABLED = True
    return True


def span(name: str, **attrs):
    """Context manager: a span named `name`; yields an object with set_attribute()."""
    if not ENABLED:
        return contextlib.nullcontext(_Null())
    return logfire.span(name, **attrs)


def event(name: str, **attrs):
    if ENABLED:
        logfire.info(name, **attrs)


def capture():
    """The current trace context, to re-enter later from another task. None when disabled."""
    if not ENABLED:
        return None
    from opentelemetry import context
    return context.get_current()


@contextlib.contextmanager
def activate(ctx):
    if ctx is None:
        yield
        return
    from opentelemetry import context
    token = context.attach(ctx)
    try:
        yield
    finally:
        context.detach(token)


def flush():
    if ENABLED:
        logfire.force_flush()
