#!/usr/bin/env python3
"""Fetch nationwide official live road sections from the MOTC TDX API."""
import gzip, json, os, re, sys, time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from email.utils import parsedate_to_datetime


CLIENT_ID = os.environ.get("TDX_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("TDX_CLIENT_SECRET", "").strip()
if not CLIENT_ID or not CLIENT_SECRET:
    raise SystemExit("Missing TDX_CLIENT_ID / TDX_CLIENT_SECRET GitHub Secrets")

TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
API_ROOT = "https://tdx.transportdata.tw/api/basic/v2"
TW = timezone(timedelta(hours=8))
MIN_REQUEST_INTERVAL = 15.0
_last_request_at = 0.0

# TDX 各路況端點的 City enum 不相同，依錯誤回應列出的清單分開設定。
LIVE_CITIES = [
    ("YilanCounty","宜蘭縣","yilan"),("MiaoliCounty","苗栗縣","miaoli"),
    ("ChanghuaCounty","彰化縣","changhua"),("YunlinCounty","雲林縣","yunlin"),
    ("PingtungCounty","屏東縣","pingtung"),("Keelung","基隆市","keelung"),
    ("Taipei","臺北市","taipei"),("Taichung","臺中市","taichung"),
    ("Tainan","臺南市","tainan"),("Taoyuan","桃園市","taoyuan")
]
SHAPE_CITIES = [
    ("YilanCounty","宜蘭縣","yilan"),("HsinchuCounty","新竹縣","hsinchu_county"),
    ("MiaoliCounty","苗栗縣","miaoli"),("YunlinCounty","雲林縣","yunlin"),
    ("PingtungCounty","屏東縣","pingtung"),("Keelung","基隆市","keelung"),
    ("Taipei","臺北市","taipei"),("Kaohsiung","高雄市","kaohsiung"),
    ("Taichung","臺中市","taichung"),("Tainan","臺南市","tainan"),
    ("Taoyuan","桃園市","taoyuan")
]
LIVE_BY_CODE = {item[0]: item for item in LIVE_CITIES}
SHAPE_BY_CODE = {item[0]: item for item in SHAPE_CITIES}
CITY_PAIRS = [(LIVE_BY_CODE[code], SHAPE_BY_CODE[code]) for code in LIVE_BY_CODE if code in SHAPE_BY_CODE]


def decode_response(raw, headers=None):
    # urllib 不會自動解壓 gzip；依標頭或 gzip magic bytes 判斷後再解析。
    encoding = (headers.get("Content-Encoding", "") if headers else "").lower()
    if encoding == "gzip" or raw[:2] == b"\\x1f\\x8b":
        raw = gzip.decompress(raw)
    return raw.decode("utf-8-sig", "replace")

def get_json(url, headers=None, body=None):
    global _last_request_at
    request_headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    request_headers.update(headers or {})
    req = Request(url, headers=request_headers, data=body, method="POST" if body is not None else "GET")
    for attempt in range(6):
        # TDX API 共用節流；保守降低請求頻率，避免 429。
        elapsed = time.monotonic() - _last_request_at
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        try:
            with urlopen(req, timeout=60) as response:
                _last_request_at = time.monotonic()
                raw = response.read()
                return json.loads(decode_response(raw, response.headers))
        except HTTPError as exc:
            _last_request_at = time.monotonic()
            raw_error = exc.read()
            try:
                detail = decode_response(raw_error, exc.headers)[:800]
            except Exception:
                detail = raw_error[:300].decode("utf-8", "replace")
            if exc.code == 429 and attempt < 5:
                retry_after = exc.headers.get("Retry-After", "")
                wait = None
                if retry_after:
                    try:
                        wait = max(1, int(retry_after))
                    except ValueError:
                        try:
                            wait = max(1, int((parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()))
                        except Exception:
                            wait = None
                if wait is None:
                    wait = min(180, 30 * (2 ** attempt))
                print(f"[RATE LIMIT] TDX HTTP 429; cooldown {wait}s before retry {attempt + 1}/5", file=sys.stderr)
                time.sleep(wait)
                continue
            raise RuntimeError(f"HTTP {exc.code} for {url}: {detail}") from exc
        except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"Invalid TDX response for {url}: {type(exc).__name__}: {exc}") from exc

def token():
    body = urlencode({"grant_type":"client_credentials","client_id":CLIENT_ID,"client_secret":CLIENT_SECRET}).encode()
    result = get_json(TOKEN_URL, {"Content-Type":"application/x-www-form-urlencoded","Accept":"application/json"}, body)
    if not result.get("access_token"):
        raise RuntimeError("TDX OAuth did not return an access_token")
    return result["access_token"]

def extract_items(payload):
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    if payload.get("success") is False:
        error = payload.get("error") or {}
        raise RuntimeError(f"TDX returned {error.get('message') or error or 'success=false'}")
    # TDX 路況資料常以 LiveTrafficList/LiveTraffic、SectionShapeList/SectionShapes 包裝。
    preferred = ("LiveTraffic", "SectionShape", "LiveTraffics", "SectionShapes", "value", "data", "Data")
    for key in preferred:
        if key in payload:
            found = extract_items(payload[key])
            if found:
                return found
    for value in payload.values():
        if isinstance(value, (dict, list)):
            found = extract_items(value)
            if found:
                return found
    return []

def rows(path, bearer):
    result, skip, top = [], 0, 1000
    while True:
        query = urlencode({"$format":"JSON","$top":top,"$skip":skip})
        payload = get_json(f"{API_ROOT}/{path}?{query}", {
            "Authorization":f"Bearer {bearer}",
            "Accept":"application/json",
            "Accept-Encoding":"identity"
        })
        batch = extract_items(payload)
        result.extend(batch)
        if len(batch) < top:
            return result
        skip += len(batch)
        time.sleep(0.6)

def section_id(item):
    for key in ("SectionID","SectionId","sectionID","sectionId"):
        if item.get(key) is not None:
            return str(item[key]).strip()
    return ""

def geometry_points(item):
    geom = item.get("Geometry") or item.get("geometry") or item.get("WKT")
    if isinstance(geom, dict):
        if geom.get("type") == "LineString":
            coords = geom.get("coordinates", [])
            return [[float(lat),float(lon)] for lon,lat,*_ in coords if len([lon,lat]) == 2]
        return []
    if not isinstance(geom, str):
        return []
    match = re.search(r"LINESTRING\s*(?:Z\s*)?\((.+)\)", geom, re.I)
    if not match:
        return []
    points=[]
    for pair in match.group(1).split(","):
        values=pair.strip().split()
        if len(values)<2: continue
        try:
            lon,lat=float(values[0]),float(values[1])
            if 118 <= lon <= 123 and 20 <= lat <= 27:
                points.append([lat,lon])
        except ValueError:
            pass
    return points if len(points)>=2 else []

def speed_value(item):
    for key in ("TravelSpeed","AverageSpeed","AvgSpeed","Speed","SectionSpeed","SpeedValue","TrafficSpeed"):
        value=item.get(key)
        try:
            number=float(value)
            if number >= 0: return number
        except (TypeError,ValueError):
            pass
    return None

def first_text(item, keys, fallback):
    for key in keys:
        value=item.get(key)
        if isinstance(value,str) and value.strip():
            return value.strip()
    return fallback

def build_segments(live_rows, shape_rows, region, label, source, section_rows=None):
    shape_by_id={}
    for item in shape_rows:
        sid=section_id(item)
        coords=geometry_points(item)
        if sid and coords:
            shape_by_id[sid]=coords
    name_by_id={}
    for item in (section_rows or []):
        sid=section_id(item)
        if sid:
            name_by_id[sid]=first_text(item,("SectionName","RoadName","RoadNameZh","Name"),f"{label} 路段 {sid}")
    output=[]
    for item in live_rows:
        sid=section_id(item)
        coords=shape_by_id.get(sid)
        speed=speed_value(item)
        if not sid or not coords or speed is None:
            continue
        output.append({
            "id":f"{region}:{sid}",
            "name":first_text(item,("SectionName","RoadName","RoadNameZh","Name"),name_by_id.get(sid,f"{label} 路段 {sid}")),
            "region":region,
            "regionLabel":label,
            "speed":speed,
            "coords":coords,
            "sourceLabel":source,
            "updateTime":first_text(item,("DataCollectTime","SrcUpdateTime","UpdateTime"),"")
        })
    return output

def main():
    bearer=token()
    all_segments=[]
    stats=[]
    # 市區即時車速與線型圖資各自使用支援的 City enum，只處理兩端點的交集。
    for live_city, shape_city in CITY_PAIRS:
        city_code, label, region = live_city
        try:
            live = rows(f"Road/Traffic/Live/City/{city_code}", bearer)
            shapes = rows(f"Road/Traffic/SectionShape/City/{shape_city[0]}", bearer)
            segments = build_segments(live, shapes, region, label, "TDX 縣市即時路況")
            all_segments.extend(segments)
            stats.append({"name":label,"liveRecords":len(live),"shapeRecords":len(shapes),"mappedSegments":len(segments),"ok":True})
            print(f"[OK] {label}: live={len(live)} shape={len(shapes)} mapped={len(segments)}")
        except Exception as exc:
            stats.append({"name":label,"ok":False,"error":str(exc)[:240]})
            print(f"[WARN] {label}: {exc}",file=sys.stderr)

    # 國道、省道使用全國性端點，不帶縣市代碼。
    for category, label, region in (("Highway","全台省道","highway"),("Freeway","全台國道","freeway")):
        try:
            live = rows(f"Road/Traffic/Live/{category}", bearer)
            shapes = rows(f"Road/Traffic/SectionShape/{category}", bearer)
            segments = build_segments(live, shapes, region, label, f"TDX {label}即時路況")
            all_segments.extend(segments)
            stats.append({"name":label,"liveRecords":len(live),"shapeRecords":len(shapes),"mappedSegments":len(segments),"ok":True})
            print(f"[OK] {label}: live={len(live)} shape={len(shapes)} mapped={len(segments)}")
        except Exception as exc:
            stats.append({"name":label,"ok":False,"error":str(exc)[:240]})
            print(f"[WARN] {label}: {exc}",file=sys.stderr)
    if not all_segments:
        raise SystemExit("No mappable TDX live road sections. Refusing to publish fabricated traffic data.")
    payload={
        "source":"交通部 TDX 路況資訊 v2",
        "generatedAt":datetime.now(TW).isoformat(timespec="seconds"),
        "totalSegments":len(all_segments),
        "segments":all_segments,
        "sources":stats
    }
    os.makedirs("data",exist_ok=True)
    with open("data/tdx-traffic.json","w",encoding="utf-8") as file:
        json.dump(payload,file,ensure_ascii=False,separators=(",",":"))
    print(f"Saved {len(all_segments)} official road sections.")

if __name__ == "__main__":
    main()
