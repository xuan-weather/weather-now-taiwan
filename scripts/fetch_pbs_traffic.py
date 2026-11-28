#!/usr/bin/env python3
"""Fetch the official Police Broadcasting Service live traffic open data."""
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

URL = "https://rtr.pbs.gov.tw/NMP103_PbsWS/resources/roadData/opendata"
OUT = Path("data/pbs-traffic.json")


def prefer_ipv4():
    """GitHub-hosted runners may have no IPv6 route to this government host."""
    original = socket.getaddrinfo

    def ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        results = original(host, port, family, type, proto, flags)
        ipv4 = [item for item in results if item[0] == socket.AF_INET]
        return ipv4 or results

    socket.getaddrinfo = ipv4_getaddrinfo


def main():
    prefer_ipv4()
    retry = Retry(
        total=5,
        connect=5,
        read=3,
        status=4,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    response = session.get(
        URL,
        timeout=(12, 35),
        headers={"User-Agent": "Xuan-Traffic/1.1", "Accept": "application/json"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, (dict, list)):
        raise ValueError("Unexpected PBS traffic payload type")
    result = {
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "source": URL,
        "count": len(payload) if isinstance(payload, list) else None,
        "records": payload,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(
        json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(
        "Saved PBS traffic snapshot: "
        + str(result["count"] if result["count"] is not None else "JSON object")
    )


if __name__ == "__main__":
    main()
