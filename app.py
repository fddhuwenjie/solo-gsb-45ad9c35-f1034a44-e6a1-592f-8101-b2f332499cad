"""低空航线空域时窗审查台 — Flask + SQLite 后端。

模型（画布局部平面坐标系，单位由用户约定，例如 km；高度单位 m；速度 km/h）：
- 航线由有序航点构成，航段折线按“走廊半宽”缓冲成扫掠走廊（平面 2D）。
- 每段速度独立，起飞时刻 + 航段距离/速度 推出各航点预达时刻（ETA）。
- 禁飞多边形带高度层 [minAlt, maxAlt] 与生效时窗 [startTime, endTime]（端点可空）。
- 冲突 = 平面走廊与多边形相交 ∩ 高度层重叠 ∩ 时间窗重叠（三维 + 时间）。
"""
import json
import math
import os
import sqlite3
from datetime import datetime, timedelta

from flask import Flask, g, jsonify, request, send_from_directory
from shapely.geometry import LineString, Polygon, mapping
from shapely.validation import explain_validity

DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "console.db"))

app = Flask(__name__, static_folder="static", static_url_path="/static")

# 仅供并发回归测试使用的写事务钩子（默认 None，不影响生产路径）：
# on_write_begin 在拿到 BEGIN IMMEDIATE 后、业务逻辑前调用；
# on_write_commit 在提交前调用。测试借此确定性地排列交错事务的先后。
on_write_begin = None
on_write_commit = None


# ---------------------------------------------------------------- 数据库
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute(PRAGMA_FOREIGN_KEYS)
    return g.db


PRAGMA_FOREIGN_KEYS = "PRAGMA foreign_keys=ON"


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """CREATE TABLE IF NOT EXISTS kv(
               k TEXT PRIMARY KEY,
               v TEXT NOT NULL
           )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS versions(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               name TEXT NOT NULL,
               created_at TEXT NOT NULL,
               payload TEXT NOT NULL
           )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS releases(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               created_at TEXT NOT NULL,
               source_version_id INTEGER NOT NULL,
               source_version_name TEXT NOT NULL,
               workspace_rev INTEGER NOT NULL,
               state TEXT NOT NULL,
               analysis TEXT NOT NULL
           )"""
    )
    db.execute(
        """CREATE TABLE IF NOT EXISTS voyages(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               release_seq INTEGER NOT NULL,
               created_at TEXT NOT NULL,
               departure_time TEXT NOT NULL,
               route_snapshot TEXT NOT NULL,
               ended_at TEXT
           )"""
    )
    if db.execute("SELECT 1 FROM kv WHERE k='state'").fetchone() is None:
        db.execute(
            "INSERT INTO kv(k,v) VALUES('state',?)",
            (json.dumps(SEED_STATE, ensure_ascii=False),),
        )
    if db.execute("SELECT 1 FROM kv WHERE k='state_rev'").fetchone() is None:
        db.execute("INSERT INTO kv(k,v) VALUES('state_rev','1')")
    db.commit()
    db.close()


# ---------------------------------------------------------------- 初始样例
SEED_STATE = {
    "route": {
        "departureTime": "2026-09-24T08:00",
        "unitScale": 100,
        "corridorHalfWidth": 0.3,
        "speeds": [22, 26, 24],
        "waypoints": [
            {"id": "w1", "name": "WP-1", "x": 120, "y": 420, "alt": 200},
            {"id": "w2", "name": "WP-2", "x": 380, "y": 380, "alt": 200},
            {"id": "w3", "name": "WP-3", "x": 640, "y": 300, "alt": 350},
            {"id": "w4", "name": "WP-4", "x": 880, "y": 360, "alt": 250},
        ],
    },
    "nfzs": [
        {
            "id": "n1",
            "name": "临时禁飞区-A",
            "vertices": [[450, 280], [620, 280], [620, 350], [450, 350]],
            "minAlt": 150,
            "maxAlt": 300,
            "startTime": "2026-09-24T08:08",
            "endTime": "2026-09-24T08:13",
            "enabled": True,
        },
        {
            "id": "n2",
            "name": "高空限制区(500m+)",
            "vertices": [[700, 140], [830, 140], [830, 220], [700, 220]],
            "minAlt": 500,
            "maxAlt": 1200,
            "startTime": "",
            "endTime": "",
            "enabled": True,
        },
        {
            "id": "n3",
            "name": "夜间管控区",
            "vertices": [[760, 330], [920, 330], [920, 430], [760, 430]],
            "minAlt": 0,
            "maxAlt": 400,
            "startTime": "2026-09-24T20:00",
            "endTime": "2026-09-24T21:00",
            "enabled": True,
        },
    ],
}


# ---------------------------------------------------------------- 工具
def parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.strip().replace(" ", "T"))
    except (ValueError, AttributeError):
        return None


def fmt_dt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M") if dt else None


def to_float(v, default=None):
    try:
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (TypeError, ValueError):
        return default


def interp(a, b, f):
    return a + (b - a) * f


def altitude_frac_ranges(a0, a1, lo, hi):
    """返回航段参数 f∈[0,1] 上线性高度落在 [lo,hi] 的子区间列表。"""
    eps = 1e-9
    if lo - eps <= a0 <= hi + eps and lo - eps <= a1 <= hi + eps:
        return [(0.0, 1.0)]
    cuts = {0.0, 1.0}
    if abs(a1 - a0) > eps:
        for bound in (lo, hi):
            f = (bound - a0) / (a1 - a0)
            if -eps <= f <= 1 + eps:
                cuts.add(min(1.0, max(0.0, f)))
    out = []
    for f0, f1 in zip(sorted(cuts), sorted(cuts)[1:]):
        mid = (f0 + f1) / 2
        a = interp(a0, a1, mid)
        if lo - eps <= a <= hi + eps:
            out.append((f0, f1))
    return out


def overlap_frac(ranges, f_lo, f_hi):
    """区间列表与单区间求交；保留落在区间内的退化点（对应边界擦碰）。"""
    out = []
    for a, b in ranges:
        lo, hi = max(a, f_lo), min(b, f_hi)
        if hi >= lo - 1e-9:
            out.append((lo, max(lo, hi)))
    return out


def merge_contained_fracs(ranges):
    """丢弃被其他区间完全包含的区间，避免同一冲突重复成条（保留并列区间）。"""
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][0] + 1e-9 and b <= out[-1][1] + 1e-9:
            continue
        out.append((a, b))
    return out


