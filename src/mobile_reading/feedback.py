"""实际服务回录与下一轮需求反馈。

三条防误判规则（对应业务要求）：

1. **取消不是低需求**：取消场次造成的缺席标记为供给侧取消，不更新意愿、
   不降低需求权重，并生成同站点的补兑需求信号。
2. **临时访客不进居民基数**：出席者中不在名册内的编号单独计数，只反映
   场次流量，绝不加入当地长期居民需求池。
3. **被拒请求是未满足需求**：席位不足导致的拒绝按站点累计，作为下一轮
   优先扩容信号；同时区分“计划未到（no_show）”与“根本无场次（未覆盖）”。

回录结果同时输出：计划兑现核对（逐场）、覆盖审计（新增覆盖 vs 重复服务）、
下一轮需求调整信号（结构化，供下一个规划周使用）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .models import DataBundle
from .planner import PlannedSession, WeekPlan


@dataclass(frozen=True)
class ServiceOutcome:
    """一场计划服务的实际回录。"""

    planned_session_id: str
    on: date
    site_id: str
    seats_available: int
    attendee_ids: tuple[str, ...]
    rejected_requests: int
    cancelled: bool
    cancel_reason: str | None
    no_show_ids: tuple[str, ...] = field(default_factory=tuple)
    delivered_format: str | None = None


@dataclass(frozen=True)
class SessionReconciliation:
    planned_session_id: str
    site_id: str
    on: date
    planned: bool
    cancelled: bool
    planned_reachable: int
    attended_roster: int
    attended_visitors: int
    rejected_requests: int
    no_shows: int
    seats_utilization: float | None
    flags: tuple[str, ...]


@dataclass(frozen=True)
class CoverageAudit:
    served_people: tuple[str, ...]
    newly_covered_people: tuple[str, ...]
    repeat_served_people: tuple[str, ...]
    served_households: tuple[str, ...]
    visitor_attendances: int
    by_site: dict[str, dict]
    note: str


@dataclass(frozen=True)
class DemandAdjustment:
    site_id: str
    carryover_rejected: int
    carryover_cancelled: int
    carryover_no_show: int
    outstanding_promises: int
    signal: str
    detail: dict


def reconcile_sessions(
    bundle: DataBundle, plan: WeekPlan, outcomes: list[ServiceOutcome]
) -> list[SessionReconciliation]:
    by_id = {o.planned_session_id: o for o in outcomes}
    rows: list[SessionReconciliation] = []
    roster = set(bundle.people)
    for session in plan.sessions:
        outcome = by_id.get(session.id)
        flags: list[str] = []
        if outcome is None:
            flags.append("未回录：缺少实际服务记录")
            rows.append(
                SessionReconciliation(
                    session.id, session.site_id, session.on, True, False,
                    len(session.reachable_person_ids), 0, 0, 0, 0, None,
                    tuple(flags),
                )
            )
            continue
        roster_att = [p for p in outcome.attendee_ids if p in roster]
        visitors = [p for p in outcome.attendee_ids if p not in roster]
        if visitors:
            flags.append(
                f"{len(visitors)} 名临时访客不在名册，不计入居民基数"
            )
        if outcome.cancelled:
            flags.append(
                f"供给侧取消（{outcome.cancel_reason or '原因未注明'}），"
                "缺席不得解释为需求降低"
            )
        if outcome.rejected_requests:
            flags.append(
                f"{outcome.rejected_requests} 个被拒请求，代表未满足的席位需求"
            )
        if outcome.no_show_ids:
            flags.append(
                f"{len(outcome.no_show_ids)} 名计划可达但未到，需另查个人原因"
            )
        util = None
        if not outcome.cancelled and outcome.seats_available > 0:
            util = round(len(roster_att) / outcome.seats_available, 3)
        rows.append(
            SessionReconciliation(
                session.id, session.site_id, session.on, True,
                outcome.cancelled, len(session.reachable_person_ids),
                len(roster_att), len(visitors), outcome.rejected_requests,
                len(outcome.no_show_ids), util, tuple(flags),
            )
        )
    return rows


def coverage_audit(
    bundle: DataBundle,
    plan: WeekPlan,
    outcomes: list[ServiceOutcome],
    prior_served_people: set[str] | None = None,
) -> CoverageAudit:
    """区分一次计划究竟扩大了覆盖，还是只重复服务原来的人。"""
    prior = set(prior_served_people) if prior_served_people else set()
    if not prior:
        # 默认以历史八周名册内出席者作为“此前已服务人群”
        for visit in bundle.visits:
            if not visit.cancelled:
                prior.update(visit.attendee_ids)
    roster = set(bundle.people)

    served: set[str] = set()
    visitors = 0
    by_site: dict[str, dict] = {}
    for outcome in outcomes:
        if outcome.cancelled:
            by_site.setdefault(
                outcome.site_id,
                {"served": [], "visitors": 0, "cancelled": True, "rejected": 0},
            )
            by_site[outcome.site_id]["cancelled"] = True
            continue
        roster_att = [p for p in outcome.attendee_ids if p in roster]
        visitors += len(outcome.attendee_ids) - len(roster_att)
        served.update(roster_att)
        slot = by_site.setdefault(
            outcome.site_id,
            {"served": [], "visitors": 0, "cancelled": False, "rejected": 0},
        )
        slot["served"].extend(roster_att)
        slot["visitors"] += len(outcome.attendee_ids) - len(roster_att)
        slot["rejected"] += outcome.rejected_requests

    for site_id, slot in by_site.items():
        people = sorted(set(slot["served"]))
        slot["unique_served"] = len(people)
        slot["newly_covered"] = sorted(set(people) - prior)
        slot["repeat_served"] = sorted(set(people) & prior)
        slot["households"] = sorted(
            {bundle.people[p].household for p in people if p in bundle.people}
        )
        del slot["served"]

    newly = sorted(served - prior)
    repeat = sorted(served & prior)
    households = sorted(
        {bundle.people[p].household for p in served if p in bundle.people}
    )
    return CoverageAudit(
        served_people=tuple(sorted(served)),
        newly_covered_people=tuple(newly),
        repeat_served_people=tuple(repeat),
        served_households=tuple(households),
        visitor_attendances=visitors,
        by_site=by_site,
        note=(
            "新增覆盖=本周首次服务的名册人员；重复服务=历史八周已服务过；"
            "临时访客只计流量，不进入居民基数；取消场次不计覆盖"
        ),
    )


def next_round_demand_signals(
    bundle: DataBundle,
    plan: WeekPlan,
    outcomes: list[ServiceOutcome],
    outstanding_households: dict[str, list[str]] | None = None,
) -> list[DemandAdjustment]:
    """把回录事实转换为下一规划周的站点级需求调整信号。

    ``outstanding_households``：站点 -> 尚未兑现承诺的家庭列表（来自取消台账）。
    """
    outstanding_households = outstanding_households or {}
    agg: dict[str, dict[str, int]] = {}
    for outcome in outcomes:
        slot = agg.setdefault(
            outcome.site_id,
            {"rejected": 0, "cancelled": 0, "no_show": 0},
        )
        slot["rejected"] += outcome.rejected_requests
        if outcome.cancelled:
            slot["cancelled"] += 1
        slot["no_show"] += len(outcome.no_show_ids)

    adjustments: list[DemandAdjustment] = []
    for site_id in sorted(bundle.sites):
        slot = agg.get(site_id, {"rejected": 0, "cancelled": 0, "no_show": 0})
        outstanding = len(outstanding_households.get(site_id, []))
        signals: list[str] = []
        if slot["rejected"]:
            signals.append("席位拒绝累积，下一轮优先扩容或加场")
        if slot["cancelled"]:
            signals.append("取消场次产生补兑需求，权重不下调")
        if outstanding:
            signals.append("存在未兑现家庭承诺，须优先安排等效场次")
        if slot["no_show"]:
            signals.append("计划可达但未到，需回访个人原因而非削减服务")
        if not signals:
            signals.append("无额外调整信号，维持基础需求估计")
        adjustments.append(
            DemandAdjustment(
                site_id=site_id,
                carryover_rejected=slot["rejected"],
                carryover_cancelled=slot["cancelled"],
                carryover_no_show=slot["no_show"],
                outstanding_promises=outstanding,
                signal="；".join(signals),
                detail={
                    "rule": "取消不视为需求降低；临时访客不计入居民基数；"
                    "拒绝与未兑现承诺按加权需求信号结转",
                },
            )
        )
    return adjustments
