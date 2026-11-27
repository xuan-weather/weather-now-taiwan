#!/usr/bin/env python3
"""Fetch nationwide official live road sections from the MOTC TDX API."""
import json, os, re, sys, time
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
MIN_REQUEST_INTERVAL = 4.0
_last_request_at = 0.0
CITIES = [
    ("Taipei","臺北市","taipei"),("NewTaipei","新北市","newtaipei"),
    ("Taoyuan","桃園市","taoyuan"),("Taichung","臺中市","taichung"),
    ("Tainan","臺南市","tainan"),("Kaohsiung","高雄市","kaohsiung"),
    ("Keelung","基隆市","keelung"),("Hsinchu","新竹市","hsinchu_city"),
    ("HsinchuCounty","新竹縣","hsinchu_county"),("MiaoliCounty","苗栗縣","miaoli"),
    ("ChanghuaCounty","彰化縣","changhua"),("NantouCounty","南投縣","nantou"),
    ("YunlinCounty","雲林縣","yunlin"),("ChiayiCounty","嘉義縣","chiayi_county"),
    ("Chiayi","嘉義市","chiayi_city"),("PingtungCounty","屏東縣","pingtung"),
    ("YilanCounty","宜蘭縣","yilan"),("HualienCounty","花蓮縣","hualien"),
    ("TaitungCounty","臺東縣","taitung"),("PenghuCounty","澎湖縣","penghu"),
    ("KinmenCounty","金門縣","kinmen"),("LienchiangCounty","連江縣","lienchiang")
]

def get_json(url, headers=None, body=None):
    global _last_request_at
    req = Request(url, headers=headers or {}, data=body, method="POST" if body is not None else "GET")
    for attempt in range(6):
        # TDX 依 API Key 方案限制呼叫頻率；所有端點共用節流，避免 22 縣市連續打爆。
        elapsed = time.monotonic() - _last_request_at
        if elapsed < MIN_REQUEST_INTERVAL:
            time.sleep(MIN_REQUEST_INTERVAL - elapsed)
        try:
            with urlopen(req, timeout=60) as response:
                _last_request_at = time.monotonic()
                return json.loads(response.read().decode("utf-8-sig"))
        except HTTPError as exc:
            _last_request_at = time.monotonic()
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
                    wait = min(120, 20 * (2 ** attempt))
                print(f"[RATE LIMIT] TDX HTTP 429; cooldown {wait}s before retry {attempt + 1}/5", file=sys.stderr)
                time.sleep(wait)
                continue
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"HTTP {exc.code} for {url}: {detail}") from exc

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
            "Accept-Encoding":"gzip"
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
    sources=[(f"Road/Traffic/Live/City/{city}",f"Road/Traffic/SectionShape/City/{city}",f"Road/Traffic/Section/City/{city}",label,region,"縣市") for city,label,region in CITIES]
    sources += [
        ("Road/Traffic/Live/Highway","Road/Traffic/SectionShape/Highway","Road/Traffic/Section/Highway","全台省道","highway","省道"),
        ("Road/Traffic/Live/Freeway","Road/Traffic/SectionShape/Freeway","Road/Traffic/Section/Freeway","全台國道","freeway","國道")
    ]
    for live_path,shape_path,section_path,label,region,kind in sources:
        try:
            live=rows(live_path,bearer)
            time.sleep(0.6)
            shapes=rows(shape_path,bearer)
            time.sleep(0.6)
            sections=rows(section_path,bearer)
            segments=build_segments(live,shapes,region,label,f"TDX {kind}即時路況",sections)
            all_segments.extend(segments)
            stats.append({"name":label,"liveRecords":len(live),"shapeRecords":len(shapes),"sectionRecords":len(sections),"mappedSegments":len(segments),"ok":True})
            print(f"[OK] {label}: live={len(live)} shape={len(shapes)} sections={len(sections)} mapped={len(segments)}")
        except Exception as exc:
            stats.append({"name":label,"ok":False,"error":str(exc)[:240]})
            print(f"[WARN] {label}: {exc}",file=sys.stderr)
        time.sleep(1.2)
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
