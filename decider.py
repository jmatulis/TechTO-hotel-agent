"""When to stop waiting for phone calls: the price/latency trade-off, as arithmetic.

We phone ~12 hotels at once. Calls finish one by one; after each one `Board.decide()` answers:
stop now with the cheapest confirmed room, or keep waiting. Pure functions on numbers: no I/O.

The table (one Entry per hotel): listed stay price from the search, call state, and once the call
is read: available? and the price the hotel quoted for the whole stay.

Rule, with B = cheapest confirmed quote so far:
  1. Nothing confirmed yet                    -> wait.
  2. Nobody left on the phone                 -> stop; B wins.
  3. For each hotel still on the phone, p:
       best case  O_p = listed_p x (1 - slack)       the quote can land a bit under the listing
       segment    prices are cut into fixed bands of width w = max(segment_min, segment_pct x cheapest
                  listing), the first band starting at the cheapest best case. If O_p is in the same band
                  as B, or a dearer one, p could save less than a band at most: ignore it.
       otherwise  expected saving   = q x (B - O_p)
                  q = (confirmed + 1) / (answered + 2)   observed hit rate, 1/2 prior when nothing is in
                  cost of waiting   = latency_cost x seconds p still needs
                                      (rest of a typical call; half the time to the cut-off once it is older
                                      than typical; never under min_wait_s)
                  worth waiting iff expected saving > cost of waiting.
  4. Any hotel worth waiting for              -> wait. Otherwise stop; B wins.

So: if the cheapest hotel confirms, everything else sits in its band or above -> stop at once.
A far cheaper hotel still ringing is waited for only while its odds-weighted saving beats the
seconds it costs. `latency_cost` is the knob: currency you'd pay to save one second.
"""
from __future__ import annotations

from dataclasses import dataclass
from statistics import median


@dataclass
class Policy:
    latency_cost: float = 0.5      # currency per second of waiting
    segment_pct: float = 0.10      # band width as a share of the cheapest listed stay price
    segment_min: float = 10.0      # ... but never narrower than this (currency)
    slack: float = 0.05            # a quote may come in this far under the listing
    call_s: float = 40.0           # typical call length until we have measured some
    min_wait_s: float = 3.0        # a pending call needs at least this long to report
    deadline_s: float = 60.0       # calls are dropped after this long


@dataclass
class Entry:
    key: str
    name: str
    listed: float                  # stay price from the search
    started: float | None = None   # seconds on the board clock when the call began
    answered: bool = False         # call finished (any outcome) and its transcript was read
    available: bool | None = None
    quote: float | None = None     # stay price quoted on the call
    seconds: float | None = None   # how long the call took

    @property
    def price(self) -> float:
        return self.quote if self.quote is not None else self.listed


@dataclass
class Decision:
    stop: bool
    winner: str | None             # Entry.key of the cheapest confirmed room (None: nothing confirmed)
    reason: str


class Board:
    def __init__(self, entries: list[Entry], policy: Policy | None = None):
        self.entries = entries
        self.policy = policy or Policy()
        self._by_key = {e.key: e for e in entries}

    # ---- updates
    def start(self, key: str, now: float):
        self._by_key[key].started = now

    def answer(self, key: str, now: float, available: bool | None, quote: float | None = None):
        e = self._by_key[key]
        e.answered, e.available = True, available
        e.quote = quote if available else None
        e.seconds = now - e.started if e.started is not None else None

    # ---- the table
    @property
    def width(self) -> float:
        cheapest = min((e.listed for e in self.entries), default=0.0)
        return max(self.policy.segment_min, self.policy.segment_pct * cheapest)

    def segment(self, price: float) -> int:
        """Band 0 starts at the cheapest best case (cheapest listing less the slack)."""
        base = min((e.listed for e in self.entries), default=0.0) * (1 - self.policy.slack)
        return int((price - base) // self.width)

    def _call_seconds(self) -> float:
        done = [e.seconds for e in self.entries if e.answered and e.seconds]
        return median(done) if done else self.policy.call_s

    def _still_needs(self, e: Entry, now: float) -> float:
        p = self.policy
        if e.started is None:
            return max(p.min_wait_s, self._call_seconds())
        age, typical = now - e.started, self._call_seconds()
        left = p.deadline_s - age
        # younger than a typical call: the rest of a typical call. Older: it is probably stuck on a
        # menu or on hold, so expect it to run about half way to the cut-off.
        return max(p.min_wait_s, min(left, typical - age if age < typical else left / 2))

    # ---- the decision
    def decide(self, now: float) -> Decision:
        p = self.policy
        answered = [e for e in self.entries if e.answered]
        pending = [e for e in self.entries if not e.answered]
        confirmed = [e for e in answered if e.available is True]
        best = min(confirmed, key=lambda e: e.price, default=None)
        if not pending:
            return Decision(True, best.key if best else None,
                            "all calls finished" if best else "all calls finished, nothing confirmed")
        if best is None:
            return Decision(False, None, f"nothing confirmed yet, {len(pending)} still on the phone")
        q = (len(confirmed) + 1) / (len(answered) + 2)
        for e in sorted(pending, key=lambda e: e.listed):
            best_case = e.listed * (1 - p.slack)
            if self.segment(best_case) >= self.segment(best.price):
                continue                                    # same band as the winner or dearer
            gain, cost = q * (best.price - best_case), p.latency_cost * self._still_needs(e, now)
            if gain > cost:
                return Decision(False, None, f"waiting for {e.name}: expected saving {gain:.0f} > "
                                             f"{cost:.0f} of latency")
        return Decision(True, best.key, f"{best.name} at {best.price:.0f}: nobody still ringing is "
                                        f"worth the wait")

    def render(self, now: float) -> str:
        out = [f"{'hotel':<28} {'listed':>8} {'seg':>3}  {'state':<12} {'quote':>8}"]
        for e in sorted(self.entries, key=lambda e: e.listed):
            state = ("confirmed" if e.available else "no" if e.answered else
                     "ringing" if e.started is not None else "queued")
            out.append(f"{e.name[:28]:<28} {e.listed:>8.0f} {self.segment(e.listed):>3}  {state:<12} "
                       f"{('%.0f' % e.quote) if e.quote is not None else '-':>8}")
        return "\n".join(out)
