"""Offline tests for ranker.py. Run: python tests/test_ranker.py"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root, for agent.py etc.
import asyncio

import hotel_search as hs
from ranker import cheapest, final_rank, merge, rank
from schemas import CallQuery, Hotel, RankWeights


def hotel(name, price, dist=10.0, rating=None, reviews=None):
    return Hotel(id=name, name=name, lat=0, lng=0, distance_km=dist, price=price, currency="CAD",
                 review_rating=rating, review_count=reviews)


def test_merge_and_final_rank():
    """merge(): one entry per hotel, cheapest price, gaps filled. final_rank(): confirmed rooms only."""
    def h(name, price, source, lat=43.65, phone=None, **kw):
        return Hotel(source=source, id=f"{source}-{name}", name=name, lat=lat, lng=-79.38, distance_km=1,
                     price=price, currency="CAD", phone=phone, **kw)

    serp = [h("The Skye Hotel & Residences", 120, "serpapi"), h("Alpha Inn", 90, "serpapi")]
    lite = [h("Skye Hotel and Residences", 108, "liteapi", phone="+14165551111", review_rating=4.0, review_count=50),
            h("Alpha Inn", 95, "liteapi", lat=44.5),              # same name, 100 km away: a different hotel
            h("Beta Suites", 150, "liteapi")]
    m = merge(serp, lite)
    assert sorted(x.name for x in m) == ["Alpha Inn", "Alpha Inn", "Beta Suites", "Skye Hotel and Residences"], m
    skye = next(x for x in m if "Skye" in x.name)
    assert (skye.price, skye.source, skye.phone, skye.review_count) == (108, "liteapi", "+14165551111", 50), skye
    # same phone but clearly different names (Places mismatch) is NOT merged
    a, b = h("NU Hotel Toronto Airport", 106, "liteapi", phone="+19056777331"), h("Quality Inn", 117, "liteapi", phone="+19056777331")
    assert len(merge([a], [b])) == 2

    # final rank: available False / None are ignored; cheapest quote wins; no quote falls back to listing
    def called(name, listed, available, quote=None):
        x = h(name, listed, "serpapi")
        x.call_query = CallQuery(available=available, price=quote)
        return x
    pool = [called("full", 80, False), called("unclear", 70, None), called("quoted-high", 90, True, 300),
            called("quoted-low", 200, True, 180), h("never-called", 60, "serpapi"), called("no-quote", 170, True)]
    assert [x.name for x in final_rank(pool)] == ["no-quote", "quoted-low", "quoted-high"], final_rank(pool)
    assert cheapest(pool).name == "no-quote" and cheapest([called("full", 80, False)]) is None
    print("merge + final_rank ok")


def main():
    # price is the main term: a clearly cheaper hotel wins despite being farther / worse rated
    r = rank([hotel("pricey", 200, 1, 4.8, 900), hotel("cheap", 100, 60, 3.9, 900)])
    assert [h.name for h in r] == ["cheap", "pricey"], r

    # same price: closer wins, then better reviews win
    assert rank([hotel("far", 100, 80), hotel("near", 100, 5)])[0].name == "near"
    assert rank([hotel("bad", 100, 10, 3.0, 500), hotel("good", 100, 10, 4.5, 500)])[0].name == "good"

    # a rating backed by almost no reviews is ignored, not punished
    few = rank([hotel("few", 100, 10, 1.0, 2), hotel("none", 101, 10)])
    assert few[0].name == "few" and few[0].score == 101.5, few

    # weights switch criteria off; input is not mutated; score is set
    src = [hotel("a", 100, 100), hotel("b", 99, 1)]
    assert rank(src, RankWeights(distance=0))[0].name == "b" and src[0].score is None
    assert all(h.score for h in rank(src))
    print("ranker tests ok")


async def serp_to_hotels():
    """hotel_search.find_hotels returns Hotel objects (SerpApi faked)."""
    def prop(name, price, lat, lng, token):
        return {"name": name, "rate_per_night": {"extracted_lowest": price}, "overall_rating": 4.1,
                "reviews": 812, "extracted_hotel_class": 3, "property_token": token,
                "gps_coordinates": {"latitude": lat, "longitude": lng}}

    async def fake_serp(params):
        if params["engine"] == "google_maps":
            return {"place_results": {"gps_coordinates": {"latitude": 43.65, "longitude": -79.38}}}
        if "property_token" in params:
            return {"phone": "(416) 555-0101", "address": "1 Test St"}
        return {"properties": [prop("Near", 120, 43.66, -79.38, "t1"), prop("Cheap", 90, 43.70, -79.38, "t2"),
                               prop("Too Far", 50, 44.65, -79.38, "t3")]}   # ~111 km north

    saved, hs.serp, hs.HTTP = hs.serp, fake_serp, None
    try:
        got = await hs.find_hotels(city="Toronto", check_in="2026-10-18", limit=5)
    finally:
        hs.serp = saved
    assert hs.HTTP is None                                    # own client is closed and cleared
    assert [h.name for h in got] == ["Cheap", "Near"], got    # cheapest first, 100 km radius applied
    h = got[0]
    assert (h.source, h.id, h.price, h.currency) == ("serpapi", "t2", 90.0, hs.CURRENCY)
    assert (h.phone, h.address, h.review_rating, h.review_count, h.stars) == ("+14165550101", "1 Test St", 4.1, 812, 3)
    assert 5 < h.distance_km < 7 and h.rating is None
    assert rank(got)[0].name == "Cheap"                       # works with the ranker as-is
    print("serp -> Hotel ok")


main()
test_merge_and_final_rank()
asyncio.run(serp_to_hotels())
