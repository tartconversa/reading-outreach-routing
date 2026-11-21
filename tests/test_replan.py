"""承诺保护、取消台账与等效替代测试。"""
from datetime import date

from support import AppTestCase

from mobile_reading.planner import PlannedSession, build_week_plan
from mobile_reading.replan import (
    audit_promises,
    cancel_session,
    household_equivalent,
    validate_locked_routes,
)


class PromiseAuditTests(AppTestCase):
    def test_all_notified_households_kept_when_locked_sessions_remain(self):
        plan = build_week_plan(self.bundle, self.cfg)
        promises = audit_promises(self.bundle, plan)
        households = {(p.confirmed_id, p.household) for p in promises}
        self.assertEqual(
            households,
            {("CW1", "H001"), ("CW1", "H002"), ("CW2", "H003")},
        )
        self.assertTrue(all(p.status == "kept" for p in promises))

    def test_locked_routes_valid_under_disruption(self):
        plan = build_week_plan(self.bundle, self.cfg)
        violations = validate_locked_routes(self.bundle, plan)
        self.assertEqual(violations, {})


class CancellationTests(AppTestCase):
    def test_cancel_locked_without_substitute_marks_outstanding(self):
        plan = build_week_plan(self.bundle, self.cfg)
        outcome = cancel_session(self.bundle, plan, "CW1", "车辆故障", None)
        self.assertEqual(
            sorted(outcome.outstanding_households), ["H001", "H002"]
        )
        self.assertTrue(outcome.makegood_required)
        self.assertTrue(
            all(p.status == "outstanding" for p in outcome.household_outcomes)
        )

    def test_cancel_non_promise_session_has_no_household_ledger(self):
        plan = build_week_plan(self.bundle, self.cfg)
        target = next(s.id for s in plan.sessions if not s.locked)
        outcome = cancel_session(self.bundle, plan, target, "道路管制", None)
        self.assertEqual(outcome.household_outcomes, ())
        self.assertFalse(outcome.makegood_required)

    def test_substitute_must_pass_whole_route_check(self):
        plan = build_week_plan(self.bundle, self.cfg)
        # 在 V1 周一插入一个过早的 S9 场次：S9 09:00 开放，安排 08:00 违规
        bad = PlannedSession(
            id="BAD", site_id="S9", on=date(2026, 8, 3),
            start=480, end=540, format="paper", vehicle_id="V1",
            staff_id=None, locked=False, guarantee="marginal",
            justification="test", reachable_person_ids=(),
        )
        with self.assertRaises(ValueError) as ctx:
            cancel_session(self.bundle, plan, "CW1", "test", bad)
        self.assertIn("整条路线检验", str(ctx.exception))

    def test_substitute_that_breaks_peer_route_rejected(self):
        plan = build_week_plan(self.bundle, self.cfg)
        # EQ 挤在 V2 的 S1 与 S6 之间且时间不足，导致后续 S6 迟到
        eq = PlannedSession(
            id="EQ", site_id="S4", on=date(2026, 8, 3),
            start=780, end=840, format="audio", vehicle_id="V2",
            staff_id=None, locked=False, guarantee="marginal",
            justification="test", reachable_person_ids=(),
        )
        with self.assertRaises(ValueError):
            cancel_session(self.bundle, plan, "CW1", "test", eq)

    def test_equivalent_judgement_uses_household_members_only(self):
        plan = build_week_plan(self.bundle, self.cfg)
        cw1 = next(c for c in self.bundle.confirmed if c.id == "CW1")
        # 数据中 H001 成员在 S7 无需求登记，任何 S7 替代都不等效
        candidate = next(
            s for s in plan.sessions if s.site_id == "S7" and not s.locked
        ) if any(s.site_id == "S7" and not s.locked for s in plan.sessions) else None
        if candidate is not None:
            self.assertFalse(
                household_equivalent(self.bundle, cw1, candidate)
            )

    def test_remote_absence_not_offset_by_neighbor_attendance(self):
        # 取消 S7 承诺时，台账只认 CW1 通知的 H001/H002，
        # 其他站点的出席人数不进入该承诺的判定
        plan = build_week_plan(self.bundle, self.cfg)
        outcome = cancel_session(self.bundle, plan, "CW1", "道路中断", None)
        affected = {p.household for p in outcome.household_outcomes}
        self.assertEqual(affected, {"H001", "H002"})
        reason_text = " ".join(p.reason for p in outcome.household_outcomes)
        self.assertIn("不得由邻近站点人数抵消", reason_text)
