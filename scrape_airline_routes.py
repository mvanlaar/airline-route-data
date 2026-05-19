#!/usr/bin/python
import sys
import json
import asyncio
from collections import defaultdict
import time

import nodriver as uc
from curl_cffi import requests
import lxml.html
from geopy.distance import geodesic

BASE_URL = "https://www.flightsfrom.com"


async def get_cf_clearance() -> tuple[str, str]:
    """
    Launch a real browser via nodriver, navigate to the target site so
    Cloudflare's JS challenge is solved, then extract:
      - cf_clearance cookie value
      - User-Agent string (must match the one used by the browser)
    """
    print("Launching browser to solve Cloudflare challenge...")
    browser = await uc.start(headless=True)
    try:
        page = await browser.get(BASE_URL)

        # Wait until cf_clearance appears (CF challenge can take a few seconds)
        for _ in range(30):
            cookies = await page.send(uc.cdp.network.get_cookies([BASE_URL]))
            cf = next((c for c in cookies if c.name == "cf_clearance"), None)
            if cf:
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError("cf_clearance cookie never appeared — challenge not solved in time")

        user_agent = await page.evaluate("navigator.userAgent")
        print(f"CF challenge solved. UA: {user_agent}")
        return cf.value, user_agent
    finally:
        browser.stop()


def build_session(cf_clearance: str, user_agent: str) -> requests.Session:
    """
    Return a curl_cffi Session pre-loaded with the cf_clearance cookie and a
    matching User-Agent.  impersonate="chrome" keeps the TLS/HTTP2 fingerprint
    consistent with what the browser presented.
    """
    session = requests.Session(impersonate="chrome")
    session.headers.update({
        "User-Agent": user_agent,
        "Origin": BASE_URL,
        "Referer": BASE_URL + "/",
    })
    session.cookies.set("cf_clearance", cf_clearance, domain="www.flightsfrom.com")
    return session


def refresh_clearance_if_needed(session: requests.Session, response: requests.Response) -> bool:
    """
    Returns True if a 403/503 Cloudflare block was detected, meaning the
    caller should re-solve the challenge and retry.
    """
    if response.status_code in (403, 503):
        body = response.text.lower()
        if "cloudflare" in body or "cf-ray" in response.headers:
            return True
    return False


if __name__ == "__main__":

    # ------------------------------------------------------------------ #
    # 1. Solve the CF challenge once up-front                             #
    # ------------------------------------------------------------------ #
    cf_clearance, user_agent = asyncio.run(get_cf_clearance())
    session = build_session(cf_clearance, user_agent)

    # ------------------------------------------------------------------ #
    # 2. Fetch the airports list                                          #
    # ------------------------------------------------------------------ #
    print("Fetching airports list...")

    response = session.get(
        f"{BASE_URL}/airports",
        headers={"Accept": "application/json"},
    )

    if refresh_clearance_if_needed(session, response):
        print("CF block on airports fetch — re-solving challenge...")
        cf_clearance, user_agent = asyncio.run(get_cf_clearance())
        session = build_session(cf_clearance, user_agent)
        response = session.get(f"{BASE_URL}/airports", headers={"Accept": "application/json"})

    try:
        airports_json = json.loads(response.content)
    except json.JSONDecodeError:
        print("Failed to load airport JSON, page body was: '%s'" % response.content)
        sys.exit(1)

    iatas = [
        airport["IATA"]
        for airport in airports_json["response"]["airports"]
        if airport["country_code"] == "CO"
    ]

    airports: dict = defaultdict(dict)

    # ------------------------------------------------------------------ #
    # 3. Scrape each airport's destinations page                          #
    # ------------------------------------------------------------------ #
    while iatas:
        iata = iatas.pop()
        if iata in airports:
            continue

        print("Fetching #%s: %s" % (len(airports), iata))

        while True:
            try:
                response = session.get(
                    f"{BASE_URL}/{iata}/destinations",
                    headers={"Accept": "text/html"},
                )

                # Re-solve challenge if Cloudflare blocked us mid-scrape
                if refresh_clearance_if_needed(session, response):
                    print("CF block detected on %s — re-solving challenge..." % iata)
                    cf_clearance, user_agent = asyncio.run(get_cf_clearance())
                    session = build_session(cf_clearance, user_agent)
                    continue

                root = lxml.html.document_fromstring(response.content)
                metadata_nodes = root.xpath('//script[contains(., "window.airport")]')
                metadata_tag = metadata_nodes[0].text_content()
                metadata_bits = metadata_tag.split("window.")
                break

            except Exception as e:
                print("! Error fetching %s, sleeping 5 min before retry: %s" % (iata, e))
                time.sleep(60 * 5)

        metadata = {}
        for bit in metadata_bits:
            split = bit.find("=")
            if split != -1:
                metadata[bit[:split].strip()] = json.loads(bit.strip()[split + 2:-1])

        airport_fields = [
            "city_name",
            "continent",
            "country",
            "country_code",
            "display_name",
            "elevation",
            "IATA",
            "ICAO",
            "latitude",
            "longitude",
            "name",
            "timezone",
        ]
        airport = {field.lower(): metadata["airport"][field] for field in airport_fields}
        if airport["elevation"]:
            airport["elevation"] = int(airport["elevation"])

        routes = []
        for route in metadata["routes"]:
            carrier_fields = ["name", "IATA"]
            carriers = []
            for aroute in route["airlineroutes"]:
                is_passenger = (
                    str(aroute["airline"]["is_scheduled_passenger"]) == "1"
                    or str(aroute["airline"]["is_nonscheduled_passenger"]) == "1"
                )
                is_active = str(aroute["airline"]["active"]) == "1"
                if is_active and is_passenger:
                    carriers.append(
                        {field.lower(): aroute["airline"][field] for field in carrier_fields}
                    )

            orig_ll = (airport["latitude"], airport["longitude"])
            dest_ll = (route["airport"]["latitude"], route["airport"]["longitude"])
            distance = int(geodesic(orig_ll, dest_ll).km)

            routes.append({
                "carriers": carriers,
                "km": distance,
                "min": int(route["common_duration"]),
                "iata": route["iata_to"],
            })

            iatas.append(route["iata_to"])

        airport["routes"] = routes
        airports[iata] = airport

        time.sleep(1)

    with open("airline_routes.json", "w") as f:
        f.write(json.dumps(airports, indent=4, sort_keys=True, separators=(",", ": ")))

    print("Done. Written to airline_routes.json")
