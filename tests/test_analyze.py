# -*- coding: utf-8 -*-
"""POST /api/analyze 判定口径回归测试。

覆盖：
- 凹形禁飞区走廊两次相交、中间留空隙，时窗只覆盖空隙时不得误报；
- 多部件相交分别报为相互独立的冲突段；
- 生效时窗截短某一段时，enterFrac/exitFrac、时刻、高度与 witness 同属最终段，
  见证点随实际进入时刻移动；
- 边界擦碰（线接触 / 点接触 / 时窗端点接触）按实际重叠判定。
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402

# —— 公共夹具：一条水平航段穿凹形（U 形）禁飞区 ——
# 走廊半宽 0.14km × 100(单位/km) = 14 画布单位；航段 (100,300)->(500,300)。
# U 形开口向下，走廊只在 f∈[0.25,0.50] 与 f∈[0.65,0.90] 与两翼相交，
# f∈(0.50,0.65) 为缺口空隙。
U_VERTICES = [(200, 280), (300, 280), (300, 315), (360, 315),
              (360, 280), (460, 280), (460, 360), (200, 360)]
# 速度 240km/h × 航程 4km = 60s：
#   f .25/.50/.65/.90 对应 08:00:15 / 08:00:30 / 08:00:39 / 08:00:54
DEP = "2026-09-24T08:00"


def make_state(start="", end="", vertices=None, half_width=0.14,
               alt0=200, alt1=200, lo=0, hi=400):
    return {
        "route": {
            "departureTime": DEP, "unitScale": 100,
            "corridorHalfWidth": half_width, "speeds": [240.0],
            "waypoints": [
                {"id": "w1", "name": "A", "x": 100, "y": 300, "alt": alt0},
                {"id": "w2", "name": "B", "x": 500, "y": 300, "alt": alt1},
            ],
        },
        "nfzs": [{
            "id": "nz1", "name": "凹形禁飞区", "enabled": True,
            "vertices": vertices if vertices is not None else U_VERTICES,
            "minAlt": lo, "maxAlt": hi, "startTime": start, "endTime": end,
        }],
    }


def parse(iso):
    return datetime.fromisoformat(iso)


def assert_time_minute(testcase, iso, expected):
    """API 时刻按分钟展示（既有响应格式），断言其分钟刻度与期望一致。"""
    testcase.assertEqual(parse(iso).replace(second=0, microsecond=0),
                         expected.replace(second=0, microsecond=0))


class ConcaveNFZTests(unittest.TestCase):
    def assertNoError(self, res):
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["errors"], [])

    # —— 空隙误报：时窗只覆盖两段之间的空隙 ——
    def test_window_only_over_gap_reports_nothing(self):
        # 空隙为 08:00:30..08:00:39；时窗严格落在空隙内。
        res = app.analyze(make_state("2026-09-24T08:00:32",
                                     "2026-09-24T08:00:37"))
        self.assertNoError(res)
        self.assertEqual(res["entries"], [])
        self.assertIsNone(res["first"])

    def test_disabled_nfz_gap_window_still_clean(self):
        st = make_state("2026-09-24T08:00:32", "2026-09-24T08:00:37")
        st["nfzs"][0]["enabled"] = False
        res = app.analyze(st)
        self.assertNoError(res)
        self.assertEqual(res["entries"], [])

    # —— 两段命中：多部件相交各自独立成条 ——
    def test_window_covering_both_components_reports_two_entries(self):
        res = app.analyze(make_state("2026-09-24T08:00:00",
                                     "2026-09-24T08:02:00"))
        self.assertNoError(res)
        self.assertEqual(len(res["entries"]), 2)
        e0, e1 = res["entries"]
        self.assertAlmostEqual(e0["enterFrac"], 0.25, places=4)
        self.assertAlmostEqual(e0["exitFrac"], 0.50, places=4)
        self.assertAlmostEqual(e1["enterFrac"], 0.65, places=4)
        self.assertAlmostEqual(e1["exitFrac"], 0.90, places=4)
        # 见证点分别落在各自段的进入端中心线上
        self.assertAlmostEqual(e0["witness"]["x"], 200.0, places=1)
        self.assertAlmostEqual(e1["witness"]["x"], 360.0, places=1)
        # 首个冲突取实际最早进入的一段（first 结构沿用既有字段）
        self.assertEqual(res["first"]["nfzId"], "nz1")
        self.assertEqual(res["first"]["witness"], e0["witness"])
        assert_time_minute(self, res["first"]["enterTime"],
                           parse("2026-09-24T08:00:15"))

    def test_components_are_not_merged_in_time(self):
        """同一禁飞区的两段冲突各自独立，参数区间上保留缺口。"""
        res = app.analyze(make_state("2026-09-24T08:00:00",
                                     "2026-09-24T08:02:00"))
        e0, e1 = res["entries"]
        self.assertLess(e0["exitFrac"], e1["enterFrac"],
                        "两段之间应保留空隙，不得合并成一条连续区间")
        self.assertAlmostEqual(e0["enterFrac"], 0.25, places=4)
        self.assertAlmostEqual(e0["exitFrac"], 0.50, places=4)
        self.assertAlmostEqual(e1["enterFrac"], 0.65, places=4)
        self.assertAlmostEqual(e1["exitFrac"], 0.90, places=4)
        self.assertEqual(e0["durationMinutes"], 0.25)
        self.assertEqual(e1["durationMinutes"], 0.25)

    # —— 时窗截断：只覆盖第一段的后半部分 ——
    def test_window_truncates_entry_head(self):
        # 第一段 08:00:15..08:00:30；窗 08:00:20..08:00:37（窗尾落入空隙）。
        res = app.analyze(make_state("2026-09-24T08:00:20",
                                     "2026-09-24T08:00:37"))
        self.assertNoError(res)
        self.assertEqual(len(res["entries"]), 1)
        e = res["entries"][0]
        # 最终区间为被截短的第一段：f 20/60≈0.3333 .. 0.5
        self.assertAlmostEqual(e["enterFrac"], 20 / 60, places=4)
        self.assertAlmostEqual(e["exitFrac"], 0.5, places=4)
        assert_time_minute(self, e["enterTime"], parse("2026-09-24T08:00:20"))
        assert_time_minute(self, e["exitTime"], parse("2026-09-24T08:00:30"))
        self.assertAlmostEqual(e["durationMinutes"], 10 / 60, places=2)
        # 见证点随进入时刻移动到窗沿 f=20/60 → x=100+400·f=233.33
        self.assertAlmostEqual(e["witness"]["x"], 100 + 400 * (20 / 60), places=1)
        self.assertAlmostEqual(e["witness"]["y"], 300.0, places=2)
        self.assertFalse(e["instant"])
        # first 与该段一致
        assert_time_minute(self, res["first"]["enterTime"],
                           parse("2026-09-24T08:00:20"))
        self.assertEqual(res["first"]["witness"], e["witness"])

    def test_window_truncates_entry_tail(self):
        # 窗从第二段中间开始、到航段结束之后：第二段被截短头部。
        res = app.analyze(make_state("2026-09-24T08:00:45",
                                     "2026-09-24T08:02:00"))
        self.assertNoError(res)
        self.assertEqual(len(res["entries"]), 1)
        e = res["entries"][0]
        self.assertAlmostEqual(e["enterFrac"], 45 / 60, places=4)
        self.assertAlmostEqual(e["exitFrac"], 0.9, places=4)
        assert_time_minute(self, e["enterTime"], parse("2026-09-24T08:00:45"))
        assert_time_minute(self, e["exitTime"], parse("2026-09-24T08:00:54"))
        self.assertAlmostEqual(e["witness"]["x"], 100 + 400 * (45 / 60), places=1)

    def test_first_conflict_picks_earliest_entering_segment(self):
        # 窗同时覆盖两段但起点更早；首条即最早进入的第一段。
        res = app.analyze(make_state("2026-09-24T08:00:10",
                                     "2026-09-24T08:02:00"))
        self.assertEqual(len(res["entries"]), 2)
        assert_time_minute(self, res["first"]["enterTime"],
                           parse("2026-09-24T08:00:15"))
        self.assertEqual(res["first"]["witness"]["x"],
                         res["entries"][0]["witness"]["x"])

    # —— 边界擦碰 ——
    def test_line_graze_reports_when_window_covers(self):
        # 禁飞区顶边 y=286 恰好贴走廊下沿，相交为 f∈[0.25,0.9] 的线段。
        verts = [(200, 250), (460, 250), (460, 286), (200, 286)]
        res = app.analyze(make_state("2026-09-24T08:00:00",
                                     "2026-09-24T08:02:00", vertices=verts))
        self.assertNoError(res)
        self.assertEqual(len(res["entries"]), 1)
        e = res["entries"][0]
        self.assertFalse(e["instant"])
        self.assertAlmostEqual(e["enterFrac"], 0.25, places=4)
        self.assertAlmostEqual(e["exitFrac"], 0.90, places=4)

    def test_line_graze_silent_when_window_misses(self):
        verts = [(200, 250), (460, 250), (460, 286), (200, 286)]
        res = app.analyze(make_state("2026-09-24T09:00",
                                     "2026-09-24T10:00", vertices=verts))
        self.assertNoError(res)
        self.assertEqual(res["entries"], [])

    def test_point_graze_at_cap_tip_reports_as_instant(self):
        # 禁飞区左边界 x=514 恰好碰到航段圆端帽尖点（f=1，08:01:00）。
        verts = [(514, 260), (600, 260), (600, 340), (514, 340)]
        res = app.analyze(make_state("2026-09-24T08:00:30",
                                     "2026-09-24T08:01:30", vertices=verts))
        self.assertNoError(res)
        self.assertEqual(len(res["entries"]), 1)
        e = res["entries"][0]
        self.assertTrue(e["instant"])
        self.assertAlmostEqual(e["enterFrac"], 1.0, places=4)
        self.assertAlmostEqual(e["exitFrac"], 1.0, places=4)
        assert_time_minute(self, e["enterTime"], parse("2026-09-24T08:01:00"))
        self.assertEqual(e["durationMinutes"], 0.0)
        self.assertAlmostEqual(e["witness"]["x"], 500.0, places=1)

    def test_point_graze_silent_when_window_misses(self):
        verts = [(514, 260), (600, 260), (600, 340), (514, 340)]
        res = app.analyze(make_state("2026-09-24T09:00",
                                     "2026-09-24T10:00", vertices=verts))
        self.assertNoError(res)
        self.assertEqual(res["entries"], [])

    def test_window_endpoint_touch_counts_as_overlap(self):
        # 时窗退化为单点 08:00:30，恰好是第一段退出瞬间：闭区间接触算命中。
        res = app.analyze(make_state("2026-09-24T08:00:30",
                                     "2026-09-24T08:00:30"))
        self.assertNoError(res)
        self.assertEqual(len(res["entries"]), 1)
        e = res["entries"][0]
        self.assertTrue(e["instant"])
        self.assertAlmostEqual(e["enterFrac"], 0.5, places=4)
        self.assertAlmostEqual(e["exitFrac"], 0.5, places=4)

    # —— 高度层仍逐段参与判定 ——
    def test_altitude_layer_filters_components_independently(self):
        # 全程 200m 等高度；禁飞高度层 250m 起 → 两段都不应命中。
        res = app.analyze(make_state("2026-09-24T08:00:00",
                                     "2026-09-24T08:02:00", lo=250, hi=400))
        self.assertNoError(res)
        self.assertEqual(res["entries"], [])

    def test_climbing_segment_maps_window_to_altitude_range(self):
        # 高度 100→300 线性爬升，层 [150,250] 对应 f∈[0.25,0.75]：
        # 第一段 [0.25,0.5] 全保留，第二段 [0.65,0.9] 截到 [0.65,0.75]。
        res = app.analyze(make_state("2026-09-24T08:00:00",
                                     "2026-09-24T08:02:00",
                                     alt0=100, alt1=300, lo=150, hi=250))
        self.assertNoError(res)
        self.assertEqual(len(res["entries"]), 2)
        e0, e1 = res["entries"]
        self.assertAlmostEqual(e0["enterFrac"], 0.25, places=4)
        self.assertAlmostEqual(e0["exitFrac"], 0.50, places=4)
        self.assertAlmostEqual(e1["enterFrac"], 0.65, places=4)
        self.assertAlmostEqual(e1["exitFrac"], 0.75, places=4)
        # 高度范围落在最终段内并夹到高度层
        self.assertGreaterEqual(e0["altLow"], 150 - 1e-6)
        self.assertLessEqual(e0["altHigh"], 250 + 1e-6)
        # 见证点高度 = 实际进入端高度
        self.assertAlmostEqual(e0["witness"]["alt"], 100 + 200 * 0.25, places=1)
        self.assertAlmostEqual(e1["witness"]["alt"], 100 + 200 * 0.65, places=1)


class SeedStateTests(unittest.TestCase):
    """保持既有演示数据的审查行为（字段与首个冲突结果不回归）。"""

    def test_seed_analysis_shape(self):
        res = app.analyze(app.SEED_STATE)
        self.assertTrue(res["ok"])
        self.assertEqual(res["errors"], [])
        self.assertIsInstance(res["entries"], list)
        for e in res["entries"]:
            for key in ("nfzId", "nfzName", "segIndex", "enterFrac", "exitFrac",
                        "enterTime", "exitTime", "durationMinutes",
                        "altLow", "altHigh", "witness", "instant"):
                self.assertIn(key, e)
            self.assertIn("x", e["witness"])
            self.assertIn("y", e["witness"])
            self.assertIn("alt", e["witness"])

    def test_seed_first_conflict_unchanged(self):
        res = app.analyze(app.SEED_STATE)
        self.assertEqual(len(res["entries"]), 1)
        e = res["entries"][0]
        self.assertEqual(e["nfzId"], "n1")
        self.assertEqual(e["segIndex"], 1)
        self.assertAlmostEqual(e["enterFrac"], 0.27838, places=4)
        self.assertAlmostEqual(e["exitFrac"], 0.66667, places=4)
        self.assertEqual(e["enterTime"], "2026-09-24T08:08")
        self.assertEqual(e["exitTime"], "2026-09-24T08:11")
        self.assertAlmostEqual(e["witness"]["x"], 452.38, places=1)
        self.assertAlmostEqual(e["witness"]["y"], 357.73, places=1)
        self.assertFalse(e["instant"])
        self.assertEqual(res["first"]["nfzId"], "n1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