def iter_geom_components(geom):
    """把相交结果展平为各自独立的连通部件（Multi* / GeometryCollection 递归展开）。"""
    gtype = geom.geom_type
    if gtype in ("GeometryCollection", "MultiPolygon", "MultiLineString", "MultiPoint"):
        for sub in geom.geoms:
            if not sub.is_empty:
                yield from iter_geom_components(sub)
    else:
        yield geom


def component_frac_ranges(inter, seg):
    """平面相交的每个独立部件，分别沿航段方向投影为 f 区间。

    部件之间的空隙不参与合并：凹形禁飞区走廊先穿一侧、再穿另一侧时，
    返回两个相互独立的区间。
    """
    x0, y0 = seg["x0"], seg["y0"]
    ux, uy, d = seg["ux"], seg["uy"], seg["d"]
    out = []
    for part in iter_geom_components(inter):
        if part.geom_type == "Point":
            pts = [(part.x, part.y)]
        elif part.geom_type == "Polygon":
            pts = list(part.exterior.coords)
        else:  # LineString / LinearRing
            pts = list(part.coords)
        if not pts:
            continue
        fs = sorted(
            min(1.0, max(0.0, ((x - x0) * ux + (y - y0) * uy) / d))
            for x, y in pts
        )
        out.append((fs[0], fs[-1]))
    return out


def windows_for_nfz(nfz):
    st, en = parse_dt(nfz.get("startTime")), parse_dt(nfz.get("endTime"))
    if st and en and en < st:
        return None, "禁飞区结束时刻早于开始时刻"
    return [(st, en)], None


# ---------------------------------------------------------------- 发布辅助
def workspace_rev(db):
    """当前工作区修订号：每次 PUT /api/state 递增。"""
    row = db.execute("SELECT v FROM kv WHERE k='state_rev'").fetchone()
    return int(row["v"]) if row else 0


def current_release_seq(db):
    """当前已发布号（最新一条发布的 id；从未发布为 0）。"""
    row = db.execute("SELECT MAX(id) AS m FROM releases").fetchone()
    return row["m"] or 0


def bump_workspace_rev(db):
    """工作区修订号 +1（工作区内容每次落库时调用）。"""
    db.execute(
        "INSERT INTO kv(k,v) VALUES('state_rev','2') "
        "ON CONFLICT(k) DO UPDATE SET v=CAST(CAST(v AS INTEGER)+1 AS TEXT)"
    )


def stale_workspace_details(db, ws_rev):
    """乐观并发明细：提交的工作区修订号与当前不一致时返回 {workspaceRev:{expected,actual}}。"""
    cur_rev = workspace_rev(db)
    if ws_rev != cur_rev:
        return {"workspaceRev": {"expected": ws_rev, "actual": cur_rev}}
    return {}


def stale_details(db, ws_rev, rel_seq):
    """乐观并发明细：提交的号与当前不一致时返回 {字段: {expected, actual}}。"""
    details = stale_workspace_details(db, ws_rev)
    cur_seq = current_release_seq(db)
    if rel_seq != cur_seq:
        details["releaseSeq"] = {"expected": rel_seq, "actual": cur_seq}
    return details


def route_has_nonzero_segment(route):
    """航线是否至少存在一条非零长度航段（相邻航点平面距离 > 0）。"""
    wps = (route or {}).get("waypoints") or []
    for a, b in zip(wps, wps[1:]):
        x0, y0 = to_float(a.get("x")), to_float(a.get("y"))
        x1, y1 = to_float(b.get("x")), to_float(b.get("y"))
        if None in (x0, y0, x1, y1):
            continue
        if math.hypot(x1 - x0, y1 - y0) > 0:
            return True
    return False


def release_dict(row):
    return {
        "seq": row["id"],
        "sourceVersionId": row["source_version_id"],
        "sourceVersionName": row["source_version_name"],
        "workspaceRev": row["workspace_rev"],
        "createdAt": row["created_at"],
        "state": json.loads(row["state"]),
        "analysis": json.loads(row["analysis"]),
    }


def voyage_dict(row):
    return {
        "id": row["id"],
        "releaseSeq": row["release_seq"],
        "createdAt": row["created_at"],
        "departureTime": row["departure_time"],
        "routeSnapshot": json.loads(row["route_snapshot"]),
        "endedAt": row["ended_at"],
        "open": row["ended_at"] is None,
    }


