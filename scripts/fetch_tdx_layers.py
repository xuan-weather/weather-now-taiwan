#!/usr/bin/env python3
"""Fetch one independent nationwide TDX supplementary map layer."""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import fetch_tdx_traffic as tdx

# The layers run as separate parallel jobs. Keep a modest per-job request pace;
# the shared helper still honors TDX Retry-After responses for HTTP 429.
tdx.MIN_REQUEST_INTERVAL = 1.0
tdx._last_request_at = 0.0

TW = timezone(timedelta(hours=8))
OUT = {
    "incidents": ("data/tdx-incidents.json", "incidents"),
    "cctv": ("data/tdx-cctv.json", "cctv"),
    "parking": ("data/tdx-parking.json", "parking"),
}

# TDX returned this accepted City enum list for its CCTV v2 endpoint.
CCTV_CITIES = [
    ("YilanCounty","宜蘭縣"),("HsinchuCounty","新竹縣"),("ChanghuaCounty","彰化縣"),
    ("NantouCounty","南投縣"),("YunlinCounty","雲林縣"),("PingtungCounty","屏東縣"),
    ("TaitungCounty","臺東縣"),("Keelung","基隆市"),("Hsinchu","新竹市"),
    ("Chiayi","嘉義市"),("Taipei","臺北市"),("Kaohsiung","高雄市"),
    ("NewTaipei","新北市"),("Taichung","臺中市"),("Tainan","臺南市"),
    ("Taoyuan","桃園市"),("KinmenCounty","金門縣")
]
PARKING_CITIES = [
    ("Taipei","臺北市"),("NewTaipei","新北市"),("Keelung","基隆市"),
    ("Taoyuan","桃園市"),("Hsinchu","新竹市"),("HsinchuCounty","新竹縣"),
    ("MiaoliCounty","苗栗縣"),("Taichung","臺中市"),("ChanghuaCounty","彰化縣"),
    ("NantouCounty","南投縣"),("YunlinCounty","雲林縣"),("Chiayi","嘉義市"),
    ("ChiayiCounty","嘉義縣"),("Tainan","臺南市"),("Kaohsiung","高雄市"),
    ("PingtungCounty","屏東縣"),("YilanCounty","宜蘭縣"),("HualienCounty","花蓮縣"),
    ("TaitungCounty","臺東縣"),("PenghuCounty","澎湖縣"),("KinmenCounty","金門縣"),
    ("LienchiangCounty","連江縣")
]

def now_text():
    return datetime.now(TW).isoformat(timespec="seconds")

def write_payload(layer, rows, sources, ok=True, error=None):
    path, key = OUT[layer]
    os.makedirs("data", exist_ok=True)
    payload = {
        "ok": bool(ok),
        "source": "交通部 TDX 官方資料",
        "generatedAt": now_text(),
        key: rows,
        "sources": sources,
    }
    if error:
        payload["error"] = str(error)[:300]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"[{layer.upper()}] wrote {path}: {len(rows)} records; ok={ok}")

def value(item, *keys):
    return tdx.field_value(item, *keys)

def get_rows_v2(path, bearer):
    return tdx.rows(path, bearer)

def get_rows_v1(path, bearer):
    return tdx.rows_v1(path, bearer)

def normalize_point(item):
    point = tdx.coordinates(item)
    if point:
        return point
    for key in ("Position","position","Geometry","geometry"):
        pos = item.get(key)
        if isinstance(pos, dict):
            point = tdx.coordinates({"Position":pos})
            if point:
                return point
    return None

def fetch_cctv(bearer):
    cameras, statuses = {}, []
    endpoints = [
        ("Road/Traffic/CCTV/Highway","省道 CCTV"),
        ("Road/Traffic/CCTV/Freeway","國道 CCTV")
    ] + [(f"Road/Traffic/CCTV/City/{code}", label) for code,label in CCTV_CITIES]
    for path,label in endpoints:
        try:
            records = get_rows_v2(path, bearer)
            added = 0
            for item in records:
                if not isinstance(item, dict):
                    continue
                point = normalize_point(item)
                if not point:
                    continue
                cid = value(item,"CCTVID","CameraID","DeviceID","ID")
                key = str(cid or f"{path}-{point[0]}-{point[1]}")
                cameras[key] = {
                    "id":key,
                    "name":str(value(item,"CCTVName","RoadName","LocationDescription","Description") or label),
                    "direction":str(value(item,"RoadDirection","Direction") or ""),
                    "lat":point[0],"lng":point[1],
                    "streamUrl":value(item,"VideoStreamURL","VideoURL","StreamURL"),
                    "imageUrl":value(item,"VideoImageURL","ImageURL","SnapshotURL"),
                    "source":"TDX","region":label,
                    "updatedAt":value(item,"UpdateTime","SrcUpdateTime")
                }
                added += 1
            statuses.append({"source":label,"ok":True,"count":added})
            print(f"[CCTV] {label}: {len(records)} records, {added} mapped")
        except Exception as exc:
            statuses.append({"source":label,"ok":False,"error":str(exc)[:200]})
            print(f"[CCTV WARN] {label}: {exc}",file=sys.stderr)
    return list(cameras.values()), statuses

