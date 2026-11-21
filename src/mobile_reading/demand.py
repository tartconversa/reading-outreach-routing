"""需求漏斗估计：潜在 / 愿意 / 可达，三层不混用。

口径约定（对应业务要求）：

1. **去重放大**：同一人可在学校调查与站点报名中重复出现（一人多记录）。
   所有人数口径都按 *人* 去重；推断总体时使用资料自带的 ``sample_weight``，
   并以 *家庭* 为聚类单位计算方差，避免把同家庭的两名成员当成独立观测。

2. **三层漏斗**：
   - 潜在受益人：与站点有关联需求记录的去重人数；
   - 愿意参加：至少一条记录表达意愿（``willing=true``）；
   - 实际可达：在给定站点、给定时间地点条件下，形式匹配且存在可参加的
     60 分钟服务窗。可达性分两个口径：
       a) ``potential_reachable``：站点开放窗与个人可用窗的函数（不依赖具体排期）；
       b) 对具体场次的可达判断见 :func:`session_reachable`，供排期引擎调用。

3. **不确定性**：给出加权点估计、家庭聚类稳健标准误与 95% t 区间，以及
   估计适用范围（样本量、家庭数）。样本权重是基础权重，不做事后校准。

4. **未到场原因**：只统计未到场者自己提供的 ``unmet_reason``，出席者无法
   替他们回答；历史场次侧的事实（拒绝数、取消）单独报告，不并入自报原因。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .models import DataBundle, NeedRecord, Person, Site
from . import SESSION_MINUTES

# t 分布双侧 95% 临界值（按自由度查表，末尾按大样本正态 1.96 收敛）
_T_CRIT = {
    30: 2.042, 40: 2.021, 50: 2.009, 60: 2.000, 80: 1.990,
    100: 1.984, 120: 1.980, 1_000_000: 1.96,
}


def _t95(df: int) -> float:
    if df <= 0:
        return float("nan")
    for cap in sorted(_T_CRIT):
        if df <= cap:
            return _T_CRIT[cap]
    return 1.96


@dataclass(frozen=True)
class Estimate:
    """加权总量估计与家庭聚类稳健不确定性。"""

    point: float
    se: float
    ci_low: float
    ci_high: float
    observed_people: int
    observed_households: int
    basis: str  # 适用范围说明

    def as_dict(self) -> dict:
        return {
            "point_estimate": round(self.point, 2),
            "standard_error": None if math.isnan(self.se) else round(self.se, 3),
            "ci95": [
                None if math.isnan(self.ci_low) else round(max(self.ci_low, 0.0), 2),
                None if math.isnan(self.ci_high) else round(self.ci_high, 2),
            ],
            "observed_people": self.observed_people,
            "observed_households": self.observed_households,
            "basis": self.basis,
        }


def weighted_estimate(
    bundle: DataBundle,
    persons: list[Person],
    indicator: dict[str, int],
    basis: str,
) -> Estimate:
    """对一组去重人员的 0/1 指标做加权总量 + 家庭聚类方差估计。

    家庭聚类稳健方差（单阶整群抽样近似）：
        V = m/(m-1) * Σ_h (Y_h - W_h * p_hat)^2
    其中 ``Y_h`` 为家庭 h 的加权指标和，``W_h`` 为家庭 h 的权重和，
    ``p_hat`` 为总体加权比例。该式在等概率整群抽样下无偏，这里作为
    设计效应的保守近似；家庭内两人相关时不会低估方差。
    """
    members = bundle.people_by_household()
    chosen = {p.id for p in persons}
    if not chosen:
        return Estimate(0.0, float("nan"), float("nan"), float("nan"), 0, 0, basis)

    total_w = sum(bundle.people[pid].sample_weight for pid in chosen)
    total_wy = sum(
        bundle.people[pid].sample_weight * indicator.get(pid, 0) for pid in chosen
    )
    p_hat = total_wy / total_w if total_w else 0.0

    used_households = {
        bundle.people[pid].household
        for pid in chosen
    }
    m = len(used_households)
    cluster_sum = 0.0
    for hh in used_households:
        yh = wh = 0.0
        for member in members[hh]:
            if member.id not in chosen:
                continue
            w = member.sample_weight
            wh += w
            yh += w * indicator.get(member.id, 0)
        cluster_sum += (yh - wh * p_hat) ** 2
    if m > 1:
        variance = m / (m - 1) * cluster_sum
        se = math.sqrt(max(variance, 0.0))
        tcrit = _t95(m - 1)
    else:
        se = float("nan")
        tcrit = float("nan")
    point = total_wy
    return Estimate(
        point=point,
        se=se,
        ci_low=point - tcrit * se if not math.isnan(se) else float("nan"),
        ci_high=point + tcrit * se if not math.isnan(se) else float("nan"),
        observed_people=sum(1 for pid in chosen if indicator.get(pid, 0) == 1),
        observed_households=len(
            {bundle.people[pid].household for pid in chosen if indicator.get(pid, 0) == 1}
        ),
        basis=basis,
    )


def has_feasible_window(rec: NeedRecord, site: Site) -> bool:
    """站点开放窗 ∩ 个人可用窗内，能否容纳一场 60 分钟服务。"""
    lo = max(site.open_start, rec.available_start)
    hi = min(site.open_end, rec.available_end)
    return hi - lo >= SESSION_MINUTES


def person_site_records(
    bundle: DataBundle, person_id: str, site_id: str
) -> list[NeedRecord]:
    return [
        r
        for r in bundle.needs_by_person().get(person_id, ())
        if r.site_id == site_id
    ]


def potential_reachable(bundle: DataBundle, person_id: str, site_id: str) -> bool:
    """不依赖具体排期的可达判断：至少一条该站点记录满足时间窗，且形式可供给。

    形式口径：个人必要访问形式为 audio 时，站点必须支持 audio；
    text 需求不限制形式（普通文本可由 paper 场次覆盖）。
    """
    site = bundle.sites[site_id]
    person = bundle.people[person_id]
    if person.access_need == "audio" and not site.supports("audio"):
        return False
    records = person_site_records(bundle, person_id, site_id)
    return any(has_feasible_window(r, site) for r in records)


def session_reachable(
    bundle: DataBundle,
    person_id: str,
    site_id: str,
    session_start: int,
    session_end: int,
    session_format: str,
) -> bool:
    """对一个具体场次的可达判断（排期引擎与兑现复核共用）。

    条件全部满足才可达：
    1. 场次形式覆盖个人必要访问形式（audio 必须 audio；text 不限制）；
    2. 场次落在站点开放窗内；
    3. 个人在 ``start + travel_minutes`` 前可抵达，且散场时间不晚于其可用止时。
    """
    site = bundle.sites[site_id]
    person = bundle.people[person_id]
    if person.access_need == "audio" and session_format != "audio":
        return False
    if session_start < site.open_start or session_end > site.open_end:
        return False
    records = person_site_records(bundle, person_id, site_id)
    for rec in records:
        # 可参加条件：场次开始不早于个人可用起时；含出行时间后能在散场前抵达；
        # 散场不晚于个人可用止时
        if (
            session_start >= rec.available_start
            and session_start + rec.travel_minutes <= session_end
            and session_start + rec.travel_minutes <= rec.available_end
            and session_end <= rec.available_end
        ):
            return True
    return False


@dataclass(frozen=True)
class Funnel:
    site_id: str
    potential: Estimate
    willing: Estimate
    reachable: Estimate
    # 口径核对用的登记侧计数（不去重来源，仅作下限对照，不参与放大）
    raw_registration_rows: int
    duplicate_rows_removed: int
    school_rows: int
    site_rows: int


@dataclass(frozen=True)
class BarrierReport:
    """未到场者自报障碍（只含本人登记记录），与出席侧事实分开。"""

    reason: str | None
    willing_people: int
    all_rows: int
    weighted_people_estimate: Estimate


@dataclass(frozen=True)
class SiteDemandReport:
    funnel: Funnel
    barriers: tuple[BarrierReport, ...]
    attendance_side_facts: dict
    notes: tuple[str, ...]


def _site_persons(bundle: DataBundle, site_id: str) -> list[Person]:
    ids = {r.person_id for r in bundle.needs_by_site().get(site_id, ())}
    return [bundle.people[pid] for pid in sorted(ids)]


def build_site_funnel(bundle: DataBundle, site_id: str) -> Funnel:
    site = bundle.sites[site_id]
    records = bundle.needs_by_site().get(site_id, [])
    persons = _site_persons(bundle, site_id)
    person_ids = {p.id for p in persons}

    willing_ind: dict[str, int] = {}
    reach_ind: dict[str, int] = {}
    for pid in person_ids:
        recs = person_site_records(bundle, pid, site_id)
        willing_ind[pid] = 1 if any(r.willing for r in recs) else 0
        reach_ind[pid] = 1 if potential_reachable(bundle, pid, site_id) else 0

    basis = (
        f"站点 {site_id} 关联登记去重人员；sample_weight 为家庭调查基础权重，"
        "方差按家庭聚类估计；结果仅外推至该站点同类服务人群"
    )
    potential = weighted_estimate(bundle, persons, {pid: 1 for pid in person_ids}, basis)
    willing = weighted_estimate(
        bundle, persons, willing_ind, basis + "（至少一条登记表达意愿）"
    )
    reachable = weighted_estimate(
        bundle, persons, reach_ind,
        basis + "（形式可供给且开放窗与可用窗存在 60 分钟交集）",
    )

    source_count = {"school": 0, "site": 0}
    for r in records:
        source_count[r.source] += 1
    unique_person_site = len(person_ids)
    return Funnel(
        site_id=site_id,
        potential=potential,
        willing=willing,
        reachable=reachable,
        raw_registration_rows=len(records),
        duplicate_rows_removed=len(records) - unique_person_site,
        school_rows=source_count["school"],
        site_rows=source_count["site"],
    )


def build_barriers(bundle: DataBundle, site_id: str) -> tuple[BarrierReport, ...]:
    """按自报原因分组的未到场障碍。

    严格只用登记人本人记录；``unmet_reason is None`` 单列一组表示“未声明障碍”。
    另按是否愿意参加拆分，避免把拒绝参加者混入服务障碍。
    """
    records = [r for r in bundle.needs_by_site().get(site_id, ())]
    reasons = sorted({r.unmet_reason for r in records}, key=lambda x: (x is None, x))
    out: list[BarrierReport] = []
    persons = _site_persons(bundle, site_id)
    for reason in reasons:
        matched = [r for r in records if r.unmet_reason == reason]
        willing_people = len({r.person_id for r in matched if r.willing})
        indicator = {
            pid: 1 if any(
                r.unmet_reason == reason for r in person_site_records(bundle, pid, site_id)
            )
            else 0
            for pid in {p.id for p in persons}
        }
        est = weighted_estimate(
            bundle, persons, indicator,
            f"站点 {site_id} 自报原因为 {reason or '未声明'} 的去重人员估计",
        )
        out.append(
            BarrierReport(
                reason=reason,
                willing_people=willing_people,
                all_rows=len(matched),
                weighted_people_estimate=est,
            )
        )
    return tuple(out)


def attendance_side_facts(bundle: DataBundle, site_id: str) -> dict:
    """历史场次侧事实：取消不是低需求，拒绝数是供给不足信号。"""
    visits = [v for v in bundle.visits if v.site_id == site_id]
    held = [v for v in visits if not v.cancelled]
    cancelled = [v for v in visits if v.cancelled]
    attendances = sum(len(v.attendee_ids) for v in held)
    unique_attendees = len({a for v in held for a in v.attendee_ids})
    rejected = sum(v.rejected for v in held)
    return {
        "historical_sessions": len(visits),
        "held_sessions": len(held),
        "cancelled_sessions": len(cancelled),
        "cancelled_dates": [v.on.isoformat() for v in cancelled],
        "total_attendances": attendances,
        "unique_attendees": unique_attendees,
        "rejected_requests": rejected,
        "note": "取消场次出席为 0 属供给侧事实，不得计入需求下降；拒绝请求代表未被满足的需求",
    }


def build_demand_report(bundle: DataBundle) -> list[SiteDemandReport]:
    reports: list[SiteDemandReport] = []
    for site_id in sorted(bundle.sites):
        funnel = build_site_funnel(bundle, site_id)
        barriers = build_barriers(bundle, site_id)
        facts = attendance_side_facts(bundle, site_id)
        notes: list[str] = []
        if funnel.duplicate_rows_removed:
            notes.append(
                f"学校与站点来源有 {funnel.duplicate_rows_removed} 条重复登记，"
                "已按人去重，未重复放大需求"
            )
        notes.append("未到场原因仅来自登记人自报，出席者不代答")
        reports.append(
            SiteDemandReport(
                funnel=funnel,
                barriers=barriers,
                attendance_side_facts=facts,
                notes=tuple(notes),
            )
        )
    return reports


def global_funnel(bundle: DataBundle) -> dict:
    """全县口径：跨站点再次按人去重后的三层总量。"""
    all_persons = list(bundle.people.values())
    willing_ind = {
        p.id: 1 if any(r.willing for r in bundle.needs_by_person().get(p.id, ())) else 0
        for p in all_persons
    }
    reach_ind = {
        p.id: 1
        if any(
            potential_reachable(bundle, p.id, r.site_id)
            for r in bundle.needs_by_person().get(p.id, ())
        )
        else 0
        for p in all_persons
    }
    basis = "全县合成名册；跨站点按人去重，家庭聚类稳健方差"
    return {
        "potential": weighted_estimate(
            bundle, all_persons, {p.id: 1 for p in all_persons}, basis
        ).as_dict(),
        "willing": weighted_estimate(bundle, all_persons, willing_ind, basis).as_dict(),
        "reachable_any_site": weighted_estimate(
            bundle, all_persons, reach_ind, basis
        ).as_dict(),
        "dedup_fact": {
            "raw_need_rows": len(bundle.needs),
            "unique_people": len(bundle.people),
            "people_in_both_sources": _people_in_both_sources(bundle),
        },
    }


def _people_in_both_sources(bundle: DataBundle) -> int:
    seen: dict[str, set[str]] = {}
    for r in bundle.needs:
        seen.setdefault(r.person_id, set()).add(r.source)
    return sum(1 for srcs in seen.values() if len(srcs) > 1)