# ---------------------------------------------------------------- 核心分析
def analyze(state):
    errors = []
    route = state.get("route") or {}
    wps = route.get("waypoints") or []
    speeds = route.get("speeds") or []
    half_w = to_float(route.get("corridorHalfWidth"), 3.0)
    if half_w <= 0:
        errors.append({"scope": "route", "message": "走廊半宽必须为正数"})
        half_w = 3.0

    # —— 航段距离与 ETA（画布单位 ÷ unitScale = km）
    unit_scale = to_float(route.get("unitScale"), 100.0)
    if unit_scale <= 0:
        errors.append({"scope": "route", "message": "画布比例（单位/km）必须为正数"})
        unit_scale = 100.0
    distances, times = [], [None] * len(wps)
    dep = parse_dt(route.get("departureTime"))
    if wps:
        if dep is None:
            errors.append({"scope": "route", "message": "起飞时刻缺失或格式错误"})
        else:
            times[0] = dep
    total_len = 0.0

    for k in range(max(0, len(wps) - 1)):
        p0, p1 = wps[k], wps[k + 1]
        d_px = math.hypot(to_float(p1.get("x"), 0) - to_float(p0.get("x"), 0),
                          to_float(p1.get("y"), 0) - to_float(p0.get("y"), 0))
        d = d_px / unit_scale  # km
        distances.append(round(d, 3))
        total_len += d
        if times[k] is not None:
            spd = to_float(speeds[k] if k < len(speeds) else None)
            if spd is None or spd <= 0:
                errors.append({"scope": f"seg{k}", "message": f"航段 {k + 1} 速度无效，后续时刻无法推算"})
            elif d_px <= 0:
                times[k + 1] = times[k]
            else:
                times[k + 1] = times[k] + timedelta(minutes=d / spd * 60.0)

    route_info = {
        "length": round(total_len, 3),
        "distances": distances,
        "times": [fmt_dt(t) for t in times],
        "durationMinutes": round((times[-1] - dep).total_seconds() / 60.0, 2)
        if dep and len(times) > 1 and times[-1] is not None else None,
    }

    # —— 航段走廊
    corridors = []
    for k in range(max(0, len(wps) - 1)):
        p0, p1 = wps[k], wps[k + 1]
        x0, y0 = to_float(p0.get("x")), to_float(p0.get("y"))
        x1, y1 = to_float(p1.get("x")), to_float(p1.get("y"))
        a0, a1 = to_float(p0.get("alt")), to_float(p1.get("alt"))
        if None in (x0, y0, x1, y1, a0, a1):
            errors.append({"scope": f"seg{k}", "message": f"航段 {k + 1} 坐标/高度数据不完整"})
            corridors.append(None)
            continue
        d_px = math.hypot(x1 - x0, y1 - y0)
        if d_px <= 0:
            corridors.append(None)
            continue
        # 走廊半宽按 km 录入，缓冲换算成画布单位
        corridor = LineString([(x0, y0), (x1, y1)]).buffer(half_w * unit_scale)
        ux, uy = (x1 - x0) / d_px, (y1 - y0) / d_px  # 单位方向
        corridors.append({"corridor": corridor, "x0": x0, "y0": y0, "ux": ux, "uy": uy,
                          "d": d_px, "a0": a0, "a1": a1})

    # —— 禁飞多边形
    nfz_polys = []
    for idx, nfz in enumerate(state.get("nfzs") or []):
        if not nfz.get("enabled", True):
            nfz_polys.append(None)
            continue
        raw = nfz.get("vertices") or []
        coords = []
        for v in raw:
            x, y = to_float(v[0] if isinstance(v, (list, tuple)) else v.get("x")), \
                   to_float(v[1] if isinstance(v, (list, tuple)) else v.get("y"))
            if x is not None and y is not None:
                coords.append((x, y))
        lo, hi = to_float(nfz.get("minAlt"), 0.0), to_float(nfz.get("maxAlt"), 0.0)
        if lo is None or hi is None or hi < lo:
            errors.append({"scope": nfz.get("id", f"nfz{idx}"),
                           "message": f"禁飞区「{nfz.get('name', idx)}」高度层无效"})
            nfz_polys.append(None)
            continue
        wins, werr = windows_for_nfz(nfz)
        if werr:
            errors.append({"scope": nfz.get("id", f"nfz{idx}"),
                           "message": f"禁飞区「{nfz.get('name', idx)}」{werr}"})
            nfz_polys.append(None)
            continue
        if len(coords) < 3:
            errors.append({"scope": nfz.get("id", f"nfz{idx}"),
                           "message": f"禁飞区「{nfz.get('name', idx)}」顶点少于 3 个"})
            nfz_polys.append(None)
            continue
        poly = Polygon(coords).buffer(0)
        if poly.is_empty or not poly.geom_type.startswith(("Polygon", "MultiPolygon")):
            errors.append({"scope": nfz.get("id", f"nfz{idx}"),
                           "message": f"禁飞区「{nfz.get('name', idx)}」多边形无效：{explain_validity(Polygon(coords))}"})
            nfz_polys.append(None)
            continue
        nfz_polys.append({"poly": poly, "lo": lo, "hi": hi, "windows": wins})

    # —— 相交检测
    entries = []
    for k, seg in enumerate(corridors):
        if seg is None:
            continue
        t0, t1 = times[k] if k < len(times) else None, times[k + 1] if k + 1 < len(times) else None
        for idx, nz in enumerate(nfz_polys):
            if nz is None:
                continue
            nfz = state["nfzs"][idx]
            inter = seg["corridor"].intersection(nz["poly"])
            if inter.is_empty:
                continue

            # 平面相交：按连通部件分别投影，得到各自独立的 f 区间（空隙保留为空隙）
            f_parts = component_frac_ranges(inter, seg)
            if not f_parts:
                continue

            # ∩ 高度重叠子区间（逐部件求交，不跨部件合并；高度层随禁飞区而定）
            alt_ranges = altitude_frac_ranges(seg["a0"], seg["a1"], nz["lo"], nz["hi"])
            spatial = []
            for fa, fb in f_parts:
                spatial.extend(overlap_frac(alt_ranges, fa, fb))
            spatial = merge_contained_fracs(spatial)
            if not spatial:
                continue

            # ∩ 生效时窗：逐段求交，最终结果全部落在同一个（可能被截短的）子段上
            for f_a, f_b in spatial:
                spatial_instant = f_b - f_a <= 1e-9
                for w_start, w_end in nz["windows"]:
                    if t0 is not None:
                        enter = t0 + (t1 - t0) * f_a
                        exit_t = t0 + (t1 - t0) * f_b
                    else:
                        enter = exit_t = None
                    if spatial_instant:
                        # 瞬时擦碰：仅当该时刻落在生效窗内才算
                        if w_start and enter and enter < w_start:
                            continue
                        if w_end and enter and enter > w_end:
                            continue
                    else:
                        # 闭区间按实际重叠判定：区间端点落在窗沿上也算命中
                        if w_start and exit_t and exit_t < w_start:
                            continue
                        if w_end and enter and enter > w_end:
                            continue
                        # 时窗截短区间：进入/退出时刻收到窗沿
                        if w_start and enter and enter < w_start:
                            enter = w_start
                        if w_end and exit_t and exit_t > w_end:
                            exit_t = w_end
                    if enter and exit_t and exit_t < enter:
                        continue

                    # 区间被时窗截短后，按实际进入/退出时刻反推最终 f，
                    # 保证 frac / 时刻 / 见证点同属一段最终冲突
                    ef, xf = f_a, f_b
                    if t0 is not None and t1 is not None and t1 > t0:
                        span = (t1 - t0)
                        ef = min(1.0, max(0.0, (enter - t0) / span))
                        xf = min(1.0, max(0.0, (exit_t - t0) / span))
                    if xf < ef:
                        xf = ef
                    # 报告的瞬时标记以最终区间（可能被时窗夹成一点）为准
                    instant = xf - ef <= 1e-9

                    alt_at_enter = round(interp(seg["a0"], seg["a1"], ef), 2)
                    alt_at_exit = round(interp(seg["a0"], seg["a1"], xf), 2)
                    lo_t, hi_t = nz["lo"], nz["hi"]
                    alt_clip_lo = round(max(min(alt_at_enter, alt_at_exit), lo_t), 2)
                    alt_clip_hi = round(min(max(alt_at_enter, alt_at_exit), hi_t), 2)
                    # 见证点随实际进入时刻移动（位于被截短区间的进入端中心线上）
                    wx = interp(to_float(wps[k]["x"]), to_float(wps[k + 1]["x"]), ef)
                    wy = interp(to_float(wps[k]["y"]), to_float(wps[k + 1]["y"]), ef)
                    entries.append({
                        "nfzId": nfz.get("id"),
                        "nfzName": nfz.get("name", f"禁飞区{idx + 1}"),
                        "segIndex": k,
                        "enterFrac": round(ef, 5),
                        "exitFrac": round(xf, 5),
                        "enterTime": fmt_dt(enter),
                        "exitTime": fmt_dt(exit_t),
                        "durationMinutes": round((exit_t - enter).total_seconds() / 60.0, 2)
                        if enter and exit_t else None,
                        "altLow": alt_clip_lo,
                        "altHigh": alt_clip_hi,
                        "witness": {"x": round(wx, 2), "y": round(wy, 2), "alt": alt_at_enter},
                        "instant": instant,
                    })

    entries.sort(key=lambda e: (e["enterTime"] is None, e["enterTime"] or ""))
    first = None
    if entries:
        e = entries[0]
        first = {
            "nfzId": e["nfzId"], "nfzName": e["nfzName"], "segIndex": e["segIndex"],
            "enterTime": e["enterTime"], "exitTime": e["exitTime"],
            "durationMinutes": e["durationMinutes"],
            "witness": e["witness"], "altLow": e["altLow"], "altHigh": e["altHigh"],
        }

    return {"ok": True, "errors": errors, "route": route_info,
            "entries": entries, "first": first}


