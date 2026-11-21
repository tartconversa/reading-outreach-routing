"""道路网络与整日路线可行性。

关键口径（见资料契约补充口径）：
- 坐标只用于解释，行程时间一律以 ``roads`` 的有向边为准；
- 道路有生效期，规划日只采用当天有效的边；中断（``disruptions``）是对
  同日同方向边的临时覆盖，不修改原始资料；
- “每站分别可行”不等于“同一天整条路线可行”，:func:`check_day_route`
  对一条完整的车辆日路线做顺序时刻、返场、班次和人员上限的联合检验。
"""
from __future__ import annotations

import heapq
from dataclasses import dataclass
from datetime import date

from .models import ConfirmedSession, DataBundle, RoadEdge, Site, Staff, Vehicle


class RoadGraph:
    """某日有效的有向行驶时间图，可叠加临时中断。"""

    def __init__(self, bundle: DataBundle, day: date, apply_disruptions: bool = True):
        self.day = day
        self._adj: dict[str, dict[str, int]] = {sid: {} for sid in bundle.sites}
        self._edge_origin: dict[tuple[str, str], str] = {}
        for edge in bundle.roads:
            if edge.valid_on(day):
                self._put(edge.from_site, edge.to_site, edge.minutes, edge.id)
        self.overrides: list[str] = []
        if apply_disruptions:
            # 中断是当日行政性通知，无论变长变短都强制覆盖同日同方向边，
            # 不参与“多条有效边取较短者”的规则
            for d in bundle.disruptions:
                if d.on == day:
                    self._force_put(d.from_site, d.to_site, d.minutes, d.id)
                    self.overrides.append(d.id)

    def _put(self, u: str, v: str, minutes: int, edge_id: str) -> None:
        # 同日同方向若存在多条有效边，取较短者，并记录来源以便审计
        if v not in self._adj[u] or minutes < self._adj[u][v]:
            self._adj[u][v] = minutes
            self._edge_origin[(u, v)] = edge_id

    def _force_put(self, u: str, v: str, minutes: int, edge_id: str) -> None:
        self._adj[u][v] = minutes
        self._edge_origin[(u, v)] = edge_id

    def sites(self) -> list[str]:
        return list(self._adj)

    def direct_minutes(self, u: str, v: str) -> int | None:
        return self._adj.get(u, {}).get(v)

    def edge_origin(self, u: str, v: str) -> str | None:
        return self._edge_origin.get((u, v))

    def shortest(self, source: str) -> dict[str, int]:
        """Dijkstra 最短路分钟数；不可达站点不出现在结果中。"""
        if source not in self._adj:
            raise ValueError(f"未知站点：{source}")
        dist = {source: 0}
        heap = [(0, source)]
        while heap:
            du, u = heapq.heappop(heap)
            if du != dist[u]:
                continue
            for v, w in self._adj[u].items():
                nd = du + w
                if v not in dist or nd < dist[v]:
                    dist[v] = nd
                    heapq.heappush(heap, (nd, v))
        return dist

    def travel(self, u: str, v: str) -> int:
        """最短路行驶分钟；不可达时抛出 :class:`RouteInfeasible`。"""
        dist = self.shortest(u)
        if v not in dist:
            raise RouteInfeasible(f"{self.day} {u} 到 {v} 当日无有效道路")
        return dist[v]


class RouteInfeasible(Exception):
    """整条日路线联合检验失败。"""


@dataclass(frozen=True)
class Stop:
    """车辆日路线上的一个停靠场次。"""

    site_id: str
    start: int
    end: int
    format: str
    label: str  # 审计用编号（确认场次 id 或计划场次 id）

    @classmethod
    def from_confirmed(cls, c: ConfirmedSession) -> "Stop":
        return cls(c.site_id, c.start, c.end, c.format, c.id)


@dataclass(frozen=True)
class DayRouteResult:
    feasible: bool
    reasons: tuple[str, ...]
    timeline: tuple[tuple[str, str, int, int], ...]  # (类型, 站点/边来源, 起, 止)
    work_minutes: int
    drive_minutes: int
    service_minutes: int


