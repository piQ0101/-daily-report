import requests
import time
import math
import os
from datetime import datetime, timedelta

# ============================================================
# 配置
# ============================================================
LATITUDE, LONGITUDE = 34.3416, 108.9398   # 西安
FORECAST_DAYS = 3
UPSTREAM_OFFSETS_KM = [80, 160]
MAX_RETRIES = 2
TIMEOUT = (8, 40)

FORECAST_URLS = [
    "https://api.open-meteo.com/v1/forecast",
    "https://historical-forecast-api.open-meteo.com/v1/forecast",
]
AIR_URLS = [
    "https://air-quality-api.open-meteo.com/v1/air-quality",
]
ENSEMBLE_URLS = [
    "https://ensemble-api.open-meteo.com/v1/ensemble",
]

# Server酱 SendKey：优先从环境变量读，也可以直接在这里填写
SENDKEY = os.environ.get("SENDKEY", "")   # 或改成 SENDKEY = "SCTxxxxxxxx"


# ============================================================
# 网络层
# ============================================================
def _get_json(urls, params, timeout=TIMEOUT, retries=MAX_RETRIES):
    session = requests.Session()
    session.trust_env = False
    last_exc = None
    for url in urls:
        for attempt in range(retries):
            try:
                r = session.get(url, params=params, timeout=timeout)
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_exc = e
                time.sleep(1.2)
    raise RuntimeError(f"所有数据源均失败，最后错误：{last_exc}")


def fetch_batch_forecast(lats, lons, models="ecmwf_ifs025"):
    params = {
        "latitude": ",".join(f"{x:.4f}" for x in lats),
        "longitude": ",".join(f"{x:.4f}" for x in lons),
        "hourly": "cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
                  "precipitation_probability,visibility,boundary_layer_height",
        "daily": "sunrise,sunset",
        "timezone": "auto",
        "forecast_days": FORECAST_DAYS,
        "models": models,
    }
    return _get_json(FORECAST_URLS, params)


def fetch_batch_aod(lats, lons):
    params = {
        "latitude": ",".join(f"{x:.4f}" for x in lats),
        "longitude": ",".join(f"{x:.4f}" for x in lons),
        "hourly": "aerosol_optical_depth",
        "timezone": "auto",
        "forecast_days": FORECAST_DAYS,
    }
    try:
        return _get_json(AIR_URLS, params, retries=1)
    except Exception:
        return None


def fetch_ensemble_cloud(lat, lon):
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "cloud_cover_low,cloud_cover_mid,cloud_cover_high",
        "models": "ecmwf_ifs025_ensemble",
        "timezone": "auto",
        "forecast_days": 2,
    }
    try:
        return _get_json(ENSEMBLE_URLS, params, retries=1)
    except Exception:
        return None


# ============================================================
# 数据整理
# ============================================================
def parse_hourly(data, var):
    if data is None or var not in data.get("hourly", {}):
        return {}
    return {datetime.strptime(t, "%Y-%m-%dT%H:%M"): v
            for t, v in zip(data["hourly"]["time"], data["hourly"][var])}


def nearest(d, dt):
    if not d:
        return None
    if dt in d:
        return d[dt]
    cand = [t for t in d if abs((t - dt).total_seconds()) <= 5400]
    return d[min(cand, key=lambda t: abs((t - dt).total_seconds()))] if cand else None


def window_mean(d, center, before=1, after=1):
    vals = [nearest(d, center + timedelta(hours=h))
            for h in range(-before, after + 1)]
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def sf(v, default=None):
    return float(v) if v is not None else default


def solar_elevation_deg(lat_deg, dt):
    day = dt.timetuple().tm_yday
    decl = 23.44 * math.sin(math.radians(360 / 365 * (day - 81)))
    ha = 15 * (dt.hour + dt.minute / 60 - 12)
    lat = math.radians(lat_deg)
    decl_r = math.radians(decl)
    ha_r = math.radians(ha)
    sin_e = (math.sin(lat) * math.sin(decl_r) +
             math.cos(lat) * math.cos(decl_r) * math.cos(ha_r))
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_e))))


