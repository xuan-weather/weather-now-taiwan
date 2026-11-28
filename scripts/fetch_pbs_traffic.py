#!/usr/bin/env python3
"""Fetch official PBS live traffic, preferring the official JSON feed and
falling back to the official PBS website when the legacy API is unreachable.
A failed/empty response never replaces the last known-good snapshot.
"""
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import re
import zipfile
import io
import xml.etree.ElementTree as ET
from math import radians, sin, cos, sqrt, atan2

MILEPOST_URL = "https://www.freeway.gov.tw/UserFiles/file/%E5%9C%8B%E9%81%93%E7%99%BE%E5%85%AC%E5%B0%BA%E9%87%8C%E7%A8%8B%E6%A8%81KML/%E6%95%B4%E6%A8%81%E5%8F%8A%E7%99%BE%E5%85%AC%E5%B0%BA%E9%87%8C%E7%A8%8B%E6%A8%81KML%28schema%E5%8A%A0%E5%85%A5%29.zip"
MILEPOST_CACHE = Path("data/highway-mileposts.json")
MILEPOST_CACHE_TTL = 30 * 24 * 60 * 60
CHINESE_DIGITS = {"一":"1","二":"2","三":"3","四":"4","五":"5","六":"6","七":"7","八":"8","九":"9","十":"10"}


def normalize_road_text(value):
    value = str(value or "").translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    for chinese, number in CHINESE_DIGITS.items():
        value = re.sub(r"國道\s*" + chinese + r"(?:號)?", "國道" + number + "號", value)
    return value


def parse_milepost_context(text, require_direction=False):
    """Parse route/km even when the official KML omits direction metadata."""
    text = normalize_road_text(text)
    road_match = re.search(r"(?:國道|國)\s*(\d{1,2})\s*(?:號)?", text)
    # Support decimal km (337.8km), K+metres (337K+800), and compact KML names.
    km_match = re.search(r"(\d{1,3})\s*K\s*\+\s*(\d{1,3})", text, re.I)
    if km_match:
        km = int(km_match.group(1)) + int(km_match.group(2)) / 1000
    else:
        km_match = re.search(r"(\d{1,3})(?:\.(\d{1,3}))?\s*(?:KM|K|公里)", text, re.I)
        if not km_match:
            return None
        km = int(km_match.group(1)) + float("0." + (km_match.group(2) or "0"))
    if not road_match:
        return None
    direction = ""
    if re.search(r"南下|南向|南行|南向線|南下線|往南|SOUTHBOUND|\bS\b|\bS-", text, re.I):
        direction = "S"
    elif re.search(r"北上|北向|北行|北向線|北上線|往北|NORTHBOUND|\bN\b|\bN-", text, re.I):
        direction = "N"
    if require_direction and not direction:
        return None
    road_code = road_match.group(1) + ("-high" if re.search(r"高架|汐止楊梅", text) else "")
    return road_code, direction, km

def parse_kml_mileposts(raw_zip):
    archive = zipfile.ZipFile(io.BytesIO(raw_zip))
    kml_name = next((name for name in archive.namelist() if name.lower().endswith(".kml")), None)
    if not kml_name:
        raise ValueError("Official freeway milepost archive contains no KML")
    root = ET.fromstring(archive.read(kml_name))
    points = []

    def local_name(element):
        return element.tag.rsplit("}", 1)[-1].lower()

    def direct_text(node, wanted):
        for element in list(node):
            if local_name(element) == wanted.lower() and element.text:
                return element.text.strip()
        return ""

    def walk(node, inherited=""):
        tag = local_name(node)
        context = inherited
        # Folder/Document names must be read from direct children only.
        # Searching descendants here accidentally picks a child Placemark name
        # and loses the road/direction context inherited by every milepost.
        if tag in ("folder", "document"):
            folder_name = direct_text(node, "name")
            if folder_name:
                context = (context + " " + folder_name).strip()
        if tag == "placemark":
            name = direct_text(node, "name")
            desc = direct_text(node, "description")
            metadata_parts = []
            for element in node.iter():
                lname = local_name(element)
                if lname in ("value", "simpledata") and element.text:
                    metadata_parts.append(" ".join((element.attrib.get("name", ""), element.text.strip())))
            # Include placemark text fields in addition to standard KML name/description fields.
            all_text = " ".join(
                (element.text or "").strip()
                for element in node.iter()
                if element.text and (element.text or "").strip()
            )
            parsed = parse_milepost_context(" ".join((context, name, desc, " ".join(metadata_parts), all_text)))
            coord_text = ""
            for element in node.iter():
                if local_name(element) == "coordinates" and element.text:
                    coord_text = element.text.strip()
                    break
            if parsed and coord_text:
                values = coord_text.split()[0].split(",")
                if len(values) >= 2:
                    try:
                        lng, lat = float(values[0]), float(values[1])
                        if 20 <= lat <= 27 and 118 <= lng <= 123:
                            road, direction, km = parsed
                            points.append({"road": road, "direction": direction, "km": km, "lat": lat, "lng": lng})
                    except (TypeError, ValueError):
                        pass
        for child in list(node):
            walk(child, context)

    walk(root)
    if not points:
        raise ValueError("No direction-specific highway milepost coordinates parsed from official KML; check KML folder metadata")
    unique = {}
    for point in points:
        key = (point["road"], point["direction"], round(point["km"], 3))
        unique[key] = point
    points = list(unique.values())
    print(f"Parsed {len(points)} direction-specific official highway milepost coordinates")
    return points