# ---------------------------------------------------------------- 航段空域占用
# 共占时窗正长度阈值（秒）：严格小于 1 秒的时窗交集视同首尾相接，允许
CO_OCCUPY_MIN_SECONDS = 1.0


def _round_coords(obj, ndigits=2):
    """递归把 GeoJSON 坐标中的数值四舍五入，减小接口负载。"""
    if isinstance(obj, (list, tuple)):
        return [_round_coords(v, ndigits) for v in obj]
    if isinstance(obj, float):
        return round(obj, ndigits)
    return obj


def corridor_geojson(geom):
    """扫掠走廊多边形序列化为 GeoJSON（坐标取整到画布 0.01）。"""
    return _round_coords(mapping(geom))


def build_voyage_segments(route):
    """按航次自己保存的航线快照逐段计算空域占用。

    每条非零长度航段返回：扫掠走廊（shapely 几何 + GeoJSON）、端点高度范围
    [altLow, altHigh]、进入/离开时刻（端点 ETA）与该段占用时长。航段之间
    端点时刻相同，故相邻航段的占用时段为首尾相接（正长度交集为空）。
    返回 (departure, segments, errors)。
    """
    route = route or {}
    wps = route.get("waypoints") or []
    speeds = route.get("speeds") or []
    errors = []

    half_w = to_float(route.get("corridorHalfWidth"))
    if half_w is None or half_w <= 0:
        errors.append({"scope": "route", "message": "走廊半宽必须为正数"})
        half_w = 3.0
    unit_scale = to_float(route.get("unitScale"))
    if unit_scale is None or unit_scale <= 0:
        errors.append({"scope": "route", "message": "画布比例（单位/km）必须为正数"})
        unit_scale = 100.0
    dep = parse_dt(route.get("departureTime"))
    if not wps or dep is None:
        errors.append({"scope": "route", "message": "起飞时刻缺失或格式错误"})
        return fmt_dt(dep) if dep else None, [], errors

    t = dep
    segments = []
    for k in range(max(0, len(wps) - 1)):
        p0, p1 = wps[k], wps[k + 1]
        x0, y0 = to_float(p0.get("x")), to_float(p0.get("y"))
        x1, y1 = to_float(p1.get("x")), to_float(p1.get("y"))
        a0, a1 = to_float(p0.get("alt")), to_float(p1.get("alt"))
        if None in (x0, y0, x1, y1, a0, a1):
            errors.append({"scope": f"seg{k}", "message": f"航段 {k + 1} 坐标/高度数据不完整"})
            continue
        d_px = math.hypot(x1 - x0, y1 - y0)
        if d_px <= 0:
            continue  # 零长度航段不形成扫掠走廊
        dist_km = d_px / unit_scale
        spd = to_float(speeds[k] if k < len(speeds) else None)
        if spd is None or spd <= 0:
            errors.append({"scope": f"seg{k}", "message": f"航段 {k + 1} 速度无效，进入/离开时段无法推算"})
            t_next = None
        else:
            t_next = t + timedelta(minutes=dist_km / spd * 60.0)
        corridor = LineString([(x0, y0), (x1, y1)]).buffer(half_w * unit_scale)
        segments.append({
            "index": k,
            "distanceKm": round(dist_km, 3),
            "altLow": round(min(a0, a1), 2),
            "altHigh": round(max(a0, a1), 2),
            "enter": t,
            "exit": t_next,
            "corridor": corridor,
        })
        if t_next is not None:
            t = t_next
    return fmt_dt(dep), segments, errors


def voyage_occupancy_payload(voyage_id, release_seq, departure, segments, ended_at):
    """把内部占用段（含 shapely 几何）序列化为接口/共占核对共用的字典。"""
    out_segments = []
    for s in segments:
        enter, exit_t = s["enter"], s["exit"]
        duration = (round((exit_t - enter).total_seconds() / 60.0, 2)
                    if enter is not None and exit_t is not None else None)
        out_segments.append({
            "segIndex": s["index"],
            "distanceKm": s["distanceKm"],
            "altLow": s["altLow"],
            "altHigh": s["altHigh"],
            "enterTime": fmt_dt(enter),
            "exitTime": fmt_dt(exit_t),
            "durationMinutes": duration,
            "corridor": corridor_geojson(s["corridor"]),
        })
    return {
        "voyageId": voyage_id,
        "releaseSeq": release_seq,
        "departureTime": departure,
        "open": ended_at is None,
        "endedAt": ended_at,
        "segments": out_segments,
    }


def _co_occupy_entries(a, b):
    """两条航次（A/B 身份 + 各自占用段）逐段核对共占。

    共占 = 两段扫掠走廊平面相交（闭集，边界擦碰也算）∩ 端点高度范围重合
    ∩ 占用时段有【正长度】交集；首尾相接（区间端点相切）允许。
    返回以 A 航次视角描述的共占明细列表。
    """
    eps = timedelta(seconds=CO_OCCUPY_MIN_SECONDS)
    entries = []
    for sa in a["segments"]:
        for sb in b["segments"]:
            if not sa["corridor"].intersects(sb["corridor"]):
                continue
            # 高度层重合：闭区间相交即满足（端点同高擦碰也算平面+高度命中，
            # 但还须通过下方“正长度时窗交集”才构成共占）
            if sa["altLow"] > sb["altHigh"] + 1e-9 or sb["altLow"] > sa["altHigh"] + 1e-9:
                continue
            ea, xa, eb, xb = sa["enter"], sa["exit"], sb["enter"], sb["exit"]
            if None in (ea, xa, eb, xb):
                continue
            ov_start = max(ea, eb)
            ov_end = min(xa, xb)
            if ov_end - ov_start < eps:
                continue  # 空交集或仅首尾相接：允许
            entries.append({
                "voyageId": b["id"],
                "releaseSeq": b["release_seq"],
                "departureTime": b["departure"],
                "segIndex": sa["index"],
                "otherSegIndex": sb["index"],
                "altLow": round(max(sa["altLow"], sb["altLow"]), 2),
                "altHigh": round(min(sa["altHigh"], sb["altHigh"]), 2),
                "startTime": fmt_dt(ov_start),
                "endTime": fmt_dt(ov_end),
                "durationMinutes": round((ov_end - ov_start).total_seconds() / 60.0, 2),
            })
    entries.sort(key=lambda e: (e["startTime"] or "", e["segIndex"], e["otherSegIndex"]))
    return entries