def destination_point(lat, lon, bearing_deg, distance_km):
    R = 6371.0
    lat1, lon1 = math.radians(lat), math.radians(lon)
    brng = math.radians(bearing_deg)
    d = distance_km / R
    lat2 = math.asin(math.sin(lat1) * math.cos(d) +
                     math.cos(lat1) * math.sin(d) * math.cos(brng))
    lon2 = lon1 + math.atan2(math.sin(brng) * math.sin(d) * math.cos(lat1),
                             math.cos(d) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)


# ============================================================
# 太阳高度角衰减
# ============================================================
def solar_weight(elev_deg):
    if elev_deg is None:
        return 1.0
    if -6.0 <= elev_deg <= 2.0:
        return 1.0
    if elev_deg > 2.0:
        return max(0.25, 1.0 - (elev_deg - 2.0) * 0.12)
    return max(0.45, 1.0 - (-6.0 - elev_deg) * 0.08)


# ============================================================
# 核心评分
# ============================================================
def vividness(low, mid, high, precip, vis, blh_m, sun_elev_deg):
    low = sf(low, 100)
    mid = sf(mid, 0)
    high = sf(high, 0)
    precip = sf(precip, 100)
    vis_raw = vis
    blh_raw = blh_m

    if low >= 90:
        return 0.0, "低云≥90%，太阳本体被遮挡"
    if precip >= 50:
        return 0.0, "降水概率≥50%"
    if mid >= 95:
        return 0.0, "中云≥95%，满天厚层云，光线无法穿透"
    if mid >= 85 and low >= 25:
        return 0.0, "中云厚+低云多，典型阴天结构"

    geo_factor = 1.0
    if blh_raw is not None and sun_elev_deg is not None and sun_elev_deg > 0:
        blh = float(blh_raw)
        blh_angular = math.degrees(math.atan(blh / 50000.0))
        if sun_elev_deg <= blh_angular:
            geo_factor = 0.55

    canvas = min(100.0, mid + high * 0.7)
    if canvas < 8:      C = 0.04
    elif canvas < 25:   C = 0.04 + (canvas - 8) / 17 * 0.26
    elif canvas < 45:   C = 0.30 + (canvas - 25) / 20 * 0.50
    elif canvas <= 65:  C = 0.80 + (canvas - 45) / 20 * 0.20
    elif canvas <= 82:  C = 1.00 - (canvas - 65) / 17 * 0.65
    elif canvas <= 92:  C = 0.35 - (canvas - 82) / 10 * 0.28
    else:               C = max(0.01, 0.07 - (canvas - 92) / 8 * 0.06)

    if low < 15:    L = 1.00
    elif low < 30:  L = 0.90
    elif low < 50:  L = 0.72
    elif low < 70:  L = 0.48
    elif low < 85:  L = 0.22
    else:           L = 0.08

    if mid >= 85:   L *= 0.40
    elif mid >= 75: L *= 0.60
    elif mid >= 60: L *= 0.82

    if vis_raw is None:
        V = 1.0
    else:
        vis_v = float(vis_raw)
        if vis_v >= 20:   V = 1.00
        elif vis_v >= 10: V = 0.82
        elif vis_v >= 5:  V = 0.50
        else:             V = 0.25

    if precip < 10:   P = 1.00
    elif precip < 20: P = 0.90
    elif precip < 35: P = 0.70
    else:             P = 0.50

    vivid = 2.5 * C * L * V * P * geo_factor
    return max(0.0, min(2.5, vivid)), None


def upstream_factor(upstream_low, upstream_mid, local_mid):
    if upstream_mid is None:
        return 1.0
    um = sf(upstream_mid, 50)
    ul = sf(upstream_low, 50)

    if um >= 85:
        return 0.60
    if um >= 70:
        return 0.80
    if um <= 30 and ul <= 20:
        if sf(local_mid, 0) >= 25:
            return 1.08
        return 1.0
    return 1.0


