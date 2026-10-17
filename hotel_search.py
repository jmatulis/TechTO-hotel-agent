"""
Cheapest hotels in a city: SerpApi Google Hotels for live rates, phone numbers from the hotel's
details page (or Google Places / Google Maps as fallbacks).

  python hotel_search.py Toronto 2026-10-18
  python hotel_search.py Toronto 2026-10-18 --nights 2 --adults 2 --top 10 --phones
  python hotel_search.py "New York" 2026-11-02 --json
  python hotel_search.py Toronto 2026-10-18 --near "CN Tower" --radius 2
  python hotel_search.py Toronto 2026-10-18 --near 43.6426,-79.3871 --radius 1.5 --pages 2

Cost: ONE google_hotels search for the whole city; --phones adds one property-details search
per hotel shown (top N only, in parallel). Radius search: Google Hotels has no lat/lng/radius
parameter, so we query "hotels near <place>" and filter on each property's gps_coordinates.
A place name costs one google_maps search to geocode; "lat,lng" is free. --pages N fetches
more result pages (~20 hotels each, 1 credit each) so a tight radius still has enough rows.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
from datetime import date, timedelta

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass


def E(key: str, default: str = "") -> str:
    return os.environ.get(key) or default


PLACES_KEY = E("GOOGLE_PLACES_KEY")
SERPAPI_KEY = E("SERPAPI_KEY")
GL = E("SERP_GL", "ca")
CURRENCY = E("CURRENCY", "CAD")
PLACES_FIELDS = "places.displayName,places.formattedAddress,places.internationalPhoneNumber"

HTTP: httpx.AsyncClient = None  # type: ignore
COUNT = {"places": 0, "serpapi": 0}                    # billed requests, for the cost report


async def post(url: str, headers: dict, body: dict) -> dict:
    """Google Places POST; tests swap it out."""
    COUNT["places"] += 1
    r = await HTTP.post(url, headers=headers, json=body)
    r.raise_for_status()
    return r.json()


async def serp(params: dict) -> dict:
    """SerpApi GET; tests swap it out."""
    COUNT["serpapi"] += 1
    r = await HTTP.get("https://serpapi.com/search.json", params={**params, "api_key": SERPAPI_KEY})
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------- SerpApi Google Hotels
def _serp_params(city: str, checkin: str, checkout: str, adults: int) -> dict:
    return {"engine": "google_hotels", "q": f"hotels in {city}", "check_in_date": checkin,
            "check_out_date": checkout, "adults": adults, "currency": CURRENCY, "gl": GL, "hl": "en"}


def serp_cheapest(props: list, nights: int) -> list[dict]:
    """One row per hotel, cheapest total first. Google Hotels lists no room names.
    sort_by=3 is only roughly ascending, so we sort again."""
    rows = []
    for p in props:
        rate, tot = p.get("rate_per_night") or {}, p.get("total_rate") or {}
        night = rate.get("extracted_lowest")
        if not night:
            continue
        total = tot.get("extracted_lowest") or night * nights
        pre_tax = tot.get("extracted_before_taxes_fees") or (
            rate.get("extracted_before_taxes_fees") or 0) * nights or None
        rows.append({"hotel_id": p.get("property_token"), "name": p.get("name") or "?",
                     "address": None, "rating": p.get("overall_rating"),
                     "stars": p.get("extracted_hotel_class"), "total": float(total), "pre_tax": pre_tax,
                     "per_night": float(night), "phone": None,
                     "lat": (p.get("gps_coordinates") or {}).get("latitude"),
                     "lng": (p.get("gps_coordinates") or {}).get("longitude")})
    return sorted(rows, key=lambda r: r["total"])


# ---------------------------------------------------------------- radius
LATLNG_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(a))


async def geocode(near: str, city: str) -> tuple[float, float]:
    """'43.64,-79.38' -> free. A place name -> one SerpApi google_maps search."""
    if m := LATLNG_RE.match(near):
        return float(m.group(1)), float(m.group(2))
    q = near if near.strip().lower() == city.strip().lower() else f"{near}, {city}"   # city centre
    # gl matters: without it "Whitby" geocodes to England while google_hotels (gl=ca) finds Ontario
    j = await serp({"engine": "google_maps", "type": "search", "q": q, "hl": "en", "gl": GL})
    g = ((j.get("place_results") or (j.get("local_results") or [{}])[0]).get("gps_coordinates") or {})
    if g.get("latitude") is None:
        raise SystemExit(f"could not locate '{near}'")
    return float(g["latitude"]), float(g["longitude"])


def sane_center(center: tuple[float, float], rows: list[dict], max_km: float = 100.0) -> tuple[float, float]:
    """If the geocoded centre is nowhere near the hotels Google returned, the geocode picked a
    namesake town (another country/province). Trust the hotels: use their median point."""
    pts = [(r["lat"], r["lng"]) for r in rows if r.get("lat") is not None and r.get("lng") is not None]
    if not pts:
        return center
    mid = (sorted(p[0] for p in pts)[len(pts) // 2], sorted(p[1] for p in pts)[len(pts) // 2])
    if km(*center, *mid) <= max_km:
        return center
    print(f"[radius] centre {center} is {km(*center, *mid):.0f} km from the hotels; "
          f"using their midpoint {mid}", file=sys.stderr)
    return mid


def within(rows: list[dict], center: tuple[float, float], radius_km: float) -> list[dict]:
    """Keep rows inside the radius, tagging each with its distance. No coordinates -> dropped."""
    out = []
    for r in rows:
        if r.get("lat") is None or r.get("lng") is None:
            continue
        r["km"] = round(km(center[0], center[1], r["lat"], r["lng"]), 2)
        if r["km"] <= radius_km:
            out.append(r)
    return out


async def serp_phone(row: dict, base: dict):
    """Property details by token: phone + address. Never raises."""
    try:
        d = await serp({**base, "property_token": row["hotel_id"]})
    except Exception as e:
        print(f"[serp] {row['name']}: {e!r}", file=sys.stderr)
        return
    row["phone"], row["address"] = e164(d.get("phone")), d.get("address")


async def maps_phone(row: dict, city: str):
    """Fallback when the details page has no phone: SerpApi Google Maps by name."""
    try:
        m = await serp({"engine": "google_maps", "type": "search", "q": f"{row['name']}, {city}", "hl": "en"})
    except Exception as e:
        print(f"[maps] {row['name']}: {e!r}", file=sys.stderr)
        return
    place = m.get("place_results") or (m.get("local_results") or [{}])[0]
    row["phone"] = e164(place.get("phone"))
    row["address"] = row.get("address") or place.get("address")


# ---------------------------------------------------------------- Google Places
def e164(raw: str | None) -> str | None:
    """'+1 416-555-0101' / '(416) 555-0101' -> '+14165550101'. Bare 10 digits are taken as NANP."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+"):
        return "+" + digits if len(digits) >= 8 else None
    if len(digits) == 10:
        return "+1" + digits
    return "+" + digits if len(digits) == 11 and digits[0] == "1" else None


