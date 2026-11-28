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
