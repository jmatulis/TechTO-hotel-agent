"""Cheapest hotels near a city centre, with phone numbers.

LiteAPI prices a pool of hotels in one call (we sort by price ourselves, because its
`limit` applies to its popularity ranking before sorting). Google Places then supplies
each hotel's phone number and reviews, in parallel.

    hotels = await find_hotels(HotelQuery(city="Toronto"))
    hotels = await find_hotels(city="Toronto", adults=1, radius_m=10_000)
"""
import asyncio
import math
import os

import httpx
from dotenv import load_dotenv

from schemas import Hotel, HotelQuery

load_dotenv()
LITE_URL = "https://api.liteapi.travel/v3.0/hotels/rates"
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"


def _distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle (haversine) distance."""
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lng2 - lng1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(a))


async def _city_center(http: httpx.AsyncClient, q: HotelQuery) -> tuple[float, float]:
    r = await http.post(PLACES_URL, json={"textQuery": q.city, "regionCode": q.country, "pageSize": 1},
                        headers={"X-Goog-Api-Key": os.environ["GOOGLE_PLATFORM_API_KEY"],
                                 "X-Goog-FieldMask": "places.location"})
    r.raise_for_status()
    places = r.json().get("places")
    if not places:
        raise ValueError(f"city not found: {q.city!r}")
    return places[0]["location"]["latitude"], places[0]["location"]["longitude"]


async def _priced_hotels(http: httpx.AsyncClient, q: HotelQuery, lat: float, lng: float) -> list[Hotel]:
    """Cheapest-rate-per-hotel inside the radius, cheapest first, without phones."""
    r = await http.post(LITE_URL, headers={"X-API-Key": os.environ["LITEAPI_API_KEY"]}, json={
        "latitude": lat, "longitude": lng, "radius": q.radius_m,
        "checkin": q.check_in.isoformat(), "checkout": q.check_out.isoformat(),
        "occupancies": [{"adults": q.adults}] * q.rooms,
        "currency": q.currency, "guestNationality": q.country,
        "maxRatesPerHotel": 1, "limit": q.pool, "timeout": q.lite_timeout})
    r.raise_for_status()
    body = r.json()
    info = {h["id"]: h for h in body.get("hotels", [])}
    hotels = []
    for row in body.get("data", []):
        h, room = info.get(row["hotelId"]), (row.get("roomTypes") or [None])[0]
        if not h or not room or h.get("latitude") is None:
            continue
        dist = _distance_km(lat, lng, h["latitude"], h["longitude"])
        if dist * 1000 > q.radius_m:
            continue
        price = room["offerRetailRate"]
        tag = ((room.get("rates") or [{}])[0].get("cancellationPolicies") or {}).get("refundableTag")
        hotels.append(Hotel(
            id=row["hotelId"], name=h["name"], address=h.get("address"),
            lat=h["latitude"], lng=h["longitude"], distance_km=round(dist, 1),
            price=price["amount"], currency=price.get("currency", q.currency),
            rating=h.get("rating"), stars=h.get("stars"),
            refundable=None if tag is None else tag == "RFN"))
    hotels.sort(key=lambda x: x.price)
    return hotels[:q.limit]


async def _add_phone(http: httpx.AsyncClient, hotel: Hotel, city: str) -> None:
    """Fill phone, matched name and Google reviews in place; leave them None if Places finds nothing."""
    try:
        r = await http.post(
            PLACES_URL, json={
                "textQuery": f"{hotel.name}, {hotel.address}, {city}", "pageSize": 1,
                "locationBias": {"circle": {"center": {"latitude": hotel.lat, "longitude": hotel.lng},
                                            "radius": 500.0}}},
            headers={"X-Goog-Api-Key": os.environ["GOOGLE_PLATFORM_API_KEY"],
                     "X-Goog-FieldMask": "places.displayName,places.internationalPhoneNumber,"
                                                "places.rating,places.userRatingCount"})
        r.raise_for_status()
        place = (r.json().get("places") or [{}])[0]
    except httpx.HTTPError:
        return
    hotel.phone = place.get("internationalPhoneNumber")
    hotel.places_name = (place.get("displayName") or {}).get("text")
    hotel.review_rating = place.get("rating")
    hotel.review_count = place.get("userRatingCount")


async def find_hotels(query: HotelQuery | None = None, **fields) -> list[Hotel]:
    """Return the `limit` cheapest hotels within `radius_m` of the city centre, cheapest first.

    Pass a HotelQuery, or its fields as keyword arguments (e.g. `city="Toronto", adults=1`).
    Needs LITEAPI_API_KEY and GOOGLE_PLATFORM_API_KEY in the environment / .env.
    """
    q = query or HotelQuery(**fields)
    async with httpx.AsyncClient(timeout=30) as http:
        lat, lng = await _city_center(http, q)
        hotels = await _priced_hotels(http, q, lat, lng)
        await asyncio.gather(*(_add_phone(http, h, q.city) for h in hotels))
    return hotels