async def phone_for(row: dict, city: str):
    """Places Text Search on name + address; first hit's phone and address. Never raises."""
    try:
        resp = await post(
            "https://places.googleapis.com/v1/places:searchText",
            {"X-Goog-Api-Key": PLACES_KEY, "X-Goog-FieldMask": PLACES_FIELDS},
            {"textQuery": f"{row['name']}, {row.get('address') or city}", "pageSize": 1},
        )
    except Exception as e:
        print(f"[places] {row['name']}: {e!r}", file=sys.stderr)
        return
    hit = (resp.get("places") or [{}])[0]
    row["phone"] = e164(hit.get("internationalPhoneNumber"))
    row["address"] = row.get("address") or hit.get("formattedAddress")


# ---------------------------------------------------------------- orchestration
async def search(city: str, checkin: str, nights: int = 1, adults: int = 2, top: int = 10,
                 phones: bool = False, fresh: bool = False, near: str | None = None,
                 radius_km: float | None = None, pages: int = 1) -> list[dict]:
    checkout = (date.fromisoformat(checkin) + timedelta(days=nights)).isoformat()
    base = _serp_params(city, checkin, checkout, adults)
    if fresh:
        base["no_cache"] = "true"                       # SerpApi otherwise reuses results up to 1h
    center = None
    if near:
        center = await geocode(near, city)
        if not LATLNG_RE.match(near) and near.strip().lower() != city.strip().lower():
            # bias Google's results toward the point
            base["q"] = f"hotels near {near}, {city}"
    props, params = [], {**base, "sort_by": "3"}        # cheapest first
    for _ in range(max(1, pages)):
        j = await serp(params)
        props += j.get("properties") or []
        token = (j.get("serpapi_pagination") or {}).get("next_page_token")
        if not token:
            break
        params = {**params, "next_page_token": token}
    rows = serp_cheapest(props, nights)
    if center:
        rows = within(rows, sane_center(center, rows), radius_km or 5.0)
    rows = rows[:top]
    if phones:
        await asyncio.gather(*(serp_phone(r, base) for r in rows if r["hotel_id"]))
    return rows


def table(rows: list[dict]) -> str:
    if not rows:
        return "No matching rates."
    dist = "km" in rows[0]
    out = [f"{'#':>2}  {'hotel':<40} {'total':>9} {'pre-tax':>8} {'/night':>8}"
           + (f" {'km':>6}" if dist else "") + "  phone"]
    for i, r in enumerate(rows, 1):
        pre = f"{r['pre_tax']:.2f}" if r.get("pre_tax") else "-"
        out.append(f"{i:>2}  {r['name'][:40]:<40} {r['total']:>9.2f} {pre:>8} {r['per_night']:>8.2f}"
                   + (f" {r['km']:>6.2f}" if dist else "") + f"  {r['phone'] or '-'}")
    out.append(f"prices in {CURRENCY}; total includes taxes and fees")
    return "\n".join(out)


async def main(argv: list[str] | None = None):
    global HTTP
    p = argparse.ArgumentParser(description="Cheapest hotels in a city, with phone numbers.")
    p.add_argument("city")
    p.add_argument("checkin", help="YYYY-MM-DD")
    p.add_argument("--nights", type=int, default=1)
    p.add_argument("--adults", type=int, default=2)
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--phones", action="store_true",
                   help="look up phones via property details (1 SerpApi credit per hotel shown)")
    p.add_argument("--fresh", action="store_true",
                   help="bypass SerpApi's ~1h result cache (always costs a credit)")
    p.add_argument("--near", help="centre of a radius search: a place ('CN Tower') or 'lat,lng'")
    p.add_argument("--radius", type=float, default=5.0, help="km around --near (default %(default)s)")
    p.add_argument("--pages", type=int, default=1,
                   help="result pages to fetch, ~20 hotels and 1 credit each (default %(default)s)")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    a = p.parse_args(argv)
    if not SERPAPI_KEY:
        raise SystemExit("set SERPAPI_KEY in .env")

    HTTP = httpx.AsyncClient(http2=True, timeout=15)
    try:
        rows = await search(a.city, a.checkin, a.nights, a.adults, a.top, a.phones, a.fresh,
                            a.near, a.radius if a.near else None, a.pages)
    finally:
        await HTTP.aclose()
    print(json.dumps(rows, indent=2) if a.json else table(rows))


if __name__ == "__main__":
    asyncio.run(main())
