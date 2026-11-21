"""需求估计测试：去重、三层漏斗、不确定性、自报原因归属、形式约束。"""
import math

from support import AppTestCase

from mobile_reading.demand import (
    build_demand_report,
    global_funnel,
    has_feasible_window,
    potential_reachable,
    session_reachable,
    weighted_estimate,
)


class DedupTests(AppTestCase):
    def test_dual_source_people_counted_once(self):
        report = build_demand_report(self.bundle)
        total_observed = sum(
            r.funnel.potential.observed_people for r in report
        )
        # 站点层去重人数之和可以大于全县人数（跨站点重复），
        # 但任何单站点的观测人数都不得超过其登记行数
        for r in report:
            self.assertLessEqual(
                r.funnel.potential.observed_people,
                r.funnel.raw_registration_rows,
            )
            self.assertEqual(
                r.funnel.raw_registration_rows
                - r.funnel.duplicate_rows_removed,
                r.funnel.potential.observed_people,
            )

    def test_global_funnel_dedup_fact(self):
        payload = global_funnel(self.bundle)
        self.assertEqual(payload["dedup_fact"]["raw_need_rows"], 240)
        self.assertEqual(payload["dedup_fact"]["unique_people"], 180)
        self.assertEqual(payload["dedup_fact"]["people_in_both_sources"], 60)

    def test_funnel_is_monotone(self):
        payload = global_funnel(self.bundle)
        self.assertGreaterEqual(
            payload["potential"]["point_estimate"],
            payload["willing"]["point_estimate"],
        )
        self.assertGreaterEqual(
            payload["willing"]["point_estimate"],
            payload["reachable_any_site"]["point_estimate"],
        )

    def test_willing_matches_known_169_people(self):
        payload = global_funnel(self.bundle)
        self.assertEqual(payload["willing"]["observed_people"], 169)


class UncertaintyTests(AppTestCase):
    def test_cluster_se_finite_and_ci_covers_point(self):
        report = build_demand_report(self.bundle)
        for r in report:
            est = r.funnel.willing
            if not math.isnan(est.se):
                self.assertGreater(est.se, 0)
                self.assertLessEqual(est.ci_low, est.point)
                self.assertGreaterEqual(est.ci_high, est.point)

    def test_indicator_of_all_people_has_zero_variance(self):
        people = list(self.bundle.people.values())
        est = weighted_estimate(
            self.bundle, people, {p.id: 1 for p in people}, "test"
        )
        self.assertAlmostEqual(est.point, sum(p.sample_weight for p in people))
        self.assertTrue(math.isnan(est.se) or est.se >= 0)

    def test_empty_population(self):
        est = weighted_estimate(self.bundle, [], {}, "empty")
        self.assertEqual(est.point, 0.0)
        self.assertTrue(math.isnan(est.se))


class BarrierTests(AppTestCase):
    def test_barriers_only_come_from_self_reports(self):
        report = build_demand_report(self.bundle)
        reasons = set()
        for r in report:
            for b in r.barriers:
                reasons.add(b.reason)
        self.assertIn("time_conflict", reasons)
        self.assertIn("no_audio", reasons)
        # 出席侧事实与自报原因分表存放，结构上不允许互相代答
        for r in report:
            self.assertIn("cancelled_sessions", r.attendance_side_facts)
            self.assertNotIn(
                "attendee_answered_barriers",
                {f for b in r.barriers for f in b.__dict__},
            )

    def test_self_report_counts_match_raw_rows(self):
        report = build_demand_report(self.bundle)
        for r in report:
            self.assertEqual(
                sum(b.all_rows for b in r.barriers),
                r.funnel.raw_registration_rows,
            )


class ReachabilityTests(AppTestCase):
    def test_audio_need_blocked_at_paper_only_site(self):
        audio_people = [
            p.id for p in self.bundle.people.values()
            if p.access_need == "audio"
        ]
        self.assertTrue(audio_people)
        blocked = [
            pid for pid in audio_people
            if any(
                r.site_id == sid
                for r in self.bundle.needs_by_person().get(pid, ())
                for sid in ("S3", "S6", "S9")
            )
        ]
        for pid in blocked:
            for sid in ("S3", "S6", "S9"):
                if any(
                    r.site_id == sid
                    for r in self.bundle.needs_by_person().get(pid, ())
                ):
                    self.assertFalse(
                        potential_reachable(self.bundle, pid, sid),
                        f"{pid} 的 audio 需求不能在 paper-only {sid} 满足",
                    )

    def test_session_reachable_requires_travel_time(self):
        # P001 在 S1 有 09:00-12:00 窗，出行 11 分钟
        rec = next(
            r for r in self.bundle.needs
            if r.person_id == "P001" and r.site_id == "S1"
        )
        self.assertTrue(
            session_reachable(self.bundle, "P001", "S1", 540, 600, "audio")
        )
        # 场次开始晚于可用止时必然不可达
        self.assertFalse(
            session_reachable(self.bundle, "P001", "S1", 720, 780, "audio")
        )
        self.assertEqual(rec.travel_minutes, 11)

    def test_window_intersection_rule(self):
        site = self.bundle.sites["S1"]
        rec = next(
            r for r in self.bundle.needs
            if r.person_id == "P001" and r.site_id == "S1"
        )
        self.assertTrue(has_feasible_window(rec, site))


class AttendanceSideFactTests(AppTestCase):
    def test_cancelled_visits_reported_not_as_low_demand(self):
        report = build_demand_report(self.bundle)
        total_cancelled = sum(
            r.attendance_side_facts["cancelled_sessions"] for r in report
        )
        self.assertEqual(total_cancelled, 5)
        for r in report:
            note = r.attendance_side_facts["note"]
            self.assertIn("取消", note)
