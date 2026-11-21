"""周计划排期引擎。

业务规则落到代码的方式：

1. **承诺先行**：``confirmed_week`` 中已通知家庭的场次作为锁定停靠进入计划，
   排期只能在其外围增补，不能改动或使其不可达；锁定场次在当日中断下的
   整条路线可行性首先被检验，失败时原样上报，不静默改点。

2. **最低频率保障**：以规划周前 28 天历史场次（取消场次不计）加本周已
   确认场次计算缺口，缺口场次（含偏远站点）用回溯搜索优先落位，排不下
   时进入未兑现保障清单，绝不被高到场站点挤掉。

3. **边际覆盖/成本**：剩余候选按“新增加权可达人数 + 偏远保障加权”除以
   增量车辆小时成本排序，而不是按历史预计到场人数排满。覆盖按人去重，
   同一人在同周多场只计一次新增。

4. **整日联合检验**：每次插入都调用 :func:`mobile_reading.network.check_day_route`
   对该车辆当日 *整条* 路线重新检验，逐站可行不等于同日可行。

5. **朴素对照基线**：另生成一个仅按历史场均出席排序的计划，用于展示
   “按到场人数排满”会如何遗漏偏远站点与覆盖缺口。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from . import SESSION_MINUTES
from .demand import session_reachable
from .models import ConfirmedSession, DataBundle, Site, Vehicle
from .network import DayRouteResult, RoadGraph, Stop, check_day_route


@dataclass(frozen=True)
class PlannedSession:
    id: str
    site_id: str
    on: date
    start: int
    end: int
    format: str
    vehicle_id: str
    staff_id: str | None
    locked: bool
    guarantee: str  # locked / minimum_frequency / marginal
    justification: str
    reachable_person_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GuaranteeGap:
    site_id: str
    required: int
    historical_held: int
    already_locked: int
    missing: int
    remote: bool


@dataclass
class WeekPlan:
    week_start: date
    sessions: list[PlannedSession]
    gaps: list[GuaranteeGap]
    audit: list[str]
    route_results: dict[str, DayRouteResult]
    staff_warnings: list[str]

    def sessions_on(self, vehicle_id: str, day: date) -> list[PlannedSession]:
        return sorted(
            (s for s in self.sessions if s.vehicle_id == vehicle_id and s.on == day),
            key=lambda s: s.start,
        )

    def vehicle_days(self) -> list[tuple[str, date]]:
        return sorted({(s.vehicle_id, s.on) for s in self.sessions})


@dataclass(frozen=True)
class PlannerConfig:
    week_start: date
    days: int = 5
    max_sessions_per_site: int = 2
    candidate_step_minutes: int = 60
    search_node_limit: int = 20_000
    fresh_cycle: bool = False
    """True 时视为新四周周期起点，历史场次不冲抵最低频率（用于保障机制验证）。"""


# ---------------------------------------------------------------------------
# 候选与覆盖
# ---------------------------------------------------------------------------

def candidate_slots(site: Site, cfg: PlannerConfig) -> list[tuple[int, int]]:
    """站点开放窗内所有整点 60 分钟候选时段。"""
    slots: list[tuple[int, int]] = []
    start = site.open_start
    while start + SESSION_MINUTES <= site.open_end:
        slots.append((start, start + SESSION_MINUTES))
        start += cfg.candidate_step_minutes
    return slots


def reachable_people_for_session(
    bundle: DataBundle, site_id: str, start: int, end: int, fmt: str
) -> list[str]:
    """该场次口径下实际可到达的去重人员（含形式与时间双重条件）。"""
    ids = {r.person_id for r in bundle.needs_by_site().get(site_id, ())}
    return sorted(
        pid
        for pid in ids
        if session_reachable(bundle, pid, site_id, start, end, fmt)
    )


def _graphs(bundle: DataBundle, cfg: PlannerConfig) -> dict[date, RoadGraph]:
    days = [cfg.week_start + timedelta(days=i) for i in range(cfg.days)]
    return {day: RoadGraph(bundle, day) for day in days}


# ---------------------------------------------------------------------------
# 保障缺口
# ---------------------------------------------------------------------------

def compute_guarantee_gaps(bundle: DataBundle, cfg: PlannerConfig) -> list[GuaranteeGap]:
    """滚动四周（规划周前 28 天）实际举办场次 + 本周锁定场次 vs 最低约定。"""
    horizon_start = cfg.week_start - timedelta(days=28)
    gaps: list[GuaranteeGap] = []
    locked_by_site: dict[str, int] = {}
    for c in bundle.confirmed:
        if cfg.week_start <= c.on < cfg.week_start + timedelta(days=cfg.days):
            locked_by_site[c.site_id] = locked_by_site.get(c.site_id, 0) + 1
    for site_id, site in sorted(bundle.sites.items()):
        if cfg.fresh_cycle:
            held = 0
        else:
            held = sum(
                1
                for v in bundle.visits
                if v.site_id == site_id
                and not v.cancelled
                and horizon_start <= v.on < cfg.week_start
            )
        locked = locked_by_site.get(site_id, 0)
        missing = max(0, site.minimum_per_4weeks - held - locked)
        gaps.append(
            GuaranteeGap(
                site_id=site_id,
                required=site.minimum_per_4weeks,
                historical_held=held,
                already_locked=locked,
                missing=missing,
                remote=site.is_remote,
            )
        )
    return gaps


# ---------------------------------------------------------------------------
# 路线状态机：对 (vehicle, day) 维护停靠列表并做整日联合检验
# ---------------------------------------------------------------------------

class _ScheduleState:
    def __init__(self, bundle: DataBundle, cfg: PlannerConfig, graphs: dict[date, RoadGraph]):
        self.bundle = bundle
        self.cfg = cfg
        self.graphs = graphs
        # (vehicle_id, day) -> list[PlannedSession]
        self._by_day: dict[tuple[str, date], list[PlannedSession]] = {}
        self.results: dict[str, DayRouteResult] = {}
        self._counter = 0

    def clone_sessions(self) -> list[PlannedSession]:
        return [s for day_sessions in self._by_day.values() for s in day_sessions]

    def count_site(self, site_id: str) -> int:
        return sum(1 for s in self.clone_sessions() if s.site_id == site_id)

    def check_insert(
        self, vehicle: Vehicle, day: date, stop: Stop, staff_id: str | None
    ) -> DayRouteResult:
        existing = [
            Stop(s.site_id, s.start, s.end, s.format, s.id)
            for s in self._by_day.get((vehicle.id, day), ())
        ]
        stops = sorted(existing, key=lambda s: s.start) + [stop]
        staff = self.bundle.staff.get(staff_id) if staff_id else None
        return check_day_route(
            self.graphs[day], vehicle, staff, stops, self.bundle.sites
        )

    def insert(
        self,
        site_id: str,
        day: date,
        start: int,
        end: int,
        fmt: str,
        vehicle: Vehicle,
        guarantee: str,
        justification: str,
        locked: bool = False,
        staff_id: str | None = None,
        session_id: str | None = None,
    ) -> PlannedSession:
        self._counter += 1
        sid = session_id or f"PL-{self._counter:03d}"
        reach = tuple(
            reachable_people_for_session(self.bundle, site_id, start, end, fmt)
        )
        session = PlannedSession(
            id=sid, site_id=site_id, on=day, start=start, end=end, format=fmt,
            vehicle_id=vehicle.id, staff_id=staff_id, locked=locked,
            guarantee=guarantee, justification=justification,
            reachable_person_ids=reach,
        )
        self._by_day.setdefault((vehicle.id, day), []).append(session)
        stops = [
            Stop(s.site_id, s.start, s.end, s.format, s.id)
            for s in sorted(self._by_day[(vehicle.id, day)], key=lambda x: x.start)
        ]
        staff = self.bundle.staff.get(staff_id) if staff_id else None
        result = check_day_route(
            self.graphs[day], vehicle, staff, stops, self.bundle.sites
        )
        self.results[f"{vehicle.id}|{day.isoformat()}"] = result
        return session

    def all_sessions(self) -> list[PlannedSession]:
        return sorted(
            self.clone_sessions(),
            key=lambda s: (s.on, s.vehicle_id, s.start, s.site_id),
        )


def _lock_confirmed(
    bundle: DataBundle, cfg: PlannerConfig, graphs: dict[date, RoadGraph]
) -> tuple[_ScheduleState, list[str]]:
    """把已通知场次锁定进计划，并在当日道路条件下检验整条路线。"""
    state = _ScheduleState(bundle, cfg, graphs)
    audit: list[str] = []
    # 同一 vehicle/day 的锁定场次必须一起检验，先收集再逐车日插入
    grouped: dict[tuple[str, date], list[ConfirmedSession]] = {}
    for c in sorted(bundle.confirmed, key=lambda x: (x.on, x.start)):
        grouped.setdefault((c.vehicle_id, c.on), []).append(c)
    for (vehicle_id, day), items in grouped.items():
        vehicle = bundle.vehicles[vehicle_id]
        for c in items:
            stop = Stop.from_confirmed(c)
            result = state.check_insert(vehicle, day, stop, c.staff_id)
            if not result.feasible:
                audit.append(
                    f"承诺场次 {c.id}（{c.site_id} {day}）在当前道路条件下整条路线不可行："
                    + "；".join(result.reasons)
                )
            state.insert(
                c.site_id, day, c.start, c.end, c.format, vehicle,
                guarantee="locked",
                justification=(
                    f"已通知家庭 {','.join(c.notified_households)} 的服务承诺，"
                    "时段与形式锁定"
                ),
                locked=True, staff_id=c.staff_id, session_id=c.id,
            )
    return state, audit


# ---------------------------------------------------------------------------
# 保障场次回溯
# ---------------------------------------------------------------------------

def _candidate_for_site(
    bundle: DataBundle, state: _ScheduleState, site_id: str
) -> list[tuple[date, int, int, str]]:
    cfg = state.cfg
    site = bundle.sites[site_id]
    out: list[tuple[date, int, int, str]] = []
    # audio 优先（可同时覆盖 text 需求人员），paper 作为无 audio 站点的唯一形式
    formats = ("audio", "paper") if site.supports("audio") else ("paper",)
    for i in range(cfg.days):
        day = cfg.week_start + timedelta(days=i)
        for start, end in candidate_slots(site, cfg):
            for fmt in formats:
                out.append((day, start, end, fmt))
    return out


def _backtrack_guarantees(
    bundle: DataBundle,
    state: _ScheduleState,
    gaps: list[GuaranteeGap],
) -> tuple[list[GuaranteeGap], list[str]]:
    audit: list[str] = []
    nodes = 0
    # 偏远站点、缺口大的先排；同序按站点号保证确定性
    work = sorted(
        gaps, key=lambda g: (not g.remote, -g.missing, g.site_id)
    )
    remaining = {g.site_id: g.missing for g in work if g.missing > 0}
    vehicles = sorted(bundle.vehicles.values(), key=lambda v: v.id)

    def place_one(site_id: str) -> bool:
        nonlocal nodes
        site = bundle.sites[site_id]
        if state.count_site(site_id) >= site.minimum_per_4weeks:
            return True
        candidates = _candidate_for_site(bundle, state, site_id)
        for day, start, end, fmt in candidates:
            for vehicle in vehicles:
                nodes += 1
                if nodes > state.cfg.search_node_limit:
                    return False
                stop = Stop(site_id, start, end, fmt, f"try-{site_id}")
                result = state.check_insert(vehicle, day, stop, None)
                if not result.feasible:
                    continue
                state.insert(
                    site_id, day, start, end, fmt, vehicle,
                    guarantee="minimum_frequency",
                    justification=(
                        f"补齐每四周最低 {site.minimum_per_4weeks} 场约定"
                        + ("（偏远保障站点）" if site.is_remote else "")
                    ),
                )
                if place_one(site_id):
                    return True
                # 回滚
                day_sessions = state._by_day[(vehicle.id, day)]
                state._by_day[(vehicle.id, day)] = [
                    s for s in day_sessions
                    if not (s.site_id == site_id and s.start == start and s.guarantee == "minimum_frequency")
                ]
                if not state._by_day[(vehicle.id, day)]:
                    del state._by_day[(vehicle.id, day)]
                    state.results.pop(f"{vehicle.id}|{day.isoformat()}", None)
        return False

    unresolved: list[GuaranteeGap] = []
    for gap in work:
        if gap.missing <= 0:
            continue
        ok = place_one(gap.site_id)
        placed = state.count_site(gap.site_id) - gap.already_locked
        if not ok or placed < gap.missing:
            still = gap.missing - max(0, placed)
            unresolved.append(
                GuaranteeGap(
                    gap.site_id, gap.required, gap.historical_held,
                    gap.already_locked, still, gap.remote,
                )
            )
            audit.append(
                f"站点 {gap.site_id} 最低频率保障尚有 {still} 场无法排入，"
                "已保留为未兑现缺口，不得由邻近站点出席人数抵消"
            )
    return unresolved, audit


# ---------------------------------------------------------------------------
# 边际覆盖/成本贪心
# ---------------------------------------------------------------------------

def _vehicle_day_cost(
    bundle: DataBundle, graph: RoadGraph, vehicle: Vehicle, stops: list[Stop]
) -> float:
    """车辆日成本：班次内工作分钟（含行驶、服务、等待）折算小时成本。"""
    result = check_day_route(graph, vehicle, None, stops, bundle.sites)
    hours = result.work_minutes / 60.0
    return hours * vehicle.cost_per_hour


def greedy_expand(
    bundle: DataBundle,
    state: _ScheduleState,
    max_new_sessions: int,
    covered_people: set[str],
) -> list[str]:
    cfg = state.cfg
    audit: list[str] = []
    vehicles = sorted(bundle.vehicles.values(), key=lambda v: v.id)

    for _ in range(max_new_sessions):
        best: dict | None = None
        for site_id, site in sorted(bundle.sites.items()):
            if state.count_site(site_id) >= cfg.max_sessions_per_site:
                continue
            formats = ("audio", "paper") if site.supports("audio") else ("paper",)
            for day_i in range(cfg.days):
                day = cfg.week_start + timedelta(days=day_i)
                for start, end in candidate_slots(site, cfg):
                    for fmt in formats:
                        reach = reachable_people_for_session(
                            bundle, site_id, start, end, fmt
                        )
                        marginal_people = [p for p in reach if p not in covered_people]
                        marginal_weight = sum(
                            bundle.people[pid].sample_weight for pid in marginal_people
                        )
                        for vehicle in vehicles:
                            stop = Stop(site_id, start, end, fmt, "try-marginal")
                            result = state.check_insert(vehicle, day, stop, None)
                            if not result.feasible:
                                continue
                            if not marginal_people:
                                # 无新增覆盖的候选不进入边际排序；保障价值已由
                                # 回溯阶段的最低频率约束单独保证，不靠零覆盖场次刷分
                                continue
                            existing_stops = [
                                Stop(s.site_id, s.start, s.end, s.format, s.id)
                                for s in state._by_day.get((vehicle.id, day), ())
                            ]
                            base_cost = _vehicle_day_cost(
                                bundle, state.graphs[day], vehicle, existing_stops
                            )
                            new_cost = _vehicle_day_cost(
                                bundle, state.graphs[day], vehicle,
                                existing_stops + [stop],
                            )
                            incremental_cost = max(new_cost - base_cost, 1.0)
                            score = marginal_weight / incremental_cost
                            key = (
                                round(score, 6), round(marginal_weight, 6),
                                -int(day_i), -start, site_id, fmt, vehicle.id,
                            )
                            if best is None or key > best["key"]:
                                best = {
                                    "key": key, "site_id": site_id, "day": day,
                                    "start": start, "end": end, "fmt": fmt,
                                    "vehicle": vehicle, "score": score,
                                    "marginal_people": marginal_people,
                                    "incremental_cost": incremental_cost,
                                    "remote": site.is_remote,
                                }
        if best is None:
            audit.append(
                "剩余可行候选均无新增去重覆盖（只会重复服务已覆盖人员），停止增补；"
                "偏远站点保障由最低频率回溯单独保证"
            )
            break
        session = state.insert(
            best["site_id"], best["day"], best["start"], best["end"], best["fmt"],
            best["vehicle"], guarantee="marginal",
            justification=(
                f"边际评分 {best['score']:.4f}（新增加权可达 "
                f"{sum(bundle.people[p].sample_weight for p in best['marginal_people']):.1f} 人"
                + ("，站点属偏远保障区" if best["remote"] else "")
                + f"，增量成本 {best['incremental_cost']:.0f} 元）"
            ),
        )
        covered_people.update(best["marginal_people"])
        audit.append(
            f"增补 {session.id}：{best['site_id']} {best['day']} {best['fmt']}，"
            f"新增覆盖 {len(best['marginal_people'])} 人，评分 {best['score']:.4f}"
        )
    return audit


# ---------------------------------------------------------------------------
# 辅导员指派（按车日做贪心匹配，技能与当日工时联合约束）
# ---------------------------------------------------------------------------

def assign_staff(bundle: DataBundle, state: _ScheduleState) -> list[str]:
    warnings: list[str] = []
    assigned: dict[tuple[str, date], str] = {}
    for vehicle_id, day in sorted(state._by_day.keys()):
        sessions = sorted(state._by_day[(vehicle_id, day)], key=lambda s: s.start)
        locked_staff = {s.staff_id for s in sessions if s.staff_id}
        if len(locked_staff) > 1:
            warnings.append(f"{vehicle_id} {day} 锁定场次指定了多名辅导员，需人工核对")
        needed_formats = {s.format for s in sessions}
        work = state.results[f"{vehicle_id}|{day.isoformat()}"].work_minutes
        choice: str | None = None
        # 锁定场次的辅导员必须延续
        if locked_staff:
            candidate_ids = sorted(locked_staff)
        else:
            candidate_ids = sorted(bundle.staff)
        for staff_id in candidate_ids:
            staff = bundle.staff[staff_id]
            if not staff.available_on(day):
                continue
            if not all(staff.supports(fmt) for fmt in needed_formats):
                continue
            if work > staff.max_minutes:
                continue
            # 同一辅导员同一天不能上两辆车
            if any(
                assigned.get((vid, day)) == staff_id
                for vid in {v for v, _ in state._by_day}
            ):
                continue
            choice = staff_id
            break
        if choice is None:
            warnings.append(
                f"{vehicle_id} {day} 无可用辅导员覆盖 {sorted(needed_formats)}"
                f"（工作分钟 {work}），该日路线缺少人员保障"
            )
            continue
        assigned[(vehicle_id, day)] = choice
        for s in sessions:
            if s.staff_id is None:
                object.__setattr__(s, "staff_id", choice)
    return warnings


# ---------------------------------------------------------------------------
# 主入口与对照基线
# ---------------------------------------------------------------------------

def rebuild_state(
    bundle: DataBundle,
    cfg: PlannerConfig,
    graphs: dict[date, RoadGraph],
    keep_sessions: list[PlannedSession],
) -> _ScheduleState:
    """把既有计划场次重新装载进状态机（用于改期时保留已确认部分）。"""
    state = _ScheduleState(bundle, cfg, graphs)
    for s in sorted(keep_sessions, key=lambda x: (x.on, x.start)):
        vehicle = bundle.vehicles[s.vehicle_id]
        state.insert(
            s.site_id, s.on, s.start, s.end, s.format, vehicle,
            guarantee=s.guarantee, justification=s.justification,
            locked=s.locked, staff_id=s.staff_id, session_id=s.id,
        )
    return state


def build_week_plan(bundle: DataBundle, cfg: PlannerConfig) -> WeekPlan:
    graphs = _graphs(bundle, cfg)
    state, audit = _lock_confirmed(bundle, cfg, graphs)
    gaps = compute_guarantee_gaps(bundle, cfg)
    unresolved, gap_audit = _backtrack_guarantees(bundle, state, gaps)
    audit.extend(gap_audit)

    covered = {
        pid
        for s in state.all_sessions()
        for pid in s.reachable_person_ids
    }
    # 预算上限：车辆数 × 天数 × 每场一车日两场的经验容量，减去已排
    cap = len(bundle.vehicles) * cfg.days * 3 - len(state.all_sessions())
    audit.extend(greedy_expand(bundle, state, max(0, cap), covered))
    warnings = assign_staff(bundle, state)

    return WeekPlan(
        week_start=cfg.week_start,
        sessions=state.all_sessions(),
        gaps=unresolved,
        audit=audit,
        route_results=dict(state.results),
        staff_warnings=warnings,
    )


def build_attendance_baseline(bundle: DataBundle, cfg: PlannerConfig) -> WeekPlan:
    """对照基线：只按历史场均出席从高到低排满，不设保障、不看边际覆盖。"""
    graphs = _graphs(bundle, cfg)
    state = _ScheduleState(bundle, cfg, graphs)
    audit = [
        "对照基线：按历史场均出席人数排序，不执行最低频率保障与边际覆盖规则"
    ]
    held_visits = [v for v in bundle.visits if not v.cancelled]
    avg_att: dict[str, float] = {}
    for site_id in bundle.sites:
        rows = [v for v in held_visits if v.site_id == site_id]
        avg_att[site_id] = (
            sum(len(v.attendee_ids) for v in rows) / len(rows) if rows else 0.0
        )
    vehicles = sorted(bundle.vehicles.values(), key=lambda v: v.id)
    ranked = sorted(avg_att, key=lambda s: (-avg_att[s], s))
    for site_id in ranked:
        site = bundle.sites[site_id]
        formats = ("audio", "paper") if site.supports("audio") else ("paper",)
        placed = False
        for day_i in range(cfg.days):
            day = cfg.week_start + timedelta(days=day_i)
            for start, end in candidate_slots(site, cfg):
                for fmt in formats:
                    for vehicle in vehicles:
                        stop = Stop(site_id, start, end, fmt, "base-try")
                        if state.check_insert(vehicle, day, stop, None).feasible:
                            state.insert(
                                site_id, day, start, end, fmt, vehicle,
                                guarantee="marginal",
                                justification=(
                                    f"基线按历史场均出席 {avg_att[site_id]:.1f} 人排序"
                                ),
                            )
                            placed = True
                            break
                    if placed:
                        break
                if placed:
                    break
            if placed:
                break
        if not placed:
            audit.append(f"基线计划排不下站点 {site_id}")
    warnings = assign_staff(bundle, state)
    return WeekPlan(
        week_start=cfg.week_start,
        sessions=state.all_sessions(),
        gaps=compute_guarantee_gaps(bundle, cfg),
        audit=audit,
        route_results=dict(state.results),
        staff_warnings=warnings,
    )