def classify_event(item):
    text = " ".join(str(value(item,k) or "") for k in
        ("EventType","EventTypeCode","EventCategory","Description","EventTitle","Title","Comment","Remark")).lower()
    if any(x in text for x in ("施工","roadwork","workzone","維護")):
        return "roadwork"
    if any(x in text for x in ("事故","車禍","accident","collision")):
        return "accident"
    if any(x in text for x in ("故障","breakdown","拋錨")):
        return "breakdown"
    return "incident"

def event_point(item):
    point = tdx.parse_event_point(item)
    if point:
        return point
    return normalize_point(item)

def fetch_incidents(bearer):
    # TDX Road Event v1 uses Traffic/RoadEvent/LiveEvent, not Road/Traffic/LiveEvent.
    events, statuses = {}, []
    for authority,label in (("Freeway","國道道路事件"),("Highway","省道道路事件")):
        path=f"Traffic/RoadEvent/LiveEvent/{authority}"
        try:
            records = get_rows_v1(path,bearer)
            added = 0
            for item in records:
                if not isinstance(item,dict):
                    continue
                point = event_point(item)
                if not point:
                    continue
                location = item.get("Location") or {}
                impact = item.get("Impact") or {}
                road = ""
                if isinstance(location,dict):
                    road = value(location,"Description","RoadName","Road","LocationDescription") or ""
                    if not road:
                        for nested in location.values():
                            if isinstance(nested,dict):
                                road = value(nested,"Road","RoadName","Description") or road
                desc = value(impact,"Description") if isinstance(impact,dict) else None
                title = value(item,"Description","EventTitle","EventType","Title") or "道路事件"
                eid = str(value(item,"EventID","EventId","ID") or f"{authority}-{len(events)}")
                events[eid] = {
                    "id":eid,"type":classify_event(item),"title":str(title),
                    "desc":str(desc or value(item,"Comment","Remark","Description") or ""),
                    "time":str(value(item,"PublishTime","EffectiveTime","UpdateTime") or ""),
                    "lane":str(value(item,"ImpactDescription","Direction","RoadDirection") or ""),
                    "road":str(road),"lat":point[0],"lng":point[1],
                    "source":"TDX RoadEvent","authority":authority
                }
                added += 1
            statuses.append({"source":label,"ok":True,"count":added})
            print(f"[EVENT] {label}: {len(records)} records, {added} mapped")
        except Exception as exc:
            statuses.append({"source":label,"ok":False,"error":str(exc)[:200]})
            print(f"[EVENT WARN] {label}: {exc}",file=sys.stderr)
    return list(events.values()), statuses

def fetch_parking(bearer):
    # TDX exposes the live off-street availability feed directly; do not call
    # the unsupported ParkingLot endpoint (which returned HTTP 404).
    parking, statuses = {}, []
    for city,label in PARKING_CITIES:
        try:
            records = get_rows_v1(f"Parking/OffStreet/ParkingAvailability/City/{city}",bearer)
            mapped = 0
            for item in records:
                if not isinstance(item,dict):
                    continue
                point = normalize_point(item)
                if not point:
                    continue
                pid = str(value(item,"ParkingID","ParkingId","ID") or f"{city}-{point[0]}-{point[1]}")
                available = value(item,"AvailableCar","AvailableSmallCar","AvailableCarCount","AvailableNumber")
                try:
                    available = int(float(available))
                    if available < 0:
                        continue
                except (TypeError,ValueError):
                    continue
                total = value(item,"TotalCar","TotalSpaces","TotalParkingSpaces")
                try:
                    total = int(float(total)) if total is not None else None
                except (TypeError,ValueError):
                    total = None
                parking[pid] = {
                    "id":pid,
                    "name":str(value(item,"ParkingName","ParkingLotName","Name") or f"{label}停車場"),
                    "available":available,"total":total,
                    "rate":str(value(item,"FareDescription","ChargeDescription","ParkingRate") or ""),
                    "lat":point[0],"lng":point[1],"region":label,
                    "updatedAt":value(item,"UpdateTime","SrcUpdateTime","DataCollectTime"),
                    "source":"TDX"
                }
                mapped += 1
            statuses.append({"source":f"Parking {label}","ok":True,"count":mapped})
            print(f"[PARKING] {label}: records={len(records)} mapped={mapped}")
        except Exception as exc:
            statuses.append({"source":f"Parking {label}","ok":False,"error":str(exc)[:200]})
            print(f"[PARKING WARN] {label}: {exc}",file=sys.stderr)
    return list(parking.values()), statuses


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--layer", choices=OUT.keys(), required=True)
    layer = parser.parse_args().layer
    try:
        bearer = tdx.token()
    except Exception as exc:
        write_payload(layer, [], [{"source":"TDX OAuth","ok":False,"error":str(exc)[:200]}], False, exc)
        return
    fetcher = {"incidents":fetch_incidents,"cctv":fetch_cctv,"parking":fetch_parking}[layer]
    try:
        records, statuses = fetcher(bearer)
        successful = sum(1 for x in statuses if x.get("ok"))
        # Keep a partially successful layer useful; mark wholly failed layers as not-ok
        # so the browser retains its previous valid snapshot rather than clearing it.
        write_payload(layer, records, statuses, successful > 0)
    except Exception as exc:
        write_payload(layer, [], [{"source":"TDX","ok":False,"error":str(exc)[:200]}], False, exc)

if __name__ == "__main__":
    main()
