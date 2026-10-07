# -*- coding: utf-8 -*-
"""页面提交与双页面交错的回归测试。

用两个独立测试客户端模拟两个浏览器页面（各自持有自己看到的工作区修订号），
覆盖：
- 页面“仅改禁飞区 → 提交核准”：核准前后均不应多产生一次 PUT /api/state；
  审查冲突/审查错误时修订号不增加、草稿可改后再提；
- 已发布后航线自动保存携带 X-Expected-Workspace-Rev：他页已变更时被 409
  拒绝（不写库、不递增修订号、不覆盖他页已核准内容），同步最新修订号后
  重新提交成功；未携带该头时保持原“无条件保存”行为（未发布自动保存不回归）；
- 双页面交错：A 页核准成功后，B 页的航线保存被拒绝且不会用 B 页缓存的
  旧禁飞区覆盖 A 页已核准内容；B 页核准也必须使用最新修订号；
- 保存与核准相邻：保存先落库后，核准须用保存返回的新修订号（旧号 409）；
- 核准通过/失败均不改写发布快照；
- 启动竞态（GET /api/state 先、GET /api/release 后，期间他页推进修订号）：
  再次 GET /api/state 失败时页面不得放行待发保存（用 /api/release 学到的
  新修订号会把编辑前所见旧航线/旧禁飞区整份写回且服务端会返回 200），
  必须保留本页航线/禁飞区草稿并阻断自动保存及相邻核准/发布，重试取回最新
  工作区后再分流：合入他页已核准禁飞区但草稿不落库；仅他页改航线时直接
  采用远端航线且不写库；双方都改航线才进入冲突选择，确认提交按最新修订号
  条件保存。
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
BLOCK_NFZ = {
    "id": "nz1", "name": "阻断区", "enabled": True,
    "vertices": [[250, 50], [350, 50], [350, 150], [250, 150]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "2026-09-24T07:00", "endTime": "2026-09-24T09:00",
}
FAR_NFZ = {
    "id": "nzFar", "name": "远侧限制区", "enabled": True,
    "vertices": [[100, 500], [200, 500], [200, 600], [100, 600]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "", "endTime": "",
}


def clone(obj):
    return json.loads(json.dumps(obj))


class WorkspaceConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_path = app.DB_PATH
        app.DB_PATH = os.path.join(self.tmp, "test.db")
        app.init_db()
        # 两个独立客户端 ≈ 两个浏览器页面
        self.page_a = app.app.test_client()
        self.page_b = app.app.test_client()

    def tearDown(self):
        app.DB_PATH = self.old_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    # —— 辅助 ——
    def get_state(self, client):
        r = client.get("/api/state")
        self.assertEqual(r.status_code, 200)
        return r.get_json(), int(r.headers["X-Workspace-Rev"])

    def put(self, client, state, expected_rev=None):
        headers = {}
        if expected_rev is not None:
            headers["X-Expected-Workspace-Rev"] = str(expected_rev)
        return client.put("/api/state", json=state, headers=headers)

    def put_ok(self, client, state, expected_rev=None):
        r = self.put(client, state, expected_rev)
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()["rev"]

    def save_version(self, client, state, name="v1"):
        r = client.post("/api/versions", json={"name": name, "state": state})
        self.assertEqual(r.status_code, 200)
        return r.get_json()["id"]

    def approve(self, client, nfzs, rev, seq):
        return client.post("/api/nfz-approval",
                           json={"nfzs": nfzs, "workspaceRev": rev, "releaseSeq": seq})

    def publish(self, client, vid, rev, seq):
        return client.post("/api/release", json={
            "versionId": vid, "workspaceRev": rev, "releaseSeq": seq})

    def release_info(self, client):
        r = client.get("/api/release")
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def publish_route_route_in_workspace(self, client):
        """发布 #1（快照航线 ROUTE），并让发布后工作区航线也为 ROUTE。

        返回 (vid, rev, seq)；此时他页核准禁飞区/改航线都会推进修订号，
        用于模拟“本页启动两次读取之间他页推进修订号”的竞态。
        """
        _, rev = self.publish_route(client)
        rev = self.put_ok(client, {"route": clone(ROUTE), "nfzs": []}, rev)
        r = client.get("/api/release").get_json()
        return r["release"]["sourceVersionId"], rev, r["releaseSeq"]

    def publish_route(self, client):
        """以 ROUTE 发布 #1（工作区禁飞区为空），返回 (vid, rev)。"""
        rev = self.put_ok(client, {"route": {"waypoints": []}, "nfzs": []})
        vid = self.save_version(client, {"route": ROUTE, "nfzs": []})
        r = self.publish(client, vid, rev, 0)
        self.assertEqual(r.status_code, 200, r.get_json())
        return vid, rev

    def release_snapshot(self, client):
        r = client.get("/api/release")
        self.assertEqual(r.status_code, 200)
        return r.get_json()["release"]

    # —— 条件 PUT：乐观并发 ——
    def test_conditional_put_rejects_stale_without_writing(self):
        rev = self.put_ok(self.page_a, {"route": {"waypoints": []}, "nfzs": []})
        # 他页先保存，修订号推进
        rev2 = self.put_ok(self.page_b, {"route": {"note": "他页内容"}, "nfzs": []})
        self.assertEqual(rev2, rev + 1)
        # 本页携带旧修订号保存 → 409，附明细，不写库、不递增
        r = self.put(self.page_a, {"route": {"note": "本页内容"}, "nfzs": []}, rev)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "stale")
        self.assertEqual(body["details"]["workspaceRev"],
                         {"expected": rev, "actual": rev2})
        state, hdr_rev = self.get_state(self.page_a)
        self.assertEqual(hdr_rev, rev2)
        self.assertEqual(state["route"], {"note": "他页内容"})

    def test_conditional_put_succeeds_with_fresh_rev(self):
        rev = self.put_ok(self.page_a, {"route": {"waypoints": []}, "nfzs": []})
        rev2 = self.put_ok(self.page_a, {"route": {"note": "x"}, "nfzs": []}, rev)
        self.assertEqual(rev2, rev + 1)

    def test_conditional_put_bad_header(self):
        r = self.page_a.put("/api/state", json={"route": {}, "nfzs": []},
                            headers={"X-Expected-Workspace-Rev": "abc"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "missing_fields")

    def test_unconditional_put_keeps_unpublished_autosave(self):
        # 不携带条件头时始终成功（未发布时的自动保存行为不回归）
        _, rev = self.get_state(self.page_a)
        self.put_ok(self.page_b, {"route": {"note": "他页"}, "nfzs": []})
        r2 = self.put(self.page_a, {"route": {"note": "本页"}, "nfzs": []})
        self.assertEqual(r2.status_code, 200)

    # —— 页面提交：仅改禁飞区不得预先 PUT ——
    def test_page_approval_only_does_not_pre_save(self):
        _, rev = self.publish_route(self.page_a)
        # 页面只改禁飞区草稿：不调用 PUT，直接提交核准
        r = self.approve(self.page_a, [clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["rev"], rev + 1)
        state, hdr_rev = self.get_state(self.page_a)
        self.assertEqual(hdr_rev, rev + 1)   # 只因核准本身 +1，无额外保存
        self.assertEqual(state["nfzs"], [FAR_NFZ])
        self.assertEqual(state["route"], {"waypoints": []})

    def test_conflict_approval_bumps_nothing_and_keeps_draft(self):
        _, rev = self.publish_route(self.page_a)
        snap_before = self.release_snapshot(self.page_a)
        # 候选禁飞区压住发布快照航线 → 拒绝，修订号不变、草稿内容未落库
        r = self.approve(self.page_a, [clone(BLOCK_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "analysis_conflicts")
        self.assertTrue(body["entries"])
        state, hdr_rev = self.get_state(self.page_a)
        self.assertEqual(hdr_rev, rev)
        self.assertEqual(state["nfzs"], [])
        # 发布快照不动；草稿改后可再次提交
        self.assertEqual(self.release_snapshot(self.page_a), snap_before)
        r2 = self.approve(self.page_a, [clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r2.status_code, 200, r2.get_json())
        self.assertEqual(r2.get_json()["rev"], rev + 1)

    # —— 双页面交错：他页核准后本页保存不得覆盖已核准禁飞区 ——
    def test_two_page_interleave_approval_then_stale_save(self):
        _, rev = self.publish_route(self.page_a)
        # 两页都看到同一修订号
        _, rev_a = self.get_state(self.page_a)
        _, rev_b = self.get_state(self.page_b)
        self.assertEqual((rev_a, rev_b), (rev, rev))

        # A 页仅改禁飞区并核准成功：工作区禁飞区变为 FAR_NFZ，修订号 +1
        r = self.approve(self.page_a, [clone(FAR_NFZ)], rev_a, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev_after = r.get_json()["rev"]
        self.assertEqual(rev_after, rev + 1)

        # B 页随后保存航线（按前端语义：携带旧修订号、带本页缓存的旧禁飞区 []）
        route_b = clone(ROUTE)
        route_b["waypoints"][0]["name"] = "B页改名"
        r = self.put(self.page_b, {"route": route_b, "nfzs": []}, rev_b)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["details"]["workspaceRev"],
                         {"expected": rev, "actual": rev_after})
        # 关键：B 页缓存的旧禁飞区没有覆盖 A 页已核准内容
        state, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev_after)
        self.assertEqual(state["nfzs"], [FAR_NFZ])
        self.assertEqual(state["route"], {"waypoints": []})

        # B 页同步最新修订号后重试保存：只落航线，禁飞区沿用已核准内容
        route_merged = clone(route_b)
        rev_b2 = self.put_ok(self.page_b,
                             {"route": route_merged, "nfzs": [clone(FAR_NFZ)]},
                             rev_after)
        state, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev_b2)
        self.assertEqual(state["nfzs"], [FAR_NFZ])
        self.assertEqual(state["route"]["waypoints"][0]["name"], "B页改名")

        # B 页用旧修订号提交核准仍被拒，明细告知当前值；用新号则通过
        r = self.approve(self.page_b, [], rev, 1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["details"]["workspaceRev"],
                         {"expected": rev, "actual": rev_b2})
        r = self.approve(self.page_b, [], rev_b2, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        state, _ = self.get_state(self.page_b)
        self.assertEqual(state["nfzs"], [])          # 新核准生效
        self.assertEqual(state["route"]["waypoints"][0]["name"], "B页改名")  # 航线保留

    def test_two_page_interleave_release_seq_change(self):
        vid, rev = self.publish_route(self.page_a)
        # A 页再次发布，发布号推进到 2（工作区修订号不变）
        r = self.publish(self.page_a, vid, rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        # B 页拿旧发布号提交核准 → 409 附 releaseSeq 明细，且修订号不增加
        r = self.approve(self.page_b, [clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "stale")
        self.assertEqual(body["details"]["releaseSeq"],
                         {"expected": 1, "actual": 2})
        _, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev)

    # —— 保存与核准相邻：顺序与修订号协调 ——
    def test_adjacent_save_then_approval_uses_new_rev(self):
        _, rev = self.publish_route(self.page_a)
        # 先有一次航线自动保存落库（前端会等它完成），修订号推进
        route2 = clone(ROUTE)
        route2["waypoints"][0]["name"] = "自动保存改名"
        rev2 = self.put_ok(self.page_a,
                           {"route": route2, "nfzs": []}, rev)
        # 核准必须携带保存后的新修订号：旧号被拒、新号通过
        r = self.approve(self.page_a, [clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 409)
        r = self.approve(self.page_a, [clone(FAR_NFZ)], rev2, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        # 核准通过：仅替换禁飞区，最新工作区航线保留
        state, hdr_rev = self.get_state(self.page_a)
        self.assertEqual(state["nfzs"], [FAR_NFZ])
        self.assertEqual(state["route"]["waypoints"][0]["name"], "自动保存改名")
        self.assertEqual(hdr_rev, rev2 + 1)

    def test_approval_never_touches_release_snapshot(self):
        _, rev = self.publish_route(self.page_a)
        snap_before = self.release_snapshot(self.page_a)
        r = self.approve(self.page_a, [clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.release_snapshot(self.page_a), snap_before)
        # 失败路径同样不动快照
        _, rev2 = self.get_state(self.page_a)
        r = self.approve(self.page_a, [clone(BLOCK_NFZ)], rev2, 1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.release_snapshot(self.page_a), snap_before)

    # —— 双页交错：两页各改航线（修复“409 后换新号整份覆盖且提示成功”）——
    def test_two_page_both_edit_route_no_silent_overwrite(self):
        # 发布 #1：发布快照航线为 ROUTE，发布时工作区航线为空、禁飞区为空
        _, rev = self.publish_route(self.page_a)
        # 发布后工作区航线先恢复为 ROUTE（两页随后都基于它编辑）
        rev = self.put_ok(self.page_a, {"route": clone(ROUTE), "nfzs": []}, rev)
        _, rev_a = self.get_state(self.page_a)
        _, rev_b = self.get_state(self.page_b)
        self.assertEqual(rev_a, rev_b)

        route_a = clone(ROUTE)
        route_a["waypoints"][0]["name"] = "A页航线"
        route_b = clone(ROUTE)
        route_b["waypoints"][1]["name"] = "B页航线"

        # A 页先保存成功
        rev_a2 = self.put_ok(self.page_a, {"route": route_a, "nfzs": []}, rev_a)
        # B 页携带旧修订号保存 → 409，服务器航线仍是 A 页的，未被覆盖；
        # 关键：即使 B 页随后“换新号重试整份写入”，服务器也必须保留 A 页内容
        # 直到 B 页走“确认提交本页航线”的显式分流（旧前端曾在此直接覆盖）
        r = self.put(self.page_b, {"route": route_b, "nfzs": []}, rev_b)
        self.assertEqual(r.status_code, 409)
        st, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev_a2)
        self.assertEqual(st["route"]["waypoints"][0]["name"], "A页航线")
        self.assertEqual(st["route"]["waypoints"][1]["name"], "B")
        self.assertEqual(st["nfzs"], [])

        # B 页取回最新内容并比对：服务器航线 ≠ 编辑前所见（双方都改了），
        # 本页航线与禁飞区草稿保留，暂停自动保存——不自动重试
        # （前端协议：只有在用户“确认提交本页航线”后才用最新修订号条件写入）
        rev_b_fresh = hdr_rev
        # 用户确认提交 B 页航线（禁飞区沿用已核准基线 []）→ 成功，服务器变为 B 页航线
        rev_b2 = self.put_ok(self.page_b, {"route": route_b, "nfzs": []}, rev_b_fresh)
        st, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev_b2)
        self.assertEqual(st["route"]["waypoints"][0]["name"], "A")  # B 页未改首点
        self.assertEqual(st["route"]["waypoints"][1]["name"], "B页航线")

        # 发布快照始终是最初发布的 ROUTE，未被任何自动保存改写
        snap = self.release_snapshot(self.page_a)
        self.assertEqual([w["name"] for w in snap["state"]["route"]["waypoints"]],
                         ["A", "B"])
        self.assertEqual(snap["state"]["nfzs"], [])

    def test_two_page_route_conflict_then_second_409_keeps_local(self):
        # 确认提交后再遇 409：继续保留本页编辑，不覆盖、不提示成功
        _, rev = self.publish_route(self.page_a)
        rev = self.put_ok(self.page_a, {"route": clone(ROUTE), "nfzs": []}, rev)
        route_a = clone(ROUTE); route_a["waypoints"][0]["name"] = "A-1"
        route_b = clone(ROUTE); route_b["waypoints"][1]["name"] = "B-1"
        rev_a2 = self.put_ok(self.page_a, {"route": route_a, "nfzs": []}, rev)

        # B 页用旧号 → 409；A 页又保存了一次（A-2），B 页用刚取回的号仍 409
        r = self.put(self.page_b, {"route": route_b, "nfzs": []}, rev)
        self.assertEqual(r.status_code, 409)
        route_a2 = clone(route_a); route_a2["waypoints"][0]["name"] = "A-2"
        rev_a3 = self.put_ok(self.page_a, {"route": route_a2, "nfzs": []}, rev_a2)
        r = self.put(self.page_b, {"route": route_b, "nfzs": []}, rev_a2)
        self.assertEqual(r.status_code, 409)
        # 服务器仍是 A 页最新航线；B 页内容从未写库
        st, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev_a3)
        self.assertEqual(st["route"]["waypoints"][0]["name"], "A-2")
        self.assertEqual(st["route"]["waypoints"][1]["name"], "B")
        # B 页最终用最新号确认提交 → 成功
        rev_b = self.put_ok(self.page_b, {"route": route_b, "nfzs": []}, rev_a3)
        st, _ = self.get_state(self.page_b)
        self.assertEqual([w["name"] for w in st["route"]["waypoints"]], ["A", "B-1"])
        self.assertEqual(rev_b, rev_a3 + 1)

    def test_two_page_nfz_draft_survives_route_conflict_and_approval(self):
        # 双方都改航线时，B 页还带着禁飞区草稿：冲突处理全程草稿不得落库；
        # B 页确认提交航线时请求体只含已核准禁飞区（此处为空），草稿仍在本页
        _, rev = self.publish_route(self.page_a)
        rev = self.put_ok(self.page_a, {"route": clone(ROUTE), "nfzs": []}, rev)
        route_a = clone(ROUTE); route_a["corridorHalfWidth"] = 0.5
        route_b = clone(ROUTE); route_b["unitScale"] = 200
        rev_a2 = self.put_ok(self.page_a, {"route": route_a, "nfzs": []}, rev)
        r = self.put(self.page_b, {"route": route_b, "nfzs": []}, rev)
        self.assertEqual(r.status_code, 409)

        # B 页本地持有一个禁飞区草稿（模拟画布草稿，绝不随航线保存落库）
        # 确认提交航线：请求体禁飞区仍为已核准基线 []，草稿不在请求中
        rev_b = self.put_ok(self.page_b, {"route": route_b, "nfzs": []}, rev_a2)
        st, _ = self.get_state(self.page_b)
        self.assertEqual(st["nfzs"], [])                # 草稿未落库
        self.assertEqual(st["route"]["unitScale"], 200)  # B 页航线已保存

        # 随后 B 页提交草稿核准（FAR_NFZ 不压发布快照航线）→ 通过；
        # 工作区航线保留为 B 页航线，发布快照航线不变
        r = self.approve(self.page_b, [clone(FAR_NFZ)], rev_b, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        st, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(st["nfzs"], [FAR_NFZ])
        self.assertEqual(st["route"]["unitScale"], 200)
        snap = self.release_snapshot(self.page_b)
        self.assertEqual(snap["state"]["nfzs"], [])
        self.assertEqual(snap["state"]["route"]["unitScale"], 100)

    def test_adjacent_approval_after_route_conflict_must_use_latest_rev(self):
        # 保存定时器/在途请求与禁飞区核准相邻：航线冲突未解决时，
        # 不得拿旧修订号把核准做成；用旧号必 409，最新号通过且快照不动
        _, rev = self.publish_route(self.page_a)
        rev = self.put_ok(self.page_a, {"route": clone(ROUTE), "nfzs": []}, rev)
        route_a = clone(ROUTE); route_a["waypoints"][0]["alt"] = 300
        route_b = clone(ROUTE); route_b["waypoints"][1]["alt"] = 300
        rev_a2 = self.put_ok(self.page_a, {"route": route_a, "nfzs": []}, rev)
        # B 页在途保存撞 409（双方航线冲突，前端此刻暂停，不继续核准）
        r = self.put(self.page_b, {"route": route_b, "nfzs": []}, rev)
        self.assertEqual(r.status_code, 409)
        # 若仍拿旧号提交核准 → 409 stale，工作区/发布均不变
        r = self.approve(self.page_b, [clone(FAR_NFZ)], rev, 1)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "stale")
        snap = self.release_snapshot(self.page_b)
        st, hdr_rev = self.get_state(self.page_b)
        self.assertEqual(hdr_rev, rev_a2)
        self.assertEqual(st["route"]["waypoints"][0]["alt"], 300)  # A 页航线保留
        # B 页解决冲突（确认提交本页航线）后，再用最新号核准 → 通过
        rev_b = self.put_ok(self.page_b, {"route": route_b, "nfzs": []}, rev_a2)
        r = self.approve(self.page_b, [clone(FAR_NFZ)], rev_b, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        st, _ = self.get_state(self.page_b)
        self.assertEqual(st["nfzs"], [FAR_NFZ])
        self.assertEqual(st["route"]["waypoints"][1]["alt"], 300)
        # 自动保存/核准全程不改写发布快照
        self.assertEqual(self.release_snapshot(self.page_b), snap)

    # —— 启动竞态：GET /api/state → GET /api/release 之间他页推进修订号 ——
    # 下面的 StartupPage 严格按前端启动协调协议走 HTTP：
    #   ① GET /api/state（记录 seenRev/基线航线/已核准禁飞区，可先在本页编辑）
    #   ② GET /api/release（得到权威 workspaceRev；与 seenRev 不同必须协调）
    #   ③ 协调再 GET /api/state：失败 → 阻断一切写入（coord_failed=True），
    #      重试成功后再分流；成功 → 合入已核准禁飞区（草稿不落库），并按
    #      本页/远端航线是否相对基线变化分流（远端未变→条件保存；仅远端变
    #      →采用远端不写库；双方都变→挂起冲突，确认提交才条件写入）。
    class StartupPage:
        def __init__(self, outer, client):
            self.o = outer
            self.c = client
            self.route = None
            self.nfzs = None
            self.saved_nfzs = None
            self.base_route = None
            self.seen_rev = None
            self.rev = None
            self.seq = None
            self.coord_failed = False
            self.route_conflict = None
            self.puts = 0   # 协调/重试期间发出的 PUT /api/state 次数

        def get_state(self):
            st, rev = self.o.get_state(self.c)
            self.route, self.nfzs = st["route"], st["nfzs"]
            self.saved_nfzs = clone(self.nfzs)
            self.base_route = clone(self.route)
            # 编辑前已核准禁飞区基线：协调时据此判断本页是否存在禁飞区草稿
            self._nfz_baseline = clone(self.nfzs)
            self.seen_rev = self.rev = rev
            return st

        def edit_route(self, **changes):
            self.route = clone(self.route)
            self.route.update(changes)

        def edit_nfz_draft(self, nfzs):
            self.nfzs = clone(nfzs)  # 已有发布：仅页面草稿，不进任何请求体

        def load_release_and_coordinate(self, coord_fails=0):
            info = self.o.release_info(self.c)
            self.seq = info["releaseSeq"]
            if isinstance(info["workspaceRev"], int):
                self.rev = info["workspaceRev"]
            if self.rev == self.seen_rev:
                return
            self._coordinate(failures_left=coord_fails)

        def _coordinate(self, failures_left):
            if failures_left > 0:
                self.coord_failed = True
                return
            st, fresh_rev = self.o.get_state(self.c)
            self.coord_failed = False
            remote_route, remote_nfzs = st["route"], st["nfzs"]
            remote_changed = remote_route != self.base_route
            local_changed = self.route != self.base_route
            # 任何情况下先合入他页已核准禁飞区：有草稿时只换基线，草稿不落库
            self.saved_nfzs = clone(remote_nfzs)
            if self.nfzs == self._nfz_baseline:
                self.nfzs = clone(remote_nfzs)
            if not remote_changed:
                self.rev = fresh_rev
                if not local_changed:
                    self.route = clone(remote_route)
                self.base_route = clone(remote_route)
                if local_changed:
                    self._save_route()
            elif not local_changed:
                self.rev = fresh_rev
                self.route = clone(remote_route)
                self.base_route = clone(remote_route)
            else:
                self.rev = fresh_rev
                self.base_route = clone(remote_route)
                self.route_conflict = clone(remote_route)

        def retry_coordination(self, failures_left=0):
            self.o.assertTrue(self.coord_failed)
            self._coordinate(failures_left=failures_left)

        def _save_route(self):
            # 已发布航线条件保存：请求体只含本页航线 + 已核准禁飞区基线，
            # 绝不携带本页草稿；携带所见最新修订号
            self.puts += 1
            r = self.o.put(self.c,
                           {"route": self.route, "nfzs": clone(self.saved_nfzs)},
                           self.rev)
            if r.status_code == 200:
                self.rev = r.get_json()["rev"]
                self.base_route = clone(self.route)
                self.route_conflict = None
            else:
                self.o.assertEqual(r.status_code, 409, r.get_json())
            return r

        def pending_save(self):
            """协调期间被挡下的待发保存：仅在未失败/未挂冲突时才允许发出。"""
            if self.coord_failed or self.route_conflict:
                return None
            return self._save_route()

        def approve_draft(self, nfzs):
            if self.coord_failed:
                return None  # 前端：协调失败期间相邻核准写入一律阻断
            return self.o.approve(self.c, nfzs, self.rev, self.seq)

        def resolve_conflict_submit_local(self):
            self.o.assertIsNotNone(self.route_conflict)
            self.route_conflict = None
            return self._save_route()

    def test_startup_coord_fetch_failure_blocks_writes_until_retry(self):
        # 前置：发布 #1，发布后工作区航线为 ROUTE，禁飞区为空，修订号 rev0
        _, rev0, _ = self.publish_route_route_in_workspace(self.page_a)
        page = self.StartupPage(self, self.page_b)
        page.get_state()               # ① 先读工作区：seen_rev=rev0
        page.edit_route(corridorHalfWidth=0.5)                 # 本页改了航线
        page.edit_nfz_draft([clone(FAR_NFZ)])                 # 本页禁飞区草稿

        # 他页在两次读取之间核准禁飞区（修订号推进到 rev0+1）
        r = self.approve(self.page_a, [clone(FAR_NFZ)], rev0, 1)
        self.assertEqual(r.status_code, 200, r.get_json())

        # ② 读发布状态拿到新修订号；③ 协调再 GET 失败 → 阻断
        page.load_release_and_coordinate(coord_fails=1)
        self.assertTrue(page.coord_failed)
        # 关键：失败期间待发保存/相邻核准一律不得发出
        self.assertIsNone(page.pending_save())
        self.assertIsNone(page.approve_draft([clone(FAR_NFZ)]))
        self.assertEqual(page.puts, 0)
        st, hdr = self.get_state(self.page_b)
        self.assertEqual(hdr, rev0 + 1)
        self.assertEqual(st["nfzs"], [FAR_NFZ])     # 他页已核准内容完好
        self.assertEqual(st["route"], ROUTE)
        # 本页草稿仍在手里（航线编辑、禁飞区草稿都未丢）
        self.assertEqual(page.route["corridorHalfWidth"], 0.5)
        self.assertEqual(page.nfzs, [FAR_NFZ])

        # 重试取回最新工作区：远端航线未变（仅禁飞区被核准）→ 合入已核准
        # 禁飞区基线、用最新修订号条件保存本页航线；草稿不进请求体
        page.retry_coordination(failures_left=0)
        self.assertFalse(page.coord_failed)
        self.assertEqual(page.puts, 1)
        st, hdr = self.get_state(self.page_b)
        self.assertEqual(hdr, rev0 + 2)
        self.assertEqual(st["route"]["corridorHalfWidth"], 0.5)
        self.assertEqual(st["nfzs"], [FAR_NFZ])     # 基线合入，非草稿写回
        # 草稿与已核准内容一致与否都不影响服务端：随后核准空草稿会替换禁飞区
        r = self.approve(self.page_b, [], hdr, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        st, hdr2 = self.get_state(self.page_b)
        self.assertEqual(st["nfzs"], [])
        self.assertEqual(st["route"]["corridorHalfWidth"], 0.5)

    def test_startup_coord_failure_then_retry_sees_remote_route_change(self):
        # 他页在两次读取之间改了航线（并顺带核准了禁飞区）；本页航线未改：
        # 重试取回后应直接采用远端航线，不发任何 PUT，已核准禁飞区合入
        _, rev0, _ = self.publish_route_route_in_workspace(self.page_a)
        page = self.StartupPage(self, self.page_b)
        page.get_state()
        page.edit_nfz_draft([clone(FAR_NFZ)])  # 只有禁飞区草稿，航线没动

        route_a = clone(ROUTE); route_a["corridorHalfWidth"] = 0.42
        rev1 = self.put_ok(self.page_a, {"route": route_a, "nfzs": []}, rev0)
        r = self.approve(self.page_a, [clone(FAR_NFZ)], rev1, 1)
        self.assertEqual(r.status_code, 200, r.get_json())

        page.load_release_and_coordinate(coord_fails=1)
        self.assertTrue(page.coord_failed)
        self.assertIsNone(page.pending_save())     # 失败期间不写
        page.retry_coordination(failures_left=0)
        self.assertFalse(page.coord_failed)
        self.assertEqual(page.puts, 0)             # 直接采用远端航线：无需写库
        self.assertEqual(page.route, route_a)
        self.assertEqual(page.saved_nfzs, [FAR_NFZ])  # 已核准禁飞区合入基线
        self.assertEqual(page.nfzs, [FAR_NFZ])         # 草稿恰好同形，仍只作草稿
        st, hdr = self.get_state(self.page_b)
        self.assertEqual(hdr, rev1 + 1)
        self.assertEqual(st["route"], route_a)

    def test_startup_coord_both_routes_changed_enters_conflict_choice(self):
        # 双方都改航线：重试取回后挂起冲突，不放行待发保存；用户确认提交后
        # 才用最新修订号条件写入本页航线（禁飞区沿用已核准基线，草稿不落库）
        _, rev0, _ = self.publish_route_route_in_workspace(self.page_a)
        page = self.StartupPage(self, self.page_b)
        page.get_state()
        page.edit_route(unitScale=200)
        page.edit_nfz_draft([clone(FAR_NFZ)])

        route_a = clone(ROUTE); route_a["corridorHalfWidth"] = 0.42
        rev1 = self.put_ok(self.page_a, {"route": route_a, "nfzs": []}, rev0)

        page.load_release_and_coordinate(coord_fails=1)
        page.retry_coordination(failures_left=0)
        self.assertIsNotNone(page.route_conflict)   # 进入冲突选择
        self.assertIsNone(page.pending_save())      # 自动保存仍暂停
        self.assertEqual(page.puts, 0)
        self.assertEqual(page.nfzs, [FAR_NFZ])      # 草稿保留
        self.assertEqual(page.saved_nfzs, [])       # 远端禁飞区基线（无核准）
        # 确认提交本页航线：最新修订号条件写入，请求体禁飞区为已核准基线 []
        r = page.resolve_conflict_submit_local()
        self.assertEqual(r.status_code, 200, r.get_json())
        st, hdr = self.get_state(self.page_b)
        self.assertEqual(hdr, rev1 + 1)
        self.assertEqual(st["route"]["unitScale"], 200)
        self.assertEqual(st["route"]["corridorHalfWidth"], 0.3)  # 本页未改此项
        self.assertEqual(st["nfzs"], [])            # 草稿从未落库

    def test_startup_new_rev_with_stale_payload_is_accepted_without_coord(self):
        # 记录被修复的危害本身（服务端无法替前端判断“旧航线”）：若协调失败时
        # 仍放行待发保存，前端用 /api/release 学到的新修订号携带编辑前所见的
        # 旧航线 + 旧禁飞区条件 PUT，服务端会 200 接受——他页航线改动与已
        # 核准禁飞区被整份覆盖。因此前端必须在再 GET 失败时阻断（上一条用例）。
        _, rev0, _ = self.publish_route_route_in_workspace(self.page_a)
        # 本页启动读到 rev0 / ROUTE / []
        st_b, rev_b = self.get_state(self.page_b)
        self.assertEqual(rev_b, rev0)
        # 他页改航线并核准禁飞区，修订号推进两次
        route_a = clone(ROUTE); route_a["corridorHalfWidth"] = 0.42
        rev1 = self.put_ok(self.page_a, {"route": route_a, "nfzs": []}, rev0)
        r = self.approve(self.page_a, [clone(FAR_NFZ)], rev1, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev_after = r.get_json()["rev"]
        info = self.release_info(self.page_b)
        self.assertEqual(info["workspaceRev"], rev_after)
        # 危险写法（修复前前端在协调再 GET 失败时会这么发）：新号 + 旧内容
        r = self.put(self.page_b,
                     {"route": clone(st_b["route"]), "nfzs": clone(st_b["nfzs"])},
                     rev_after)
        self.assertEqual(r.status_code, 200, r.get_json())  # 条件头“恰好最新”→ 覆盖成功
        st, hdr = self.get_state(self.page_b)
        self.assertEqual(hdr, rev_after + 1)
        self.assertEqual(st["route"], ROUTE)       # 他页航线改动被旧内容抹掉
        self.assertEqual(st["nfzs"], [])           # 他页已核准禁飞区被旧内容抹掉


if __name__ == "__main__":
    unittest.main(verbosity=2)
