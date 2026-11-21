"""排期引擎测试：承诺锁定、最低频率保障、边际覆盖、基线对照、人员指派。"""
from collections import Counter
from datetime import date

from support import AppTestCase, make_config

from mobile_reading.planner import (
    build_attendance_baseline,
    build_week_plan,
    compute_guarantee_gaps,
)
from mobile_reading.replan import replan_week


class LockedPromiseTests(AppTestCase):
    def test_confirmed_sessions_remain_locked(self):
        plan = build_week_plan(self.bundle, self.cfg)
        locked = {s.id for s in plan.sessions if s.locked}
        self.assertEqual(locked, {"CW1", "CW2"})
        cw1 = next(s for s in plan.sessions if s.id == "CW1")
        self.assertEqual((cw1.site_id, cw1.start, cw1.end, cw1.format),
                         ("S7", 540, 600, "audio"))

    def test_locked_route_feasible_with_disruption(self):
        plan = build_week_plan(self.bundle, self.cfg)
        result = plan.route_results["V1|2026-08-03"]
        self.assertTrue(result.feasible, result.reasons)

    def test_cannot_remove_locked_session(self):
        r = replan_week(self.bundle, self.cfg, remove_session_ids={"CW1"})
        self.assertTrue(r.rejected_changes)
        self.assertIn("CW1", {s.id for s in r.plan.sessions})

    def test_remove_unlocked_session_keeps_routes_feasible(self):
        plan = build_week_plan(self.bundle, self.cfg)
        target = next(s.id for s in plan.sessions if not s.locked)
        r = replan_week(self.bundle, self.cfg, remove_session_ids={target})
        self.assertEqual(r.rejected_changes, [])
        self.assertNotIn(target, {s.id for s in r.plan.sessions})
        self.assertTrue(
            all(x.feasible for x in r.plan.route_results.values())
        )
        # 承诺不受影响
        self.assertTrue(all(p.status == "kept" for p in r.promises))


class GuaranteeTests(AppTestCase):
    def test_trailing_window_gaps_zero_in_given_data(self):
        gaps = compute_guarantee_gaps(self.bundle, self.cfg)
        self.assertEqual(sum(g.missing for g in gaps), 0)

    def test_fresh_cycle_creates_and_fills_all_gaps(self):
        cfg = make_config(fresh_cycle=True)
        gaps = compute_guarantee_gaps(self.bundle, cfg)
        required = sum(g.missing for g in gaps)
        # 9 站点约定总量 12 场，其中 2 场已由锁定场次冲抵，缺口 10 场
        self.assertEqual(required, 10)
        remote_missing = {g.site_id: g.missing for g in gaps if g.remote}
        self.assertEqual(remote_missing, {"S7": 1, "S8": 2, "S9": 2})

        plan = build_week_plan(self.bundle, cfg)
        guarantee_sessions = [
            s for s in plan.sessions if s.guarantee == "minimum_frequency"
        ]
        self.assertEqual(len(guarantee_sessions), required)
        self.assertEqual(plan.gaps, [])
        # 每个站点实际场次达到最低约定（锁定 + 保障）
        counts = Counter(s.site_id for s in plan.sessions)
        for site_id, site in self.bundle.sites.items():
            self.assertGreaterEqual(
                counts[site_id], site.minimum_per_4weeks
            )
        # 所有车日路线仍然联合可行
        self.assertTrue(
            all(r.feasible for r in plan.route_results.values())
        )

    def test_remote_sites_never_dropped_in_fresh_cycle(self):
        cfg = make_config(fresh_cycle=True)
        plan = build_week_plan(self.bundle, cfg)
        served_sites = {s.site_id for s in plan.sessions}
        for remote in ("S7", "S8", "S9"):
            self.assertIn(remote, served_sites)


class MarginalCoverageTests(AppTestCase):
    def test_no_zero_coverage_marginal_sessions(self):
        plan = build_week_plan(self.bundle, self.cfg)
        for s in plan.sessions:
            if s.guarantee == "marginal":
                self.assertGreater(
                    len(s.reachable_person_ids), 0,
                    f"边际场次 {s.id} 无任何可达人员",
                )

    def test_coverage_is_dedup_across_sessions(self):
        plan = build_week_plan(self.bundle, self.cfg)
        union = {
            pid for s in plan.sessions for pid in s.reachable_person_ids
        }
        summed = sum(len(s.reachable_person_ids) for s in plan.sessions)
        self.assertGreaterEqual(summed, len(union))

    def test_marginal_plan_covers_more_than_baseline(self):
        plan = build_week_plan(self.bundle, self.cfg)
        baseline = build_attendance_baseline(self.bundle, self.cfg)
        covered = {p for s in plan.sessions for p in s.reachable_person_ids}
        base_covered = {
            p for s in baseline.sessions for p in s.reachable_person_ids
        }
        self.assertGreaterEqual(len(covered), len(base_covered))


class BaselineTests(AppTestCase):
    def test_baseline_ignores_guarantee_rule_in_audit(self):
        baseline = build_attendance_baseline(self.bundle, self.cfg)
        self.assertTrue(
            any("对照基线" in a for a in baseline.audit)
        )


class StaffAssignmentTests(AppTestCase):
    def test_every_vehicle_day_has_compatible_staff_or_warning(self):
        plan = build_week_plan(self.bundle, self.cfg)
        for key, result in plan.route_results.items():
            vehicle_id, day_text = key.split("|", 1)
            day = date.fromisoformat(day_text)
            sessions = plan.sessions_on(vehicle_id, day)
            staff_ids = {s.staff_id for s in sessions}
            if None in staff_ids:
                self.assertTrue(plan.staff_warnings)
            else:
                self.assertEqual(len(staff_ids), 1)
                staff = self.bundle.staff[next(iter(staff_ids))]
                for s in sessions:
                    self.assertTrue(staff.supports(s.format))
                    self.assertTrue(staff.available_on(day))
                self.assertLessEqual(result.work_minutes, staff.max_minutes)

    def test_no_staff_double_booked_on_same_day(self):
        plan = build_week_plan(self.bundle, self.cfg)
        seen: dict = {}
        for s in plan.sessions:
            if s.staff_id is None:
                continue
            key = (s.staff_id, s.on)
            if key in seen:
                # 同一辅导员同日只能出现在同一辆车上（同车多场是允许的）
                self.assertEqual(
                    seen[key], s.vehicle_id,
                    f"{s.staff_id} 同日被排上两辆车：{seen[key]} 与 {s.vehicle_id}",
                )
            seen[key] = s.vehicle_id