def load_highway_mileposts():
    try:
        cached = json.loads(MILEPOST_CACHE.read_text(encoding="utf-8"))
        fetched = datetime.fromisoformat(cached.get("fetchedAt", "").replace("Z", "+00:00"))
        if (datetime.now(timezone.utc) - fetched).total_seconds() < MILEPOST_CACHE_TTL and cached.get("points"):
            return cached["points"]
    except (OSError, ValueError, TypeError):
        pass
    response = requests.get(MILEPOST_URL, headers={"User-Agent": "Mozilla/5.0 Xuan-Traffic"}, timeout=(15, 60))
    response.raise_for_status()
    points = parse_kml_mileposts(response.content)
    MILEPOST_CACHE.parent.mkdir(parents=True, exist_ok=True)
    MILEPOST_CACHE.write_text(json.dumps({"fetchedAt": datetime.now(timezone.utc).isoformat(), "source": MILEPOST_URL, "points": points}, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Loaded {len(points)} official freeway milepost coordinates")
    return points


def haversine_km(a, b):
    lat1, lon1, lat2, lon2 = map(radians, (a[0], a[1], b[0], b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 6371 * 2 * atan2(sqrt(h), sqrt(1 - h))


def geocode_highway_incidents(records):
    try:
        mileposts = load_highway_mileposts()
    except Exception as exc:
        print(f"::warning::Official highway milepost lookup unavailable; coordinate-free crash reports remain in the feed: {exc}")
        return 0
    index = {}
    for point in mileposts:
        index.setdefault((point["road"], point["direction"]), []).append(point)
    geocoded = 0
    for row in records:
        if not isinstance(row, dict) or not re.search(r"國道", normalize_road_text(row.get("road") or row.get("areaNm"))):
            continue
        if not re.search(r"事故|車禍", str(row.get("roadtype", "")) + " " + str(row.get("comment", ""))):
            continue
        # Keep original official coordinates when the PBS JSON feed supplies them.
        try:
            lat = float(row.get("y1") or row.get("lat"))
            lng = float(row.get("x1") or row.get("lng"))
            if 20 <= lat <= 27 and 118 <= lng <= 123:
                row["lat"], row["lng"] = lat, lng
                row["coordinateSource"] = "PBS official coordinates"
                row["locationPrecision"] = "official"
                continue
        except (TypeError, ValueError):
            pass
        parsed = parse_milepost_context(row.get("road") or row.get("areaNm"))
        if not parsed:
            parsed = parse_milepost_context(str(row.get("road") or "") + " " + str(row.get("comment") or ""))
        if not parsed:
            continue
        road, direction, km = parsed
        # Prefer a direction-specific point; fall back to route-wide nearest
        # when the official KML does not provide direction metadata.
        candidates = index.get((road, direction), []) if direction else []
        if not candidates:
            candidates = [
                point for (candidate_road, _candidate_direction), values in index.items()
                if candidate_road == road
                for point in values
            ]
        if not candidates:
            continue
        nearest = min(candidates, key=lambda point: abs(point["km"] - km))
        delta = abs(nearest["km"] - km)
        # The KML is spaced at 100 m; reject a match farther than 0.6 km.
        if delta > 0.6:
            continue
        row["lat"], row["lng"] = nearest["lat"], nearest["lng"]
        row["coordinateSource"] = "Freeway Bureau 100m milepost KML"
        row["locationPrecision"] = "milepost-estimate"
        row["matchedMilepostKm"] = nearest["km"]
        geocoded += 1
    print(f"Geocoded {geocoded} coordinate-free active highway crash reports using official 100m milepost points")
    return geocoded

JSON_URL = "https://rtr.pbs.gov.tw/NMP103_PbsWS/resources/roadData/opendata"
PBS_PAGE_URL = "https://www.pbs.gov.tw/cht/index.php?code=list&ids=30"
OUT = Path("data/pbs-traffic.json")


def count_records(payload):
    if isinstance(payload, list):
        return len(payload)
    if not isinstance(payload, dict):
        return 0
    for key in ("records", "data", "Data", "result", "Result", "roadData", "RoadData", "items", "Items"):
        value = payload.get(key)
        if isinstance(value, (list, dict)):
            count = count_records(value)
            if count:
                return count
    if any(key in payload for key in ("UID", "uid", "road", "Road")):
        return 1
    return 0


def fetch_json_feed():
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; Xuan-Traffic/1.3)",
        "Accept": "application/json, text/plain, */*",
        "Connection": "close",
    }
    response = requests.get(JSON_URL, headers=headers, timeout=(8, 18))
    response.raise_for_status()
    payload = response.json()
    count = count_records(payload)
    if count < 1:
        raise ValueError("PBS legacy JSON feed returned an empty/unrecognized payload")
    return payload, count, JSON_URL


def clean_cell(value):
    return " ".join(str(value or "").replace("\u00a0", " ").split()).strip()


def fetch_official_web_page():
    # The PBS official website publishes the same live incident feed as a
    # rendered table. Playwright waits for client-side table rows before reading.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(locale="zh-TW", timezone_id="Asia/Taipei")
        try:
            page.goto(PBS_PAGE_URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_function(
                """() => Array.from(document.querySelectorAll('table tr'))
                  .some(row => row.querySelectorAll('td').length >= 7)""",
                timeout=45000,
            )
            rows = page.locator("table tr").all()
            records = []
            for row in rows:
                cells = [clean_cell(x) for x in row.locator("td").all_inner_texts()]
                if len(cells) < 7:
                    continue
                # Official PBS table columns:
                # number, category, location, description, date, time, source.
                number, category, location, description, date, clock, source = cells[:7]
                if not (location or description or date or clock):
                    continue
                region = "A"
                for label, code in (("北部", "N"), ("中部", "M"), ("南部", "S"), ("東部", "E")):
                    if label in location:
                        region = code
                        break
                records.append({
                    "UID": number,
                    "roadtype": category,
                    "road": location,
                    "areaNm": location,
                    "comment": description,
                    "happendate": date,
                    "happentime": clock,
                    "srcdetail": source,
                    "region": region,
                    "direction": "",
                    "x1": "",
                    "y1": "",
                    "modDttm": "",
                })
            if not records:
                raise ValueError("PBS official website loaded but no traffic rows were found")
            return records, len(records), PBS_PAGE_URL
        finally:
            browser.close()


def fetch_payload():
    errors = []
    try:
        payload, count = fetch_json_feed()
        return payload, count, JSON_URL
    except Exception as exc:
        errors.append(f"legacy JSON: {type(exc).__name__}: {exc}")
        print(f"::warning::Legacy PBS JSON feed unavailable: {errors[-1]}")
    try:
        payload, count, source = fetch_official_web_page()
        print(f"Fetched PBS records from official rendered website: {count}")
        return payload, count, source
    except Exception as exc:
        errors.append(f"official PBS website: {type(exc).__name__}: {exc}")
        raise RuntimeError("All official PBS sources failed: " + " | ".join(errors)) from exc


def atomic_write(content):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix="pbs-traffic-", suffix=".json", dir=str(OUT.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(content, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, OUT)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def main():
    try:
        payload, count, source = fetch_payload()
    except Exception as exc:
        print(f"::warning::PBS sources unavailable; preserving previous snapshot: {exc}")
        if OUT.exists():
            try:
                previous = json.loads(OUT.read_text(encoding="utf-8"))
                print(f"Preserved previous PBS snapshot ({count_records(previous.get('records', [])) if isinstance(previous, dict) else 0} records).")
            except (OSError, ValueError, TypeError):
                print("Existing PBS snapshot could not be parsed; file was left untouched.")
        else:
            print("No previous PBS snapshot exists; waiting for a successful official source response.")
        return

    records = payload if isinstance(payload, list) else next((payload.get(key) for key in ("records", "data", "Data", "result", "Result", "roadData", "RoadData", "items", "Items") if isinstance(payload.get(key), list)), [])
    geocode_highway_incidents(records)
    result = {
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "count": count,
        "records": payload,
        "status": "ok",
    }
    atomic_write(result)
    print(f"Saved PBS traffic snapshot: {count} records from {source}")


if __name__ == "__main__":
    main()
