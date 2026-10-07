# -*- coding: utf-8 -*-
"""发布航线（POST /api/release）回归测试。

覆盖：
- 发布成功：版本航线 + 当前工作区禁飞区组合审查，发布号递增，
  落库来源版本 ID、组合状态与审查结果；
- 版本已删除 / 航线无非零长度航段 / 审查有错误 / 审查有冲突（附明细）；
- 工作区修订号或已发布号变化 → 409 并附 expected/actual 明细；
- 失败发布不改写原发布；发布后编辑工作区、删除来源版本均不改写已发布内容；
- 工作区修订号随 PUT /api/state 递增（GET 响应头 / PUT 响应体）。
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

# 干净航线：4km 直线，200m 等高，08:00 起飞
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
# 覆盖航线中段、时窗覆盖全程的禁飞区（与 ROUTE 必冲突）
BLOCK_NFZ = {
    "id": "nz1", "name": "阻断区", "enabled": True,
    "vertices": [[250, 50], [350, 50], [350, 150], [250, 150]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "2026-09-24T07:00", "endTime": "2026-09-24T09:00",
}


def clone(obj):
    return json.loads(json.dumps(obj))


class ReleaseApiTests(unittest.TestCase):
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
        body = r.get_json()
        self.assertTrue(body["ok"])
        return body["rev"]

    def save_version(self, state, name="v1"):
        r = self.client.post("/api/versions", json={"name": name, "state": state})
        self.assertEqual(r.status_code, 200)
        return r.get_json()["id"]

    def publish(self, vid, rev, seq):
        return self.client.post("/api/release", json={
            "versionId": vid, "workspaceRev": rev, "releaseSeq": seq})

    def current_release(self):
        r = self.client.get("/api/release")
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def clean_workspace(self):
        """空禁飞区工作区，返回其修订号。"""
        return self.put_state({"route": {"waypoints": []}, "nfzs": []})

    # —— 修订号流转 ——
    def test_workspace_rev_flow(self):
        r = self.client.get("/api/state")
        rev0 = int(r.headers["X-Workspace-Rev"])
        rev1 = self.put_state({"route": {}, "nfzs": []})
        self.assertEqual(rev1, rev0 + 1)
        r = self.client.get("/api/state")
        self.assertEqual(int(r.headers["X-Workspace-Rev"]), rev1)
        cur = self.current_release()
        self.assertEqual(cur["workspaceRev"], rev1)
        self.assertEqual(cur["releaseSeq"], 0)
        self.assertIsNone(cur["release"])

    # —— 成功发布 ——
    def test_publish_combines_version_route_with_workspace_nfzs(self):
        # 工作区禁飞区为空；版本自带一个禁飞区（发布时不得采用）
        rev = self.clean_workspace()
        vid = self.save_version({"route": ROUTE, "nfzs": [BLOCK_NFZ]}, name="发布源")
        r = self.publish(vid, rev, 0)
        self.assertEqual(r.status_code, 200, r.get_json())
        rel = r.get_json()["release"]
        self.assertEqual(rel["seq"], 1)
        self.assertEqual(rel["sourceVersionId"], vid)
        self.assertEqual(rel["sourceVersionName"], "发布源")
        self.assertEqual(rel["workspaceRev"], rev)
        # 组合状态：航线来自版本，禁飞区来自工作区（空）
        self.assertEqual(rel["state"]["route"]["waypoints"], ROUTE["waypoints"])
        self.assertEqual(rel["state"]["nfzs"], [])
        self.assertEqual(rel["analysis"]["errors"], [])
        self.assertEqual(rel["analysis"]["entries"], [])
        # 当前发布可查
        cur = self.current_release()
        self.assertEqual(cur["releaseSeq"], 1)
        self.assertEqual(cur["release"]["seq"], 1)

    def test_publish_seq_increments(self):
        rev = self.clean_workspace()
        vid = self.save_version({"route": ROUTE, "nfzs": []})
        r1 = self.publish(vid, rev, 0).get_json()
        self.assertTrue(r1["ok"], r1)
        r2 = self.publish(vid, rev, 1).get_json()
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(r2["release"]["seq"], 2)
        self.assertEqual(self.current_release()["releaseSeq"], 2)

    # —— 失败情形：原发布不变 ——
    def test_version_deleted(self):
        rev = self.clean_workspace()
        vid = self.save_version({"route": ROUTE, "nfzs": []})
        self.client.delete(f"/api/versions/{vid}")
        r = self.publish(vid, rev, 0)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["reason"], "version_not_found")
        self.assertIsNone(self.current_release()["release"])

    def test_no_nonzero_segment(self):
        rev = self.clean_workspace()
        dup = clone(ROUTE)
        dup["waypoints"][1] = dict(dup["waypoints"][0])  # 两航点重合
        vid = self.save_version({"route": dup, "nfzs": []})
        r = self.publish(vid, rev, 0)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "no_nonzero_segment")
        self.assertIsNone(self.current_release()["release"])

    def test_single_waypoint_route_has_no_segment(self):
        rev = self.clean_workspace()
        vid = self.save_version({"route": {"waypoints": [clone(ROUTE["waypoints"][0])]},
                                 "nfzs": []})
        r = self.publish(vid, rev, 0)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "no_nonzero_segment")

    def test_analysis_errors_reject(self):
        rev = self.clean_workspace()
        bad = clone(ROUTE)
        bad["departureTime"] = ""  # 起飞时刻缺失 → 审查错误
        vid = self.save_version({"route": bad, "nfzs": []})
        r = self.publish(vid, rev, 0)
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertEqual(body["reason"], "analysis_errors")
        self.assertTrue(body["errors"])
        self.assertIsNone(self.current_release()["release"])

    def test_analysis_conflicts_reject_with_details(self):
        rev = self.put_state({"route": {"waypoints": []}, "nfzs": [BLOCK_NFZ]})
        vid = self.save_version({"route": ROUTE, "nfzs": []})
        r = self.publish(vid, rev, 0)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "analysis_conflicts")
        self.assertTrue(body["entries"])
        self.assertEqual(body["entries"][0]["nfzId"], "nz1")
        self.assertEqual(body["first"]["nfzId"], "nz1")
        self.assertIsNone(self.current_release()["release"])

    def test_stale_workspace_rev(self):
        rev = self.clean_workspace()
        vid = self.save_version({"route": ROUTE, "nfzs": []})
        rev2 = self.put_state({"route": {}, "nfzs": [{"note": "changed"}]})
        self.assertEqual(rev2, rev + 1)
        r = self.publish(vid, rev, 0)  # 提交旧修订号
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "stale")
        self.assertEqual(body["details"]["workspaceRev"],
                         {"expected": rev, "actual": rev2})
        self.assertIsNone(self.current_release()["release"])

    def test_stale_release_seq(self):
        rev = self.clean_workspace()
        vid = self.save_version({"route": ROUTE, "nfzs": []})
        self.assertTrue(self.publish(vid, rev, 0).get_json()["ok"])
        r = self.publish(vid, rev, 0)  # 已发布号已是 1
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "stale")
        self.assertEqual(body["details"]["releaseSeq"],
                         {"expected": 0, "actual": 1})
        # 原发布不变
        self.assertEqual(self.current_release()["release"]["seq"], 1)

    def test_missing_fields(self):
        r = self.client.post("/api/release", json={"versionId": 1})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "missing_fields")

    # —— 不可变性 ——
    def test_failed_publish_keeps_existing_release(self):
        rev = self.clean_workspace()
        vid = self.save_version({"route": ROUTE, "nfzs": []})
        ok = self.publish(vid, rev, 0).get_json()
        self.assertTrue(ok["ok"], ok)
        # 工作区引入冲突后再发布 → 失败，原发布不变
        rev2 = self.put_state({"route": {"waypoints": []}, "nfzs": [BLOCK_NFZ]})
        r = self.publish(vid, rev2, 1)
        self.assertEqual(r.status_code, 409)
        cur = self.current_release()["release"]
        self.assertEqual(cur["seq"], 1)
        self.assertEqual(cur["state"]["nfzs"], [])

    def test_release_immutable_after_workspace_edit_and_version_delete(self):
        rev = self.clean_workspace()
        vid = self.save_version({"route": ROUTE, "nfzs": []})
        self.assertTrue(self.publish(vid, rev, 0).get_json()["ok"])
        before = self.current_release()["release"]
        # 之后编辑工作区、删除来源版本
        self.put_state({"route": {"waypoints": []}, "nfzs": [BLOCK_NFZ]})
        self.client.delete(f"/api/versions/{vid}")
        after = self.current_release()["release"]
        self.assertEqual(before, after)
        self.assertEqual(after["state"]["route"]["waypoints"], ROUTE["waypoints"])
        self.assertEqual(after["sourceVersionId"], vid)

    # —— 既有行为不回归 ——
    def test_analyze_endpoint_unchanged(self):
        r = self.client.post("/api/analyze",
                             json={"route": clone(ROUTE), "nfzs": [clone(BLOCK_NFZ)]})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["errors"], [])
        self.assertEqual(len(body["entries"]), 1)
        self.assertEqual(body["first"]["nfzId"], "nz1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
