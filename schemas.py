"""Data models for hotel search: the query going in and the hotels coming out."""
from datetime import date, timedelta
from typing import Optional

from pydantic import BaseModel, Field, model_validator


def _tomorrow() -> date:
    return date.today() + timedelta(days=1)


class HotelQuery(BaseModel):
    """What to search for. Everything except `city` has a default."""
    city: str
    country: str = "CA"                  # ISO-2; also used as guest nationality
    check_in: date = Field(default_factory=_tomorrow)
    check_out: Optional[date] = None        # defaults to check_in + 1 night
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
    price: Optional[float] = None
    available: Optional[bool] = None        # None until the call has a clear answer
    call_url: Optional[str] = None          # Twilio console page for this call (log, duration, cost)


class Hotel(BaseModel):
    """One priced hotel with its phone number, cheapest first in search results."""
    source: str = "liteapi"              # which search found it (liteapi, serpapi, ...)
    id: str
    name: str
    address: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    distance_km: Optional[float] = None     # from the city centre; None if unknown
    price: float                         # total for the stay, in the query currency
    currency: str
    rating: Optional[float] = None          # LiteAPI scale, 0-10
    stars: Optional[float] = None
    refundable: Optional[bool] = None
    phone: Optional[str] = None             # international format, from Google Places
    places_name: Optional[str] = None       # name Google matched; compare with `name` to spot bad matches
    review_rating: Optional[float] = None   # Google Places rating, 0-5
    review_count: Optional[int] = None      # number of Google reviews
    score: Optional[float] = None           # set by ranker.rank(); lower is better
    call_query: Optional[CallQuery] = None  # set after the phone call


class RankWeights(BaseModel):
    """How much each secondary criterion inflates the price-based score (price stays the main term)."""
    distance: float = 0.15               # +15% score at `distance_ref_km` from the centre
    distance_ref_km: float = 100.0
    review: float = 0.3                  # +30% per Google star below `review_target`
    review_target: float = 4.2
    min_reviews: int = 20                # fewer reviews than this: rating is ignored (neutral)
