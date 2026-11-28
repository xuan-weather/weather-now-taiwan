#!/usr/bin/env python3
"""Fetch the official Police Broadcasting Service live traffic open data.

A failed/empty upstream response must never replace the last known-good snapshot.
"""
import json
import os
import socket
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URL = "https://rtr.pbs.gov.tw/NMP103_PbsWS/resources/roadData/opendata"
OUT = Path("data/pbs-traffic.json")


def prefer_ipv4():
    """Prefer IPv4 because some GitHub-hosted runner routes to this host fail on IPv6."""
    original = socket.getaddrinfo

    def ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        results = original(host, port, family, type, proto, flags)
        ipv4 = [item for item in results if item[0] == socket.AF_INET]
        return ipv4 or results

    socket.getaddrinfo = ipv4_getaddrinfo


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
    # Some source responses may expose one record as a single object.
    if any(key in payload for key in ("UID", "uid", "road", "Road")):
        return 1
    return 0


def fetch_payload():
    prefer_ipv4()
    retry = Retry(
        total=3,
        connect=3,
        read=2,
        status=3,
        backoff_factor=2,
        backoff_max=20,
        status_forcelist=(408, 429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; Xuan-Traffic/1.2; +https://xuan-weather.github.io/)",
        "Accept": "application/json, text/plain, */*",
        "Connection": "close",
    })

    last_error = None
    for attempt in range(1, 3):
        try:
            response = session.get(URL, timeout=(15, 45))
            response.raise_for_status()
            payload = response.json()
            count = count_records(payload)
            if count < 1:
                raise ValueError("PBS API returned an empty or unrecognized payload; keeping previous snapshot")
            return payload, count
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            print(f"[PBS RETRY] attempt {attempt}/2 failed: {type(exc).__name__}: {exc}")
            if attempt < 2:
                time.sleep(10)
    raise RuntimeError(f"PBS official endpoint unavailable after retries: {last_error}") from last_error


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
    payload, count = fetch_payload()
    result = {
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "source": URL,
        "count": count,
        "records": payload,
        "status": "ok",
    }
    # Write only after a complete, non-empty response has been validated.
    # On network/API failure, the existing last-known-good file is untouched.
    atomic_write(result)
    print(f"Saved PBS traffic snapshot: {count} records")


if __name__ == "__main__":
    main()
