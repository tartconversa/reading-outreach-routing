"""服务回录与反馈测试：取消不算低需求、访客不进基数、覆盖去重、拒绝结转。"""
from datetime import date

from support import AppTestCase

from mobile_reading.feedback import (
    ServiceOutcome,
    coverage_audit,
    next_round_demand_signals,
    reconcile_sessions,
)
from mobile_reading.planner import build_week_plan


def _outcome(session, **kwargs):
    defaults = dict(
        planned_session_id=session.id,
        on=session.on,
        site_id=session.site_id,
        seats_available=24,
        attendee_ids=tuple(session.reachable_person_ids[:5]),
        rejected_requests=0,
        cancelled=False,
        cancel_reason=None,
        no_show_ids=tuple(),
        delivered_format=session.format,
    )
    defaults.update(kwargs)
    return ServiceOutcome(**defaults)


class ReconcileTests(AppTestCase):
    def test_every_planned_session_reconciled(self):
        plan = build_week_plan(self.bundle, self.cfg)
        outcomes = [_outcome(s) for s in plan.sessions]
        rows = reconcile_sessions(self.bundle, plan, outcomes)
        self.assertEqual(len(rows), len(plan.sessions))
        self.assertTrue(all(r.planned for r in rows))

    def test_missing_outcome_flagged(self):
        plan = build_week_plan(self.bundle, self.cfg)
        outcomes = [_outcome(plan.sessions[0])]
        rows = reconcile_sessions(self.bundle, plan, outcomes)
        flagged = [r for r in rows if any("未回录" in f for f in r.flags)]
        self.assertEqual(len(flagged), len(plan.sessions) - 1)

    def test_cancelled_flagged_as_supply_side(self):
        plan = build_week_plan(self.bundle, self.cfg)
        target = plan.sessions[0]
        outcomes = [
            _outcome(
                target, attendee_ids=(), cancelled=True,
                cancel_reason="道路管制",
            )
        ]
        rows = reconcile_sessions(self.bundle, plan, outcomes)
        row = next(r for r in rows if r.planned_session_id == target.id)
        self.assertTrue(row.cancelled)
        self.assertTrue(any("供给侧取消" in f for f in row.flags))


class VisitorTests(AppTestCase):
    def test_visitors_excluded_from_resident_base(self):
        plan = build_week_plan(self.bundle, self.cfg)
        outcomes = []
        for i, s in enumerate(plan.sessions):
            extra = ("VISITOR-X", "VISITOR-Y") if i == 0 else ()
            outcomes.append(
                _outcome(s, attendee_ids=tuple(s.reachable_person_ids[:3]) + extra)
            )
        cov = coverage_audit(self.bundle, plan, outcomes)
        self.assertEqual(cov.visitor_attendances, 2)
        self.assertNotIn("VISITOR-X", cov.served_people)
        self.assertNotIn("VISITOR-Y", cov.served_people)
        roster = set(self.bundle.people)
        self.assertTrue(set(cov.served_people) <= roster)


class CoverageAuditTests(AppTestCase):
    def test_new_vs_repeat_split_against_history(self):
        plan = build_week_plan(self.bundle, self.cfg)
        # 让出席者全部来自历史已服务人群
        historical = set()
        for v in self.bundle.visits:
            if not v.cancelled:
                historical.update(v.attendee_ids)
        attendees = tuple(sorted(historical)[:5])
        outcomes = [
            _outcome(s, attendee_ids=attendees if s.reachable_person_ids else ())
            for s in plan.sessions
        ]
        cov = coverage_audit(self.bundle, plan, outcomes)
        self.assertTrue(set(cov.newly_covered_people).isdisjoint(
            cov.repeat_served_people
        ))
        self.assertTrue(set(cov.repeat_served_people) <= historical)

    def test_cancelled_session_adds_no_coverage(self):
        plan = build_week_plan(self.bundle, self.cfg)
        target = plan.sessions[0]
        outcomes = [
            _outcome(target, attendee_ids=(), cancelled=True,
                     cancel_reason="x")
        ]
        cov = coverage_audit(self.bundle, plan, outcomes)
        slot = cov.by_site[target.site_id]
        self.assertTrue(slot["cancelled"])
        self.assertEqual(slot["unique_served"], 0)


class DemandSignalTests(AppTestCase):
    def test_cancellation_does_not_lower_demand_signal(self):
        plan = build_week_plan(self.bundle, self.cfg)
        target = plan.sessions[0]
        outcomes = [
            _outcome(target, attendee_ids=(), cancelled=True,
                     cancel_reason="道路管制")
        ]
        signals = next_round_demand_signals(self.bundle, plan, outcomes)
        site_signal = next(a for a in signals if a.site_id == target.site_id)
        self.assertEqual(site_signal.carryover_cancelled, 1)
        self.assertIn("补兑", site_signal.signal)
        self.assertIn("权重不下调", site_signal.signal)

    def test_rejected_requests_carry_over(self):
        plan = build_week_plan(self.bundle, self.cfg)
        target = plan.sessions[0]
        outcomes = [_outcome(target, rejected_requests=4)]
        signals = next_round_demand_signals(self.bundle, plan, outcomes)
        site_signal = next(a for a in signals if a.site_id == target.site_id)
        self.assertEqual(site_signal.carryover_rejected, 4)
        self.assertIn("扩容", site_signal.signal)

    def test_no_show_distinguished_from_missing_demand(self):
        plan = build_week_plan(self.bundle, self.cfg)
        target = plan.sessions[0]
        no_shows = tuple(target.reachable_person_ids[:2])
        outcomes = [_outcome(
            target,
            attendee_ids=tuple(p for p in target.reachable_person_ids[2:5]),
            no_show_ids=no_shows,
        )]
        signals = next_round_demand_signals(self.bundle, plan, outcomes)
        site_signal = next(a for a in signals if a.site_id == target.site_id)
        self.assertEqual(site_signal.carryover_no_show, 2)
        self.assertIn("回访", site_signal.signal)

    def test_outstanding_promises_prioritized(self):
        plan = build_week_plan(self.bundle, self.cfg)
        outcomes = [_outcome(s) for s in plan.sessions]
        signals = next_round_demand_signals(
            self.bundle, plan, outcomes, {"S7": ["H001", "H002"]}
        )
        s7 = next(a for a in signals if a.site_id == "S7")
        self.assertEqual(s7.outstanding_promises, 2)
        self.assertIn("未兑现", s7.signal)
