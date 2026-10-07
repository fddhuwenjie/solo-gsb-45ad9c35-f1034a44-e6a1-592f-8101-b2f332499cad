# -*- coding: utf-8 -*-
"""未结束航次的航段空域占用与共占核对回归测试。

占用表（GET /api/voyages/<id>/occupancy）：
- 按各航次自己保存的航线快照逐段给出扫掠走廊（GeoJSON）、端点高度范围、
  进入至离开时段；该时段内视为占用整段走廊；
- 未结束航次 open=true，已结束航次仍可查历史占用表但 open=false；
- 旧发布航次始终按自身快照计算，后续再发布不影响其占用；
- 航次不存在 → 404 voyage_not_found。

共占（登记 / 改期在原禁飞区审查通过后的同一写事务内核对）：
- 两段走廊相交 ∩ 端点高度重合 ∩ 占用时段【正长度】交集 = 共占，409
  voyage_co_occupancy，附对方航次编号、双方航段与重叠时段，整次拒绝、
  原记录不变；首尾相接（区间端点相切）允许；
- 走廊不相交 / 高度不重合 / 时间不重叠或仅相接 → 不构成共占；
- 多航段跨段共占逐对报告；改期跳过自身；结束航次即释放占用；
- 禁飞区冲突优先于共占（原审查先执行）；
- 并发登记 / 改期 / 结束在 BEGIN IMMEDIATE 串行写事务下不漏检。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

# 4km 直线（y=100），24 km/h → 每段恰好 10 分钟；200m 等高
ROUTE = {
    "departureTime": "2026-09-24T08:00",
    "unitScale": 100,
    "corridorHalfWidth": 0.3,
    "speeds": [24],
    "waypoints": [
        {"id": "w1", "name": "A", "x": 100, "y": 100, "alt": 200},
        {"id": "w2", "name": "B", "x": 500, "y": 100, "alt": 200},
    ],
}
# 爬升 200→300m 的同一条走廊
ROUTE_CLIMB = json.loads(json.dumps(ROUTE))
ROUTE_CLIMB["waypoints"][1]["alt"] = 300
# 走廊在 y=400，平面相距 300（走廊半宽仅 0.3km=30 画布单位）→ 永不相交
ROUTE_Y400 = json.loads(json.dumps(ROUTE))
ROUTE_Y400["waypoints"][0]["y"] = 400
ROUTE_Y400["waypoints"][1]["y"] = 400
# 同走廊但高度 400→500m，与 200m 等高航线高度不重合
ROUTE_ALT400 = json.loads(json.dumps(ROUTE))
for wp in ROUTE_ALT400["waypoints"]:
    wp["alt"] = 400
# 同走廊、300m 等高：与 ROUTE_CLIMB（200→300）仅在端点高度 300 点重合
ROUTE_ALT300 = json.loads(json.dumps(ROUTE))
for wp in ROUTE_ALT300["waypoints"]:
    wp["alt"] = 300
# 双段平行航线：A(100,100)→B(500,100)→C(900,100)，两段同为 y=100 的横线；
# 24km/h → 每段 10 分钟：段0 [08:00,08:10]，段1 [08:10,08:20]
ROUTE_TWO_SEG = {
    "departureTime": "2026-09-24T08:00",
    "unitScale": 100,
    "corridorHalfWidth": 0.3,
    "speeds": [24, 24],
    "waypoints": [
        {"id": "w1", "name": "A", "x": 100, "y": 100, "alt": 200},
        {"id": "w2", "name": "B", "x": 500, "y": 100, "alt": 200},
        {"id": "w3", "name": "C", "x": 900, "y": 100, "alt": 200},
    ],
}
# 同样双段横线，但整体向 +y 平移 40 画布单位：走廊半宽各 30 → 走廊仍相交，
# 且两段一一对应（段0 对段0、段1 对段1），段0 与段1 沿航段方向不重叠
ROUTE_TWO_SEG_OFFSET = json.loads(json.dumps(ROUTE_TWO_SEG))
for wp in ROUTE_TWO_SEG_OFFSET["waypoints"]:
    wp["y"] = 140
# 单段横线，覆盖双段航线的段1 远端、与段0 平面不相交（x≥560，段0 缓冲止于 530）
ROUTE_OVER_SEG1 = {
    "departureTime": "2026-09-24T08:00",
    "unitScale": 100,
    "corridorHalfWidth": 0.3,
    "speeds": [24],
    "waypoints": [
        {"id": "w1", "name": "E", "x": 560, "y": 140, "alt": 200},
        {"id": "w2", "name": "F", "x": 900, "y": 140, "alt": 200},
    ],
}
# 覆盖航线中段（y=100）、时窗 07:00–09:00 的禁飞区
BLOCK_NFZ = {
    "id": "nz1", "name": "阻断区", "enabled": True,
    "vertices": [[250, 50], [350, 50], [350, 150], [250, 150]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "2026-09-24T07:00", "endTime": "2026-09-24T09:00",
}
# 同位置、全天生效：任何时刻起飞都冲突
BLOCK_ALLDAY = dict(BLOCK_NFZ, startTime="", endTime="")


def clone(obj):
    return json.loads(json.dumps(obj))


def open_voyage_pairs_cooccupy(voyages):
    """用生产逻辑对当前所有未结束航次两两核对共占，返回命中对数（测试不变量）。"""
    built = []
    for v in voyages:
        if not v["open"]:
            continue
        dep, segs, errors = app.build_voyage_segments(v["routeSnapshot"])
        if errors:
            continue
        built.append({"id": v["id"], "release_seq": v["releaseSeq"],
                      "departure": dep, "segments": segs})
    hits = 0
    for i, a in enumerate(built):
        for b in built[i + 1:]:
            if app._co_occupy_entries(a, b) or app._co_occupy_entries(b, a):
                hits += 1
    return hits


class VoyageOccupancyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_path = app.DB_PATH
        app.DB_PATH = os.path.join(self.tmp, "test.db")
        app.init_db()
        self.client = app.app.test_client()

    def tearDown(self):
        app.DB_PATH = self.old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    # —— 辅助 ——
    def put_state(self, state):
        r = self.client.put("/api/state", json=state)
        self.assertEqual(r.status_code, 200)
        return r.get_json()["rev"]

    def save_version(self, state, name="v1"):
        r = self.client.post("/api/versions", json={"name": name, "state": state})
        self.assertEqual(r.status_code, 200)
        return r.get_json()["id"]

    def publish_payload(self, route, nfzs=None, seq=0):
        rev = self.put_state({"route": {"waypoints": []}, "nfzs": nfzs or []})
        vid = self.save_version({"route": route, "nfzs": []})
        r = self.client.post("/api/release", json={
            "versionId": vid, "workspaceRev": rev, "releaseSeq": seq})
        self.assertEqual(r.status_code, 200, r.get_json())
        return rev, r.get_json()["release"]["seq"]

    def register(self, seq, dep):
        return self.client.post("/api/voyages", json={
            "releaseSeq": seq, "departureTime": dep})

    def reschedule(self, vid, dep, orig):
        return self.client.post(f"/api/voyages/{vid}/reschedule", json={
            "departureTime": dep, "originalDepartureTime": orig})

    def end_voyage(self, vid):
        return self.client.post(f"/api/voyages/{vid}/end")

    def occupancy(self, vid):
        return self.client.get(f"/api/voyages/{vid}/occupancy")

    def list_voyages(self):
        return self.client.get("/api/voyages").get_json()["voyages"]

    def approve(self, nfzs, rev, seq):
        return self.client.post("/api/nfz-approval", json={
            "nfzs": nfzs, "workspaceRev": rev, "releaseSeq": seq})

    def assert_no_open_cooccupancy(self):
        self.assertEqual(open_voyage_pairs_cooccupy(self.list_voyages()), 0)

    # —— 占用表 ——
    def test_occupancy_table_from_saved_snapshot(self):
        self.publish_payload(ROUTE)
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        r = self.occupancy(1)
        self.assertEqual(r.status_code, 200, r.get_json())
        occ = r.get_json()["occupancy"]
        self.assertEqual(occ["voyageId"], 1)
        self.assertEqual(occ["releaseSeq"], 1)
        self.assertEqual(occ["departureTime"], "2026-09-24T08:00")
        self.assertTrue(occ["open"])
        self.assertIsNone(occ["endedAt"])
        self.assertEqual(occ["errors"], [])
        self.assertEqual(len(occ["segments"]), 1)
        s = occ["segments"][0]
        self.assertEqual(s["segIndex"], 0)
        self.assertEqual(s["distanceKm"], 4.0)
        self.assertEqual(s["altLow"], 200.0)
        self.assertEqual(s["altHigh"], 200.0)
        self.assertEqual(s["enterTime"], "2026-09-24T08:00")
        self.assertEqual(s["exitTime"], "2026-09-24T08:10")
        self.assertEqual(s["durationMinutes"], 10.0)
        gj = s["corridor"]
        self.assertEqual(gj["type"], "Polygon")
        ring = gj["coordinates"][0]
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        # 走廊半宽 0.3km = 30 画布单位：缓冲外包在中心线两侧各 30
        self.assertAlmostEqual(min(xs), 70.0, delta=0.5)
        self.assertAlmostEqual(max(xs), 530.0, delta=0.5)
        self.assertAlmostEqual(min(ys), 70.0, delta=0.5)
        self.assertAlmostEqual(max(ys), 130.0, delta=0.5)

    def test_occupancy_two_segments_times_are_end_to_end(self):
        self.publish_payload(ROUTE_TWO_SEG)
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        occ = self.occupancy(1).get_json()["occupancy"]
        self.assertEqual([s["segIndex"] for s in occ["segments"]], [0, 1])
        s0, s1 = occ["segments"]
        self.assertEqual((s0["enterTime"], s0["exitTime"]),
                         ("2026-09-24T08:00", "2026-09-24T08:10"))
        self.assertEqual((s1["enterTime"], s1["exitTime"]),
                         ("2026-09-24T08:10", "2026-09-24T08:20"))
        # 爬升段的端点高度范围按 min/max（发布为 #2 后用不重叠时刻登记，
        # 避免与航次 #1 共占：#1 占用 08:00–08:20，新航次 09:00 起飞）
        self.publish_payload(ROUTE_CLIMB, seq=1)
        self.assertEqual(self.register(2, "2026-09-24T09:00").status_code, 200)
        occ2 = self.occupancy(2).get_json()["occupancy"]
        self.assertEqual((occ2["segments"][0]["altLow"],
                          occ2["segments"][0]["altHigh"]), (200.0, 300.0))

    def test_occupancy_after_end_is_historical_open_false(self):
        self.publish_payload(ROUTE)
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        self.assertEqual(self.end_voyage(1).status_code, 200)
        occ = self.occupancy(1).get_json()["occupancy"]
        self.assertFalse(occ["open"])
        self.assertIsNotNone(occ["endedAt"])
        # 历史占用表仍按自身快照完整给出
        self.assertEqual(len(occ["segments"]), 1)
        self.assertEqual(occ["segments"][0]["enterTime"], "2026-09-24T08:00")

    def test_occupancy_of_old_release_keeps_its_own_snapshot(self):
        rev, seq = self.publish_payload(ROUTE)
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        # 再发布 #2（y=400 几何）：旧航次 #1 的占用表仍按发布 #1 快照
        self.publish_payload(ROUTE_Y400, seq=1)
        occ = self.occupancy(1).get_json()["occupancy"]
        self.assertEqual(occ["releaseSeq"], 1)
        ys = [p[1] for p in occ["segments"][0]["corridor"]["coordinates"][0]]
        self.assertAlmostEqual(min(ys), 70.0, delta=0.5)  # 中心线仍是 y=100
        self.assertEqual(self.client.get("/api/release").get_json()["releaseSeq"], 2)

    def test_occupancy_404(self):
        r = self.occupancy(999)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["reason"], "voyage_not_found")

    # —— 共占：登记 ——
    def test_register_overlapping_same_corridor_rejected_with_details(self):
        self.publish_payload(ROUTE)
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        r = self.register(1, "2026-09-24T08:05")  # 08:05–08:15 与 08:00–08:10 重叠 5 分钟
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "voyage_co_occupancy")
        cos = body["coOccupancy"]
        self.assertEqual(len(cos), 1)
        co = cos[0]
        self.assertEqual(co["voyageId"], 1)          # 对方航次编号
        self.assertEqual(co["releaseSeq"], 1)
        self.assertEqual(co["segIndex"], 0)          # 双方航段
        self.assertEqual(co["otherSegIndex"], 0)
        self.assertEqual(co["startTime"], "2026-09-24T08:05")
        self.assertEqual(co["endTime"], "2026-09-24T08:10")
        self.assertEqual(co["durationMinutes"], 5.0)
        self.assertEqual((co["altLow"], co["altHigh"]), (200.0, 200.0))
        self.assertEqual(body["first"], cos[0])
        # 整次拒绝、原记录不变：仍只有航次 #1
        self.assertEqual([v["id"] for v in self.list_voyages()], [1])
        self.assert_no_open_cooccupancy()

    def test_register_head_to_tail_allowed(self):
        self.publish_payload(ROUTE)
        # v1 占用 08:00–08:10；v2 恰在 08:10 进入：正长度交集为 0，允许
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        r = self.register(1, "2026-09-24T08:10")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual([v["id"] for v in self.list_voyages()], [2, 1])
        self.assert_no_open_cooccupancy()
        # 时间完全不重叠（更早）也允许
        r = self.register(1, "2026-09-24T07:00")  # 07:00–07:10
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_register_disjoint_geometry_no_cooccupancy(self):
        self.publish_payload(ROUTE)
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        # 发布 y=400 的航线为发布 #2，同一时刻登记：平面走廊不相交
        self.publish_payload(ROUTE_Y400, seq=1)
        r = self.register(2, "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assert_no_open_cooccupancy()

    def test_register_disjoint_altitude_no_cooccupancy(self):
        self.publish_payload(ROUTE)
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        # 同走廊、400m 等高的发布 #2：端点高度范围 [400,500] 与 [200,200] 不重合
        self.publish_payload(ROUTE_ALT400, seq=1)
        r = self.register(2, "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assert_no_open_cooccupancy()

    def test_register_altitude_point_overlap_with_time_overlap_is_cooccupancy(self):
        self.publish_payload(ROUTE_CLIMB)
        # v1 爬升 200→300，08:00–08:10；v2 恒 300，08:05 起：
        # 高度范围 [200,300]∩[300,300]={300}（端点擦碰），时段正长度重叠 → 共占
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        self.publish_payload(ROUTE_ALT300, seq=1)
        r = self.register(2, "2026-09-24T08:05")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "voyage_co_occupancy")
        co = r.get_json()["coOccupancy"][0]
        self.assertEqual((co["altLow"], co["altHigh"]), (300.0, 300.0))
        # 但首尾相接（v2 08:10 进入）时即便高度点重合也放行（无正长度交集）
        r = self.register(2, "2026-09-24T08:10")
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_register_reports_both_conflicting_open_voyages(self):
        # v1、v2 前后相邻占用同一走廊：08:00–08:10 与 08:10–08:20
        self.publish_payload(ROUTE)
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        self.assertEqual(self.register(1, "2026-09-24T08:10").status_code, 200)
        # v3 于 08:05 起飞：与 v1 重叠 [08:05,08:10]、与 v2 重叠 [08:10,08:15]
        r = self.register(1, "2026-09-24T08:05")
        self.assertEqual(r.status_code, 409)
        cos = r.get_json()["coOccupancy"]
        by_other = {co["voyageId"]: co for co in cos}
        self.assertEqual(sorted(by_other), [1, 2])
        self.assertEqual((by_other[1]["startTime"], by_other[1]["endTime"]),
                         ("2026-09-24T08:05", "2026-09-24T08:10"))
        self.assertEqual((by_other[2]["startTime"], by_other[2]["endTime"]),
                         ("2026-09-24T08:10", "2026-09-24T08:15"))
        # 按重叠开始时刻排序：与 v1 的更早，排在首位
        self.assertEqual(cos[0]["voyageId"], 1)
        self.assertEqual([v["id"] for v in self.list_voyages()], [2, 1])

    def test_register_multi_segment_cross_pairs(self):
        # v1 为双段直航线（段0、段1 首尾相接，共享 B 点的圆端帽缓冲），
        # 占用时段段0 08:00–08:10、段1 08:10–08:20；候选为同一几何、08:05
        # 起飞（08:05–08:25）。整段走廊在各自时段内全程占用，因此：
        # 段0↔段0（08:05–08:10）、段1↔段1（08:15–08:20）各有正长度重叠；
        # 段1（v1）走廊含 B 端帽，候选段0 在 08:05–08:10 内仍覆盖 B 附近
        # → 段0↔段1 也在 08:10 之前的正长度时段内共占；反向的段1↔段0 则因
        # v1 段0 在候选段1 开始（08:15）前已离开而不命中。
        self.publish_payload(ROUTE_TWO_SEG)
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        self.publish_payload(ROUTE_TWO_SEG, seq=1)
        r = self.register(2, "2026-09-24T08:05")
        self.assertEqual(r.status_code, 409)
        cos = r.get_json()["coOccupancy"]
        pairs = sorted((co["segIndex"], co["otherSegIndex"]) for co in cos)
        self.assertEqual(pairs, [(0, 0), (0, 1), (1, 1)])

    def test_register_single_segment_hits_only_one_other_segment(self):
        # 跨段定向命中：候选单段走廊只贴近双段航线的段1（x≥560、y=140），
        # 与段0 平面不相交。候选 08:12–08:20.5 与段1 的 08:10–08:20 正长度
        # 重叠 → 恰好一条 (segIndex=0, otherSegIndex=1)。
        self.publish_payload(ROUTE_TWO_SEG)
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        self.publish_payload(ROUTE_OVER_SEG1, seq=1)
        r = self.register(2, "2026-09-24T08:12")
        self.assertEqual(r.status_code, 409)
        cos = r.get_json()["coOccupancy"]
        self.assertEqual([(c["segIndex"], c["otherSegIndex"]) for c in cos], [(0, 1)])
        # 同一几何推迟到 08:20 进入：与段1 首尾相接 → 放行
        r = self.register(2, "2026-09-24T08:20")
        self.assertEqual(r.status_code, 200, r.get_json())

    # —— 共占：改期 ——
    def test_reschedule_into_overlap_rejected_original_unchanged(self):
        self.publish_payload(ROUTE)
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        self.assertEqual(self.register(1, "2026-09-24T09:00").status_code, 200)
        r = self.reschedule(2, "2026-09-24T08:05", "2026-09-24T09:00")
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "voyage_co_occupancy")
        co = body["coOccupancy"][0]
        self.assertEqual(co["voyageId"], 1)
        self.assertEqual(co["segIndex"], 0)
        self.assertEqual(co["otherSegIndex"], 0)
        # 原记录不变
        v2 = [v for v in self.list_voyages() if v["id"] == 2][0]
        self.assertEqual(v2["departureTime"], "2026-09-24T09:00")
        self.assertEqual(v2["routeSnapshot"]["departureTime"], "2026-09-24T09:00")
        # 改到首尾相接 → 通过
        r = self.reschedule(2, "2026-09-24T08:10", "2026-09-24T09:00")
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_reschedule_skips_self(self):
        self.publish_payload(ROUTE_TWO_SEG)
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        # 改期到任何时刻都只与“其他”未结束航次核对；库里只有自己 → 通过
        r = self.reschedule(1, "2026-09-24T08:05", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.reschedule(1, "2026-09-24T08:00", "2026-09-24T08:05")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(len(self.list_voyages()), 1)

    # —— 结束即释放 ——
    def test_ending_voyage_frees_its_corridor(self):
        self.publish_payload(ROUTE)
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        r = self.register(1, "2026-09-24T08:05")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "voyage_co_occupancy")
        self.assertEqual(self.end_voyage(1).status_code, 200)
        r = self.register(1, "2026-09-24T08:05")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assert_no_open_cooccupancy()

    # —— 原禁飞区审查优先 ——
    def test_nfz_conflict_is_reported_before_cooccupancy(self):
        # 原禁飞区审查在同一事务内先于共占核对：工作区直接落入全天阻断区后，
        # 08:05 的登记既与禁飞区冲突、也与 08:00 的 v1 共占——必须先报
        # analysis_conflicts，且整次拒绝不留记录。
        self.publish_payload(ROUTE)
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        self.put_state({"route": {"waypoints": []}, "nfzs": [clone(BLOCK_ALLDAY)]})
        r = self.register(1, "2026-09-24T08:05")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "analysis_conflicts")
        self.assertEqual([v["id"] for v in self.list_voyages()], [1])

    # —— 并发：登记 / 改期 / 结束不漏检 ——
    # 各并发用例使用互不重叠的日期，避免跨用例的已登记航次互相共占
    def test_concurrent_registrations_no_missed_cooccupancy(self):
        self.publish_payload(ROUTE)
        for round_idx in range(8):
            # 两个不同线程在同一时刻登记同一走廊：恰好一个成功。
            # 每轮换一天，避免与已成功登记的其他时刻产生共占
            dep = f"2026-10-{5 + round_idx:02d}T08:00"
            results = []

            def one_register():
                cl = app.app.test_client()
                barrier.wait()
                r = cl.post("/api/voyages", json={
                    "releaseSeq": 1, "departureTime": dep})
                results.append((r.status_code, r.get_json().get("reason")))

            barrier = threading.Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as ex:
                futs = [ex.submit(one_register), ex.submit(one_register)]
                for f in futs:
                    f.result(timeout=15)
            oks = [x for x in results if x[0] == 200]
            rejected = [x for x in results if x[0] == 409]
            self.assertEqual(len(oks), 1, (round_idx, results))
            self.assertEqual(len(rejected), 1, (round_idx, results))
            self.assertEqual(rejected[0][1], "voyage_co_occupancy")
            self.assert_no_open_cooccupancy()
        # 加上一批互不重叠的时刻登记（另一批日期，每天一个），全部成功
        for day in range(1, 6):
            r = self.register(1, f"2026-11-{day:02d}T08:00")
            self.assertEqual(r.status_code, 200, (day, r.get_json()))
        self.assert_no_open_cooccupancy()

    def test_concurrent_register_racing_with_end(self):
        self.publish_payload(ROUTE)
        for round_idx in range(8):
            dep0 = f"2026-10-{15 + round_idx:02d}T08:00"
            holder = self.register(1, dep0)
            self.assertEqual(holder.status_code, 200, holder.get_json())
            holder_id = holder.get_json()["voyage"]["id"]
            results = {}

            def end_first():
                cl = app.app.test_client()
                barrier.wait()
                r = cl.post(f"/api/voyages/{holder_id}/end")
                results["end"] = r.status_code

            def register_same_time():
                cl = app.app.test_client()
                barrier.wait()
                r = cl.post("/api/voyages", json={
                    "releaseSeq": 1, "departureTime": dep0})
                results["register"] = (r.status_code,
                                       r.get_json().get("reason"))

            barrier = threading.Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as ex:
                f1 = ex.submit(end_first)
                f2 = ex.submit(register_same_time)
                f1.result(timeout=15)
                f2.result(timeout=15)
            self.assertEqual(results["end"], 200)
            reg = results["register"]
            # 两种串行次序都合法：结束先提交 → 登记成功；登记先提交 → 共占拒绝。
            # 无论哪种，库里最终都不存在共占的未结束航次对。
            self.assertIn(reg, [(200, None), (409, "voyage_co_occupancy")], results)
            self.assert_no_open_cooccupancy()

    def _run_race(self, order, month, day):
        """确定性地排列“结束锚点航次”与“把另一航次改期进锚点占用窗”的
        写事务先后（order: end_first / reschedule_first），返回
        (结束状态码, (改期状态码, reason))。"""
        anchor_dep = f"2026-{month:02d}-{day:02d}T08:00"
        holder = self.register(1, anchor_dep)
        self.assertEqual(holder.status_code, 200, holder.get_json())
        anchor_id = holder.get_json()["voyage"]["id"]
        move = self.register(1, f"2026-{month:02d}-{day:02d}T11:00")
        self.assertEqual(move.status_code, 200, move.get_json())
        move_id = move.get_json()["voyage"]["id"]

        first_kind = "voyage_end" if order == "end_first" else "voyage_reschedule"
        first_locked = threading.Event()
        release_first = threading.Event()

        def on_begin(kind):
            if kind == first_kind:
                # 先到事务已拿到写锁、完成校验；通知主线程可以发起后到请求
                first_locked.set()

        def on_commit(kind):
            if kind == first_kind:
                # 先到事务停在“校验完毕、提交之前”：后到者的 BEGIN IMMEDIATE
                # 正阻塞在写锁上；放开后先到者提交，后到者才读到最终状态
                release_first.wait(5)

        hooks = {"begin": on_begin, "commit": on_commit}
        app.on_write_begin = hooks["begin"]
        app.on_write_commit = hooks["commit"]
        try:
            def do_end():
                return app.app.test_client().post(f"/api/voyages/{anchor_id}/end").status_code

            def do_reschedule():
                r = app.app.test_client().post(f"/api/voyages/{move_id}/reschedule", json={
                    "departureTime": f"2026-{month:02d}-{day:02d}T08:05",
                    "originalDepartureTime": f"2026-{month:02d}-{day:02d}T11:00"})
                return r.status_code, r.get_json().get("reason")

            with ThreadPoolExecutor(max_workers=2) as ex:
                if order == "end_first":
                    f_end = ex.submit(do_end)
                    self.assertTrue(first_locked.wait(5))
                    f_res = ex.submit(do_reschedule)
                    # 给后到者一点时间在写锁上排队，再放开先到者提交
                    threading.Event().wait(0.2)
                    release_first.set()
                    end_code = f_end.result(timeout=15)
                    res = f_res.result(timeout=15)
                else:
                    f_res = ex.submit(do_reschedule)
                    self.assertTrue(first_locked.wait(5))
                    f_end = ex.submit(do_end)
                    threading.Event().wait(0.2)
                    release_first.set()
                    res = f_res.result(timeout=15)
                    end_code = f_end.result(timeout=15)
        finally:
            app.on_write_begin = None
            app.on_write_commit = None
        return end_code, res

    def test_concurrent_reschedule_racing_with_end(self):
        # 锚点航次（08:00 占用）与把另一条航次改期到 08:05（与锚点共占）
        # 竞速结束锚点：结束先落 → 改期成功；改期先到 → voyage_co_occupancy。
        # 两种串行次序用写事务钩子确定性排列；任一最终状态都不得留下共占的
        # 未结束航次对。
        self.publish_payload(ROUTE)
        end_code, res = self._run_race("end_first", 9, 28)
        self.assertEqual(end_code, 200)
        self.assertEqual(res, (200, None), res)
        self.assert_no_open_cooccupancy()

        end_code, res = self._run_race("reschedule_first", 9, 29)
        self.assertEqual(end_code, 200)
        self.assertEqual(res, (409, "voyage_co_occupancy"), res)
        self.assert_no_open_cooccupancy()

    def test_concurrent_registers_cover_both_serial_orders(self):
        # 两个同一时刻的登记竞速：先到者在“校验完毕、提交之前”停住，让后到者
        # 在写锁上排队；先到者提交后，后到者在自己的同一写事务内必检到刚落库
        # 的未结束航次 → 共占拒绝。两种顺序各跑一次，双方各当一次先到/后到。
        self.publish_payload(ROUTE)
        for day in (28, 29):
            dep = f"2026-09-{day:02d}T08:00"
            first_locked = threading.Event()
            release_first = threading.Event()

            def on_begin(kind):
                if kind == "voyage_register":
                    first_locked.set()

            def on_commit(kind):
                if kind == "voyage_register":
                    release_first.wait(5)

            app.on_write_begin = on_begin
            app.on_write_commit = on_commit
            try:
                def do_register():
                    r = app.app.test_client().post(
                        "/api/voyages",
                        json={"releaseSeq": 1, "departureTime": dep})
                    return r

                with ThreadPoolExecutor(max_workers=2) as ex:
                    f1 = ex.submit(do_register)
                    self.assertTrue(first_locked.wait(5))
                    f2 = ex.submit(do_register)
                    threading.Event().wait(0.2)  # 后到者在写锁上排队
                    release_first.set()
                    r1 = f1.result(timeout=15)
                    r2 = f2.result(timeout=15)
            finally:
                app.on_write_begin = None
                app.on_write_commit = None
            codes = sorted([r1.status_code, r2.status_code])
            self.assertEqual(codes, [200, 409], (day, r1.get_json(), r2.get_json()))
            rejected = r1 if r1.status_code == 409 else r2
            self.assertEqual(rejected.get_json()["reason"], "voyage_co_occupancy")
            self.assertEqual(len(rejected.get_json()["coOccupancy"]), 1)
            self.assert_no_open_cooccupancy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