def co_occupancy_against_open_voyages(db, candidate, skip_voyage_id=None):
    """在当前写事务内核对候选航次与其他未结束航次的共占。

    candidate: {"id"(可空), "release_seq", "departure"(str), "segments"}。
    读取以本事务快照为准（BEGIN IMMEDIATE 下全部写操作串行），改期传
    skip_voyage_id 跳过自身，结束航次不进入未结束集合即天然释放占用。
    返回 (entries, per_voyage_errors)；调用方据此整次拒绝、原记录不变。
    """
    rows = db.execute(
        "SELECT * FROM voyages WHERE ended_at IS NULL ORDER BY id"
    ).fetchall()
    entries, per_voyage_errors = [], []
    for row in rows:
        if skip_voyage_id is not None and row["id"] == skip_voyage_id:
            continue
        other_dep, other_segs, other_errors = build_voyage_segments(
            json.loads(row["route_snapshot"]))
        if other_errors:
            per_voyage_errors.append({"voyageId": row["id"], "errors": other_errors})
            continue
        other = {"id": row["id"], "release_seq": row["release_seq"],
                 "departure": other_dep, "segments": other_segs}
        entries.extend(_co_occupy_entries(candidate, other))
    entries.sort(key=lambda e: (e["startTime"] or "", e["voyageId"],
                                e["segIndex"], e["otherSegIndex"]))
    return entries, per_voyage_errors


def reject_co_occupancy(entries, action):
    """构造共占拒绝响应：附对方航次编号、双方航段与正长度重叠时段。"""
    others = sorted({e["voyageId"] for e in entries})
    return jsonify({"ok": False, "reason": "voyage_co_occupancy",
                    "error": f"与 {len(others)} 个其他未结束航次存在 "
                             f"{len(entries)} 处航段共占（走廊相交 ∩ 高度重合 ∩ "
                             f"时段正长度重叠），航次未{action}",
                    "coOccupancy": entries, "first": entries[0] if entries else None}), 409


# ---------------------------------------------------------------- 路由
@app.get("/")
def index():
    return send_from_directory("templates", "index.html")


@app.get("/api/state")
def get_state():
    db = get_db()
    row = db.execute("SELECT v FROM kv WHERE k='state'").fetchone()
    resp = jsonify(json.loads(row["v"]) if row else {"route": {}, "nfzs": []})
    resp.headers["X-Workspace-Rev"] = str(workspace_rev(db))
    return resp


