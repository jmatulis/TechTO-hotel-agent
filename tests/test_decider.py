"""Offline tests for decider.py: when to stop waiting for calls. Run: python tests/test_decider.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root

from decider import Board, Entry, Policy


def board(listed: dict, **policy) -> Board:
    return Board([Entry(n, n, p) for n, p in listed.items()], Policy(**policy))


def ring(b, *names, at=0.0):
    for n in names:
        b.start(n, at)


def main():
    # nothing confirmed yet: keep waiting; everything answered and no yes: stop with no winner
    b = board({"A": 100, "B": 110})
    ring(b, "A", "B")
    assert not b.decide(5).stop
    b.answer("A", 6, False)
    assert not b.decide(6).stop
    b.answer("B", 8, False)
    d = b.decide(8)
    assert d.stop and d.winner is None, d

    # the cheapest listing confirms: everything else is in its band or dearer -> stop at once
    b = board({"A": 100, "B": 104, "C": 130})
    ring(b, "A", "B", "C")
    b.answer("A", 5, True, 100)
    d = b.decide(5)
    assert d.stop and d.winner == "A", d

    # a dear hotel confirms while a far cheaper one rings: 0.5/s x ~40 s = 20 < expected saving -> wait ...
    b = board({"Cheap": 100, "Dear": 200})
    ring(b, "Cheap", "Dear")
    b.answer("Dear", 5, True, 200)
    d = b.decide(5)
    assert not d.stop and "Cheap" in d.reason, d
    # ... and it says no: stop with the dear one
    b.answer("Cheap", 12, False)
    d = b.decide(12)
    assert d.stop and d.winner == "Dear", d
    # ... or it says yes: it wins
    b = board({"Cheap": 100, "Dear": 200})
    ring(b, "Cheap", "Dear")
    b.answer("Dear", 5, True, 200)
    b.answer("Cheap", 12, True, 105)
    assert b.decide(12).winner == "Cheap"

    # the same situation with latency priced very high: take the dear one now
    b = board({"Cheap": 100, "Dear": 200}, latency_cost=50)
    ring(b, "Cheap", "Dear")
    b.answer("Dear", 5, True, 200)
    d = b.decide(5)
    assert d.stop and d.winner == "Dear", d

    # a small potential saving (inside one price band) is never worth a wait
    b = board({"X": 100, "Y": 104}, latency_cost=0.0001)
    ring(b, "X", "Y")
    b.answer("Y", 5, True, 104)
    d = b.decide(5)           # X's best case 95 and Y's 104 share band 0: one band is max(10, 10% of 100) wide
    assert d.stop and d.winner == "Y", d

    # ... but one band cheaper is enough to wait (when waiting is nearly free)
    b = board({"X": 100, "Y": 112}, latency_cost=0.0001)
    ring(b, "X", "Y")
    b.answer("Y", 5, True, 112)
    assert not b.decide(5).stop

    # quotes, not listings, decide among confirmed rooms (and a quote above the listing is fine)
    b = board({"A": 100, "B": 120})
    ring(b, "A", "B")
    b.answer("A", 5, True, 180)
    b.answer("B", 6, True, 130)
    d = b.decide(6)
    assert d.stop and d.winner == "B", d

    # hit rate: after many "no" answers a cheaper pending hotel is less likely to say yes -> less worth waiting
    def waits(nos):
        b = board({**{f"n{i}": 90 for i in range(nos)}, "Cheap": 100, "Dear": 140}, latency_cost=1.0)
        ring(b, *b._by_key)
        for i in range(nos):
            b.answer(f"n{i}", 40, False)
        b.answer("Dear", 40, True, 140)
        return not b.decide(41).stop
    assert waits(0) and not waits(8)

    # how long a pending call still needs: rest of a typical (40 s) call, half the time to the cut-off once
    # older than that, never under 3 s
    b = board({"A": 100})
    for age, want in ((0, 40), (30, 10), (38, 3), (50, 5), (59, 3)):
        b.start("A", 0)
        assert b._still_needs(b._by_key["A"], age) == want, (age, b._still_needs(b._by_key["A"], age))
    b = board({"A": 100})
    assert b._still_needs(b._by_key["A"], 0) == 40                 # not dialed yet

    # the table renders
    b = board({"Cheap": 100, "Dear": 200})
    ring(b, "Cheap")
    b.answer("Cheap", 5, True, 105)
    r = b.render(6)
    assert "Cheap" in r and "confirmed" in r and "105" in r and "queued" in r, r
    print("decider tests ok")


main()
