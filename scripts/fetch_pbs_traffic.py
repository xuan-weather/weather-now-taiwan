#!/usr/bin/env python3
"""Fetch the official Police Broadcasting Service live traffic open data."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

URL = "https://rtr.pbs.gov.tw/NMP103_PbsWS/resources/roadData/opendata"
OUT = Path("data/pbs-traffic.json")


def main():
    response = requests.get(URL, timeout=45, headers={"User-Agent": "Xuan-Traffic/1.0"})
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, (dict, list)):
        raise ValueError("Unexpected PBS traffic payload")
    # Preserve the official response; the frontend handles its documented field names.
    result = {
        "fetchedAt": datetime.now(timezone.utc).isoformat(),
        "source": URL,
        "count": len(payload) if isinstance(payload, list) else None,
        "records": payload,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"Saved PBS traffic snapshot: {result['count'] if result['count'] is not None else 'object'}")


if __name__ == "__main__":
    main()