def check_day_route(
    graph: RoadGraph,
    vehicle: Vehicle,
    staff: Staff | None,
    stops: list[Stop],
    site_index: dict[str, Site],
) -> DayRouteResult:
    """按时间顺序联合检验一条车辆日路线。

    检验项：
    1. 每个停靠段落在站点开放窗内，且时段不重叠；
    2. 相邻停靠之间按当日最短路行驶，到达不晚于下一场开始；
    3. 车辆从自有 ``from_site`` 始发并返场；
    4. 车辆班次窗与每日工作分钟上限（含行驶与服务）；
    5. 辅导员当日可用、技能覆盖全部场次形式，且其工作分钟上限。
    """
    reasons: list[str] = []
    timeline: list[tuple[str, str, int, int]] = []
    drive = service = 0
    day = graph.day

    if staff is not None and not staff.available_on(day):
        reasons.append(f"辅导员 {staff.id} 在 {day}（周{day.weekday()}）不可用")

    ordered = sorted(stops, key=lambda s: s.start)
    # 时段重叠与开放窗、形式
    for prev, cur in zip(ordered, ordered[1:]):
        if cur.start < prev.end:
            reasons.append(f"场次时段重叠：{prev.label} 与 {cur.label}")
    for stop in ordered:
        site = site_index.get(stop.site_id)
        if site is None:
            reasons.append(f"未知站点 {stop.site_id}")
            continue
        if stop.start < site.open_start or stop.end > site.open_end:
            reasons.append(
                f"{stop.label} 时段 {stop.start}-{stop.end} 超出 {site.id} 开放窗 "
                f"{site.open_start}-{site.open_end}"
            )
        if stop.end <= stop.start:
            reasons.append(f"{stop.label} 服务时长必须为正")
        if not site.supports(stop.format):
            reasons.append(f"站点 {site.id} 不支持形式 {stop.format}（{stop.label}）")
        if staff is not None and not staff.supports(stop.format):
            reasons.append(f"辅导员 {staff.id} 不具备 {stop.format} 技能（{stop.label}）")

    # 顺序行驶模拟：depot -> stop1 -> ... -> depot
    cursor = vehicle.from_site
    clock = vehicle.shift_start
    timeline.append(("depot_start", cursor, clock, clock))
    feasible_so_far = True
    for stop in ordered:
        try:
            leg = graph.travel(cursor, stop.site_id)
        except RouteInfeasible as exc:
            reasons.append(str(exc))
            feasible_so_far = False
            break
        arrival = clock + leg
        timeline.append(("drive", f"{cursor}->{stop.site_id}", clock, arrival))
        drive += leg
        clock = arrival
        if clock > stop.start:
            reasons.append(
                f"{stop.label}：到达 {stop.site_id} 为 {clock}，晚于场次开始 {stop.start}"
            )
        # 提前到达视为在场等待，工作分钟从班次开始连续计算，等待计入随车工作
        clock = stop.end
        timeline.append(("service", stop.site_id, stop.start, stop.end))
        service += stop.end - stop.start
        cursor = stop.site_id

    if feasible_so_far:
        try:
            back = graph.travel(cursor, vehicle.from_site)
            clock_back = clock + back
            timeline.append(("drive", f"{cursor}->{vehicle.from_site}", clock, clock_back))
            drive += back
            clock = clock_back
        except RouteInfeasible as exc:
            reasons.append(str(exc))
        timeline.append(("depot_end", vehicle.from_site, clock, clock))

    work_minutes = clock - vehicle.shift_start
    if clock > vehicle.shift_end:
        reasons.append(f"返场 {clock} 晚于班次结束 {vehicle.shift_end}")
    if work_minutes > vehicle.max_minutes:
        reasons.append(
            f"车辆 {vehicle.id} 当日工作 {work_minutes} 分钟，超过上限 {vehicle.max_minutes}"
        )
    if staff is not None and work_minutes > staff.max_minutes:
        reasons.append(
            f"辅导员 {staff.id} 当日工作 {work_minutes} 分钟，超过上限 {staff.max_minutes}"
        )

    return DayRouteResult(
        feasible=not reasons,
        reasons=tuple(reasons),
        timeline=tuple(timeline),
        work_minutes=work_minutes,
        drive_minutes=drive,
        service_minutes=service,
    )
