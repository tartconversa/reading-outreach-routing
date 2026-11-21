"""承诺保护、中断改期与取消台账。

核心不可违背规则：

1. 已通知家庭的场次是服务承诺。任何路线调整后，锁定场次必须仍在原时段、
   原形式、原站点可达；调整导致其不可达时，调整动作被拒绝而不是悄悄改点。
2. 取消/改点必须逐户分类：哪些家庭通过等效替代场次仍得到服务，哪些承诺
   尚未兑现（进入补兑清单）。等效 = 同必要形式、新时段对该家庭仍可达。
3. 偏远站点的缺席不能用邻近站点出现过的人数抵消——覆盖以 *人/家庭* 为单位，
   且只认真正属于该承诺的通知家庭。
4. 替代场次在确认前必须通过整条车辆日路线与辅导员联合检验。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from .demand import session_reachable
from .models import ConfirmedSession, DataBundle
from .network import RoadGraph, Stop, check_day_route
from .planner import (
    PlannerConfig,
    PlannedSession,
    WeekPlan,
    _backtrack_guarantees,
    build_week_plan,
    compute_guarantee_gaps,
    greedy_expand,
    rebuild_state,
)


@dataclass(frozen=True)
class HouseholdPromise:
    confirmed_id: str
    household: str
    site_id: str
    format: str
    on: date
    members: tuple[str, ...]
    status: str  # kept / equivalent_substitute / outstanding
    substitute_session_id: str | None
    reason: str


def promise_members(bundle: DataBundle, confirmed: ConfirmedSession) -> list[str]:
    return sorted(
        p.id
        for p in bundle.people.values()
        if p.household in confirmed.notified_households
    )


def household_equivalent(
    bundle: DataBundle,
    confirmed: ConfirmedSession,
    candidate: PlannedSession,
) -> bool:
    """等效服务判断：形式覆盖每个成员的必要形式，且新场次对成员可达。"""
    members = promise_members(bundle, confirmed)
    if not members:
        return False
    for pid in members:
        person = bundle.people[pid]
        if person.access_need == "audio" and candidate.format != "audio":
            return False
        if not session_reachable(
            bundle, pid, candidate.site_id,
            candidate.start, candidate.end, candidate.format,
        ):
            return False
    return True


def validate_locked_routes(
    bundle: DataBundle, plan: WeekPlan
) -> dict[str, list[str]]:
    """在当前（含中断的）道路条件下复核所有锁定场次所在的整条车日路线。"""
    out: dict[str, list[str]] = {}
    days = {s.on for s in plan.sessions}
    graphs = {day: RoadGraph(bundle, day) for day in days}
    for vehicle_id, day in sorted({(s.vehicle_id, s.on) for s in plan.sessions}):
        day_sessions = plan.sessions_on(vehicle_id, day)
        vehicle = bundle.vehicles[vehicle_id]
        staff_ids = {s.staff_id for s in day_sessions if s.staff_id}
        staff = bundle.staff[next(iter(staff_ids))] if len(staff_ids) == 1 else None
        stops = [Stop(s.site_id, s.start, s.end, s.format, s.id) for s in day_sessions]
        result = check_day_route(graphs[day], vehicle, staff, stops, bundle.sites)
        if not result.feasible:
            key = f"{vehicle_id}|{day.isoformat()}"
            out[key] = list(result.reasons)
    return out


def audit_promises(
    bundle: DataBundle, plan: WeekPlan
) -> list[HouseholdPromise]:
    """逐户核对每个已通知家庭的承诺状态。"""
    promises: list[HouseholdPromise] = []
    sessions_by_id = {s.id: s for s in plan.sessions}
    for confirmed in sorted(bundle.confirmed, key=lambda c: c.id):
        member_ids = promise_members(bundle, confirmed)
        kept = sessions_by_id.get(confirmed.id)
        for hh in sorted(confirmed.notified_households):
            hh_members = tuple(
                sorted(pid for pid in member_ids if bundle.people[pid].household == hh)
            )
            if kept is not None:
                status, sub_id, reason = (
                    "kept", None, "原锁定场次在计划中保留，时段形式未变",
                )
            else:
                substitute = _find_equivalent_substitute(
                    bundle, confirmed, hh, plan.sessions
                )
                if substitute is not None:
                    status, sub_id, reason = (
                        "equivalent_substitute",
                        substitute.id,
                        f"替代场次 {substitute.id} 满足该家庭必要形式且时段可达",
                    )
                else:
                    status, sub_id, reason = (
                        "outstanding", None,
                        "原承诺场次不在计划中且无等效替代，列入补兑清单",
                    )
            promises.append(
                HouseholdPromise(
                    confirmed_id=confirmed.id, household=hh,
                    site_id=confirmed.site_id, format=confirmed.format,
                    on=confirmed.on, members=hh_members, status=status,
                    substitute_session_id=sub_id, reason=reason,
                )
            )
    return promises


def _find_equivalent_substitute(
    bundle: DataBundle,
    confirmed: ConfirmedSession,
    household: str,
    sessions: list[PlannedSession],
) -> PlannedSession | None:
    members = [
        p for p in bundle.people.values() if p.household == household
    ]
    for cand in sorted(sessions, key=lambda s: (s.on, s.start)):
        if cand.locked:
            continue
        ok = True
        for person in members:
            if person.access_need == "audio" and cand.format != "audio":
                ok = False
                break
            if not session_reachable(
                bundle, person.id, cand.site_id,
                cand.start, cand.end, cand.format,
            ):
                ok = False
                break
        if ok:
            return cand
    return None


@dataclass
class ReplanResult:
    plan: WeekPlan
    promises: list[HouseholdPromise]
    locked_violations: dict[str, list[str]]
    rejected_changes: list[str]
    notes: list[str] = field(default_factory=list)


def replan_week(
    bundle: DataBundle,
    cfg: PlannerConfig,
    remove_session_ids: set[str] | None = None,
    extra_session_cap: int = 6,
) -> ReplanResult:
    """在不破坏任何承诺的前提下重新排期。

    - 锁定场次一律保留，并在含中断的当日道路图上重验整条路线；
    - ``remove_session_ids`` 只允许引用非锁定场次，删除锁定场次的请求被拒绝；
    - 删除后用边际覆盖贪心补位，保障缺口重新计算。
    """
    remove_session_ids = remove_session_ids or set()
    rejected: list[str] = []
    locked_ids = {c.id for c in bundle.confirmed}
    for sid in sorted(remove_session_ids):
        if sid in locked_ids:
            rejected.append(
                f"拒绝删除 {sid}：该场次已通知家庭，属服务承诺，"
                "只能走取消/等效替代流程"
            )
    removable = remove_session_ids - locked_ids

    # 从当前周计划出发：锁定场次与已确认的其余行程保留，仅移除获准的非锁定场次
    current = build_week_plan(bundle, cfg)
    kept = [s for s in current.sessions if s.id not in removable]
    graphs = {
        cfg.week_start + timedelta(days=i): RoadGraph(
            bundle, cfg.week_start + timedelta(days=i)
        )
        for i in range(cfg.days)
    }
    state = rebuild_state(bundle, cfg, graphs, kept)
    if removable:
        # 移除后立即重验受影响车日的整条路线
        for vehicle_id, day in sorted({(s.vehicle_id, s.on) for s in kept}):
            stops = [
                Stop(s.site_id, s.start, s.end, s.format, s.id)
                for s in state._by_day.get((vehicle_id, day), ())
            ]
            check = check_day_route(
                graphs[day], bundle.vehicles[vehicle_id], None, stops, bundle.sites
            )
            if not check.feasible:
                rejected.append(
                    f"移除 {sorted(removable)} 后 {vehicle_id} {day} 路线不可行："
                    + "；".join(check.reasons)
                )

    # 保障缺口在新状态下重新计算并补齐，再做边际补位
    gaps = compute_guarantee_gaps(bundle, cfg)
    unresolved, gap_audit = _backtrack_guarantees(bundle, state, gaps)
    covered = {pid for s in state.all_sessions() for pid in s.reachable_person_ids}
    expand_audit = greedy_expand(bundle, state, extra_session_cap, covered)

    plan = WeekPlan(
        week_start=cfg.week_start,
        sessions=state.all_sessions(),
        gaps=unresolved,
        audit=gap_audit + expand_audit
        + [f"已按请求移除非锁定场次：{sorted(removable)}" if removable else "无移除请求"],
        route_results=dict(state.results),
        staff_warnings=[],
    )
    violations = validate_locked_routes(bundle, plan)
    if violations:
        rejected.append(
            "当前道路/人员条件下锁定路线不可行："
            + "; ".join(f"{k}: {v}" for k, v in violations.items())
        )
    promises = audit_promises(bundle, plan)
    return ReplanResult(
        plan=plan, promises=promises, locked_violations=violations,
        rejected_changes=rejected,
    )


@dataclass(frozen=True)
class CancellationOutcome:
    cancelled_session_id: str
    reason: str
    household_outcomes: tuple[HouseholdPromise, ...]
    equivalent_session_id: str | None
    outstanding_households: tuple[str, ...]
    makegood_required: bool


def cancel_session(
    bundle: DataBundle,
    plan: WeekPlan,
    session_id: str,
    reason: str,
    substitute: PlannedSession | None = None,
) -> CancellationOutcome:
    """取消一场服务并逐户判定等效服务与未兑现承诺。

    替代场次若提供，必须先通过其所在车辆日的整条路线检验，否则拒绝接受。
    """
    target = next((s for s in plan.sessions if s.id == session_id), None)
    if target is None:
        raise ValueError(f"计划中不存在场次 {session_id}")
    if substitute is not None:
        graph = RoadGraph(bundle, substitute.on)
        vehicle = bundle.vehicles[substitute.vehicle_id]
        peers = [
            s for s in plan.sessions
            if s.vehicle_id == substitute.vehicle_id and s.on == substitute.on
            and s.id not in (session_id, substitute.id)
        ]
        stops = [Stop(s.site_id, s.start, s.end, s.format, s.id) for s in peers]
        stops.append(
            Stop(substitute.site_id, substitute.start, substitute.end,
                 substitute.format, substitute.id)
        )
        check = check_day_route(graph, vehicle, None, stops, bundle.sites)
        if not check.feasible:
            raise ValueError(
                f"替代场次 {substitute.id} 未通过整条路线检验：{'；'.join(check.reasons)}"
            )

    confirmed = next(
        (c for c in bundle.confirmed if c.id == session_id), None
    )
    outcomes: list[HouseholdPromise] = []
    outstanding: list[str] = []
    if confirmed is None:
        # 非承诺场次：无通知家庭，取消只影响计划供给，不产生家庭承诺台账
        return CancellationOutcome(
            session_id, reason, tuple(), None, tuple(), False
        )

    for hh in sorted(confirmed.notified_households):
        members = tuple(
            sorted(p.id for p in bundle.people.values() if p.household == hh)
        )
        equivalent = False
        if substitute is not None:
            equivalent = all(
                session_reachable(
                    bundle, pid, substitute.site_id,
                    substitute.start, substitute.end, substitute.format,
                )
                and (
                    bundle.people[pid].access_need != "audio"
                    or substitute.format == "audio"
                )
                for pid in members
            )
        if equivalent:
            outcomes.append(
                HouseholdPromise(
                    confirmed.id, hh, confirmed.site_id, confirmed.format,
                    confirmed.on, members, "equivalent_substitute",
                    substitute.id if substitute else None,
                    "替代场次形式与时段对该家庭等效",
                )
            )
        else:
            outstanding.append(hh)
            outcomes.append(
                HouseholdPromise(
                    confirmed.id, hh, confirmed.site_id, confirmed.format,
                    confirmed.on, members, "outstanding", None,
                    "取消后无等效服务，承诺未兑现，须后续补兑且不得由邻近站点人数抵消",
                )
            )
    return CancellationOutcome(
        session_id, reason, tuple(outcomes),
        substitute.id if substitute else None,
        tuple(outstanding), bool(outstanding),
    )
