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
    radius_m: int = 20_000               # hard limit from the city centre
    currency: str = "CAD"
    limit: int = 5                       # how many cheapest hotels to return
    pool: int = 300                      # hotels LiteAPI prices before we sort by price
    lite_timeout: int = 2                # seconds LiteAPI waits for live rates (~ latency)

    @model_validator(mode="after")
    def _default_checkout(self):
        self.check_out = self.check_out or self.check_in + timedelta(days=1)
        return self


class Hotel(BaseModel):
    """One priced hotel with its phone number, cheapest first in search results."""
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
