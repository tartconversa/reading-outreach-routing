"""结构化结果与人工复核报告。

负责人“不看地图也要查清”的三类问题，对应三份结构化输出：

1. ``demand_report``：三层漏斗、不确定性、自报障碍与场次侧事实分开；
2. ``plan_report``：每条车日路线的安排理由、保障对象、成本与边际覆盖，
   以及与“按到场排满”基线的覆盖差异；
3. ``fulfilment_report``：承诺逐户状态、取消补兑台账、新增覆盖 vs 重复服务。

所有 JSON 均为纯标准类型，键名用英文 snake_case，说明文字用中文。
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from pathlib import Path

from .demand import SiteDemandReport, global_funnel
from .feedback import (
    CoverageAudit,
    DemandAdjustment,
    ServiceOutcome,
    SessionReconciliation,
)
from .models import DataBundle
from .planner import PlannedSession, WeekPlan
from .replan import HouseholdPromise


def _json_default(obj):
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, set):
        return sorted(obj)
    raise TypeError(f"不可序列化类型：{type(obj)}")


def write_json(path: str | Path, payload) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    import json

    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return path


def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ---------------------------------------------------------------------------
# 需求报告
# ---------------------------------------------------------------------------

def demand_report_payload(bundle: DataBundle) -> dict:
    reports: list[SiteDemandReport] = []
    from .demand import build_demand_report

    reports = build_demand_report(bundle)
    sites = []
    for report in reports:
        f = report.funnel
        sites.append(
            {
                "site_id": f.site_id,
                "remote": bundle.sites[f.site_id].is_remote,
                "registration_dedup": {
                    "raw_rows": f.raw_registration_rows,
                    "duplicate_rows_removed": f.duplicate_rows_removed,
                    "school_source_rows": f.school_rows,
                    "site_source_rows": f.site_rows,
                },
                "funnel": {
                    "potential_beneficiaries": f.potential.as_dict(),
                    "willing_to_attend": f.willing.as_dict(),
                    "reachable_at_site_conditions": f.reachable.as_dict(),
                },
                "self_reported_barriers_non_attenders": [
                    {
                        "reason": b.reason or "未声明",
                        "willing_people": b.willing_people,
                        "rows": b.all_rows,
                        "weighted_estimate": b.weighted_people_estimate.as_dict(),
                        "provenance": "仅来自登记人本人自报，出席者不代答",
                    }
                    for b in report.barriers
                ],
                "attendance_side_facts": report.attendance_side_facts,
                "notes": list(report.notes),
            }
        )
    return {
        "report": "demand_funnel",
        "materials_fingerprint": bundle.fingerprint,
        "county_level": global_funnel(bundle),
        "sites": sites,
    }


# ---------------------------------------------------------------------------
# 计划报告与对照
# ---------------------------------------------------------------------------

def plan_metrics(bundle: DataBundle, plan: WeekPlan) -> dict:
    covered_people = {
        pid for s in plan.sessions for pid in s.reachable_person_ids
    }
    covered_weight = sum(bundle.people[p].sample_weight for p in covered_people)
    households = {
        bundle.people[p].household
        for p in covered_people
        if p in bundle.people
    }
    total_cost = 0.0
    for key, result in plan.route_results.items():
        vehicle_id = key.split("|", 1)[0]
        total_cost += result.work_minutes / 60.0 * bundle.vehicles[vehicle_id].cost_per_hour
    remote_sites = {sid for sid, s in bundle.sites.items() if s.is_remote}
    remote_covered = {
        pid
        for s in plan.sessions
        if s.site_id in remote_sites
        for pid in s.reachable_person_ids
    }
    return {
        "sessions": len(plan.sessions),
        "locked_sessions": sum(1 for s in plan.sessions if s.locked),
        "minimum_frequency_sessions": sum(
            1 for s in plan.sessions if s.guarantee == "minimum_frequency"
        ),
        "marginal_sessions": sum(1 for s in plan.sessions if s.guarantee == "marginal"),
        "unique_reachable_people": len(covered_people),
        "weighted_reachable_people": round(covered_weight, 2),
        "reachable_households": len(households),
        "remote_reachable_people": len(remote_covered),
        "unresolved_guarantee_gaps": [
            {
                "site_id": g.site_id,
                "missing": g.missing,
                "remote": g.remote,
            }
            for g in plan.gaps
        ],
        "estimated_vehicle_cost_yuan": round(total_cost, 2),
        "cost_per_weighted_person_yuan": round(
            total_cost / covered_weight, 3
        ) if covered_weight else None,
        "staff_warnings": plan.staff_warnings,
    }


def plan_report_payload(
    bundle: DataBundle, plan: WeekPlan, baseline: WeekPlan | None = None
) -> dict:
    vehicle_days = []
    for vehicle_id, day in plan.vehicle_days():
        sessions = plan.sessions_on(vehicle_id, day)
        result = plan.route_results[f"{vehicle_id}|{day.isoformat()}"]
        vehicle_days.append(
            {
                "vehicle_id": vehicle_id,
                "date": day.isoformat(),
                "weekday": day.weekday(),
                "route_feasible": result.feasible,
                "feasibility_reasons": list(result.reasons),
                "work_minutes": result.work_minutes,
                "drive_minutes": result.drive_minutes,
                "service_minutes": result.service_minutes,
                "stops": [
                    {
                        "session_id": s.id,
                        "site_id": s.site_id,
                        "window": f"{_hm(s.start)}-{_hm(s.end)}",
                        "format": s.format,
                        "staff_id": s.staff_id,
                        "locked": s.locked,
                        "guarantee_class": s.guarantee,
                        "reachable_people": len(s.reachable_person_ids),
                        "why": s.justification,
                    }
                    for s in sessions
                ],
                "timeline": [
                    {"kind": k, "segment": seg, "from_min": a, "to_min": b}
                    for k, seg, a, b in result.timeline
                ],
            }
        )
    payload = {
        "report": "week_plan",
        "week_start": plan.week_start.isoformat(),
        "metrics": plan_metrics(bundle, plan),
        "vehicle_days": vehicle_days,
        "audit_trail": plan.audit,
        "guarantee_gaps": [asdict(g) for g in plan.gaps],
    }
    if baseline is not None:
        payload["attendance_first_baseline"] = {
            "metrics": plan_metrics(bundle, baseline),
            "comparison": _comparison(bundle, plan, baseline),
            "audit_trail": baseline.audit,
        }
    return payload


def _comparison(bundle: DataBundle, plan: WeekPlan, baseline: WeekPlan) -> dict:
    m1 = plan_metrics(bundle, plan)
    m0 = plan_metrics(bundle, baseline)
    covered1 = {p for s in plan.sessions for p in s.reachable_person_ids}
    covered0 = {p for s in baseline.sessions for p in s.reachable_person_ids}
    remote = {sid for sid, site in bundle.sites.items() if site.is_remote}
    base_counts: dict[str, int] = {}
    for s in baseline.sessions:
        base_counts[s.site_id] = base_counts.get(s.site_id, 0) + 1
    plan_counts: dict[str, int] = {}
    for s in plan.sessions:
        plan_counts[s.site_id] = plan_counts.get(s.site_id, 0) + 1
    # 基线对最低频率约定的违反（偏远站点按每四周 2 场衡量）
    baseline_minimum_shortfalls = [
        {
            "site_id": sid,
            "minimum_required": site.minimum_per_4weeks,
            "baseline_sessions": base_counts.get(sid, 0),
            "shortfall": max(
                0, site.minimum_per_4weeks - base_counts.get(sid, 0)
            ),
            "remote": site.is_remote,
        }
        for sid, site in sorted(bundle.sites.items())
        if base_counts.get(sid, 0) < site.minimum_per_4weeks
    ]
    return {
        "extra_people_covered_by_guarantee_plan": sorted(covered1 - covered0),
        "people_only_in_baseline": sorted(covered0 - covered1),
        "baseline_missing_remote_sites": sorted(
            sid
            for sid in remote
            if not any(s.site_id == sid for s in baseline.sessions)
        ),
        "baseline_minimum_frequency_shortfalls": baseline_minimum_shortfalls,
        "guarantee_plan_site_counts": dict(sorted(plan_counts.items())),
        "baseline_site_counts": dict(sorted(base_counts.items())),
        "weighted_coverage_delta": round(
            m1["weighted_reachable_people"] - m0["weighted_reachable_people"], 2
        ),
        "cost_delta_yuan": round(
            m1["estimated_vehicle_cost_yuan"] - m0["estimated_vehicle_cost_yuan"], 2
        ),
        "interpretation": (
            "保障计划优先补齐偏远最低频率，再按边际覆盖/成本增补；"
            "基线只按历史场均出席排满，常把每站压成 1 场，"
            "无法兑现偏远站点每四周 2 场的约定"
        ),
    }


# ---------------------------------------------------------------------------
# 兑现与覆盖报告
# ---------------------------------------------------------------------------

def fulfilment_report_payload(
    bundle: DataBundle,
    plan: WeekPlan,
    outcomes: list[ServiceOutcome],
    promises: list[HouseholdPromise],
    coverage: CoverageAudit,
    adjustments: list[DemandAdjustment],
    cancellations: list | None = None,
) -> dict:
    return {
        "report": "fulfilment_and_coverage",
        "household_promises": [asdict(p) for p in promises],
        "promise_summary": {
            "kept": sum(1 for p in promises if p.status == "kept"),
            "equivalent_substitute": sum(
                1 for p in promises if p.status == "equivalent_substitute"
            ),
            "outstanding": sum(1 for p in promises if p.status == "outstanding"),
            "rule": "偏远站点缺席不得由邻近站点出席人数抵消",
        },
        "cancellations": [asdict(c) for c in (cancellations or [])],
        "session_reconciliation": [
            asdict(r)
            for r in _reconciliation(bundle, plan, outcomes)
        ],
        "coverage_audit": asdict(coverage),
        "next_round_demand_signals": [asdict(a) for a in adjustments],
    }


def _reconciliation(
    bundle: DataBundle, plan: WeekPlan, outcomes: list[ServiceOutcome]
) -> list[SessionReconciliation]:
    from .feedback import reconcile_sessions

    return reconcile_sessions(bundle, plan, outcomes)


# ---------------------------------------------------------------------------
# 中文纯文本复核视图
# ---------------------------------------------------------------------------

def render_demand_text(bundle: DataBundle) -> str:
    payload = demand_report_payload(bundle)
    lines = ["# 需求漏斗复核（去重·加权·不确定性）", ""]
    county = payload["county_level"]
    lines.append(
        "全县：登记行 {raw} 行、去重 {uniq} 人，其中双来源重复 {dup} 人".format(
            raw=county["dedup_fact"]["raw_need_rows"],
            uniq=county["dedup_fact"]["unique_people"],
            dup=county["dedup_fact"]["people_in_both_sources"],
        )
    )
    for layer, label in (
        ("potential", "潜在受益人"),
        ("willing", "愿意参加"),
        ("reachable_any_site", "任一站点可达"),
    ):
        est = county[layer]
        lines.append(
            f"  {label}：估计 {est['point_estimate']} 人，95%区间 "
            f"{est['ci95'][0]}~{est['ci95'][1]}（样本 {est['observed_people']} 人/"
            f"{est['observed_households']} 户）"
        )
    lines.append("")
    for site in payload["sites"]:
        f = site["funnel"]
        lines.append(
            f"站点 {site['site_id']}{'（偏远保障）' if site['remote'] else ''}："
            f"原始登记 {site['registration_dedup']['raw_rows']} 行，"
            f"跨来源去重 {site['registration_dedup']['duplicate_rows_removed']} 行"
        )
        for key, label in (
            ("potential_beneficiaries", "潜在"),
            ("willing_to_attend", "愿意"),
            ("reachable_at_site_conditions", "条件可达"),
        ):
            est = f[key]
            lines.append(
                f"  {label}：估计 {est['point_estimate']}，区间 "
                f"{est['ci95'][0]}~{est['ci95'][1]}"
            )
        barriers = [
            b for b in site["self_reported_barriers_non_attenders"]
            if b["reason"] != "未声明"
        ]
        if barriers:
            lines.append(
                "  未到场自报障碍："
                + "；".join(f"{b['reason']} {b['willing_people']} 人" for b in barriers)
            )
        facts = site["attendance_side_facts"]
        lines.append(
            f"  场次侧：举办 {facts['held_sessions']}、取消 {facts['cancelled_sessions']}"
            f"、拒绝请求 {facts['rejected_requests']}（取消不计需求下降）"
        )
    return "\n".join(lines)


def render_plan_text(bundle: DataBundle, payload: dict) -> str:
    lines = ["# 周计划复核（路线·理由·保障·成本）", ""]
    m = payload["metrics"]
    lines.append(
        f"共 {m['sessions']} 场：锁定 {m['locked_sessions']}、保障 "
        f"{m['minimum_frequency_sessions']}、边际增补 {m['marginal_sessions']}；"
        f"覆盖 {m['unique_reachable_people']} 人/{m['reachable_households']} 户，"
        f"加权 {m['weighted_reachable_people']} 人，估算车时成本 "
        f"{m['estimated_vehicle_cost_yuan']} 元"
    )
    if m["unresolved_guarantee_gaps"]:
        lines.append("未兑现保障缺口：" + "；".join(
            f"{g['site_id']} 缺 {g['missing']} 场"
            for g in m["unresolved_guarantee_gaps"]
        ))
    lines.append("")
    for vd in payload["vehicle_days"]:
        lines.append(
            f"[{vd['vehicle_id']} {vd['date']}] 整条路线可行={vd['route_feasible']}，"
            f"工作 {vd['work_minutes']} 分（行驶 {vd['drive_minutes']}、"
            f"服务 {vd['service_minutes']}）"
        )
        if not vd["route_feasible"]:
            lines.append("  不可行原因：" + "；".join(vd["feasibility_reasons"]))
        for stop in vd["stops"]:
            lock = "【承诺锁定】" if stop["locked"] else ""
            lines.append(
                f"  {stop['window']} {stop['site_id']} {stop['format']} "
                f"辅导员 {stop['staff_id']} 可达 {stop['reachable_people']} 人 {lock}"
            )
            lines.append(f"      理由：{stop['why']}")
    if "attendance_first_baseline" in payload:
        cmp_ = payload["attendance_first_baseline"]["comparison"]
        bm = payload["attendance_first_baseline"]["metrics"]
        lines.append("")
        lines.append(
            "对照基线（按到场排满）：覆盖加权 "
            f"{bm['weighted_reachable_people']} 人 vs 保障计划 "
            f"{m['weighted_reachable_people']} 人；基线漏掉偏远站点："
            f"{cmp_['baseline_missing_remote_sites'] or '无'}"
        )
    lines.append("")
    lines.append("审计轨迹：")
    lines.extend(f"  - {a}" for a in payload["audit_trail"])
    return "\n".join(lines)


def render_fulfilment_text(payload: dict) -> str:
    lines = ["# 承诺兑现与覆盖审计", ""]
    s = payload["promise_summary"]
    lines.append(
        f"家庭承诺：保持 {s['kept']}、等效替代 {s['equivalent_substitute']}、"
        f"未兑现 {s['outstanding']}"
    )
    for p in payload["household_promises"]:
        lines.append(
            f"  {p['confirmed_id']} 家庭 {p['household']} -> {p['status']}"
            + (f"（替代 {p['substitute_session_id']}）" if p["substitute_session_id"] else "")
        )
    for c in payload["cancellations"]:
        lines.append(
            f"取消 {c['cancelled_session_id']}（{c['reason']}）："
            f"未兑现家庭 {c['outstanding_households'] or '无'}"
        )
    cov = payload["coverage_audit"]
    lines.append("")
    lines.append(
        f"覆盖审计：新覆盖 {len(cov['newly_covered_people'])} 人、"
        f"重复服务 {len(cov['repeat_served_people'])} 人、"
        f"临时访客 {cov['visitor_attendances']} 人次（不计入居民基数）"
    )
    lines.append("")
    lines.append("下一轮需求信号：")
    for a in payload["next_round_demand_signals"]:
        lines.append(f"  {a['site_id']}：{a['signal']}")
    return "\n".join(lines)