@app.put("/api/state")
def put_state():
    state = request.get_json(force=True, silent=True)
    if not isinstance(state, dict):
        return jsonify({"ok": False, "error": "请求体必须是对象"}), 400
    # 可选乐观并发：携带 X-Expected-Workspace-Rev 时，修订号不符直接拒绝，
    # 不写库、不递增修订号（页面用它防止航线自动保存覆盖他页已核准的禁飞区）
    expected_raw = request.headers.get("X-Expected-Workspace-Rev")
    expected_rev = None
    if expected_raw is not None:
        try:
            expected_rev = int(expected_raw)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "reason": "missing_fields",
                            "error": "X-Expected-Workspace-Rev 必须是整数"}), 400
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        if expected_rev is not None:
            details = stale_workspace_details(db, expected_rev)
            if details:
                db.rollback()
                return jsonify({"ok": False, "reason": "stale",
                                "error": "工作区已在他处变更，请刷新后重试",
                                "details": details}), 409
        db.execute(
            "INSERT INTO kv(k,v) VALUES('state',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (json.dumps(state, ensure_ascii=False),),
        )
        bump_workspace_rev(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({"ok": True, "rev": workspace_rev(db)})


@app.post("/api/analyze")
def api_analyze():
    state = request.get_json(force=True, silent=True)
    if not isinstance(state, dict):
        return jsonify({"ok": False, "error": "请求体必须是对象"}), 400
    return jsonify(analyze(state))


@app.get("/api/versions")
def list_versions():
    rows = get_db().execute(
        "SELECT id,name,created_at FROM versions ORDER BY id DESC"
    ).fetchall()
    return jsonify([{"id": r["id"], "name": r["name"], "createdAt": r["created_at"]} for r in rows])


@app.post("/api/versions")
def create_version():
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    state = body.get("state")
    if not isinstance(state, dict):
        return jsonify({"ok": False, "error": "缺少 state"}), 400
    created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not name:
        name = f"版本 {created}"
    db = get_db()
    cur = db.execute(
        "INSERT INTO versions(name,created_at,payload) VALUES(?,?,?)",
        (name, created, json.dumps(state, ensure_ascii=False)),
    )
    db.commit()
    return jsonify({"ok": True, "id": cur.lastrowid, "name": name, "createdAt": created})


@app.get("/api/versions/<int:vid>")
def get_version(vid):
    row = get_db().execute(
        "SELECT name,created_at,payload FROM versions WHERE id=?", (vid,)
    ).fetchone()
    if row is None:
        return jsonify({"ok": False, "error": "版本不存在"}), 404
    return jsonify({"id": vid, "name": row["name"], "createdAt": row["created_at"],
                    "state": json.loads(row["payload"])})


@app.delete("/api/versions/<int:vid>")
def delete_version(vid):
    db = get_db()
    db.execute("DELETE FROM versions WHERE id=?", (vid,))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- 发布
@app.get("/api/release")
def get_release():
    """当前发布（最新一条）及当前工作区修订号/已发布号。"""
    db = get_db()
    row = db.execute("SELECT * FROM releases ORDER BY id DESC LIMIT 1").fetchone()
    return jsonify({
        "ok": True,
        "workspaceRev": workspace_rev(db),
        "releaseSeq": current_release_seq(db),
        "release": release_dict(row) if row else None,
    })


@app.post("/api/release")
def publish_release():
    """发布航线：取版本航线 + 当前工作区禁飞区，组合后重新审查。

    提交 {versionId, workspaceRev, releaseSeq}；全部检查在单个 IMMEDIATE
    事务内完成，任一不满足即整体回滚，原发布保持不变。
    """
    body = request.get_json(force=True, silent=True) or {}
    try:
        vid = int(body.get("versionId"))
        ws_rev = int(body.get("workspaceRev"))
        rel_seq = int(body.get("releaseSeq"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "missing_fields",
                        "error": "必须提交整数 versionId、workspaceRev、releaseSeq"}), 400

    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        vrow = db.execute(
            "SELECT name,payload FROM versions WHERE id=?", (vid,)
        ).fetchone()
        if vrow is None:
            db.rollback()
            return jsonify({"ok": False, "reason": "version_not_found",
                            "error": f"版本 {vid} 不存在或已删除"}), 404

        # 乐观并发：提交时读到的工作区修订号/已发布号须与当前一致
        details = stale_details(db, ws_rev, rel_seq)
        if details:
            db.rollback()
            return jsonify({"ok": False, "reason": "stale",
                            "error": "工作区或已发布号已变化，请刷新后重试",
                            "details": details}), 409

        vstate = json.loads(vrow["payload"])
        ws_row = db.execute("SELECT v FROM kv WHERE k='state'").fetchone()
        ws_state = json.loads(ws_row["v"]) if ws_row else {}
        combined = {"route": vstate.get("route") or {},
                    "nfzs": ws_state.get("nfzs") or []}

        if not route_has_nonzero_segment(combined["route"]):
            db.rollback()
            return jsonify({"ok": False, "reason": "no_nonzero_segment",
                            "error": "航线没有非零长度航段"}), 400

        result = analyze(combined)
        if result["errors"]:
            db.rollback()
            return jsonify({"ok": False, "reason": "analysis_errors",
                            "error": "审查存在错误，未发布",
                            "errors": result["errors"]}), 400
        if result["entries"]:
            db.rollback()
            return jsonify({"ok": False, "reason": "analysis_conflicts",
                            "error": f"审查发现 {len(result['entries'])} 处冲突，未发布",
                            "entries": result["entries"],
                            "first": result["first"]}), 409

        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = db.execute(
            "INSERT INTO releases(created_at,source_version_id,source_version_name,"
            "workspace_rev,state,analysis) VALUES(?,?,?,?,?,?)",
            (created, vid, vrow["name"], workspace_rev(db),
             json.dumps(combined, ensure_ascii=False),
             json.dumps(result, ensure_ascii=False)),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    row = db.execute("SELECT * FROM releases WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify({"ok": True, "release": release_dict(row)})


# ---------------------------------------------------------------- 航次登记
@app.get("/api/voyages")
def list_voyages():
    """全部航次（新登记在前）：航次编号、发布号、起飞时刻、航线快照与结束时刻。"""
    rows = get_db().execute(
        "SELECT * FROM voyages ORDER BY id DESC"
    ).fetchall()
    return jsonify({"ok": True, "voyages": [voyage_dict(r) for r in rows]})


@app.get("/api/voyages/<int:voyage_id>/occupancy")
def get_voyage_occupancy(voyage_id):
    """按航次自己保存的航线快照计算航段空域占用表。

    逐段给出扫掠走廊（GeoJSON）、端点高度范围与进入至离开时段；该时段内
    该航次视为占用整段走廊。未结束航次的占用仍生效；已结束航次仍可查询
    其历史占用表，但 open=false，不再阻拦任何登记/改期。旧发布航次始终
    按其自身快照计算，不受后续发布影响。
    """
    row = get_db().execute(
        "SELECT * FROM voyages WHERE id=?", (voyage_id,)
    ).fetchone()
    if row is None:
        return jsonify({"ok": False, "reason": "voyage_not_found",
                        "error": f"航次 {voyage_id} 不存在"}), 404
    route = json.loads(row["route_snapshot"])
    departure, segments, errors = build_voyage_segments(route)
    occupancy = voyage_occupancy_payload(
        row["id"], row["release_seq"], departure, segments, row["ended_at"])
    occupancy["errors"] = errors
    return jsonify({"ok": True, "occupancy": occupancy})


@app.post("/api/voyages")
def register_voyage():
    """按当前发布号登记一次航次执行。

    提交 {releaseSeq, departureTime}：以该发布快照中的航线替换起飞时刻后，
    与当前工作区已核准禁飞区重新审查；全部检查在单个 IMMEDIATE 事务内完成。
    无发布、发布号已变、时刻无效或审查报错/冲突时整体回滚，不留航次记录；
    禁飞区审查通过后还在同一写事务内按快照逐段生成占用，与其他未结束航次
    核对共占（走廊相交 ∩ 高度重合 ∩ 时段正长度交集，首尾相接允许），共占
    时附对方航次编号、双方航段与重叠时段整体拒绝。
    通过时原子落库航次编号、发布号、起飞时刻与航线快照（工作区与发布不变）。
    """
    body = request.get_json(force=True, silent=True) or {}
    try:
        rel_seq = int(body.get("releaseSeq"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "reason": "missing_fields",
                        "error": "必须提交整数 releaseSeq"}), 400
    dep_raw = body.get("departureTime")
    dep = parse_dt(dep_raw)
    if not isinstance(dep_raw, str) or dep is None:
        return jsonify({"ok": False, "reason": "invalid_departure_time",
                        "error": "起飞时刻缺失或格式错误"}), 400

    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    if on_write_begin is not None:
        on_write_begin("voyage_register")
    try:
        cur_seq = current_release_seq(db)
        if cur_seq == 0:
            db.rollback()
            return jsonify({"ok": False, "reason": "no_release",
                            "error": "尚无已发布航线，无法登记航次"}), 409
        if rel_seq != cur_seq:
            db.rollback()
            return jsonify({"ok": False, "reason": "stale",
                            "error": "已发布号已变化，请刷新后重试",
                            "details": {"releaseSeq": {"expected": rel_seq, "actual": cur_seq}}}), 409

        rel_row = db.execute(
            "SELECT state FROM releases WHERE id=?", (cur_seq,)
        ).fetchone()
        released = json.loads(rel_row["state"])
        route = json.loads(json.dumps(released.get("route") or {}, ensure_ascii=False))
        route["departureTime"] = fmt_dt(dep)
        ws_row = db.execute("SELECT v FROM kv WHERE k='state'").fetchone()
        ws_state = json.loads(ws_row["v"]) if ws_row else {}
        combined = {"route": route, "nfzs": ws_state.get("nfzs") or []}

        result = analyze(combined)
        if result["errors"]:
            db.rollback()
            return jsonify({"ok": False, "reason": "analysis_errors",
                            "error": "审查存在错误，航次未登记",
                            "errors": result["errors"]}), 400
        if result["entries"]:
            db.rollback()
            return jsonify({"ok": False, "reason": "analysis_conflicts",
                            "error": f"审查发现 {len(result['entries'])} 处冲突，航次未登记",
                            "entries": result["entries"],
                            "first": result["first"]}), 409

        # 原禁飞区审查通过后，在【同一写事务】内按候选航线快照逐段生成占用，
        # 与其他未结束航次核对共占；命中即整次拒绝、不留航次记录。
        # BEGIN IMMEDIATE 串行化全部写事务，并发登记不会漏检对方。
        _, cand_segments, cand_errors = build_voyage_segments(route)
        if cand_errors:
            db.rollback()
            return jsonify({"ok": False, "reason": "analysis_errors",
                            "error": "航段空域占用计算存在错误，航次未登记",
                            "errors": cand_errors}), 400
        candidate = {"id": None, "release_seq": cur_seq,
                     "departure": fmt_dt(dep), "segments": cand_segments}
        co_entries, co_errors = co_occupancy_against_open_voyages(db, candidate)
        if co_errors:
            db.rollback()
            return jsonify({"ok": False, "reason": "analysis_errors",
                            "error": "其他未结束航次占用复核存在错误，航次未登记",
                            "voyageErrors": co_errors}), 400
        if co_entries:
            db.rollback()
            return reject_co_occupancy(co_entries, "登记")

        created = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur = db.execute(
            "INSERT INTO voyages(release_seq,created_at,departure_time,route_snapshot)"
            " VALUES(?,?,?,?)",
            (cur_seq, created, fmt_dt(dep),
             json.dumps(route, ensure_ascii=False)),
        )
        if on_write_commit is not None:
            on_write_commit("voyage_register")
        db.commit()
    except Exception:
        db.rollback()
        raise
    row = db.execute("SELECT * FROM voyages WHERE id=?", (cur.lastrowid,)).fetchone()
    return jsonify({"ok": True, "voyage": voyage_dict(row)})


@app.post("/api/voyages/<int:voyage_id>/reschedule")
def reschedule_voyage(voyage_id):
    """航次改期：保留航次编号、登记时间与原发布号，仅替换该航次保存的
    航线快照中的起飞时刻。

    提交 {departureTime, originalDepartureTime}：originalDepartureTime 为
    页面所见原时刻，用于乐观并发——与库中保存值不一致即拒绝。全部检查在
    单个 IMMEDIATE 事务内完成：航次不存在/已结束、原时刻已被他页改动、
    新时刻无效、审查报错或冲突时整体回滚，原时刻与快照不变；通过时原子
    更新同一航次的起飞时刻与航线快照。审查用当前工作区已核准禁飞区；
    即使已有新发布，也沿用该航次自己保存的航线几何（快照仅换起飞时刻）。
    改期与结束/核准交错时，SQLite 写事务串行使核准始终读到最终生效的
    未结束航次，不漏检。
    """
    body = request.get_json(force=True, silent=True) or {}
    if "originalDepartureTime" not in body:
        return jsonify({"ok": False, "reason": "missing_fields",
                        "error": "必须提交所见原时刻 originalDepartureTime"}), 400
    orig_raw = body.get("originalDepartureTime")
    dep_raw = body.get("departureTime")
    dep = parse_dt(dep_raw)
    if not isinstance(dep_raw, str) or dep is None:
        return jsonify({"ok": False, "reason": "invalid_departure_time",
                        "error": "新起飞时刻缺失或格式错误"}), 400
    orig = parse_dt(orig_raw) if isinstance(orig_raw, str) else None

    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    if on_write_begin is not None:
        on_write_begin("voyage_reschedule")
    try:
        row = db.execute("SELECT * FROM voyages WHERE id=?", (voyage_id,)).fetchone()
        if row is None:
            db.rollback()
            return jsonify({"ok": False, "reason": "voyage_not_found",
                            "error": f"航次 {voyage_id} 不存在"}), 404
        if row["ended_at"] is not None:
            db.rollback()
            return jsonify({"ok": False, "reason": "voyage_ended",
                            "error": f"航次 {voyage_id} 已结束，不能改期"}), 409
        # 乐观并发：所见原时刻须与库中保存的一致（无法解析的原时刻视同不一致）
        stored = row["departure_time"]
        if orig is None or fmt_dt(orig) != stored:
            db.rollback()
            return jsonify({"ok": False, "reason": "stale",
                            "error": "原起飞时刻已在他处变更，请刷新后重试",
                            "details": {"departureTime": {
                                "expected": orig_raw, "actual": stored}}}), 409

        # 该航次自己保存的航线快照（几何/速度/走廊等不动），仅替换起飞时刻
        route = json.loads(row["route_snapshot"])
        route["departureTime"] = fmt_dt(dep)
        ws_row = db.execute("SELECT v FROM kv WHERE k='state'").fetchone()
        ws_state = json.loads(ws_row["v"]) if ws_row else {}
        combined = {"route": route, "nfzs": ws_state.get("nfzs") or []}

        result = analyze(combined)
        if result["errors"]:
            db.rollback()
            return jsonify({"ok": False, "reason": "analysis_errors",
                            "error": "审查存在错误，航次未改期",
                            "errors": result["errors"]}), 400
        if result["entries"]:
            db.rollback()
            return jsonify({"ok": False, "reason": "analysis_conflicts",
                            "error": f"审查发现 {len(result['entries'])} 处冲突，航次未改期",
                            "entries": result["entries"],
                            "first": result["first"]}), 409

        # 禁飞区审查通过后，在同一写事务内与【其他】未结束航次核对共占
        # （skip 自身：改期不与自己的历史占用比较）；命中即整体拒绝，
        # 原时刻与快照不变。
        _, cand_segments, cand_errors = build_voyage_segments(route)
        if cand_errors:
            db.rollback()
            return jsonify({"ok": False, "reason": "analysis_errors",
                            "error": "航段空域占用计算存在错误，航次未改期",
                            "errors": cand_errors}), 400
        candidate = {"id": voyage_id, "release_seq": row["release_seq"],
                     "departure": fmt_dt(dep), "segments": cand_segments}
        co_entries, co_errors = co_occupancy_against_open_voyages(
            db, candidate, skip_voyage_id=voyage_id)
        if co_errors:
            db.rollback()
            return jsonify({"ok": False, "reason": "analysis_errors",
                            "error": "其他未结束航次占用复核存在错误，航次未改期",
                            "voyageErrors": co_errors}), 400
        if co_entries:
            db.rollback()
            return reject_co_occupancy(co_entries, "改期")

        db.execute(
            "UPDATE voyages SET departure_time=?, route_snapshot=? WHERE id=?",
            (fmt_dt(dep), json.dumps(route, ensure_ascii=False), voyage_id),
        )
        if on_write_commit is not None:
            on_write_commit("voyage_reschedule")
        db.commit()
    except Exception:
        db.rollback()
        raise
    row = db.execute("SELECT * FROM voyages WHERE id=?", (voyage_id,)).fetchone()
    return jsonify({"ok": True, "voyage": voyage_dict(row)})


@app.post("/api/voyages/<int:voyage_id>/end")
def end_voyage(voyage_id):
    """结束航次：幂等标记 ended_at；结束后该航次不再阻拦禁飞区核准。"""
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    if on_write_begin is not None:
        on_write_begin("voyage_end")
    try:
        row = db.execute("SELECT * FROM voyages WHERE id=?", (voyage_id,)).fetchone()
        if row is None:
            db.rollback()
            return jsonify({"ok": False, "reason": "voyage_not_found",
                            "error": f"航次 {voyage_id} 不存在"}), 404
        if row["ended_at"] is None:
            db.execute(
                "UPDATE voyages SET ended_at=? WHERE id=?",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), voyage_id),
            )
        if on_write_commit is not None:
            on_write_commit("voyage_end")
        db.commit()
    except Exception:
        db.rollback()
        raise
    row = db.execute("SELECT * FROM voyages WHERE id=?", (voyage_id,)).fetchone()
    return jsonify({"ok": True, "voyage": voyage_dict(row)})


# ---------------------------------------------------------------- 禁飞区变更核准
@app.post("/api/nfz-approval")
def submit_nfz_approval():
    """已发布航线的禁飞区变更核准。

    提交 {nfzs, workspaceRev, releaseSeq}：把候选禁飞区与最新发布快照中的
    航线组合审查，全部检查在单个 IMMEDIATE 事务内完成。审查报错、有冲突
    或任一号已变化时整体回滚，工作区与已发布内容均不变（草稿可修改后
    重新提交）；通过时仅原子替换工作区禁飞区并递增修订号，最新工作区
    航线与发布快照保持不动。
    """
    body = request.get_json(force=True, silent=True) or {}
    nfzs = body.get("nfzs")
    try:
        ws_rev = int(body.get("workspaceRev"))
        rel_seq = int(body.get("releaseSeq"))
    except (TypeError, ValueError):
        ws_rev = rel_seq = None
    if not isinstance(nfzs, list) or ws_rev is None or rel_seq is None:
        return jsonify({"ok": False, "reason": "missing_fields",
                        "error": "必须提交禁飞区列表 nfzs 与整数 workspaceRev、releaseSeq"}), 400

    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        # 乐观并发：提交时读到的工作区修订号/已发布号须与当前一致
        details = stale_details(db, ws_rev, rel_seq)
        if details:
            db.rollback()
            return jsonify({"ok": False, "reason": "stale",
                            "error": "工作区或已发布号已变化，请刷新后重试",
                            "details": details}), 409

        rel_row = db.execute(
            "SELECT state FROM releases ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if rel_row is None:
            db.rollback()
            return jsonify({"ok": False, "reason": "no_release",
                            "error": "尚无已发布航线，禁飞区随工作区自动保存即可"}), 409

        # 候选禁飞区 ∩ 最新发布快照中的航线，组合审查
        released = json.loads(rel_row["state"])
        combined = {"route": released.get("route") or {}, "nfzs": nfzs}
        result = analyze(combined)
        if result["errors"]:
            db.rollback()
            return jsonify({"ok": False, "reason": "analysis_errors",
                            "error": "审查存在错误，核准未通过",
                            "errors": result["errors"]}), 400
        if result["entries"]:
            db.rollback()
            return jsonify({"ok": False, "reason": "analysis_conflicts",
                            "error": f"审查发现 {len(result['entries'])} 处冲突，核准未通过",
                            "entries": result["entries"],
                            "first": result["first"]}), 409

        # 候选禁飞区还须复核所有未结束航次（含旧发布快照登记的航次）：
        # 任一冲突即驳回并附航次及冲突明细，草稿/工作区/发布快照均不变。
        # 航次航线快照登记时已验证可审查，理论上不会再出 errors；
        # 若有则按审查错误驳回，绝不漏检。
        voyage_conflicts = []
        voyage_errors = []
        vrows = db.execute(
            "SELECT * FROM voyages WHERE ended_at IS NULL ORDER BY id"
        ).fetchall()
        for vrow in vrows:
            vroute = json.loads(vrow["route_snapshot"])
            vresult = analyze({"route": vroute, "nfzs": nfzs})
            if vresult["errors"]:
                voyage_errors.append({"voyageId": vrow["id"],
                                      "releaseSeq": vrow["release_seq"],
                                      "errors": vresult["errors"]})
            if vresult["entries"]:
                voyage_conflicts.append({
                    "voyageId": vrow["id"],
                    "releaseSeq": vrow["release_seq"],
                    "departureTime": vrow["departure_time"],
                    "entries": vresult["entries"],
                    "first": vresult["first"],
                })
        if voyage_errors:
            db.rollback()
            return jsonify({"ok": False, "reason": "analysis_errors",
                            "error": "未结束航次复核存在错误，核准未通过",
                            "voyageErrors": voyage_errors}), 400
        if voyage_conflicts:
            db.rollback()
            total = sum(len(vc["entries"]) for vc in voyage_conflicts)
            return jsonify({"ok": False, "reason": "voyage_conflicts",
                            "error": f"候选禁飞区与 {len(voyage_conflicts)} 个未结束航次"
                                     f"共 {total} 处冲突，核准未通过",
                            "voyageConflicts": voyage_conflicts}), 409

        # 通过：仅替换工作区禁飞区（保留最新工作区航线），修订号 +1
        ws_row = db.execute("SELECT v FROM kv WHERE k='state'").fetchone()
        ws_state = json.loads(ws_row["v"]) if ws_row else {}
        ws_state["nfzs"] = nfzs
        db.execute(
            "INSERT INTO kv(k,v) VALUES('state',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (json.dumps(ws_state, ensure_ascii=False),),
        )
        bump_workspace_rev(db)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return jsonify({"ok": True, "rev": workspace_rev(db),
                    "releaseSeq": current_release_seq(db)})


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
