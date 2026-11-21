"""极端情形测试：中断升级为不可行、跨车辆人员冲突、输入不可变保护。"""
from datetime import date

from support import AppTestCase, WEEK_START

from mobile_reading.models import Disruption
from mobile_reading.network import RoadGraph, Stop, check_day_route
from mobile_reading.planner import PlannerConfig, build_week_plan
from mobile_reading.replan import replan_week, validate_locked_routes


class SevereDisruptionTests(AppTestCase):
    def test_locked_violation_reported_when_route_becomes_infeasible(self):
        bundle = self.bundle
        # 构造极端中断：把周一所有进入 S7 的有向边拉长到超出班次窗，
        # 使已确认的 S7 场次无法在 09:00 前到达（含全部绕行路径）
        incoming = [
            r for r in bundle.roads
            if r.to_site == "S7" and r.valid_on(WEEK_START)
        ]
        self.assertTrue(incoming)
        new_disruptions = [
            Disruption(
                id=f"DX-{r.from_site}", from_site=r.from_site, to_site="S7",
                on=WEEK_START, minutes=600, received_on=WEEK_START,
            )
            for r in incoming
        ]
        from mobile_reading.models import DataBundle

        hard = DataBundle(
            schools=bundle.schools, people=bundle.people, sites=bundle.sites,
            needs=bundle.needs, roads=bundle.roads, vehicles=bundle.vehicles,
            staff=bundle.staff, visits=bundle.visits, confirmed=bundle.confirmed,
            disruptions=tuple(list(bundle.disruptions) + new_disruptions),
            fingerprint=bundle.fingerprint, row_counts=bundle.row_counts,
            raw_path=bundle.raw_path,
        )
        graph = RoadGraph(hard, WEEK_START)
        self.assertGreaterEqual(graph.shortest("S1")["S7"], 600)

        plan = build_week_plan(hard, self.cfg)
        violations = validate_locked_routes(hard, plan)
        # 必须如实上报，而不是悄悄改点
        self.assertTrue(violations)
        self.assertTrue(any("V1" in k for k in violations))

    def test_original_bundle_unchanged_after_analysis(self):
        before = len(self.bundle.disruptions)
        build_week_plan(self.bundle, self.cfg)
        replan_week(self.bundle, self.cfg)
        self.assertEqual(len(self.bundle.disruptions), before)
        # 原始锁定场次编号仍在
        self.assertEqual(
            {c.id for c in self.bundle.confirmed}, {"CW1", "CW2"}
        )


class CrossVehicleStaffTests(AppTestCase):
    def test_assign_staff_never_double_books_across_vehicles(self):
        # 直接构造同一辅导员被两辆车引用的冲突状态，验证指派逻辑不会放行
        from mobile_reading.planner import _ScheduleState, _graphs

        graphs = _graphs(self.bundle, self.cfg)
        state = _ScheduleState(self.bundle, self.cfg, graphs)
        vehicle1 = self.bundle.vehicles["V1"]
        vehicle2 = self.bundle.vehicles["V2"]
        # V1 周一 S1 上午场（带锁定辅导员 T1）
        state.insert(
            "S1", WEEK_START, 540, 600, "audio", vehicle1,
            guarantee="locked", justification="x", locked=True, staff_id="T1",
            session_id="L1",
        )
        # V2 周一另一场也尝试用 T1：assign_staff 应改派他人或告警
        state.insert(
            "S2", WEEK_START, 780, 840, "audio", vehicle2,
            guarantee="marginal", justification="y",
        )
        warnings = __import__(
            "mobile_reading.planner", fromlist=["assign_staff"]
        ).assign_staff(self.bundle, state)
        v2_staff = {
            s.staff_id
            for s in state._by_day.get(("V2", WEEK_START), ())
        }
        # T1 已在 V1，V2 不得再用 T1
        self.assertNotIn("T1", v2_staff)
        # 要么有人可用（T2），要么产生告警，二者必居其一且路线本身可行
        self.assertTrue(
            v2_staff != {None} or warnings
        )


class WholeDayJointCheckTests(AppTestCase):
    def test_each_stop_individually_valid_but_chain_infeasible(self):
        # 两个场次各自落在站点开放窗内，但首尾相接且路程不足
        graph = RoadGraph(self.bundle, WEEK_START)
        vehicle = self.bundle.vehicles["V2"]
        stops = [
            Stop("S1", 540, 600, "audio", "A"),   # S1 09:00-10:00
            Stop("S9", 600, 660, "paper", "B"),   # S9 09:00-12:00，但 1 小时到不了
        ]
        result = check_day_route(graph, vehicle, None, stops, self.bundle.sites)
        self.assertFalse(result.feasible)
        self.assertTrue(any("晚于场次开始" in r for r in result.reasons))
