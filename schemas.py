"""Data models for hotel search: the query going in and the hotels coming out."""
from datetime import date, timedelta

from pydantic import BaseModel, Field, model_validator


def _tomorrow() -> date:
    return date.today() + timedelta(days=1)


class HotelQuery(BaseModel):
    """What to search for. Everything except `city` has a default."""
    city: str
    country: str = "CA"                  # ISO-2; also used as guest nationality
    check_in: date = Field(default_factory=_tomorrow)
    check_out: date | None = None        # defaults to check_in + 1 night
    adults: int = 2                      # adults per room
    rooms: int = 1
    radius_m: int = 100_000              # search radius around the city centre
    currency: str = "CAD"
    limit: int = 5                       # how many cheapest hotels to return
    pool: int = 300                      # hotels LiteAPI prices before we sort by price
    lite_timeout: int = 2                # seconds LiteAPI waits for live rates (~ latency)

    @model_validator(mode="after")
    def _default_checkout(self):
        self.check_out = self.check_out or self.check_in + timedelta(days=1)
        return self


class CallQuery(BaseModel):
    """Outcome of phoning a hotel. `price` is the rate quoted on the call, used for the final sort."""
    transcript: str = ""
    summary: str = ""                    # one or two sentences
    price: float | None = None
    available: bool | None = None        # None until the call has a clear answer


class Hotel(BaseModel):
    """One priced hotel with its phone number, cheapest first in search results."""
    source: str = "liteapi"              # which search found it (liteapi, serpapi, ...)
    id: str
    name: str
    address: str | None = None
    lat: float
    lng: float
    distance_km: float                   # from the city centre
    price: float                         # total for the stay, in the query currency
    currency: str
    rating: float | None = None          # LiteAPI scale, 0-10
    stars: float | None = None
    refundable: bool | None = None
    phone: str | None = None             # international format, from Google Places
    places_name: str | None = None       # name Google matched; compare with `name` to spot bad matches
    review_rating: float | None = None   # Google Places rating, 0-5
    review_count: int | None = None      # number of Google reviews
    score: float | None = None           # set by ranker.rank(); lower is better
    call_query: CallQuery | None = None  # set after the phone call


class RankWeights(BaseModel):
    """How much each secondary criterion inflates the price-based score (price stays the main term)."""
    distance: float = 0.15               # +15% score at `distance_ref_km` from the centre
    distance_ref_km: float = 100.0
    review: float = 0.3                  # +30% per Google star below `review_target`
    review_target: float = 4.2
    min_reviews: int = 20                # fewer reviews than this: rating is ignored (neutral)
