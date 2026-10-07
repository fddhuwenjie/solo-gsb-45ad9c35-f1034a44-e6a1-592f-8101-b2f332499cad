# -*- coding: utf-8 -*-
"""航段空域占用表与未结束航次共占核对的回归测试。

覆盖：
- 占用表：按航次自身保存的航线快照逐段给出扫掠走廊、端点高度范围、
  进入至离开时段；旧发布登记的航次在新发布后仍按自身快照显示；
  改期后占用时段随之重算；已结束航次占用已释放（409 voyage_ended）；
  不存在 → 404；
- 共占判定：两段走廊相交 ∩ 端点高度范围重合 ∩ 占用时段正长度交集；
  平面不相交、高度不重合（端点相接算重合）、时间首尾相接（零长度交集）
  均放行；
- 登记：原禁飞区审查通过后在同一写事务内与全部未结束航次核对，共占
  409 occupancy_conflict 附对方航次编号、双方航段、重叠时段，整次拒绝、
  不留航次记录、原记录不变；
- 改期：跳过自身；改成共占时段被拒且原时刻与快照不变；结束对方航次后
  同一改期通过（结束即释放占用）；
- 候选与多个航次/同一航次多个航段共占时逐段全部报出；
- 并发：同一时刻并发登记两个相同航次恰有一个成功；并发改期/结束不漏检。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

# 4km 水平直线（y=100），200m 等高，08:00 起飞，速度 24 → 08:10 到达
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
# 同几何慢速航线：4km 以 6km/h 飞行 → 40 分钟时段（构造跨多个航次时段用）
ROUTE_SLOW = json.loads(json.dumps(ROUTE))
ROUTE_SLOW["speeds"] = [6]
# 与 ROUTE 在 (300,100) 交叉的竖直航线（2km），速度 24 → 5 分钟
ROUTE_CROSS = {
    "departureTime": "2026-09-24T08:00",
    "unitScale": 100,
    "corridorHalfWidth": 0.3,
    "speeds": [24],
    "waypoints": [
        {"id": "c1", "name": "C", "x": 300, "y": 50, "alt": 200},
        {"id": "c2", "name": "D", "x": 300, "y": 250, "alt": 200},
    ],
}
# 水平 y=200，高度 0→300，速度 24（10 分钟）
ROUTE_ALT_0_300 = {
    "departureTime": "2026-09-24T08:00",
    "unitScale": 100,
    "corridorHalfWidth": 0.3,
    "speeds": [24],
    "waypoints": [
        {"id": "w1", "name": "A", "x": 100, "y": 200, "alt": 0},
        {"id": "w2", "name": "B", "x": 500, "y": 200, "alt": 300},
    ],
}
# 竖直穿过 ROUTE_ALT_0_300（交点 300,200），高度 300→400（仅端点相接）
ROUTE_CROSS_300_400 = {
    "departureTime": "2026-09-24T08:00",
    "unitScale": 100,
    "corridorHalfWidth": 0.3,
    "speeds": [24],
    "waypoints": [
        {"id": "c1", "name": "C", "x": 300, "y": 100, "alt": 300},
        {"id": "c2", "name": "D", "x": 300, "y": 300, "alt": 400},
    ],
}
# 竖直穿过 ROUTE_ALT_0_300，高度 250→350（正高度重叠）
ROUTE_CROSS_250_350 = json.loads(json.dumps(ROUTE_CROSS_300_400))
ROUTE_CROSS_250_350["waypoints"][0]["alt"] = 250
ROUTE_CROSS_250_350["waypoints"][1]["alt"] = 350
# 两段航线：段1 水平 y=100（4km，10 分钟），段2 在 x=500 竖直至 y=400
# （3km，7.5 分钟）；两段走廊在端点 (500,100) 圆帽相接
ROUTE_TWO_SEG = {
    "departureTime": "2026-09-24T08:00",
    "unitScale": 100,
    "corridorHalfWidth": 0.3,
    "speeds": [24, 24],
    "waypoints": [
        {"id": "t1", "name": "T1", "x": 100, "y": 100, "alt": 200},
        {"id": "t2", "name": "T2", "x": 500, "y": 100, "alt": 200},
        {"id": "t3", "name": "T3", "x": 500, "y": 400, "alt": 200},
    ],
}
# 覆盖水平 y=100 航线中段、仅 20:00–21:00 生效：08:00 安全，20:30 冲突
NIGHT_NFZ = {
    "id": "nzN", "name": "夜间管控区", "enabled": True,
    "vertices": [[250, 50], [350, 50], [350, 150], [250, 150]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "2026-09-24T20:00", "endTime": "2026-09-24T21:00",
}


def clone(obj):
    return json.loads(json.dumps(obj))


class OccupancyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_path = app.DB_PATH
        app.DB_PATH = os.path.join(self.tmp, "test.db")
        app.init_db()
        self.client = app.app.test_client()
        # 全部发布共用同一空禁飞区工作区修订号
        self.rev = self.client.put("/api/state", json={
            "route": {"waypoints": []}, "nfzs": []}).get_json()["rev"]
        self._next_seq = 0

    def tearDown(self):
        app.DB_PATH = self.old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    # —— 辅助 ——
    def publish_next(self, route):
        """发布 route 为下一个发布号（空禁飞区工作区），返回新发布号。"""
        seq = self._next_seq + 1
        r = self.client.post("/api/versions", json={
            "name": f"v{seq}", "state": {"route": route, "nfzs": []}})
        self.assertEqual(r.status_code, 200)
        vid = r.get_json()["id"]
        r = self.client.post("/api/release", json={
            "versionId": vid, "workspaceRev": self.rev,
            "releaseSeq": self._next_seq})
        self.assertEqual(r.status_code, 200, r.get_json())
        self._next_seq = seq
        return seq

    def register(self, seq, dep):
        return self.client.post("/api/voyages", json={
            "releaseSeq": seq, "departureTime": dep})

    def register_ok(self, seq, dep):
        r = self.register(seq, dep)
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()["voyage"]["id"]

    def reschedule(self, vid, dep, orig):
        return self.client.post(f"/api/voyages/{vid}/reschedule", json={
            "departureTime": dep, "originalDepartureTime": orig})

    def end_voyage(self, vid):
        return self.client.post(f"/api/voyages/{vid}/end")

    def occupancy(self, vid):
        return self.client.get(f"/api/voyages/{vid}/occupancy")

    def approve(self, nfzs, seq):
        return self.client.post("/api/nfz-approval", json={
            "nfzs": nfzs, "workspaceRev": self.rev, "releaseSeq": seq})

    def list_ids(self):
        rows = self.client.get("/api/voyages").get_json()["voyages"]
        return [v["id"] for v in rows]

    # —— 占用表 ——
    def test_occupancy_table_from_snapshot(self):
        seq = self.publish_next(ROUTE)
        self.register_ok(seq, "2026-09-24T08:00")
        r = self.occupancy(1)
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertTrue(body["open"])
        self.assertEqual(body["voyageId"], 1)
        self.assertEqual(body["releaseSeq"], 1)
        self.assertEqual(body["departureTime"], "2026-09-24T08:00")
        self.assertEqual(body["errors"], [])
        self.assertEqual(len(body["segments"]), 1)
        seg = body["segments"][0]
        self.assertEqual(seg["segIndex"], 0)
        self.assertEqual(seg["altLow"], 200)
        self.assertEqual(seg["altHigh"], 200)
        self.assertEqual(seg["enterTime"], "2026-09-24T08:00")
        self.assertEqual(seg["leaveTime"], "2026-09-24T08:10")
        self.assertAlmostEqual(seg["durationMinutes"], 10.0, places=2)
        self.assertAlmostEqual(seg["distanceKm"], 4.0, places=2)
        # 扫掠走廊：GeoJSON Polygon，半宽 0.3km=30 画布单位，4km 段长 400
        self.assertEqual(seg["corridor"]["type"], "Polygon")
        ring = seg["corridor"]["coordinates"][0]
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        self.assertAlmostEqual(min(xs), 70.0, places=6)
        self.assertAlmostEqual(max(xs), 530.0, places=6)
        self.assertAlmostEqual(min(ys), 70.0, places=6)
        self.assertAlmostEqual(max(ys), 130.0, places=6)
        # 中心线便于侧栏/画布定位
        self.assertEqual(seg["centerline"]["coordinates"],
                         [[100.0, 100.0], [500.0, 100.0]])

    def test_occupancy_uses_own_snapshot_after_new_release(self):
        # 航次 #1 登记于发布 #1（ROUTE 水平 y=100）；再发布 #2（ROUTE_CROSS
        # 竖直 x=300）：占用表仍按航次 #1 自身快照显示
        seq1 = self.publish_next(ROUTE)
        self.register_ok(seq1, "2026-09-24T08:00")
        self.publish_next(ROUTE_CROSS)
        body = self.occupancy(1).get_json()
        self.assertEqual(body["releaseSeq"], 1)
        seg = body["segments"][0]
        xs = [p[0] for p in seg["corridor"]["coordinates"][0]]
        ys = [p[1] for p in seg["corridor"]["coordinates"][0]]
        self.assertAlmostEqual(min(ys), 70.0, places=6)
        self.assertAlmostEqual(max(ys), 130.0, places=6)
        self.assertAlmostEqual(min(xs), 70.0, places=6)
        self.assertAlmostEqual(max(xs), 530.0, places=6)

    def test_occupancy_recomputed_after_reschedule(self):
        seq = self.publish_next(ROUTE)
        self.register_ok(seq, "2026-09-24T08:00")
        r = self.reschedule(1, "2026-09-24T10:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        body = self.occupancy(1).get_json()
        self.assertEqual(body["departureTime"], "2026-09-24T10:00")
        self.assertEqual(body["segments"][0]["enterTime"], "2026-09-24T10:00")
        self.assertEqual(body["segments"][0]["leaveTime"], "2026-09-24T10:10")

    def test_occupancy_ended_returns_409_released(self):
        seq = self.publish_next(ROUTE)
        self.register_ok(seq, "2026-09-24T08:00")
        self.assertEqual(self.end_voyage(1).status_code, 200)
        r = self.occupancy(1)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "voyage_ended")
        self.assertFalse(body["voyage"]["open"])

    def test_occupancy_404(self):
        self.publish_next(ROUTE)
        r = self.occupancy(999)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["reason"], "voyage_not_found")

    def test_occupancy_multi_segment_endpoint_altitudes_and_times(self):
        seq = self.publish_next(ROUTE_TWO_SEG)
        self.register_ok(seq, "2026-09-24T08:00")
        body = self.occupancy(1).get_json()
        self.assertEqual([s["segIndex"] for s in body["segments"]], [0, 1])
        s0, s1 = body["segments"]
        # 段1 [08:00,08:10]；段2 紧接 [08:10,08:17:30]（3km/24kmh=7.5 分钟，
        # 离开时刻按分序列化时取整显示为 08:17，判定仍用带秒的真实时刻）
        self.assertEqual((s0["enterTime"], s0["leaveTime"]),
                         ("2026-09-24T08:00", "2026-09-24T08:10"))
        self.assertEqual(s1["enterTime"], "2026-09-24T08:10")
        self.assertEqual(s1["leaveTime"], "2026-09-24T08:17")
        self.assertAlmostEqual(s1["durationMinutes"], 7.5, places=2)
        self.assertEqual((s0["altLow"], s0["altHigh"]), (200, 200))
        # 段2 竖直 y=100→400，x=500
        ring = s1["corridor"]["coordinates"][0]
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        self.assertAlmostEqual(min(xs), 470.0, places=6)
        self.assertAlmostEqual(max(xs), 530.0, places=6)
        self.assertAlmostEqual(min(ys), 70.0, places=6)
        self.assertAlmostEqual(max(ys), 430.0, places=6)

    # —— 共占：登记 ——
    def test_register_same_corridor_same_time_conflicts(self):
        seq = self.publish_next(ROUTE)
        self.register_ok(seq, "2026-09-24T08:00")
        # 同一航线、同一时刻 → 全段共占，拒绝不留记录
        r = self.register(seq, "2026-09-24T08:00")
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "occupancy_conflict")
        self.assertEqual(len(body["occupancyConflicts"]), 1)
        c = body["occupancyConflicts"][0]
        self.assertEqual(c["voyageId"], 1)
        self.assertEqual(c["releaseSeq"], 1)
        self.assertEqual(c["departureTime"], "2026-09-24T08:00")
        self.assertEqual(c["selfSegIndex"], 0)
        self.assertEqual(c["otherSegIndex"], 0)
        self.assertEqual(c["overlapStart"], "2026-09-24T08:00")
        self.assertEqual(c["overlapEnd"], "2026-09-24T08:10")
        self.assertAlmostEqual(c["durationMinutes"], 10.0, places=2)
        self.assertEqual(self.list_ids(), [1])  # 整次拒绝，无新航次

    def test_register_overlapping_windows_conflicts(self):
        seq = self.publish_next(ROUTE)
        self.register_ok(seq, "2026-09-24T08:00")
        # 08:05 起飞：[08:05,08:15] 与 [08:00,08:10] 正长度交集 [08:05,08:10]
        r = self.register(seq, "2026-09-24T08:05")
        self.assertEqual(r.status_code, 409)
        c = r.get_json()["occupancyConflicts"][0]
        self.assertEqual(c["overlapStart"], "2026-09-24T08:05")
        self.assertEqual(c["overlapEnd"], "2026-09-24T08:10")
        self.assertAlmostEqual(c["durationMinutes"], 5.0, places=2)
        self.assertEqual(self.list_ids(), [1])

    def test_register_end_to_end_time_touch_allowed(self):
        seq = self.publish_next(ROUTE)
        self.register_ok(seq, "2026-09-24T08:00")
        # 08:10 起飞：前者离开时刻恰为后者进入时刻，零长度交集 → 允许
        r = self.register(seq, "2026-09-24T08:10")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(sorted(self.list_ids()), [1, 2])

    def test_register_disjoint_times_allowed(self):
        seq = self.publish_next(ROUTE)
        self.register_ok(seq, "2026-09-24T08:00")
        r = self.register(seq, "2026-09-24T09:00")
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_register_crossing_corridors_same_time_conflicts(self):
        # 水平 y=100 与竖直 x=300 在 (300,100) 相交；同时刻、等高 → 共占。
        # 先发布 #1 并登记水平航次，再发布 #2（旧发布航次保留）
        seq1 = self.publish_next(ROUTE)
        self.register_ok(seq1, "2026-09-24T08:00")
        seq2 = self.publish_next(ROUTE_CROSS)
        r = self.register(seq2, "2026-09-24T08:00")
        self.assertEqual(r.status_code, 409)
        c = r.get_json()["occupancyConflicts"][0]
        self.assertEqual(c["voyageId"], 1)
        self.assertEqual(c["selfSegIndex"], 0)
        self.assertEqual(c["otherSegIndex"], 0)
        # 水平 [08:00,08:10]，竖直 [08:00,08:05]
        self.assertEqual((c["overlapStart"], c["overlapEnd"]),
                         ("2026-09-24T08:00", "2026-09-24T08:05"))
        self.assertEqual(self.list_ids(), [1])

    def test_register_crossing_but_disjoint_times_allowed(self):
        seq1 = self.publish_next(ROUTE)
        self.register_ok(seq1, "2026-09-24T08:00")
        seq2 = self.publish_next(ROUTE_CROSS)
        # 竖直 08:10 起飞 [08:10,08:15]：与水平 [08:00,08:10] 首尾相接，放行
        r = self.register(seq2, "2026-09-24T08:10")
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_register_parallel_corridors_barely_disjoint_allowed(self):
        # 两条平行走廊：半宽均 0.3km（各 30 画布单位），中线相距 61 单位时
        # 平面不相交（时间/高度相同也不共占）
        route_other = clone(ROUTE)
        for wp in route_other["waypoints"]:
            wp["y"] = 161
        seq1 = self.publish_next(ROUTE)
        self.register_ok(seq1, "2026-09-24T08:00")
        seq2 = self.publish_next(route_other)
        r = self.register(seq2, "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_register_altitude_range_endpoint_touch_overlaps(self):
        # 高度范围 [0,300] 与 [300,400]：端点 300 相接，时间/平面均交 → 共占
        seq1 = self.publish_next(ROUTE_ALT_0_300)
        self.register_ok(seq1, "2026-09-24T08:00")
        seq2 = self.publish_next(ROUTE_CROSS_300_400)
        r = self.register(seq2, "2026-09-24T08:00")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "occupancy_conflict")

    def test_register_altitude_gap_allowed(self):
        # 高度 [0,300] 与 [301,401] 不重合：几何/时间都交也放行
        route_gap = clone(ROUTE_CROSS_300_400)
        route_gap["waypoints"][0]["alt"] = 301
        route_gap["waypoints"][1]["alt"] = 401
        seq1 = self.publish_next(ROUTE_ALT_0_300)
        self.register_ok(seq1, "2026-09-24T08:00")
        seq2 = self.publish_next(route_gap)
        r = self.register(seq2, "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_register_altitude_overlap_but_time_touch_allowed(self):
        # [0,300] × [250,350] 高度正重叠、走廊相交，但竖直航段 [08:10,08:15]
        # 与水平 [08:00,08:10] 仅首尾相接 → 零长度交集，放行
        seq1 = self.publish_next(ROUTE_ALT_0_300)
        self.register_ok(seq1, "2026-09-24T08:00")
        seq2 = self.publish_next(ROUTE_CROSS_250_350)
        r = self.register(seq2, "2026-09-24T08:10")
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_register_reports_multiple_voyages_and_segments(self):
        # 依次发布 #1 ROUTE（水平 10 分钟）、#2 ROUTE_TWO_SEG（段0 水平
        # 10 分钟、段1 竖直 7.5 分钟）、#3 ROUTE_SLOW（同水平几何，40 分钟）。
        # 已登记（两者时段不相交，可共存）：
        #   航次#1 用发布 #1 在 08:30：水平 [08:30,08:40]
        #   航次#2 用发布 #2 在 08:00：段0 [08:00,08:10]、段1 [08:10,08:17:30]
        # 候选用发布 #3 在 08:05 → [08:05,08:45]，与航次#1 段0、航次#2 段0、
        # 航次#2 段1（走廊在 (500,100) 圆帽相接，时段正长度交集）逐段共占。
        seq1 = self.publish_next(ROUTE)
        self.register_ok(seq1, "2026-09-24T08:30")
        seq2 = self.publish_next(ROUTE_TWO_SEG)
        self.register_ok(seq2, "2026-09-24T08:00")
        seq3 = self.publish_next(ROUTE_SLOW)
        r = self.register(seq3, "2026-09-24T08:05")
        self.assertEqual(r.status_code, 409)
        rows = r.get_json()["occupancyConflicts"]
        pairs = sorted((c["voyageId"], c["otherSegIndex"]) for c in rows)
        self.assertEqual(pairs, [(1, 0), (2, 0), (2, 1)])
        by = {(c["voyageId"], c["otherSegIndex"]): c for c in rows}
        self.assertEqual((by[(1, 0)]["overlapStart"], by[(1, 0)]["overlapEnd"]),
                         ("2026-09-24T08:30", "2026-09-24T08:40"))
        self.assertEqual((by[(2, 0)]["overlapStart"], by[(2, 0)]["overlapEnd"]),
                         ("2026-09-24T08:05", "2026-09-24T08:10"))
        self.assertEqual(by[(2, 1)]["overlapStart"], "2026-09-24T08:10")
        self.assertAlmostEqual(by[(2, 1)]["durationMinutes"], 7.5, places=2)
        # 候选被整次拒绝，无新航次（既有 #1、#2 记录不变）
        self.assertEqual(sorted(self.list_ids()), [1, 2])

    def test_register_nfz_review_still_precedes_cooccupancy(self):
        # 禁飞区审查仍先于共占：08:00 登记航次后核准夜间窗（对发布快照
        # 08:00 与该航次都安全），再以 20:30 登记 → analysis_conflicts，
        # 原审查优先级不回归
        seq = self.publish_next(ROUTE)
        self.register_ok(seq, "2026-09-24T08:00")
        r = self.approve([clone(NIGHT_NFZ)], seq)
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.register(seq, "2026-09-24T20:30")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "analysis_conflicts")
        self.assertEqual(r.get_json()["first"]["nfzId"], "nzN")
        # 被拒航次不留记录，原航次不变
        self.assertEqual(self.list_ids(), [1])

    def test_register_occupancy_does_not_touch_workspace_or_release(self):
        # 共占拒绝不改工作区修订号/发布；通过登记也不改
        seq = self.publish_next(ROUTE)
        self.register_ok(seq, "2026-09-24T08:00")
        rel_before = self.client.get("/api/release").get_json()["release"]
        r = self.register(seq, "2026-09-24T08:00")
        self.assertEqual(r.status_code, 409)
        hdr = self.client.get("/api/state").headers["X-Workspace-Rev"]
        self.assertEqual(int(hdr), self.rev)
        self.assertEqual(self.client.get("/api/release").get_json()["release"],
                         rel_before)

    # —— 共占：改期 ——
    def test_reschedule_skips_self(self):
        # 同航线同刻改期（相当于不改）：跳过自身，不应自报共占
        seq = self.publish_next(ROUTE)
        self.register_ok(seq, "2026-09-24T08:00")
        r = self.reschedule(1, "2026-09-24T08:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_reschedule_into_other_voyage_rejected_unchanged(self):
        # 发布 #1 水平航线登记 08:00 航次；再发布 #2 竖直航线，09:00 航次
        # （与水平航次时段不相交）登记成功
        seq1 = self.publish_next(ROUTE)
        self.register_ok(seq1, "2026-09-24T08:00")
        seq2 = self.publish_next(ROUTE_CROSS)
        self.register_ok(seq2, "2026-09-24T09:00")
        # 竖直航次改到 08:00 → 与水平航次共占，整次拒绝、原时刻与快照不变
        r = self.reschedule(2, "2026-09-24T08:00", "2026-09-24T09:00")
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "occupancy_conflict")
        c = body["occupancyConflicts"][0]
        self.assertEqual(c["voyageId"], 1)
        self.assertEqual(c["selfSegIndex"], 0)
        self.assertEqual(c["otherSegIndex"], 0)
        rows = self.client.get("/api/voyages").get_json()["voyages"]
        v2 = [v for v in rows if v["id"] == 2][0]
        self.assertEqual(v2["departureTime"], "2026-09-24T09:00")
        self.assertEqual(v2["routeSnapshot"]["departureTime"], "2026-09-24T09:00")

    def test_reschedule_passes_after_other_voyage_ended(self):
        seq1 = self.publish_next(ROUTE)
        self.register_ok(seq1, "2026-09-24T08:00")
        seq2 = self.publish_next(ROUTE_CROSS)
        self.register_ok(seq2, "2026-09-24T09:00")
        r = self.reschedule(2, "2026-09-24T08:00", "2026-09-24T09:00")
        self.assertEqual(r.status_code, 409)
        # 结束水平航次 → 占用释放，同一改期通过
        self.assertEqual(self.end_voyage(1).status_code, 200)
        r = self.reschedule(2, "2026-09-24T08:00", "2026-09-24T09:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["voyage"]["departureTime"], "2026-09-24T08:00")

    def test_reschedule_to_end_to_end_touch_allowed(self):
        seq = self.publish_next(ROUTE)
        self.register_ok(seq, "2026-09-24T08:00")
        self.register_ok(seq, "2026-09-24T09:00")
        # 航次#2 从 09:00 改到 08:10（与 #1 首尾相接）→ 允许
        r = self.reschedule(2, "2026-09-24T08:10", "2026-09-24T09:00")
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_reschedule_away_releases_slot_for_new_registration(self):
        seq = self.publish_next(ROUTE)
        self.register_ok(seq, "2026-09-24T08:00")
        self.assertEqual(self.register(seq, "2026-09-24T08:05").status_code, 409)
        # #1 改到 10:00 释放 08:00 走廊后，新航次可登记该时段
        r = self.reschedule(1, "2026-09-24T10:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.register(seq, "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_ended_voyage_not_in_cooccupancy_check(self):
        seq = self.publish_next(ROUTE)
        self.register_ok(seq, "2026-09-24T08:00")
        self.assertEqual(self.end_voyage(1).status_code, 200)
        # 已结束航次不参与共占：同走廊同时刻可登记
        r = self.register(seq, "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["voyage"]["id"], 2)

    # —— 并发：写事务串行不漏检 ——
    def test_concurrent_registration_exactly_one_wins(self):
        self.publish_next(ROUTE)
        barrier = threading.Barrier(2)
        results = []

        def worker():
            c = app.app.test_client()
            barrier.wait()
            r = c.post("/api/voyages", json={
                "releaseSeq": 1, "departureTime": "2026-09-24T08:00"})
            results.append(r.status_code)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(sorted(results), [200, 409])
        # 成功者恰有一个，失败者返回共占明细、无第三条记录
        self.assertEqual(sorted(self.list_ids()), [1])

    def test_concurrent_reschedule_and_end_no_miss(self):
        # 并发地把 #2 改到 #1 的占用时段、结束 #1：写事务串行——
        # #1 先结束则改期通过（占用已释放），#2 先改则共占拒绝；
        # 不得出现“#1 已结束提交之外、#2 仍占到同一走廊”的漏检结局。
        seq = self.publish_next(ROUTE)
        self.register_ok(seq, "2026-09-24T08:00")
        self.register_ok(seq, "2026-09-24T09:00")
        barrier = threading.Barrier(2)
        outcome = {}

        def do_reschedule():
            c = app.app.test_client()
            barrier.wait()
            r = c.post("/api/voyages/2/reschedule", json={
                "departureTime": "2026-09-24T08:00",
                "originalDepartureTime": "2026-09-24T09:00"})
            outcome["reschedule"] = r.status_code

        def do_end():
            c = app.app.test_client()
            barrier.wait()
            r = c.post("/api/voyages/1/end")
            outcome["end"] = r.status_code

        threads = [threading.Thread(target=do_reschedule),
                   threading.Thread(target=do_end)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(outcome["end"], 200)
        voyages = {v["id"]: v for v in
                   self.client.get("/api/voyages").get_json()["voyages"]}
        self.assertFalse(voyages[1]["open"])  # 结束已提交，占用已释放
        v2_dep = voyages[2]["departureTime"]
        if outcome["reschedule"] == 200:
            # 结束先拿写锁：改期事务随后读到的未结束集合已不含 #1 → 放行
            self.assertEqual(v2_dep, "2026-09-24T08:00")
        else:
            # 改期先拿写锁：#1 仍在占用集合 → 必须共占拒绝、原时刻不变；
            # 随后结束提交释放占用
            self.assertEqual(outcome["reschedule"], 409)
            self.assertEqual(v2_dep, "2026-09-24T09:00")

    def test_concurrent_registrations_disjoint_times_both_win(self):
        # 并发登记首尾相接的两个航次：都应成功（串行后互不共占）
        self.publish_next(ROUTE)
        barrier = threading.Barrier(2)
        deps = ["2026-09-24T08:00", "2026-09-24T08:10"]
        results = []

        def worker(dep):
            c = app.app.test_client()
            barrier.wait()
            r = c.post("/api/voyages", json={"releaseSeq": 1, "departureTime": dep})
            results.append((dep, r.status_code))

        threads = [threading.Thread(target=worker, args=(d,)) for d in deps]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(sorted(code for _, code in results), [200, 200])
        self.assertEqual(sorted(self.list_ids()), [1, 2])


if __name__ == "__main__":
    unittest.main(verbosity=2)
