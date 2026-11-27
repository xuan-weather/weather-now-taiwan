#!/usr/bin/env python3
"""Fetch nationwide official live road sections from the MOTC TDX API."""
import json, os, re, sys, time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

CLIENT_ID = os.environ.get("TDX_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("TDX_CLIENT_SECRET", "").strip()
if not CLIENT_ID or not CLIENT_SECRET:
    raise SystemExit("Missing TDX_CLIENT_ID / TDX_CLIENT_SECRET GitHub Secrets")

TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
API_ROOT = "https://tdx.transportdata.tw/api/basic/v2"
TW = timezone(timedelta(hours=8))
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
    req = Request(url, headers=headers or {}, data=body, method="POST" if body is not None else "GET")
    with urlopen(req, timeout=50) as response:
        return json.loads(response.read().decode("utf-8-sig"))

def token():
    body = urlencode({"grant_type":"client_credentials","client_id":CLIENT_ID,"client_secret":CLIENT_SECRET}).encode()
    result = get_json(TOKEN_URL, {"Content-Type":"application/x-www-form-urlencoded","Accept":"application/json"}, body)
    if not result.get("access_token"):
        raise RuntimeError("TDX OAuth did not return an access_token")
    return result["access_token"]

def rows(path, bearer):
    result, skip, top = [], 0, 1000
    while True:
        query = urlencode({"$format":"JSON","$top":top,"$skip":skip})
        payload = get_json(f"{API_ROOT}/{path}?{query}", {"Authorization":f"Bearer {bearer}","Accept":"application/json"})
        if isinstance(payload, dict):
            payload = payload.get("value", payload.get("data", payload.get("Data", [])))
        if not isinstance(payload, list):
            raise RuntimeError(f"Unexpected TDX response for {path}")
        result.extend(payload)
        if len(payload) < top:
            return result
        skip += len(payload)
        time.sleep(0.12)

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

def build_segments(live_rows, shape_rows, region, label, source):
    shape_by_id={}
    for item in shape_rows:
        sid=section_id(item)
        coords=geometry_points(item)
        if sid and coords:
            shape_by_id[sid]=coords
    output=[]
    for item in live_rows:
        sid=section_id(item)
        coords=shape_by_id.get(sid)
        speed=speed_value(item)
        if not sid or not coords or speed is None:
            continue
        output.append({
            "id":f"{region}:{sid}",
            "name":first_text(item,("SectionName","RoadName","RoadNameZh","Name"),f"{label} 路段 {sid}"),
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
    sources=[(f"Road/Traffic/Live/City/{city}",f"Road/Traffic/SectionShape/City/{city}",label,region,"縣市") for city,label,region in CITIES]
    sources += [
        ("Road/Traffic/Live/Highway","Road/Traffic/SectionShape/Highway","全台省道","highway","省道"),
        ("Road/Traffic/Live/Freeway","Road/Traffic/SectionShape/Freeway","全台國道","freeway","國道")
    ]
    for live_path,shape_path,label,region,kind in sources:
        try:
            live=rows(live_path,bearer)
            shapes=rows(shape_path,bearer)
            segments=build_segments(live,shapes,region,label,f"TDX {kind}即時路況")
            all_segments.extend(segments)
            stats.append({"name":label,"liveRecords":len(live),"shapeRecords":len(shapes),"mappedSegments":len(segments),"ok":True})
            print(f"[OK] {label}: live={len(live)} shape={len(shapes)} mapped={len(segments)}")
        except Exception as exc:
            stats.append({"name":label,"ok":False,"error":str(exc)[:200]})
            print(f"[WARN] {label}: {exc}",file=sys.stderr)
        time.sleep(0.12)
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
