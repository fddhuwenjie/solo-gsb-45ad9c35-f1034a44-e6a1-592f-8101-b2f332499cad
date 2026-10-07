# -*- coding: utf-8 -*-
"""航次改期（POST /api/voyages/<id>/reschedule）回归测试。

覆盖：
- 改期成功：保留航次编号、登记时间与原发布号，仅替换该航次航线快照中的
  起飞时刻（几何/速度/走廊等不动），与当前已核准禁飞区重新审查后原子落库；
  工作区修订号与发布号不变；
- 已有新发布时仍沿用该航次自己保存的航线几何（旧发布航次可改期）；
- 航次不存在 → 404 voyage_not_found；已结束 → 409 voyage_ended；
- 所见原时刻与库中不一致（含他页先改期、原时刻无法解析）→ 409 stale
  附 departureTime {expected, actual} 明细；
- 新时刻无效 / 缺 originalDepartureTime → 400；
- 审查报错 / 审查冲突（附明细）→ 拒绝，原时刻与快照不变；
- 交错：改期后的航次以最终生效时刻参与核准复核（不漏检）；核准生效的
  禁飞区反过来阻拦后续改期；改期后结束航次即放行核准。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

# 干净航线：4km 直线（y=100），200m 等高，08:00 起飞
ROUTE = {
    "departureTime": "2026-09-24T08:00",
    "unitScale": 100,
    "corridorHalfWidth": 0.3,
    "speeds": [25],
    "waypoints": [
        {"id": "w1", "name": "A", "x": 100, "y": 100, "alt": 200},
        {"id": "w2", "name": "B", "x": 500, "y": 100, "alt": 200},
    ],
}
# 同位置、时窗 20:00–21:00：08:00 起飞不冲突，20:30 起飞冲突
NIGHT_NFZ = {
    "id": "nzN", "name": "夜间管控区", "enabled": True,
    "vertices": [[250, 50], [350, 50], [350, 150], [250, 150]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "2026-09-24T20:00", "endTime": "2026-09-24T21:00",
}
# y=400 的另一条干净航线（4km 直线，200m 等高）
ROUTE_Y400 = {
    "departureTime": "2026-09-24T08:00",
    "unitScale": 100,
    "corridorHalfWidth": 0.3,
    "speeds": [25],
    "waypoints": [
        {"id": "w1", "name": "C", "x": 100, "y": 400, "alt": 200},
        {"id": "w2", "name": "D", "x": 500, "y": 400, "alt": 200},
    ],
}


def clone(obj):
    return json.loads(json.dumps(obj))


class VoyageRescheduleTests(unittest.TestCase):
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

    def publish(self, vid, rev, seq):
        return self.client.post("/api/release", json={
            "versionId": vid, "workspaceRev": rev, "releaseSeq": seq})

    def approve(self, nfzs, rev, seq):
        return self.client.post("/api/nfz-approval", json={
            "nfzs": nfzs, "workspaceRev": rev, "releaseSeq": seq})

    def register(self, seq, dep):
        return self.client.post("/api/voyages", json={
            "releaseSeq": seq, "departureTime": dep})

    def reschedule(self, vid, dep, orig):
        return self.client.post(f"/api/voyages/{vid}/reschedule", json={
            "departureTime": dep, "originalDepartureTime": orig})

    def end_voyage(self, vid):
        return self.client.post(f"/api/voyages/{vid}/end")

    def list_voyages(self):
        r = self.client.get("/api/voyages")
        self.assertEqual(r.status_code, 200)
        return r.get_json()["voyages"]

    def ws_state(self):
        r = self.client.get("/api/state")
        return r.get_json(), int(r.headers["X-Workspace-Rev"])

    def current_release(self):
        return self.client.get("/api/release").get_json()

    def publish_route(self, route=ROUTE, nfzs=None):
        """发布 route（工作区禁飞区为 nfzs，默认空），返回 (vid, rev, seq)。"""
        rev = self.put_state({"route": {"waypoints": []}, "nfzs": nfzs or []})
        vid = self.save_version({"route": route, "nfzs": []})
        r = self.publish(vid, rev, 0)
        self.assertEqual(r.status_code, 200, r.get_json())
        return vid, rev, 1

    # —— 改期成功 ——
    def test_reschedule_success_keeps_identity_and_geometry(self):
        _, rev, seq = self.publish_route()
        v0 = self.register(seq, "2026-09-24T08:00").get_json()["voyage"]
        r = self.reschedule(1, "2026-09-24T09:30", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        v = r.get_json()["voyage"]
        # 航次编号、登记时间、原发布号保留
        self.assertEqual(v["id"], v0["id"])
        self.assertEqual(v["createdAt"], v0["createdAt"])
        self.assertEqual(v["releaseSeq"], v0["releaseSeq"])
        self.assertEqual(v["departureTime"], "2026-09-24T09:30")
        self.assertTrue(v["open"])
        self.assertIsNone(v["endedAt"])
        # 快照仅起飞时刻变化，几何/速度/走廊/比例不动
        snap = v["routeSnapshot"]
        self.assertEqual(snap["departureTime"], "2026-09-24T09:30")
        for key in ("waypoints", "speeds", "corridorHalfWidth", "unitScale"):
            self.assertEqual(snap[key], v0["routeSnapshot"][key])
        # 列表中同一航次（不产生新航次）
        voyages = self.list_voyages()
        self.assertEqual([x["id"] for x in voyages], [1])
        self.assertEqual(voyages[0]["departureTime"], "2026-09-24T09:30")
        # 工作区修订号与发布号不变
        _, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev)
        self.assertEqual(self.current_release()["releaseSeq"], seq)

    def test_reschedule_uses_own_snapshot_after_new_release(self):
        # 发布 #1（y=100）登记航次；再发布 #2（y=400）：改期航次 #1 仍按
        # 其自身发布快照几何审查与落库，与当前发布号无关
        _, rev, _ = self.publish_route(route=ROUTE)
        self.assertEqual(self.register(1, "2026-09-24T08:00").status_code, 200)
        vid2 = self.save_version({"route": ROUTE_Y400, "nfzs": []}, name="v2")
        self.assertEqual(self.publish(vid2, rev, 1).status_code, 200)
        self.assertEqual(self.current_release()["releaseSeq"], 2)
        r = self.reschedule(1, "2026-09-24T20:30", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        v = r.get_json()["voyage"]
        self.assertEqual(v["releaseSeq"], 1)  # 原发布号保留
        self.assertEqual(v["departureTime"], "2026-09-24T20:30")
        self.assertEqual(v["routeSnapshot"]["waypoints"], ROUTE["waypoints"])
        self.assertEqual(v["routeSnapshot"]["departureTime"], "2026-09-24T20:30")
        # 核准夜间窗（压 y=100、20–21 点）：当前发布 #2 的 y=400 快照安全，
        # 但改期后的旧发布航次（y=100，20:30）冲突 → 必须被拦
        r = self.approve([clone(NIGHT_NFZ)], rev, 2)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "voyage_conflicts")
        self.assertEqual(body["voyageConflicts"][0]["voyageId"], 1)
        self.assertEqual(body["voyageConflicts"][0]["releaseSeq"], 1)
        self.assertEqual(body["voyageConflicts"][0]["departureTime"],
                         "2026-09-24T20:30")

    # —— 拒绝情形：原时刻与快照不变 ——
    def test_reschedule_voyage_not_found(self):
        r = self.reschedule(999, "2026-09-24T09:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["reason"], "voyage_not_found")

    def test_reschedule_ended_voyage(self):
        _, _, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        self.assertEqual(self.end_voyage(1).status_code, 200)
        r = self.reschedule(1, "2026-09-24T09:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "voyage_ended")
        v = self.list_voyages()[0]
        self.assertEqual(v["departureTime"], "2026-09-24T08:00")
        self.assertFalse(v["open"])

    def test_reschedule_stale_original_time(self):
        _, _, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        # 他页先把航次改期到 09:00
        r = self.reschedule(1, "2026-09-24T09:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        # 本页仍持 08:00 作为所见原时刻提交 → 409 stale 附明细，时刻不再变
        r = self.reschedule(1, "2026-09-24T10:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "stale")
        self.assertEqual(body["details"]["departureTime"],
                         {"expected": "2026-09-24T08:00",
                          "actual": "2026-09-24T09:00"})
        v = self.list_voyages()[0]
        self.assertEqual(v["departureTime"], "2026-09-24T09:00")
        self.assertEqual(v["routeSnapshot"]["departureTime"], "2026-09-24T09:00")
        # 用最新所见原时刻（带等价的不同写法）重提 → 通过
        r = self.reschedule(1, "2026-09-24T10:00", "2026-09-24 09:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.list_voyages()[0]["departureTime"],
                         "2026-09-24T10:00")

    def test_reschedule_invalid_inputs(self):
        _, _, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        # 新时刻缺失/格式错误 → invalid_departure_time
        for bad in ("", None, "not-a-time", "2026-13-99T99:99"):
            r = self.reschedule(1, bad, "2026-09-24T08:00")
            self.assertEqual(r.status_code, 400, bad)
            self.assertEqual(r.get_json()["reason"], "invalid_departure_time", bad)
        # 缺 originalDepartureTime → missing_fields
        r = self.client.post("/api/voyages/1/reschedule",
                             json={"departureTime": "2026-09-24T09:00"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "missing_fields")
        # 原时刻无法解析 → 视同与库中不一致：stale
        r = self.reschedule(1, "2026-09-24T09:00", "garbage")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "stale")
        # 均未改动
        v = self.list_voyages()[0]
        self.assertEqual(v["departureTime"], "2026-09-24T08:00")
        self.assertEqual(v["routeSnapshot"]["departureTime"], "2026-09-24T08:00")

    def test_reschedule_analysis_errors(self):
        _, rev, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        # 工作区被直接 PUT 入一个无效禁飞区（结束早于开始）
        bad_nfz = clone(NIGHT_NFZ)
        bad_nfz["startTime"] = "2026-09-24T21:00"
        bad_nfz["endTime"] = "2026-09-24T20:00"
        rev2 = self.put_state({"route": {"waypoints": []}, "nfzs": [bad_nfz]})
        r = self.reschedule(1, "2026-09-24T09:00", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertEqual(body["reason"], "analysis_errors")
        self.assertTrue(body["errors"])
        # 原时刻与快照不变，工作区修订号不被改期改动
        v = self.list_voyages()[0]
        self.assertEqual(v["departureTime"], "2026-09-24T08:00")
        _, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev2)

    def test_reschedule_conflict_against_approved_nfz(self):
        # 核准夜间窗（对 08:00 航次安全）后，改期到 20:30 与之冲突 → 拒绝
        _, rev, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        r = self.approve([clone(NIGHT_NFZ)], rev, seq)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev2 = r.get_json()["rev"]
        r = self.reschedule(1, "2026-09-24T20:30", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "analysis_conflicts")
        self.assertEqual(body["first"]["nfzId"], "nzN")
        self.assertTrue(body["entries"])
        # 原时刻与快照不变，工作区与发布快照不变
        v = self.list_voyages()[0]
        self.assertEqual(v["departureTime"], "2026-09-24T08:00")
        self.assertEqual(v["routeSnapshot"]["departureTime"], "2026-09-24T08:00")
        _, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev2)
        # 改到窗外时刻 → 通过
        r = self.reschedule(1, "2026-09-24T08:30", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.list_voyages()[0]["departureTime"],
                         "2026-09-24T08:30")

    # —— 交错：改期 / 结束 / 核准 ——
    def test_approval_rechecks_rescheduled_voyage(self):
        # 改期后的航次以最终生效的时刻参与核准复核，不能漏检：
        # 发布 → 登记 08:00 航次 → 改期到 20:30（无禁飞区，通过）→
        # 候选夜间窗对发布快照（08:00）安全，但被改期后的航次拦住
        _, rev, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        r = self.reschedule(1, "2026-09-24T20:30", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.approve([clone(NIGHT_NFZ)], rev, seq)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "voyage_conflicts")
        vcs = body["voyageConflicts"]
        self.assertEqual(len(vcs), 1)
        self.assertEqual(vcs[0]["voyageId"], 1)
        self.assertEqual(vcs[0]["departureTime"], "2026-09-24T20:30")
        # 工作区禁飞区未被替换
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev)
        self.assertEqual(state["nfzs"], [])
        # 改期回 08:00 后同一候选通过（按最终生效的未结束航次复核）
        r = self.reschedule(1, "2026-09-24T08:00", "2026-09-24T20:30")
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.approve([clone(NIGHT_NFZ)], rev, seq)
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_reschedule_then_end_frees_approval(self):
        # 改期到 20:30 的航次拦住夜间窗候选；结束该航次后候选通过
        _, rev, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        self.assertEqual(
            self.reschedule(1, "2026-09-24T20:30", "2026-09-24T08:00")
            .status_code, 200)
        r = self.approve([clone(NIGHT_NFZ)], rev, seq)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "voyage_conflicts")
        self.assertEqual(self.end_voyage(1).status_code, 200)
        r = self.approve([clone(NIGHT_NFZ)], rev, seq)
        self.assertEqual(r.status_code, 200, r.get_json())
        state, _ = self.ws_state()
        self.assertEqual(state["nfzs"], [NIGHT_NFZ])
        # 结束后航次记录仍可查，时刻保持改期后的值
        v = self.list_voyages()[0]
        self.assertFalse(v["open"])
        self.assertEqual(v["departureTime"], "2026-09-24T20:30")

    def test_approved_nfz_blocks_later_reschedule(self):
        # 核准先生效：夜间窗核准后，改期进窗即被拒（改期检当前已核准禁飞区）
        _, rev, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        r = self.approve([clone(NIGHT_NFZ)], rev, seq)
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.reschedule(1, "2026-09-24T20:30", "2026-09-24T08:00")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "analysis_conflicts")
        self.assertEqual(self.list_voyages()[0]["departureTime"],
                         "2026-09-24T08:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
