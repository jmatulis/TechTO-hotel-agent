"""Ranking hotels: merge the searches, order what to phone, then pick the winner from the calls.

    hotels = rank(merge(serp_hotels, lite_hotels))   # initial order: which to call first
    ...phone them, fill Hotel.call_query...
    best = cheapest(hotels)                          # cheapest one that confirmed a room

Pure arithmetic: no API calls, no tokens.
"""
import math
import re
from difflib import SequenceMatcher

from schemas import Hotel, RankWeights


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle (haversine) distance."""
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lng2 - lng1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


# ---------------------------------------------------------------- 1. merge
_FILLER = {"the", "hotel", "hotels", "by"}


def _name(h: Hotel) -> str:
    return " ".join(w for w in re.sub(r"[^a-z0-9 ]", " ", h.name.lower()).split() if w not in _FILLER)


def _digits(phone: str | None) -> str:
    return re.sub(r"\D", "", phone or "")[-10:]


def same_hotel(a: Hotel, b: Hotel) -> bool:
    """Close together (when both have coordinates) and near-identical names. An equal phone number
    only lowers the name bar: Places sometimes hands two different hotels the same number."""
    if None not in (a.lat, a.lng, b.lat, b.lng) and distance_km(a.lat, a.lng, b.lat, b.lng) > 0.5:
        return False
    sim = SequenceMatcher(None, _name(a), _name(b)).ratio()
    pa = _digits(a.phone)
    return sim >= 0.85 or (bool(pa) and pa == _digits(b.phone) and sim >= 0.6)


def merge(*lists: list[Hotel]) -> list[Hotel]:
    """One list, one entry per hotel. A hotel found by several searches keeps its cheapest price
    (and that search's `source`); fields the cheaper entry lacks are filled in from the others."""
    out: list[Hotel] = []
    for h in (h for lst in lists for h in lst):
        i = next((i for i, o in enumerate(out) if same_hotel(o, h)), None)
        if i is None:
            out.append(h)
            continue
        base, other = (h, out[i]) if h.price < out[i].price else (out[i], h)
        gaps = {f: getattr(other, f) for f in Hotel.model_fields
                if getattr(base, f) is None and getattr(other, f) is not None}
        out[i] = base.model_copy(update=gaps)
    return out


# ---------------------------------------------------------------- 2. initial rank
def score(h: Hotel, w: RankWeights) -> float:
    """price x (1 + distance penalty) x (1 + review penalty). Lower is better."""
    dist = 1 if h.distance_km is None else 1 + w.distance * min(h.distance_km / w.distance_ref_km, 1.0)
    trusted = h.review_rating is not None and (h.review_count or 0) >= w.min_reviews
    rev = 1 + w.review * max(0.0, w.review_target - h.review_rating) if trusted else 1.0
    return h.price * dist * rev


def rank(hotels: list[Hotel], weights: RankWeights | None = None) -> list[Hotel]:
    """Return copies with `score` set, best (lowest) first. Does not mutate the input."""
    w = weights or RankWeights()
    scored = [h.model_copy(update={"score": round(score(h, w), 2)}) for h in hotels]
    return sorted(scored, key=lambda h: h.score)


# ---------------------------------------------------------------- 3. final rank, after the calls
def final_rank(hotels: list[Hotel]) -> list[Hotel]:
    """Only hotels whose call confirmed a room (available is True; False and None are dropped),
    cheapest first by the price quoted on the call, else the listed price."""
    ok = [h for h in hotels if h.call_query and h.call_query.available is True]
    return sorted(ok, key=lambda h: h.call_query.price if h.call_query.price is not None else h.price)


def cheapest(hotels: list[Hotel]) -> Hotel | None:
    best = final_rank(hotels)
    return best[0] if best else None
