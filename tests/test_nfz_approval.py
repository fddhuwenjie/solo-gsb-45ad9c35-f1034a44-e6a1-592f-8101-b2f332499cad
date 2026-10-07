# -*- coding: utf-8 -*-
"""禁飞区变更核准（POST /api/nfz-approval）回归测试。

覆盖：
- 尚无发布时提交 → no_release，工作区不变；
- 核准通过：候选禁飞区与最新发布快照航线组合审查，仅原子替换工作区
  禁飞区并递增修订号，最新工作区航线与发布快照均保持不变；
- 审查以发布快照航线为准：工作区航线发布后又被改动不影响判定（双向）；
- 审查错误 / 审查冲突（附明细）/ 任一号已变化（stale 附明细）→
  工作区与已发布内容均不变，草稿修改后可再次提交通过。
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
# 覆盖发布航线中段、时窗覆盖全程的禁飞区（与 ROUTE 必冲突）
BLOCK_NFZ = {
    "id": "nz1", "name": "阻断区", "enabled": True,
    "vertices": [[250, 50], [350, 50], [350, 150], [250, 150]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "2026-09-24T07:00", "endTime": "2026-09-24T09:00",
}
# 远离发布航线的合法禁飞区（与 ROUTE 不冲突，但与 y≈550 的航线冲突）
FAR_NFZ = {
    "id": "nzFar", "name": "远侧限制区", "enabled": True,
    "vertices": [[100, 500], [200, 500], [200, 600], [100, 600]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "", "endTime": "",
}


def clone(obj):
    return json.loads(json.dumps(obj))


class NfzApprovalTests(unittest.TestCase):
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

    def ws_state(self):
        """当前工作区状态与修订号（响应头）。"""
        r = self.client.get("/api/state")
        self.assertEqual(r.status_code, 200)
        return r.get_json(), int(r.headers["X-Workspace-Rev"])

    def current_release(self):
        return self.client.get("/api/release").get_json()

    def publish_route(self):
        """发布 ROUTE（工作区禁飞区为空），返回 (versionId, 当前修订号)。"""
        rev = self.put_state({"route": {"waypoints": []}, "nfzs": []})
        vid = self.save_version({"route": ROUTE, "nfzs": []})
        r = self.publish(vid, rev, 0)
        self.assertEqual(r.status_code, 200, r.get_json())
        return vid, rev

    # —— 尚无发布 ——
    def test_no_release_rejected(self):
        rev = self.put_state({"route": {"waypoints": []}, "nfzs": []})
        r = self.approve([clone(FAR_NFZ)], rev, 0)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "no_release")
        state, hdr_rev = self.ws_state()
        self.assertEqual(state["nfzs"], [])
        self.assertEqual(hdr_rev, rev)  # 修订号不变

    # —— 字段校验 ——
    def test_missing_fields(self):
        _, rev = self.publish_route()
        r = self.client.post("/api/nfz-approval",
                             json={"workspaceRev": rev, "releaseSeq": 1})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "missing_fields")
        r = self.client.post("/api/nfz-approval", json={
            "nfzs": "not-a-list", "workspaceRev": rev, "releaseSeq": 1})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "missing_fields")

    # —— 核准通过：仅替换工作区禁飞区 ——
    def test_approval_replaces_only_workspace_nfzs(self):
        _, rev = self.publish_route()
        # 发布后工作区航线被继续编辑（与发布快照不同）
        route2 = clone(ROUTE)
        route2["waypoints"][0]["name"] = "A-改"
        rev2 = self.put_state({"route": route2, "nfzs": []})
        rel_before = self.current_release()["release"]

        r = self.approve([clone(FAR_NFZ)], rev2, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["rev"], rev2 + 1)
        self.assertEqual(body["releaseSeq"], 1)

        # 工作区：禁飞区被原子替换，航线保留最新工作区航线，修订号 +1
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev2 + 1)
        self.assertEqual(state["nfzs"], [FAR_NFZ])
        self.assertEqual(state["route"]["waypoints"][0]["name"], "A-改")
        # 发布快照不变
        rel_after = self.current_release()["release"]
        self.assertEqual(rel_before, rel_after)
        self.assertEqual(rel_after["state"]["nfzs"], [])

    def test_approval_can_clear_nfzs(self):
        _, rev = self.publish_route()
        r = self.approve([], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        state, _ = self.ws_state()
        self.assertEqual(state["nfzs"], [])

    # —— 审查以最新发布快照航线为准（双向） ——
    def test_conflict_judged_against_released_route(self):
        _, rev = self.publish_route()
        # 工作区航线改到 y=500：候选 BLOCK_NFZ 压不到它，但压着发布快照航线
        route2 = clone(ROUTE)
        for wp in route2["waypoints"]:
            wp["y"] = 500
        rev2 = self.put_state({"route": route2, "nfzs": []})

        r = self.approve([clone(BLOCK_NFZ)], rev2, 1)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "analysis_conflicts")
        self.assertTrue(body["entries"])
        self.assertEqual(body["first"]["nfzId"], "nz1")
        # 工作区与发布均未改写
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev2)
        self.assertEqual(state["nfzs"], [])
        self.assertEqual(state["route"]["waypoints"][0]["y"], 500)
        self.assertEqual(self.current_release()["release"]["state"]["nfzs"], [])

        # 草稿继续修改：换成不冲突的候选后再次提交 → 通过
        r2 = self.approve([clone(FAR_NFZ)], rev2, 1)
        self.assertEqual(r2.status_code, 200, r2.get_json())
        state, _ = self.ws_state()
        self.assertEqual(state["nfzs"], [FAR_NFZ])

    def test_workspace_route_conflict_does_not_block_approval(self):
        _, rev = self.publish_route()
        # 工作区航线改到 y=550 穿过 FAR_NFZ，但发布快照航线（y=100）不冲突
        route2 = clone(ROUTE)
        for wp in route2["waypoints"]:
            wp["y"] = 550
        rev2 = self.put_state({"route": route2, "nfzs": []})
        r = self.approve([clone(FAR_NFZ)], rev2, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        state, _ = self.ws_state()
        self.assertEqual(state["nfzs"], [FAR_NFZ])
        self.assertEqual(state["route"]["waypoints"][0]["y"], 550)

    # —— 审查错误 ——
    def test_analysis_errors_reject(self):
        _, rev = self.publish_route()
        bad = clone(FAR_NFZ)
        bad["vertices"] = [[1, 2], [3, 4]]  # 顶点少于 3 个
        r = self.approve([bad], rev, 1)
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertEqual(body["reason"], "analysis_errors")
        self.assertTrue(body["errors"])
        state, hdr_rev = self.ws_state()
        self.assertEqual(state["nfzs"], [])
        self.assertEqual(hdr_rev, rev)

    # —— 乐观并发 ——
    def test_stale_workspace_rev(self):
        _, rev = self.publish_route()
        rev2 = self.put_state({"route": {}, "nfzs": [{"note": "changed"}]})
        r = self.approve([clone(FAR_NFZ)], rev, 1)  # 提交旧修订号
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "stale")
        self.assertEqual(body["details"]["workspaceRev"],
                         {"expected": rev, "actual": rev2})
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev2)
        self.assertEqual(state["nfzs"], [{"note": "changed"}])

    def test_stale_release_seq(self):
        vid, rev = self.publish_route()
        # 第二次发布把已发布号推到 2
        r = self.publish(vid, rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.approve([clone(FAR_NFZ)], rev, 1)  # 提交旧发布号
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "stale")
        self.assertEqual(body["details"]["releaseSeq"],
                         {"expected": 1, "actual": 2})
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev)
        self.assertEqual(state["nfzs"], [])

    # —— 失败不改写已发布内容 ——
    def test_failed_approval_keeps_release(self):
        _, rev = self.publish_route()
        rel_before = self.current_release()["release"]
        r = self.approve([clone(BLOCK_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.current_release()["release"], rel_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
