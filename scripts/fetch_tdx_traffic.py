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
MIN_REQUEST_INTERVAL = 12.0  # 降低 429；線型資料另做 7 天快取，減少重複 API 請求
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
    request_headers = {"Accept": "application/json", "Accept-Encoding": "gzip"}
    request_headers.update(headers or {})
    req = Request(url, headers=request_headers, data=body, method="POST" if body is not None else "GET")
    for attempt in range(6):
        # TDX API 共用節流；一般請求間隔 12 秒；遇到 429 嚴格依 Retry-After／退避策略等待。
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
    preferred = ("LiveTraffic", "LiveEvent", "RoadEvent", "CCTV", "ParkingAvailability", "ParkingLot", "SectionShape", "LiveTraffics", "LiveEvents", "RoadEventList", "LiveEventList", "CCTVList", "ParkingAvailabilityList", "ParkingLotList", "SectionShapes", "value", "data", "Data")
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

def rows_v1(path, bearer):
    result, skip, top = [], 0, 1000
    while True:
        query = urlencode({"$format":"JSON","$top":top,"$skip":skip})
        payload = get_json(f"https://tdx.transportdata.tw/api/basic/v1/{path}?{query}", {
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

def find_feed_metadata(payload):
    metadata = {"updateTime": "", "authorityCode": ""}
    def walk(node):
        if isinstance(node, dict):
            if not metadata["updateTime"]:
                value = node.get("UpdateTime") or node.get("updateTime")
                if isinstance(value, str) and value.strip():
                    metadata["updateTime"] = value.strip()
            if not metadata["authorityCode"]:
                value = node.get("AuthorityCode") or node.get("authorityCode")
                if isinstance(value, str) and value.strip():
                    metadata["authorityCode"] = value.strip()
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    walk(value)
    walk(payload)
    return metadata

def find_feed_update_time(payload):
    return find_feed_metadata(payload)["updateTime"]

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
        feed_metadata = find_feed_metadata(payload)
        feed_time = feed_metadata["updateTime"]
        for item in batch:
            if isinstance(item, dict):
                if feed_time and not any(item.get(key) for key in ("DataCollectTime", "SrcUpdateTime", "UpdateTime", "updateTime")):
                    item["_FeedUpdateTime"] = feed_time
                if feed_metadata["authorityCode"] and not item.get("AuthorityCode"):
                    item["_FeedAuthorityCode"] = feed_metadata["authorityCode"]
        result.extend(batch)
        if len(batch) < top:
            return result
        skip += len(batch)
        time.sleep(0.6)

SHAPE_CACHE_PATH = "data/tdx-shapes-cache.json"
SHAPE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60

def load_shape_cache():
    try:
        with open(SHAPE_CACHE_PATH, "r", encoding="utf-8") as file:
            payload = json.load(file)
        if isinstance(payload, dict) and isinstance(payload.get("shapes"), dict):
            return payload
    except (OSError, json.JSONDecodeError):
        pass
    return {"shapes": {}}

def cached_shape_rows(path, bearer, cache):
    entry = cache["shapes"].get(path)
    now = datetime.now(timezone.utc)
    if isinstance(entry, dict) and isinstance(entry.get("rows"), list):
        try:
            fetched = datetime.fromisoformat(entry.get("fetchedAt", "").replace("Z", "+00:00"))
            age = (now - fetched).total_seconds()
            if 0 <= age < SHAPE_CACHE_TTL_SECONDS:
                print(f"[CACHE] {path}: reuse {len(entry['rows'])} shapes (age {int(age/3600)}h)")
                return entry["rows"]
        except (TypeError, ValueError):
            pass
    try:
        fresh = rows(path, bearer)
        cache["shapes"][path] = {
            "fetchedAt": now.isoformat(timespec="seconds"),
            "rows": fresh
        }
        print(f"[CACHE] {path}: refreshed {len(fresh)} shapes")
        return fresh
    except Exception:
        if isinstance(entry, dict) and isinstance(entry.get("rows"), list) and entry["rows"]:
            print(f"[CACHE WARN] {path}: API failed; fallback to saved geometry", file=sys.stderr)
            return entry["rows"]
        raise

def save_shape_cache(cache):
    os.makedirs("data", exist_ok=True)
    cache["generatedAt"] = datetime.now(TW).isoformat(timespec="seconds")
    with open(SHAPE_CACHE_PATH, "w", encoding="utf-8") as file:
        json.dump(cache, file, ensure_ascii=False, separators=(",", ":"))

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

CONGESTION_CACHE_PATH = "data/tdx-congestion-definitions.json"
CONGESTION_CACHE_TTL_SECONDS = 24 * 60 * 60

def find_congestion_groups(payload):
    found = []
    def walk(node, inherited_authority=""):
        if isinstance(node, dict):
            authority = str(node.get("AuthorityCode") or node.get("authorityCode") or inherited_authority).strip()
            if "CongestionLevelID" in node and ("Levels" in node or "CongestionLevelName" in node):
                group = dict(node)
                if authority:
                    group["_AuthorityCode"] = authority
                found.append(group)
            for value in node.values():
                if isinstance(value, (dict, list)):
                    walk(value, authority)
        elif isinstance(node, list):
            for value in node:
                if isinstance(value, (dict, list)):
                    walk(value, inherited_authority)
    walk(payload)
    return found

def load_congestion_cache():
    try:
        with open(CONGESTION_CACHE_PATH, "r", encoding="utf-8") as file:
            value = json.load(file)
        if isinstance(value, dict) and isinstance(value.get("definitions"), dict):
            return value
    except (OSError, json.JSONDecodeError):
        pass
    return {"definitions": {}}

def get_congestion_definitions(path, bearer, cache):
    entry = cache["definitions"].get(path)
    now = datetime.now(timezone.utc)
    if isinstance(entry, dict) and isinstance(entry.get("groups"), list):
        try:
            fetched = datetime.fromisoformat(entry.get("fetchedAt", "").replace("Z", "+00:00"))
            if 0 <= (now - fetched).total_seconds() < CONGESTION_CACHE_TTL_SECONDS:
                return entry["groups"]
        except (TypeError, ValueError):
            pass
    try:
        payload = get_json(f"{API_ROOT}/{path}?{urlencode({'$format':'JSON'})}", {
            "Authorization":f"Bearer {bearer}",
            "Accept":"application/json",
            "Accept-Encoding":"gzip"
        })
        groups = find_congestion_groups(payload)
        if not groups:
            raise RuntimeError("TDX congestion definition returned no groups")
        cache["definitions"][path] = {
            "fetchedAt": now.isoformat(timespec="seconds"),
            "groups": groups
        }
        print(f"[LEVEL] {path}: {len(groups)} official groups")
        return groups
    except Exception as exc:
        if isinstance(entry, dict) and isinstance(entry.get("groups"), list) and entry["groups"]:
            print(f"[LEVEL WARN] {path}: using cached definitions ({exc})", file=sys.stderr)
            return entry["groups"]
        print(f"[LEVEL WARN] {path}: {exc}", file=sys.stderr)
        return []

def save_congestion_cache(cache):
    os.makedirs("data", exist_ok=True)
    cache["generatedAt"] = datetime.now(TW).isoformat(timespec="seconds")
    with open(CONGESTION_CACHE_PATH, "w", encoding="utf-8") as file:
        json.dump(cache, file, ensure_ascii=False, separators=(",", ":"))

def build_definition_index(groups):
    index = {}
    for group in groups:
        group_id = str(group.get("CongestionLevelID", "")).strip()
        if group_id:
            definition = {
                "authorityCode": group.get("_AuthorityCode", ""),
                "groupId": group_id,
                "groupName": group.get("CongestionLevelName", ""),
                "measureIndex": group.get("MeasureIndex", ""),
                "levels": group.get("Levels", {}).get("Level", []) if isinstance(group.get("Levels"), dict) else group.get("Levels", [])
            }
            authority = str(group.get("_AuthorityCode", "")).strip()
            if authority:
                index[f"{authority}:{group_id}"] = definition
            index[group_id] = definition
    return index

def official_congestion_status(item, definition, speed=None):
    """Use TDX level IDs when present; for Speed-based definitions, apply TDX's published bands."""
    raw_level = item.get("CongestionLevel")
    level_number = None
    try:
        level_number = int(float(raw_level))
    except (TypeError, ValueError):
        pass

    levels = definition.get("levels", []) if isinstance(definition, dict) else []
    level_name = ""
    if level_number is not None:
        for entry in levels:
            try:
                entry_level = int(float(entry.get("Level")))
            except (TypeError, ValueError, AttributeError):
                continue
            if entry_level == level_number:
                level_name = str(entry.get("LevelName") or "").strip()
                break

    # TDX Freeway/Highway congestion definitions explicitly use MeasureIndex=Speed.
    # Some live rows omit CongestionLevel, so resolve the official class from the
    # matching published LowValue/TopValue range instead of leaving every road unclassified.
    if not level_name and isinstance(definition, dict) and str(definition.get("measureIndex", "")).lower() == "speed" and speed is not None:
        try:
            speed_value_number = float(speed)
        except (TypeError, ValueError):
            speed_value_number = None
        if speed_value_number is not None and speed_value_number >= 0:
            for entry in levels:
                try:
                    level = int(float(entry.get("Level")))
                    low = float(entry["LowValue"]) if entry.get("LowValue") is not None else None
                    top = float(entry["TopValue"]) if entry.get("TopValue") is not None else None
                except (TypeError, ValueError, AttributeError, KeyError):
                    continue
                if level <= 0:
                    continue
                if low is not None and speed_value_number < low:
                    continue
                if top is not None and speed_value_number > top:
                    continue
                level_number = level
                level_name = str(entry.get("LevelName") or "").strip()
                break

    candidate = level_name or str(raw_level or "").strip()
    if "封閉" in candidate or level_number == -1:
        status = "道路封閉"
    elif "資料不足" in candidate or level_number == 0:
        status = "資料不足"
    elif candidate in ("順暢", "車多", "壅塞", "嚴重壅塞", "極度壅塞"):
        status = candidate
    elif level_number is not None and level_number > 0:
        status = {1: "順暢", 2: "車多", 3: "車多", 4: "壅塞", 5: "嚴重壅塞", 6: "極度壅塞"}.get(level_number, "官方分級未識別")
    else:
        status = "官方分級未提供"
    return status, level_number, level_name

def build_segments(live_rows, shape_rows, region, label, source, section_rows=None, level_index=None):
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
        definition = (level_index or {}).get(
            f"{first_text(item,('AuthorityCode','_FeedAuthorityCode'),'')}:{str(item.get('CongestionLevelID','')).strip()}"
        ) or (level_index or {}).get(str(item.get("CongestionLevelID", "")).strip())
        status, level_number, level_name = official_congestion_status(item, definition, speed)
        output.append({
            "id":f"{region}:{sid}",
            "sectionId":sid,
            "name":first_text(item,("SectionName","RoadName","RoadNameZh","Name"),name_by_id.get(sid,f"{label} 路段 {sid}")),
            "region":region,
            "regionLabel":label,
            "speed":speed,
            "travelTime":item.get("TravelTime"),
            "congestionLevel":item.get("CongestionLevel"),
            "congestionLevelId":item.get("CongestionLevelID"),
            "congestionLevelNumber":level_number,
            "congestionLevelName":level_name,
            "congestionStatus":status,
            "authorityCode":first_text(item,("AuthorityCode","_FeedAuthorityCode"),""),
            "congestionDefinition":definition,
            "coords":coords,
            "sourceLabel":source,
            "updateTime":first_text(item,("DataCollectTime","SrcUpdateTime","UpdateTime","_FeedUpdateTime"),"")
        })
    return output


# Supplementary nationwide layers: road events, CCTV inventory, and parking availability.
SUPPLEMENTAL_PATH = "data/tdx-layers.json"
SUPPLEMENTAL_CACHE_PATH = "data/tdx-layers-cache.json"
SUPPLEMENTAL_CACHE_TTL = 7 * 24 * 60 * 60
SUPPLEMENTAL_CITIES = [
    ("Taipei","臺北市"),("NewTaipei","新北市"),("Keelung","基隆市"),
    ("Taoyuan","桃園市"),("Hsinchu","新竹市"),("HsinchuCounty","新竹縣"),
    ("MiaoliCounty","苗栗縣"),("Taichung","臺中市"),("ChanghuaCounty","彰化縣"),
    ("NantouCounty","南投縣"),("YunlinCounty","雲林縣"),("Chiayi","嘉義市"),
    ("ChiayiCounty","嘉義縣"),("Tainan","臺南市"),("Kaohsiung","高雄市"),
    ("PingtungCounty","屏東縣"),("YilanCounty","宜蘭縣"),("HualienCounty","花蓮縣"),
    ("TaitungCounty","臺東縣"),("PenghuCounty","澎湖縣"),("KinmenCounty","金門縣"),
    ("LienchiangCounty","連江縣")
]

def load_supplemental_cache():
    try:
        with open(SUPPLEMENTAL_CACHE_PATH,"r",encoding="utf-8") as f:
            data=json.load(f)
        return data if isinstance(data,dict) else {}
    except (OSError,json.JSONDecodeError):
        return {}

def save_supplemental_cache(cache):
    os.makedirs("data",exist_ok=True)
    with open(SUPPLEMENTAL_CACHE_PATH,"w",encoding="utf-8") as f:
        json.dump(cache,f,ensure_ascii=False,separators=(",",":"))

def cached_static_rows(path,bearer,cache,fetcher=rows):
    entry=cache.get(path)
    now=datetime.now(timezone.utc)
    if isinstance(entry,dict) and isinstance(entry.get("rows"),list):
        try:
            fetched=datetime.fromisoformat(entry.get("fetchedAt","").replace("Z","+00:00"))
            if 0 <= (now-fetched).total_seconds() < SUPPLEMENTAL_CACHE_TTL:
                return entry["rows"]
        except (TypeError,ValueError):
            pass
    try:
        fresh=fetcher(path,bearer)
        cache[path]={"fetchedAt":now.isoformat(timespec="seconds"),"rows":fresh}
        print(f"[LAYER CACHE] {path}: refreshed {len(fresh)}")
        return fresh
    except Exception as exc:
        if isinstance(entry,dict) and isinstance(entry.get("rows"),list):
            print(f"[LAYER CACHE WARN] {path}: reuse saved data ({exc})",file=sys.stderr)
            return entry["rows"]
        raise

def field_value(item,*keys):
    for key in keys:
        value=item.get(key)
        if value is not None and str(value).strip() not in ("","None"):
            return value
    return None

def coordinates(item):
    pos=item.get("Position") or item.get("position") or {}
    lat=field_value(item,"PositionLat","Latitude","lat","Lat")
    lon=field_value(item,"PositionLon","Longitude","lon","Lon","Lng")
    if lat is None and isinstance(pos,dict):
        lat=field_value(pos,"PositionLat","Latitude","lat","Lat")
        lon=field_value(pos,"PositionLon","Longitude","lon","Lon","Lng")
    try:
        lat=float(lat); lon=float(lon)
        if 20 <= lat <= 27 and 118 <= lon <= 123:
            return lat,lon
    except (TypeError,ValueError):
        pass
    return None

def parse_event_point(item):
    raw=field_value(item,"Positions","Position","Geometry","WKT")
    if isinstance(raw,dict):
        return coordinates({"Position":raw})
    if isinstance(raw,str):
        match=re.search(r"POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)",raw,re.I)
        if match:
            lon,lat=float(match.group(1)),float(match.group(2))
            if 20 <= lat <= 27 and 118 <= lon <= 123:
                return lat,lon
    return coordinates(item)

def fetch_event_records(bearer,authority):
    url=f"https://tdx.transportdata.tw/api/basic/v1/Traffic/RoadEvent/LiveEvent/{authority}"
    query=urlencode({"$format":"JSON","$top":1000})
    payload=get_json(f"{url}?{query}",{"Authorization":f"Bearer {bearer}","Accept":"application/json","Accept-Encoding":"gzip"})
    items=extract_items(payload)
    output=[]
    for item in items:
        if not isinstance(item,dict):
            continue
        point=parse_event_point(item)
        if not point:
            continue
        location=item.get("Location") or {}
        impact=item.get("Impact") or {}
        loc_text=""
        if isinstance(location,dict):
            loc_text=field_value(location,"Description","RoadName","Road","LocationDescription") or ""
            if not loc_text:
                for v in location.values():
                    if isinstance(v,dict):
                        loc_text=field_value(v,"Road","RoadName","Description") or loc_text
        description=field_value(impact,"Description") if isinstance(impact,dict) else None
        title=field_value(item,"Description","EventTitle","EventType","Title") or "道路事件"
        event_code=str(field_value(item,"EventID","EventId","EventTypeCode","EventType") or "")
        searchable=(str(title)+" "+str(description or "")+" "+event_code+" "+str(field_value(item,"Comment","Remark") or "")).lower()
        if any(word in searchable for word in ("施工","roadwork","workzone","維護工程","道路工程")) or event_code.startswith("A150402"):
            event_type="roadwork"
        elif any(word in searchable for word in ("機動開放路肩","開放路肩","特殊管制","交通管制","匝道儀控","封閉道路","管制通行")) or event_code.startswith("A150401"):
            event_type="traffic_control"
        elif any(word in searchable for word in ("車禍","追撞","擦撞","自撞","翻覆","碰撞","事故現場","事故發生","發生事故","事故處理","accident","collision")):
            event_type="accident"
        elif any(word in searchable for word in ("故障車","車輛故障","breakdown","拋錨")):
            event_type="breakdown"
        elif any(word in searchable for word in ("散落物","掉落物","障礙物","異物","輪胎","鐵條")) or event_code.startswith("A150403"):
            event_type="obstruction"
        elif any(word in searchable for word in ("壅塞","阻塞","車多","車流")):
            event_type="congestion"
        else:
            event_type="other"
        output.append({
            "id":str(field_value(item,"EventID","EventId","ID") or f"{authority}-{len(output)}"),
            "type":event_type,
            "title":str(title),
            "desc":str(description or field_value(item,"Comment","Remark","Description") or ""),
            "time":str(field_value(item,"PublishTime","EffectiveTime","UpdateTime") or ""),
            "lane":str(field_value(item,"ImpactDescription","Direction","RoadDirection") or ""),
            "road":str(loc_text),
            "lat":point[0],"lng":point[1],"authority":authority,
            "source":"TDX RoadEvent"
        })
    return output

def fetch_supplemental_layers(bearer):
    """Fetch official national/provincial CCTV and road events; cache static CCTV for 7 days."""
    cache = load_supplemental_cache()
    now = datetime.now(TW).isoformat(timespec="seconds")
    cameras = {}
    incidents = []
    source_status = []

    # National freeway and provincial-road CCTV only. Per-city CCTV and parking endpoints
    # are intentionally excluded from this five-minute job to avoid API throttling/overruns.
    for path in ("Road/Traffic/CCTV/Highway", "Road/Traffic/CCTV/Freeway"):
        try:
            records = cached_static_rows(path, bearer, cache)
            count = 0
            for item in records:
                point = coordinates(item)
                if not point:
                    continue
                cam_id = field_value(item, "CCTVID", "CameraID", "DeviceID", "ID")
                key = str(cam_id or f"{path}-{point[0]}-{point[1]}")
                cameras[key] = {
                    "id": key,
                    "name": str(field_value(item, "RoadName", "LocationDescription", "CCTVName", "Description") or f"{path.split('/')[-1]} CCTV"),
                    "direction": str(field_value(item, "Direction", "RoadDirection") or ""),
                    "lat": point[0], "lng": point[1],
                    "streamUrl": field_value(item, "VideoStreamURL", "VideoURL", "StreamURL"),
                    "imageUrl": field_value(item, "VideoImageURL", "ImageURL", "SnapshotURL"),
                    "source": "TDX", "updatedAt": field_value(item, "UpdateTime", "SrcUpdateTime"),
                    "network": path.rsplit("/", 1)[-1]
                }
                count += 1
            source_status.append({"source": path, "ok": True, "count": count})
            print(f"[LAYER] {path}: {count} cameras")
        except Exception as exc:
            source_status.append({"source": path, "ok": False, "error": str(exc)[:180]})
            print(f"[LAYER WARN] {path}: {exc}", file=sys.stderr)

    # RoadEvent v1 supplies official freeway and provincial-road events.
    for authority in ("Freeway", "Highway"):
        try:
            found = fetch_event_records(bearer, authority)
            incidents.extend(found)
            source_status.append({"source": f"RoadEvent {authority}", "ok": True, "count": len(found)})
            print(f"[LAYER] RoadEvent {authority}: {len(found)}")
        except Exception as exc:
            source_status.append({"source": f"RoadEvent {authority}", "ok": False, "error": str(exc)[:180]})
            print(f"[LAYER WARN] RoadEvent {authority}: {exc}", file=sys.stderr)

    cache["generatedAt"] = now
    save_supplemental_cache(cache)
    payload = {
        "source": "交通部 TDX 官方國道／省道道路事件與 CCTV",
        "generatedAt": now,
        "incidents": incidents,
        "cctv": list(cameras.values()),
        "parking": [],
        "sources": source_status
    }
    os.makedirs("data", exist_ok=True)
    with open(SUPPLEMENTAL_PATH, "w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))
    print(f"[LAYER] saved incidents={len(incidents)} CCTV={len(cameras)}")

def main():
    bearer=token()
    all_segments=[]
    stats=[]
    shape_cache = load_shape_cache()
    level_cache = load_congestion_cache()
    section_cache = load_supplemental_cache()
    # 即時車速每輪重新抓取；道路線型快取 7 天，官方分級定義快取 24 小時。
    for live_city, shape_city in CITY_PAIRS:
        city_code, label, region = live_city
        try:
            live = rows(f"Road/Traffic/Live/City/{city_code}", bearer)
            shapes = cached_shape_rows(f"Road/Traffic/SectionShape/City/{shape_city[0]}", bearer, shape_cache)
            level_groups = get_congestion_definitions(f"Road/Traffic/CongestionLevel/City/{city_code}", bearer, level_cache)
            segments = build_segments(live, shapes, region, label, "TDX 縣市即時路況", level_index=build_definition_index(level_groups))
            all_segments.extend(segments)
            stats.append({"name":label,"liveRecords":len(live),"shapeRecords":len(shapes),"mappedSegments":len(segments),"unmappedRecords":max(0,len(live)-len(segments)),"mappingRate":round(len(segments)/len(live)*100,1) if live else 0,"ok":True})
            print(f"[OK] {label}: live={len(live)} shape={len(shapes)} mapped={len(segments)} unmapped={max(0,len(live)-len(segments))}")
        except Exception as exc:
            stats.append({"name":label,"ok":False,"error":str(exc)[:240]})
            print(f"[WARN] {label}: {exc}",file=sys.stderr)

    # 國道、省道使用全國性端點，不帶縣市代碼。
    for category, label, region in (("Highway","全台省道","highway"),("Freeway","全台國道","freeway")):
        try:
            live = rows(f"Road/Traffic/Live/{category}", bearer)
            sections = cached_static_rows(f"Road/Traffic/Section/{category}", bearer, section_cache)
            shapes = cached_shape_rows(f"Road/Traffic/SectionShape/{category}", bearer, shape_cache)
            level_groups = get_congestion_definitions(f"Road/Traffic/CongestionLevel/{category}", bearer, level_cache)
            segments = build_segments(live, shapes, region, label, f"TDX {label}即時路況", section_rows=sections, level_index=build_definition_index(level_groups))
            all_segments.extend(segments)
            stats.append({"name":label,"liveRecords":len(live),"shapeRecords":len(shapes),"mappedSegments":len(segments),"unmappedRecords":max(0,len(live)-len(segments)),"mappingRate":round(len(segments)/len(live)*100,1) if live else 0,"ok":True})
            print(f"[OK] {label}: live={len(live)} shape={len(shapes)} mapped={len(segments)} unmapped={max(0,len(live)-len(segments))}")
        except Exception as exc:
            stats.append({"name":label,"ok":False,"error":str(exc)[:240]})
            print(f"[WARN] {label}: {exc}",file=sys.stderr)
    if not all_segments:
        raise SystemExit("No mappable TDX live road sections. Refusing to publish fabricated traffic data.")
    save_shape_cache(shape_cache)
    save_congestion_cache(level_cache)
    save_supplemental_cache(section_cache)
    fetch_supplemental_layers(bearer)
    generated_at = datetime.now(TW)
    # 以各路段來源提供的觀測時間計算最新官方資料時間；
    # generatedAt 則保留為本次 JSON 快照產生時間，兩者不可混用。
    observed_times = []
    for segment in all_segments:
        raw_time = segment.get("updateTime")
        if not raw_time:
            continue
        try:
            parsed_time = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
            if parsed_time.tzinfo is None:
                parsed_time = parsed_time.replace(tzinfo=TW)
            observed_times.append(parsed_time.astimezone(TW))
        except (TypeError, ValueError):
            continue
    official_data_at = max(observed_times).isoformat(timespec="seconds") if observed_times else None
    payload={
        "source":"交通部 TDX 路況資訊 v2",
        "generatedAt":generated_at.isoformat(timespec="seconds"),
        "officialDataAt":official_data_at,
        "totalSegments":len(all_segments),
        "officialClassifiedSegments":sum(1 for item in all_segments if item.get("congestionStatus") not in ("官方分級未提供", "官方分級未識別")),
        "segments":all_segments,
        "sources":stats
    }
    os.makedirs("data",exist_ok=True)
    with open("data/tdx-traffic.json","w",encoding="utf-8") as file:
        json.dump(payload,file,ensure_ascii=False,separators=(",",":"))
    print(f"Saved {len(all_segments)} official road sections.")

if __name__ == "__main__":
    main()