# ============================================================
# 置信度
# ============================================================
def confidence_from_ensemble(ensemble_data, dt):
    if ensemble_data is None or "hourly" not in ensemble_data:
        return "中（无集合数据）"
    try:
        times = ensemble_data["hourly"]["time"]
        idx = min(range(len(times)),
                  key=lambda i: abs(datetime.strptime(times[i], "%Y-%m-%dT%H:%M") - dt))
        target = datetime.strptime(times[idx], "%Y-%m-%dT%H:%M")
        if abs((target - dt).total_seconds()) > 7200:
            return "中（时间偏差大）"

        spreads = []
        for var in ["cloud_cover_low", "cloud_cover_mid", "cloud_cover_high"]:
            members = [k for k in ensemble_data["hourly"]
                       if k.startswith(var) and "member" in k]
            vals = [ensemble_data["hourly"][k][idx] for k in members
                    if ensemble_data["hourly"][k][idx] is not None]
            if len(vals) >= 3:
                spreads.append(max(vals) - min(vals))

        if not spreads:
            return "中（成员不足）"
        avg_spread = sum(spreads) / len(spreads)
        if avg_spread <= 15:
            return "高"
        if avg_spread <= 35:
            return "中"
        return "低"
    except Exception:
        return "中（解析异常）"


# ============================================================
# 等级映射
# ============================================================
def vivid_level(v):
    if v < 0.05: return "1级·微微烧"
    if v < 0.2:  return "2级·小烧"
    if v < 0.4:  return "3级·小到中等烧"
    if v < 0.6:  return "4级·中等烧"
    if v < 0.8:  return "5级·中到大烧"
    if v < 1.0:  return "6级·大烧（有瑕疵）"
    if v < 1.5:  return "7级·典型大烧"
    if v < 2.0:  return "8级·优质大烧"
    return "8级+·世纪大烧"


def aod_level(a):
    if a is None: return "AOD缺失"
    if a < 0.1: return "8级·高级水晶天"
    if a < 0.2: return "7级·普适水晶天"
    if a < 0.3: return "6级·有蓝天"
    if a < 0.4: return "5级·普通天空"
    if a < 0.6: return "4级·有点污"
    if a < 0.8: return "3级·相当污"
    if a < 1.0: return "2级·非常污"
    return "1级·重度浑浊"


def fmt(x, suffix="", d=0):
    return f"{x:.{d}f}{suffix}" if x is not None else "缺测"


