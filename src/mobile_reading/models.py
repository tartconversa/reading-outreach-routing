"""领域模型与资料装载。

本模块把合成资料包的原始 JSON 行转换为带类型的领域对象，并集中提供
按编号索引。原始字典视为不可变事实，分析过程只读取、不改写。

设计要点：
- 一个人可能同时出现在学校调查与站点报名中，``NeedRecord`` 保留来源，
  去重规则在 :mod:`mobile_reading.demand` 中按人/家庭处理，装载层不做合并；
- ``null`` 一律保留为 ``None``，绝不解释为零；
- 时间统一在装载时换算为当日分钟数（0..1440），日期保留为 ``date``。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any


def to_minutes(text: str) -> int:
    """``"09:00"`` -> ``540``。"""
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def to_date(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()


@dataclass(frozen=True)
class School:
    id: str
    area: str
    devices: int


@dataclass(frozen=True)
class Person:
    id: str
    school_id: str
    household: str
    grade: int
    access_need: str  # audio / text
    sample_weight: float


@dataclass(frozen=True)
class Site:
    id: str
    school_id: str
    x: float
    y: float
    open_start: int
    open_end: int
    minimum_per_4weeks: int
    formats: tuple[str, ...]

    @property
    def is_remote(self) -> bool:
        """偏远站点按最低频率约定识别（每四周 2 场及以上），而非按坐标猜测。"""
        return self.minimum_per_4weeks >= 2

    def supports(self, fmt: str) -> bool:
        return fmt in self.formats


@dataclass(frozen=True)
class NeedRecord:
    """单条需求登记。同一人可有多条（跨来源或跨站点）。"""

    id: str
    person_id: str
    site_id: str
    source: str  # school / site
    willing: bool
    travel_minutes: int
    available_start: int
    available_end: int
    unmet_reason: str | None


@dataclass(frozen=True)
class RoadEdge:
    id: str
    from_site: str
    to_site: str
    minutes: int
    valid_from: date
    valid_to: date | None

    def valid_on(self, day: date) -> bool:
        if day < self.valid_from:
            return False
        return self.valid_to is None or day <= self.valid_to


@dataclass(frozen=True)
class Vehicle:
    id: str
    seats: int
    from_site: str
    shift_start: int
    shift_end: int
    max_minutes: int
    cost_per_hour: float


@dataclass(frozen=True)
class Staff:
    id: str
    formats: tuple[str, ...]
    weekdays: tuple[int, ...]
    max_minutes: int

    def available_on(self, day: date) -> bool:
        return day.weekday() in self.weekdays

    def supports(self, fmt: str) -> bool:
        return fmt in self.formats


@dataclass(frozen=True)
class Visit:
    """历史实际场次。取消场次 ``attendee_ids`` 为空，是供给侧事实，不是低需求证据。"""

    id: str
    site_id: str
    on: date
    seats: int
    attendee_ids: tuple[str, ...]
    rejected: int
    cancelled: bool
    format: str


@dataclass(frozen=True)
class ConfirmedSession:
    """已通知家庭的周行程，属于服务承诺。"""

    id: str
    site_id: str
    vehicle_id: str
    staff_id: str
    on: date
    start: int
    end: int
    format: str
    notified_households: tuple[str, ...]


@dataclass(frozen=True)
class Disruption:
    id: str
    from_site: str
    to_site: str
    on: date
    minutes: int
    received_on: date


@dataclass(frozen=True)
class DataBundle:
    """一次装载后的全部输入事实及索引。"""

    schools: dict[str, School]
    people: dict[str, Person]
    sites: dict[str, Site]
    needs: list[NeedRecord]
    roads: list[RoadEdge]
    vehicles: dict[str, Vehicle]
    staff: dict[str, Staff]
    visits: list[Visit]
    confirmed: list[ConfirmedSession]
    disruptions: list[Disruption]
    fingerprint: str
    row_counts: dict[str, int]
    raw_path: Path | None = field(default=None)

    # ---- 派生索引（惰性建立） ----
    def needs_by_person(self) -> dict[str, list[NeedRecord]]:
        out: dict[str, list[NeedRecord]] = {}
        for rec in self.needs:
            out.setdefault(rec.person_id, []).append(rec)
        return out

    def needs_by_site(self) -> dict[str, list[NeedRecord]]:
        out: dict[str, list[NeedRecord]] = {}
        for rec in self.needs:
            out.setdefault(rec.site_id, []).append(rec)
        return out

    def visits_on_or_before(self, day: date) -> list[Visit]:
        return [v for v in self.visits if v.on <= day]

    def people_by_household(self) -> dict[str, list[Person]]:
        out: dict[str, list[Person]] = {}
        for person in self.people.values():
            out.setdefault(person.household, []).append(person)
        return out


def _require_columns(row: dict[str, Any], allowed: set[str], table: str) -> None:
    if set(row) != allowed:
        raise ValueError(f"字段与契约不一致：{table}")


def load_bundle(materials_dir: str | Path, fingerprint: str = "", row_counts: dict[str, int] | None = None) -> DataBundle:
    """从资料目录读取 ``data.json`` 并构造 :class:`DataBundle`。

    结构校验由 ``inspect_materials.read_materials`` 负责；本函数只做领域转换。
    """
    materials_dir = Path(materials_dir)
    raw_path = materials_dir / "data.json"
    data = json.loads(raw_path.read_text(encoding="utf-8"))

    schools = {
        r["id"]: School(r["id"], r["area"], r["devices"])
        for r in data["schools"]
    }
    people = {
        r["id"]: Person(
            r["id"], r["school_id"], r["household"], r["grade"],
            r["access_need"], float(r["sample_weight"]),
        )
        for r in data["people"]
    }
    sites = {
        r["id"]: Site(
            r["id"], r["school_id"], float(r["x"]), float(r["y"]),
            to_minutes(r["open_start"]), to_minutes(r["open_end"]),
            int(r["minimum_per_4weeks"]), tuple(r["format"]),
        )
        for r in data["sites"]
    }
    needs = [
        NeedRecord(
            r["id"], r["person_id"], r["site_id"], r["source"],
            bool(r["willing"]), int(r["travel_minutes"]),
            to_minutes(r["available_start"]), to_minutes(r["available_end"]),
            r["unmet_reason"],
        )
        for r in data["needs"]
    ]
    roads = [
        RoadEdge(
            r["id"], r["from_site"], r["to_site"], int(r["minutes"]),
            to_date(r["valid_from"]),
            to_date(r["valid_to"]) if r["valid_to"] is not None else None,
        )
        for r in data["roads"]
    ]
    vehicles = {
        r["id"]: Vehicle(
            r["id"], int(r["seats"]), r["from_site"],
            to_minutes(r["shift_start"]), to_minutes(r["shift_end"]),
            int(r["max_minutes"]), float(r["cost_per_hour"]),
        )
        for r in data["vehicles"]
    }
    staff = {
        r["id"]: Staff(
            r["id"], tuple(r["formats"]), tuple(r["weekdays"]),
            int(r["max_minutes"]),
        )
        for r in data["staff"]
    }
    visits = [
        Visit(
            r["id"], r["site_id"], to_date(r["on"]), int(r["seats"]),
            tuple(r["attendee_ids"]), int(r["rejected"]),
            bool(r["cancelled"]), r["format"],
        )
        for r in data["visits"]
    ]
    confirmed = [
        ConfirmedSession(
            r["id"], r["site_id"], r["vehicle_id"], r["staff_id"],
            to_date(r["on"]), to_minutes(r["start"]), to_minutes(r["end"]),
            r["format"], tuple(r["notified_households"]),
        )
        for r in data["confirmed_week"]
    ]
    disruptions = [
        Disruption(
            r["id"], r["from_site"], r["to_site"], to_date(r["on"]),
            int(r["minutes"]), to_date(r["received_on"]),
        )
        for r in data["disruptions"]
    ]
    return DataBundle(
        schools=schools, people=people, sites=sites, needs=needs,
        roads=roads, vehicles=vehicles, staff=staff, visits=visits,
        confirmed=confirmed, disruptions=disruptions,
        fingerprint=fingerprint, row_counts=row_counts or {}, raw_path=raw_path,
    )
