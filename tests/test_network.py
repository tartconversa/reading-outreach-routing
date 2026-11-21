"""道路网络与整日路线联合检验测试。"""
from datetime import date

from support import AppTestCase, WEEK_START

from mobile_reading.models import Vehicle
from mobile_reading.network import (
    RoadGraph,
    RouteInfeasible,
    Stop,
    check_day_route,
)


class RoadGraphTests(AppTestCase):
    def test_disruption_overrides_direct_edge(self):
        plain = RoadGraph(self.bundle, WEEK_START, apply_disruptions=False)
        disrupted = RoadGraph(self.bundle, WEEK_START, apply_disruptions=True)
        self.assertEqual(plain.direct_minutes("S1", "S7"), 45)
        self.assertEqual(disrupted.direct_minutes("S1", "S7"), 180)
        self.assertIn("D1", disrupted.overrides)
        # 最短路绕行：S1 -> ... -> S7 = 60 分钟
        self.assertEqual(disrupted.shortest("S1")["S7"], 60)
        self.assertEqual(disrupted.shortest("S7")["S4"], 45)

    def test_other_days_unaffected(self):
        other = RoadGraph(self.bundle, date(2026, 8, 4))
        self.assertEqual(other.direct_minutes("S1", "S7"), 45)
        self.assertEqual(other.overrides, [])

    def test_unreachable_raises(self):
        graph = RoadGraph(self.bundle, WEEK_START)
        # 人为制造不可达：删除所有指向 S9 的边（仅清 S9 出边不影响别人到达它）
        for adj in graph._adj.values():
            adj.pop("S9", None)
        with self.assertRaises(RouteInfeasible):
            graph.travel("S1", "S9")


class DayRouteTests(AppTestCase):
    def _graph(self):
        return RoadGraph(self.bundle, WEEK_START)

    def test_confirmed_route_feasible_under_disruption_via_detour(self):
        graph = self._graph()
        vehicle = self.bundle.vehicles["V1"]
        staff = self.bundle.staff["T1"]
        stops = [
            Stop("S7", 540, 600, "audio", "CW1"),
            Stop("S4", 840, 900, "audio", "CW2"),
        ]
        result = check_day_route(graph, vehicle, staff, stops, self.bundle.sites)
        self.assertTrue(result.feasible, result.reasons)
        # 中断后行驶分钟必须高于基线直连（绕行生效的证据）
        self.assertGreaterEqual(result.drive_minutes, 120)

    def test_per_stop_feasible_but_whole_day_infeasible_when_timing_collides(self):
        graph = self._graph()
        vehicle = self.bundle.vehicles["V1"]
        # 两场时间重叠：单看各自都在站点开放窗内，合在同一车日不可行
        stops = [
            Stop("S7", 540, 600, "audio", "A"),
            Stop("S5", 570, 630, "audio", "B"),
        ]
        result = check_day_route(graph, vehicle, None, stops, self.bundle.sites)
        self.assertFalse(result.feasible)
        self.assertTrue(
            any("重叠" in r or "晚于" in r for r in result.reasons),
            result.reasons,
        )

    def test_open_window_violation_detected(self):
        graph = self._graph()
        vehicle = self.bundle.vehicles["V2"]
        # S2 开放窗 13:00-17:00，安排在上午必然违规
        stops = [Stop("S2", 540, 600, "audio", "X")]
        result = check_day_route(graph, vehicle, None, stops, self.bundle.sites)
        self.assertFalse(result.feasible)
        self.assertTrue(any("开放窗" in r for r in result.reasons))

    def test_format_skill_checks(self):
        graph = self._graph()
        vehicle = self.bundle.vehicles["V2"]
        paper_staff = self.bundle.staff["T3"]  # 只会 paper
        stops = [Stop("S7", 540, 600, "audio", "A")]
        result = check_day_route(
            graph, vehicle, paper_staff, stops, self.bundle.sites
        )
        self.assertFalse(result.feasible)
        self.assertTrue(any("技能" in r for r in result.reasons))

    def test_staff_unavailable_weekend(self):
        saturday = date(2026, 8, 8)
        graph = RoadGraph(self.bundle, saturday)
        vehicle = self.bundle.vehicles["V1"]
        staff = self.bundle.staff["T1"]  # 仅周一至周五
        stops = [Stop("S1", 540, 600, "audio", "A")]
        result = check_day_route(graph, vehicle, staff, stops, self.bundle.sites)
        self.assertFalse(result.feasible)
        self.assertTrue(any("不可用" in r for r in result.reasons))

    def test_work_minutes_cap_enforced(self):
        graph = self._graph()
        # 构造一辆上限极小的车辆
        tight = Vehicle(
            "V-X", 24, "S1", 480, 1080, max_minutes=60, cost_per_hour=60
        )
        stops = [Stop("S2", 900, 960, "audio", "A")]
        result = check_day_route(graph, tight, None, stops, self.bundle.sites)
        self.assertFalse(result.feasible)
        self.assertTrue(any("上限" in r for r in result.reasons))