# ============================================================
# 主流程
# ============================================================
def main():
    base = fetch_batch_forecast([LATITUDE], [LONGITUDE], models="ecmwf_ifs025")
    base_w = base if isinstance(base, dict) else base[0]

    events = []
    for rise_s, set_s in zip(base_w["daily"]["sunrise"], base_w["daily"]["sunset"]):
        events.append(("朝霞", datetime.strptime(rise_s, "%Y-%m-%dT%H:%M"), 90.0))
        events.append(("晚霞", datetime.strptime(set_s, "%Y-%m-%dT%H:%M"), 270.0))

    now = datetime.now()
    events = [(lab, dt, az) for lab, dt, az in events
              if now <= dt <= now + timedelta(hours=48)]

    coords = [(LATITUDE, LONGITUDE)]
    for _, _, az in events:
        for dist in UPSTREAM_OFFSETS_KM:
            coords.append(destination_point(LATITUDE, LONGITUDE, az, dist))
    seen = set()
    uniq = []
    for la, lo in coords:
        key = (round(la, 3), round(lo, 3))
        if key not in seen:
            seen.add(key)
            uniq.append((la, lo))
    lats = [x[0] for x in uniq]
    lons = [x[1] for x in uniq]
    idx_of = {(round(la, 3), round(lo, 3)): i for i, (la, lo) in enumerate(uniq)}

    batch = fetch_batch_forecast(lats, lons, models="ecmwf_ifs025")
    batch_list = batch if isinstance(batch, list) else [batch]

    aod_batch = fetch_batch_aod(lats, lons)
    aod_list = aod_batch if isinstance(aod_batch, list) else ([aod_batch] if aod_batch else [None])

    ens = fetch_ensemble_cloud(LATITUDE, LONGITUDE)

    def get_batch(data_list, lat, lon):
        i = idx_of.get((round(lat, 3), round(lon, 3)))
        return data_list[i] if i is not None and i < len(data_list) else None

    blocks = []
    for lab, dt, azimuth in events:
        local_w = get_batch(batch_list, LATITUDE, LONGITUDE)
        local_aod = get_batch(aod_list, LATITUDE, LONGITUDE) if aod_list else None

        low = window_mean(parse_hourly(local_w, "cloud_cover_low"), dt)
        mid = window_mean(parse_hourly(local_w, "cloud_cover_mid"), dt)
        high = window_mean(parse_hourly(local_w, "cloud_cover_high"), dt)
        pr = window_mean(parse_hourly(local_w, "precipitation_probability"), dt)
        vis = window_mean(parse_hourly(local_w, "visibility"), dt)
        blh = window_mean(parse_hourly(local_w, "boundary_layer_height"), dt)
        aod = window_mean(parse_hourly(local_aod, "aerosol_optical_depth"), dt) if local_aod else None

        sun_elev = solar_elevation_deg(LATITUDE, dt)

        up_mid_vals, up_low_vals = [], []
        for dist in UPSTREAM_OFFSETS_KM:
            ulat, ulon = destination_point(LATITUDE, LONGITUDE, azimuth, dist)
            uw = get_batch(batch_list, ulat, ulon)
            um = window_mean(parse_hourly(uw, "cloud_cover_mid"), dt)
            ul = window_mean(parse_hourly(uw, "cloud_cover_low"), dt)
            if um is not None: up_mid_vals.append(um)
            if ul is not None: up_low_vals.append(ul)
        up_mid = sum(up_mid_vals) / len(up_mid_vals) if up_mid_vals else None
        up_low = sum(up_low_vals) / len(up_low_vals) if up_low_vals else None

        v, veto = vividness(low, mid, high, pr, vis, blh, sun_elev)
        if not veto:
            v *= upstream_factor(up_low, up_mid, mid)
            v *= solar_weight(sun_elev)
            v = max(0.0, min(2.5, v))

        conf = confidence_from_ensemble(ens, dt)
        v_name = vivid_level(v)
        a_name = aod_level(aod)

        header = f"【{lab}】{dt.strftime('%m-%d %H:%M')}"
        if veto:
            combo = f"▸ 综合：无霞（{veto}）  [置信度 {conf}]"
        else:
            combo = f"▸ 综合：{v_name} · {a_name}  [置信度 {conf}]"
        params = (f"[本地] 低={fmt(low,'%')} 中={fmt(mid,'%')} 高={fmt(high,'%')} "
                  f"降水={fmt(pr,'%')} 能见度={fmt(vis,'km',1)} BLH={fmt(blh,'m')}")

        blocks.append(header)
        blocks.append(combo)
        blocks.append(params)
        blocks.append("")

    message = "\n".join(blocks).strip()
    print(message)

    # ---- 推送到微信（Server酱）----
    if not SENDKEY:
        print("\n未设置 SENDKEY（环境变量或代码内），跳过微信推送。")
        return

    url = f"https://sctapi.ftqq.com/{SENDKEY}.send"
    data = {
        "title": "西安朝晚霞预测",
        "desp": message,
    }
    try:
        resp = requests.post(url, data=data, timeout=15)
        print("\n推送结果：", resp.json())
    except Exception as e:
        print(f"\n推送失败：{e}")


if __name__ == "__main__":
    main()
