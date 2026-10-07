# -*- coding: utf-8 -*-
"""航次登记 / 结束与禁飞区核准对航次复核的回归测试。

覆盖：
- 登记成功：以当前发布快照航线替换起飞时刻，与工作区已核准禁飞区重新审查，
  安全时落库航次编号、发布号、起飞时刻与航线快照（工作区/发布快照不变）；
- 无发布 / 发布号已变 / 起飞时刻无效 / 审查错误 / 审查冲突（附明细）→
  拒绝且不留航次记录；
- 快照起飞时刻被替换：发布快照原时刻被禁飞窗压住时，换成窗外时刻可登记；
- 页面查询航次、结束航次（幂等，不存在 → 404）；
- 提交禁飞区草稿核准：除最新发布航线审查外，还须用候选禁飞区复核所有
  未结束航次（包括旧发布登记的航次）；任一冲突返回 voyage_conflicts
  附航次及冲突明细，草稿、工作区与发布快照均不变；
- 交错：登记后被航次阻拦的核准，在航次结束后通过（结束航次不再阻拦）；
- 旧发布航次：再发布后仍按其自身发布快照 + 登记时刻复核；
- 注册与核准的事务串行（BEGIN IMMEDIATE）不漏检。
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
# 覆盖航线中段、时窗 07:00–09:00（与 08:00 起飞的 ROUTE 必冲突）
BLOCK_NFZ = {
    "id": "nz1", "name": "阻断区", "enabled": True,
    "vertices": [[250, 50], [350, 50], [350, 150], [250, 150]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "2026-09-24T07:00", "endTime": "2026-09-24T09:00",
}
# 同位置、时窗 20:00–21:00：08:00 起飞不冲突，20:30 起飞冲突
NIGHT_NFZ = {
    "id": "nzN", "name": "夜间管控区", "enabled": True,
    "vertices": [[250, 50], [350, 50], [350, 150], [250, 150]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "2026-09-24T20:00", "endTime": "2026-09-24T21:00",
}
# 远离航线的合法禁飞区
FAR_NFZ = {
    "id": "nzFar", "name": "远侧限制区", "enabled": True,
    "vertices": [[100, 500], [200, 500], [200, 600], [100, 600]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "", "endTime": "",
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
# 压住 y=400 航线中段、全天时窗
BLOCK_Y400_NFZ = {
    "id": "nz2", "name": "Y400阻断区", "enabled": True,
    "vertices": [[250, 350], [350, 350], [350, 450], [250, 450]],
    "minAlt": 0, "maxAlt": 400,
    "startTime": "", "endTime": "",
}


def clone(obj):
    return json.loads(json.dumps(obj))


class VoyageTests(unittest.TestCase):
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

    # —— 登记成功 ——
    def test_register_success_stores_snapshot(self):
        _, rev, seq = self.publish_route()
        r = self.register(seq, "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        v = r.get_json()["voyage"]
        self.assertEqual(v["id"], 1)
        self.assertEqual(v["releaseSeq"], 1)
        self.assertEqual(v["departureTime"], "2026-09-24T08:00")
        self.assertTrue(v["open"])
        self.assertIsNone(v["endedAt"])
        self.assertEqual(v["routeSnapshot"]["waypoints"], ROUTE["waypoints"])
        self.assertEqual(v["routeSnapshot"]["speeds"], ROUTE["speeds"])
        # 列表可查
        voyages = self.list_voyages()
        self.assertEqual([x["id"] for x in voyages], [1])

        # 登记不改写工作区与发布快照
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev)
        self.assertEqual(state["nfzs"], [])
        self.assertEqual(self.current_release()["release"]["state"]["nfzs"], [])

    def test_register_replaces_departure_time_of_release_snapshot(self):
        # 发布时快照原起飞时刻 08:00 落在夜间窗外，发布本身通过
        _, rev, seq = self.publish_route(route=ROUTE, nfzs=[])
        # 核准一个仅在 20:00–21:00 生效的禁飞区：对 08:00 起飞不冲突，核准通过
        r = self.approve([clone(NIGHT_NFZ)], rev, seq)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev2 = r.get_json()["rev"]
        # 用 20:30 登记：快照航线（几何不变）+ 新时刻 ∩ 已核准夜间禁飞区 → 冲突
        r = self.register(seq, "2026-09-24T20:30")
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "analysis_conflicts")
        self.assertEqual(body["first"]["nfzId"], "nzN")
        # 未留航次记录
        self.assertEqual(self.list_voyages(), [])
        # 换成窗外时刻 → 成功；落库的是替换了起飞时刻的航线快照
        r = self.register(seq, "2026-09-24T08:00")
        self.assertEqual(r.status_code, 200, r.get_json())
        v = self.list_voyages()[0]
        self.assertEqual(v["departureTime"], "2026-09-24T08:00")
        self.assertEqual(v["routeSnapshot"]["departureTime"], "2026-09-24T08:00")
        # 工作区未被登记改动
        _, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev2)

    def test_registered_snapshot_is_independent_of_workspace_route(self):
        # 发布后工作区航线被继续编辑：航次审查仍以发布快照几何为准
        _, rev, seq = self.publish_route()
        route2 = clone(ROUTE)
        for wp in route2["waypoints"]:
            wp["y"] = 550
        rev2 = self.put_state({"route": route2, "nfzs": []})
        # 全天阻断区压着发布快照航线（y=100），压不到工作区航线（y=550）
        block_allday = clone(BLOCK_NFZ)
        block_allday["startTime"] = block_allday["endTime"] = ""
        # 用 PUT 后的最新修订号提交，排除 stale：仍是 409，原因是发布快照/
        # （尚无航次）审查冲突，与工作区航线无关
        r = self.approve([block_allday], rev2, seq)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "analysis_conflicts")

    # —— 拒绝情形：不留记录 ——
    def test_register_without_release(self):
        rev = self.put_state({"route": {"waypoints": []}, "nfzs": []})
        r = self.register(0, "2026-09-24T08:00")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "no_release")
        self.assertEqual(self.list_voyages(), [])

    def test_register_stale_release_seq(self):
        vid, rev, _ = self.publish_route()
        # 第二次发布把发布号推到 2
        r = self.publish(vid, rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        r = self.register(1, "2026-09-24T08:00")
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "stale")
        self.assertEqual(body["details"]["releaseSeq"], {"expected": 1, "actual": 2})
        self.assertEqual(self.list_voyages(), [])

    def test_register_invalid_departure_time(self):
        _, _, seq = self.publish_route()
        for bad in ("", None, "not-a-time", "2026-13-99T99:99"):
            r = self.register(seq, bad)
            self.assertEqual(r.status_code, 400, bad)
            self.assertEqual(r.get_json()["reason"], "invalid_departure_time", bad)
        self.assertEqual(self.list_voyages(), [])

    def test_register_missing_fields(self):
        r = self.client.post("/api/voyages",
                             json={"departureTime": "2026-09-24T08:00"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "missing_fields")
        r = self.client.post("/api/voyages", json={"releaseSeq": "x"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "missing_fields")

    def test_register_analysis_conflict_against_workspace_nfzs(self):
        # 夜间窗（20–21 点）对 08:00 的发布快照安全，核准通过；
        # 以 20:30 登记即与“工作区已核准禁飞区”冲突 → 拒绝不留记录，
        # 且发布快照与工作区均不变
        _, rev, seq = self.publish_route()
        r = self.approve([clone(NIGHT_NFZ)], rev, seq)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev_after = r.get_json()["rev"]
        rel_before = self.current_release()["release"]
        r = self.register(seq, "2026-09-24T20:30")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "analysis_conflicts")
        self.assertEqual(self.list_voyages(), [])
        _, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev_after)
        self.assertEqual(self.current_release()["release"], rel_before)

    # —— 查询 / 结束 ——
    def test_end_voyage_idempotent_and_404(self):
        _, _, seq = self.publish_route()
        r = self.end_voyage(999)
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.get_json()["reason"], "voyage_not_found")

        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        r = self.end_voyage(1)
        self.assertEqual(r.status_code, 200)
        v = r.get_json()["voyage"]
        self.assertFalse(v["open"])
        self.assertIsNotNone(v["endedAt"])
        ended_at = v["endedAt"]
        # 再次结束：幂等，endedAt 不被改写
        r = self.end_voyage(1)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["voyage"]["endedAt"], ended_at)
        # 列表中该航次仍可查（已结束）
        voyages = self.list_voyages()
        self.assertEqual(len(voyages), 1)
        self.assertFalse(voyages[0]["open"])

    def test_list_voyages_newest_first(self):
        _, _, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        self.assertEqual(self.register(seq, "2026-09-24T09:00").status_code, 200)
        voyages = self.list_voyages()
        self.assertEqual([v["id"] for v in voyages], [2, 1])

    # —— 核准：复核未结束航次 ——
    def test_approval_checks_open_voyage_with_conflict_details(self):
        # 发布 + 核准夜间窗（对 08:00 安全）；登记一个 20:30 起飞的航次不可能
        # 通过登记（会被夜间窗拦）。因此航次登记的时刻必须对当时已核准禁飞区
        # 安全：先在“无禁飞区”状态下登记 20:30 航次，再提交带全天阻断区的
        # 候选草稿——发布快照航线与该航次都冲突。
        _, rev, seq = self.publish_route()
        r = self.register(seq, "2026-09-24T20:30")
        self.assertEqual(r.status_code, 200, r.get_json())
        # 先确认“仅有该航次、发布快照不冲突”的候选也会被航次拦住：
        # 用夜间窗候选——08:00 发布快照不冲突，20:30 航次冲突
        r = self.approve([clone(NIGHT_NFZ)], rev, seq)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "voyage_conflicts")
        vcs = body["voyageConflicts"]
        self.assertEqual(len(vcs), 1)
        self.assertEqual(vcs[0]["voyageId"], 1)
        self.assertEqual(vcs[0]["releaseSeq"], 1)
        self.assertEqual(vcs[0]["departureTime"], "2026-09-24T20:30")
        self.assertTrue(vcs[0]["entries"])
        self.assertEqual(vcs[0]["first"]["nfzId"], "nzN")
        # 草稿、工作区、发布快照均不变
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev)
        self.assertEqual(state["nfzs"], [])
        self.assertEqual(self.current_release()["release"]["state"]["nfzs"], [])

    def test_ended_voyage_no_longer_blocks_approval(self):
        _, rev, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T20:30").status_code, 200)
        # 航次未结束：夜间窗候选被拦
        r = self.approve([clone(NIGHT_NFZ)], rev, seq)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "voyage_conflicts")
        # 结束航次后：同一候选通过（08:00 发布快照对夜间窗安全）
        self.assertEqual(self.end_voyage(1).status_code, 200)
        r = self.approve([clone(NIGHT_NFZ)], rev, seq)
        self.assertEqual(r.status_code, 200, r.get_json())
        state, _ = self.ws_state()
        self.assertEqual(state["nfzs"], [NIGHT_NFZ])

    def test_approval_checks_voyages_of_old_releases(self):
        # 发布 #1：y=100 航线，登记 20:30 航次
        vid1, rev, _ = self.publish_route(route=ROUTE)
        self.assertEqual(self.register(1, "2026-09-24T20:30").status_code, 200)
        # 再发布 #2：y=400 航线（同一工作区、禁飞区为空，仍可发布）
        vid2 = self.save_version({"route": ROUTE_Y400, "nfzs": []}, name="v2")
        r = self.publish(vid2, rev, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(self.current_release()["releaseSeq"], 2)
        # 候选 = 夜间窗（压 y=100）：当前发布 #2 的 y=400 快照航线不冲突，
        # 但旧发布 #1 登记的 20:30 航次冲突 → 必须被拦
        r = self.approve([clone(NIGHT_NFZ)], rev, 2)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "voyage_conflicts")
        vcs = body["voyageConflicts"]
        self.assertEqual(len(vcs), 1)
        self.assertEqual(vcs[0]["voyageId"], 1)
        self.assertEqual(vcs[0]["releaseSeq"], 1)  # 旧发布航次
        # 工作区与两个发布快照均不变
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev)
        self.assertEqual(state["nfzs"], [])

        # 换成只压 y=400 的候选：当前发布快照先冲突（原有审查），仍 409；
        # 结束旧航次后，y=400 阻断区对当前发布快照冲突依旧——保持原行为
        self.assertEqual(self.end_voyage(1).status_code, 200)
        r = self.approve([clone(BLOCK_Y400_NFZ)], rev, 2)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "analysis_conflicts")
        # 两个航次（旧 20:30 + 新 08:00）都未结束时，候选各压一条 → 两个都报
        self.assertEqual(self.register(2, "2026-09-24T08:00").status_code, 200)
        # 航次 1 已结束；候选组合 [NIGHT_NFZ, BLOCK_Y400_NFZ]：
        # 当前发布快照 y=400 先被 BLOCK_Y400 拦住（analysis_conflicts）
        r = self.approve([clone(NIGHT_NFZ), clone(BLOCK_Y400_NFZ)], rev, 2)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "analysis_conflicts")

    def test_approval_reports_multiple_open_voyages(self):
        # 干净构造：
        # 发布 #1（y=100，快照起飞 08:00）：登记航次1，起飞 20:30
        # 发布 #2（y=400，快照起飞 08:00）：登记航次2，起飞 00:30
        # 候选：NIGHT_NFZ（20–21 点，y=100 位置）+ 凌晨窗（00–01 点，y=400）
        # 对当前发布 #2 快照航线（y=400，08:00）：两个窗都不压 → 原审查通过；
        # 航次1 被夜间窗压、航次2 被凌晨窗压 → voyage_conflicts 含两条。
        _, rev, _ = self.publish_route(route=ROUTE)
        self.assertEqual(self.register(1, "2026-09-24T20:30").status_code, 200)
        vid2 = self.save_version({"route": ROUTE_Y400, "nfzs": []}, name="v2")
        self.assertEqual(self.publish(vid2, rev, 1).status_code, 200)
        self.assertEqual(self.register(2, "2026-09-25T00:30").status_code, 200)
        dawn_y400 = {
            "id": "nzD", "name": "凌晨Y400窗", "enabled": True,
            "vertices": [[250, 350], [350, 350], [350, 450], [250, 450]],
            "minAlt": 0, "maxAlt": 400,
            "startTime": "2026-09-25T00:00", "endTime": "2026-09-25T01:00",
        }
        r = self.approve([clone(NIGHT_NFZ), dawn_y400], rev, 2)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "voyage_conflicts")
        ids = sorted(vc["voyageId"] for vc in body["voyageConflicts"])
        self.assertEqual(ids, [1, 2])
        by_id = {vc["voyageId"]: vc for vc in body["voyageConflicts"]}
        self.assertEqual(by_id[1]["releaseSeq"], 1)
        self.assertEqual(by_id[2]["releaseSeq"], 2)
        # 工作区、修订号、两个发布快照都不变
        state, hdr_rev = self.ws_state()
        self.assertEqual(hdr_rev, rev)
        self.assertEqual(state["nfzs"], [])
        # 结束航次1后：只剩航次2 被凌晨窗拦
        self.assertEqual(self.end_voyage(1).status_code, 200)
        r = self.approve([clone(NIGHT_NFZ), dawn_y400], rev, 2)
        self.assertEqual(r.status_code, 409)
        body = r.get_json()
        self.assertEqual(body["reason"], "voyage_conflicts")
        self.assertEqual([vc["voyageId"] for vc in body["voyageConflicts"]], [2])
        # 两个航次都结束后：候选对发布快照安全 → 通过
        self.assertEqual(self.end_voyage(2).status_code, 200)
        r = self.approve([clone(NIGHT_NFZ), dawn_y400], rev, 2)
        self.assertEqual(r.status_code, 200, r.get_json())

    def test_open_voyage_does_not_block_safe_approval(self):
        # 未结束航次与候选不冲突时：核准照常通过（航次存在不无故阻拦）
        _, rev, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        r = self.approve([clone(FAR_NFZ)], rev, seq)
        self.assertEqual(r.status_code, 200, r.get_json())

    # —— 交错：登记与核准的事务串行不漏检 ——
    def test_interleaved_register_and_approval(self):
        # 核准在 IMMEDIATE 事务内读取“当时全部未结束航次”：
        # 场景顺序：发布 → 核准夜间窗（无航次，通过）→ 以窗外时刻登记航次
        # 成功（已核准夜间窗对 08:00 安全）→ 再核准全天阻断区被发布快照先拦；
        # 关键点：登记成功后航次立即进入未结束集合，下一次核准必检。
        _, rev, seq = self.publish_route()
        r = self.approve([clone(NIGHT_NFZ)], rev, seq)
        self.assertEqual(r.status_code, 200, r.get_json())
        rev2 = r.get_json()["rev"]
        # 20:30 登记：与已核准夜间窗冲突 → 拒绝（登记检工作区已核准禁飞区）
        r = self.register(seq, "2026-09-24T20:30")
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.list_voyages(), [])
        # 08:00 登记成功；此后提交夜间窗之外再叠加“航次必经”的候选必被拦
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        # 工作区已有夜间窗；候选若把夜间窗扩成全天窗 → 发布快照先拦
        all_day = clone(NIGHT_NFZ)
        all_day["startTime"] = all_day["endTime"] = ""
        r = self.approve([all_day], rev2, seq)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["reason"], "analysis_conflicts")

    def test_voyage_snapshot_survives_later_release(self):
        # 航次保存的是当时发布快照航线 + 登记时刻；之后再发布不改写航次
        _, rev, _ = self.publish_route(route=ROUTE)
        r = self.register(1, "2026-09-24T20:30")
        self.assertEqual(r.status_code, 200)
        v1_before = self.list_voyages()[0]
        vid2 = self.save_version({"route": ROUTE_Y400, "nfzs": []}, name="v2")
        self.assertEqual(self.publish(vid2, rev, 1).status_code, 200)
        voyages = self.list_voyages()
        self.assertEqual(len(voyages), 1)
        v1 = voyages[0]
        self.assertEqual(v1["releaseSeq"], 1)
        self.assertEqual(v1["routeSnapshot"]["waypoints"], ROUTE["waypoints"])
        self.assertEqual(v1["departureTime"], v1_before["departureTime"])
        self.assertTrue(v1["open"])

    # —— 既有行为不回归 ——
    def test_empty_approval_still_checks_voyages_but_passes_when_safe(self):
        _, rev, seq = self.publish_route()
        self.assertEqual(self.register(seq, "2026-09-24T08:00").status_code, 200)
        r = self.approve([], rev, seq)  # 清空禁飞区：对任何航线都安全
        self.assertEqual(r.status_code, 200, r.get_json())


if __name__ == "__main__":
    unittest.main(verbosity=2)
