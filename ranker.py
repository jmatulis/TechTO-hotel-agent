"""Initial ranking of hotels to phone: cheapest first, nudged by distance and Google reviews.

    ranked = rank(hotels)                  # hotels from any search module, merged
    ranked = rank(hotels, RankWeights(distance=0))

Pure arithmetic: no API calls, no tokens. The final ordering, after the calls, uses
`Hotel.call_query.price` and is a separate step.
"""
from schemas import Hotel, RankWeights


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
