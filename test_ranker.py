"""Offline tests for ranker.py. Run: python test_ranker.py"""
from ranker import rank
from schemas import Hotel, RankWeights


def hotel(name, price, dist=10.0, rating=None, reviews=None):
    return Hotel(id=name, name=name, lat=0, lng=0, distance_km=dist, price=price, currency="CAD",
                 review_rating=rating, review_count=reviews)


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


main()
