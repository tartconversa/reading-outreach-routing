"""命令行入口：分析、排期、中断改期、回录复核全流程。

输出默认写入 ``output/``（该目录在 .gitignore 约定之外另行管理，
原始资料 materials/ 始终只读）。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .feedback import (
    ServiceOutcome,
    coverage_audit,
    next_round_demand_signals,
)
from .models import load_bundle
from .planner import PlannerConfig, build_attendance_baseline, build_week_plan
from .replan import ReplanResult, audit_promises, cancel_session, replan_week
from .reports import (
    demand_report_payload,
    fulfilment_report_payload,
    plan_report_payload,
    render_demand_text,
    render_fulfilment_text,
    render_plan_text,
    write_json,
)

DEFAULT_WEEK_START = "2026-08-03"


def _load(materials: Path):
    # 复用资料包自带的结构校验与指纹
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from inspect_materials import read_materials  # type: ignore

    data, receipt = read_materials(materials)
    return load_bundle(
        materials,
        fingerprint=receipt["sha256"],
        row_counts=receipt["rows"],
    )


def _config(args) -> PlannerConfig:
    return PlannerConfig(
        week_start=date.fromisoformat(args.week_start),
        fresh_cycle=getattr(args, "fresh_cycle", False),
    )


def cmd_demand(args) -> int:
    bundle = _load(Path(args.materials))
    payload = demand_report_payload(bundle)
    out = Path(args.out)
    write_json(out / "demand_report.json", payload)
    text = render_demand_text(bundle)
    (out / "demand_report.txt").write_text(text, encoding="utf-8")
    print(text)
    print(f"\n已写出：{out / 'demand_report.json'}")
    return 0


def cmd_plan(args) -> int:
    bundle = _load(Path(args.materials))
    cfg = _config(args)
    plan = build_week_plan(bundle, cfg)
    baseline = build_attendance_baseline(bundle, cfg)
    payload = plan_report_payload(bundle, plan, baseline)
    out = Path(args.out)
    write_json(out / "plan_report.json", payload)
    text = render_plan_text(bundle, payload)
    (out / "plan_report.txt").write_text(text, encoding="utf-8")
    print(text)
    print(f"\n已写出：{out / 'plan_report.json'}")
    return 0


def cmd_replan(args) -> int:
    bundle = _load(Path(args.materials))
    cfg = _config(args)
    remove = set(args.remove or [])
    result: ReplanResult = replan_week(bundle, cfg, remove_session_ids=remove)
    payload = {
        "report": "replan",
        "rejected_changes": result.rejected_changes,
        "locked_route_violations": result.locked_violations,
        "plan": plan_report_payload(bundle, result.plan),
        "household_promises": [p.__dict__ for p in result.promises],
    }
    out = Path(args.out)
    write_json(out / "replan_report.json", payload)
    print(render_plan_text(bundle, payload["plan"]))
    print("\n承诺与拒绝项：")
    for item in result.rejected_changes:
        print("  [拒绝]", item)
    for p in result.promises:
        print(f"  [承诺] {p.confirmed_id} {p.household} -> {p.status}")
    print(f"\n已写出：{out / 'replan_report.json'}")
    return 0


def cmd_cancel(args) -> int:
    bundle = _load(Path(args.materials))
    cfg = _config(args)
    plan = build_week_plan(bundle, cfg)
    target = next((s for s in plan.sessions if s.id == args.session), None)
    if target is None:
        print(f"场次 {args.session} 不在本周计划中", file=sys.stderr)
        return 2
    substitute = None
    if args.substitute:
        substitute = next(
            (s for s in plan.sessions if s.id == args.substitute), None
        )
        if substitute is None:
            print(f"替代场次 {args.substitute} 不存在", file=sys.stderr)
            return 2
    outcome = cancel_session(
        bundle, plan, args.session, args.reason, substitute
    )
    payload = {
        "report": "cancellation",
        "cancelled_session_id": outcome.cancelled_session_id,
        "reason": outcome.reason,
        "equivalent_session_id": outcome.equivalent_session_id,
        "outstanding_households": list(outcome.outstanding_households),
        "makegood_required": outcome.makegood_required,
        "household_outcomes": [p.__dict__ for p in outcome.household_outcomes],
    }
    out = Path(args.out)
    write_json(out / "cancellation_report.json", payload)
    for p in outcome.household_outcomes:
        print(f"{p.household}: {p.status}（{p.reason}）")
    print(f"\n已写出：{out / 'cancellation_report.json'}")
    return 0


def cmd_feedback(args) -> int:
    bundle = _load(Path(args.materials))
    cfg = _config(args)
    plan = build_week_plan(bundle, cfg)
    outcomes = _demo_outcomes(bundle, plan, simulate_cancel=args.simulate_cancel)
    promises = audit_promises(bundle, plan)
    coverage = coverage_audit(bundle, plan, outcomes)
    outstanding: dict[str, list[str]] = {}
    for p in promises:
        if p.status == "outstanding":
            outstanding.setdefault(p.site_id, []).append(p.household)
    adjustments = next_round_demand_signals(
        bundle, plan, outcomes, outstanding
    )
    payload = fulfilment_report_payload(
        bundle, plan, outcomes, promises, coverage, adjustments
    )
    out = Path(args.out)
    write_json(out / "fulfilment_report.json", payload)
    text = render_fulfilment_text(payload)
    (out / "fulfilment_report.txt").write_text(text, encoding="utf-8")
    print(text)
    print(f"\n已写出：{out / 'fulfilment_report.json'}")
    return 0


def cmd_demo(args) -> int:
    """端到端业务演练：估计 -> 排期 -> 中断检验 -> 取消台账 -> 回录复核。"""
    bundle = _load(Path(args.materials))
    cfg = _config(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("步骤 1 需求漏斗估计")
    print("=" * 70)
    demand_payload = demand_report_payload(bundle)
    write_json(out / "demand_report.json", demand_payload)
    print(render_demand_text(bundle))

    print("\n" + "=" * 70)
    print("步骤 2 保障优先 + 边际覆盖排期（含按到场排满基线对照）")
    print("=" * 70)
    plan = build_week_plan(bundle, cfg)
    baseline = build_attendance_baseline(bundle, cfg)
    plan_payload = plan_report_payload(bundle, plan, baseline)
    write_json(out / "plan_report.json", plan_payload)
    print(render_plan_text(bundle, plan_payload))

    print("\n" + "=" * 70)
    print("步骤 2b 新四周周期下的最低保障回溯（--fresh-cycle 机制验证）")
    print("=" * 70)
    fresh_cfg = PlannerConfig(week_start=cfg.week_start, fresh_cycle=True)
    fresh_plan = build_week_plan(bundle, fresh_cfg)
    fresh_base = build_attendance_baseline(bundle, fresh_cfg)
    fresh_payload = plan_report_payload(bundle, fresh_plan, fresh_base)
    write_json(out / "plan_report_fresh_cycle.json", fresh_payload)
    shortfalls = fresh_payload["attendance_first_baseline"]["comparison"][
        "baseline_minimum_frequency_shortfalls"
    ]
    print(
        f"保障计划落位 {fresh_payload['metrics']['minimum_frequency_sessions']} 场"
        f"最低频率场次，未决缺口 {len(fresh_plan.gaps)} 个"
    )
    print("按到场排满基线违反最低频率约定的站点：")
    for item in shortfalls:
        print(
            f"  {item['site_id']}：要求 {item['minimum_required']} 场，"
            f"基线仅 {item['baseline_sessions']} 场，缺 {item['shortfall']} 场"
            + ("（偏远保障站点）" if item["remote"] else "")
        )

    print("\n" + "=" * 70)
    print("步骤 3 中断下的承诺路线复核（disruptions 生效日 = 规划周周一）")
    print("=" * 70)
    replan_result = replan_week(bundle, cfg)
    for key, reasons in replan_result.locked_violations.items():
        print(f"[锁定路线异常] {key}: {reasons}")
    if not replan_result.locked_violations:
        print("两条已通知场次所在的 V1 周一整条路线在中断叠加后仍可行（绕行路径生效）")
    for p in replan_result.promises:
        print(f"[承诺] {p.confirmed_id} 家庭 {p.household} -> {p.status}")

    print("\n" + "=" * 70)
    print("步骤 4 取消一场非承诺场次并建立逐户台账演练")
    print("=" * 70)
    cancel_target = next(
        (s for s in plan.sessions if not s.locked), None
    )
    cancellations = []
    if cancel_target is not None:
        co = cancel_session(
            bundle, plan, cancel_target.id,
            "道路临时管制，车辆无法到达", None
        )
        cancellations.append(co)
        print(
            f"取消 {co.cancelled_session_id}：通知家庭承诺 "
            f"{len(co.household_outcomes)} 条，未兑现 {len(co.outstanding_households)} 户"
        )

    print("\n" + "=" * 70)
    print("步骤 5 服务回录：取消不计需求下降，临时访客不计居民基数")
    print("=" * 70)
    outcomes = _demo_outcomes(bundle, plan, simulate_cancel=True)
    promises = audit_promises(bundle, plan)
    coverage = coverage_audit(bundle, plan, outcomes)
    outstanding_map: dict[str, list[str]] = {}
    for c in cancellations:
        for hh in c.outstanding_households:
            outstanding_map.setdefault(cancel_target.site_id, []).append(hh)
    adjustments = next_round_demand_signals(
        bundle, plan, outcomes, outstanding_map
    )
    fulfilment = fulfilment_report_payload(
        bundle, plan, outcomes, promises, coverage, adjustments, cancellations
    )
    write_json(out / "fulfilment_report.json", fulfilment)
    print(render_fulfilment_text(fulfilment))
    print(f"\n全部结构化结果已写出至：{out.resolve()}")
    return 0


def _demo_outcomes(
    bundle, plan, simulate_cancel: bool
) -> list[ServiceOutcome]:
    """构造与计划场次一一对应的合成回录（演练数据，非真实回录）。

    规则刻意覆盖关键业务情形：
    - 第一场边际增补场次模拟取消（供给侧缺席）；
    - 出席者中插入两个名册外编号，验证临时访客不进居民基数；
    - 每场保留 1~2 个计划可达但未到人员，区分 no_show；
    - 席位按车辆容量回录，拒绝数对高需求站点模拟 2 个。
    """
    outcomes: list[ServiceOutcome] = []
    marginal_done = False
    visitor_seq = 0
    for session in plan.sessions:
        reachable = list(session.reachable_person_ids)
        if simulate_cancel and not session.locked and not marginal_done:
            marginal_done = True
            outcomes.append(
                ServiceOutcome(
                    session.id, session.on, session.site_id,
                    bundle.vehicles[session.vehicle_id].seats,
                    (), 0, True, "道路临时管制", (), None,
                )
            )
            continue
        no_show = set(reachable[-2:]) if len(reachable) >= 3 else set()
        attendees = [p for p in reachable if p not in no_show]
        # 插入临时访客（名册外），仅两场，避免污染所有指标
        if visitor_seq < 2 and attendees:
            visitor_seq += 1
            attendees = attendees + [f"VISITOR{visitor_seq}"]
        rejected = 2 if bundle.sites[session.site_id].is_remote else 0
        outcomes.append(
            ServiceOutcome(
                session.id, session.on, session.site_id,
                bundle.vehicles[session.vehicle_id].seats,
                tuple(attendees), rejected, False, None,
                tuple(sorted(no_show)), session.format,
            )
        )
    return outcomes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mobile-reading",
        description="乡镇流动阅读服务需求覆盖与周计划兑现分析",
    )
    parser.add_argument("--materials", default="materials", help="资料目录")
    parser.add_argument("--out", default="output", help="结果输出目录")
    parser.add_argument(
        "--week-start", default=DEFAULT_WEEK_START,
        help="规划周周一日期（默认 2026-08-03，与 confirmed_week 对齐）",
    )
    parser.add_argument(
        "--fresh-cycle", action="store_true",
        help="视为新四周周期起点：历史场次不冲抵最低频率，用于验证保障回溯",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("demand", help="输出三层需求漏斗与不确定性").set_defaults(
        func=cmd_demand
    )
    sub.add_parser("plan", help="生成保障优先的周计划与基线对照").set_defaults(
        func=cmd_plan
    )
    p_replan = sub.add_parser("replan", help="在承诺保护下重新排期")
    p_replan.add_argument(
        "--remove", nargs="*", default=[], help="允许移除的非锁定场次 id"
    )
    p_replan.set_defaults(func=cmd_replan)
    p_cancel = sub.add_parser("cancel", help="取消场次并建立逐户台账")
    p_cancel.add_argument("session", help="场次 id")
    p_cancel.add_argument("--reason", required=True, help="取消原因")
    p_cancel.add_argument("--substitute", help="替代场次 id（须通过整条路线检验）")
    p_cancel.set_defaults(func=cmd_cancel)
    p_fb = sub.add_parser("feedback", help="服务回录与下一轮需求信号")
    p_fb.add_argument(
        "--simulate-cancel", action="store_true",
        help="在合成回录中模拟一场供给侧取消",
    )
    p_fb.set_defaults(func=cmd_feedback)
    sub.add_parser("demo", help="端到端业务演练").set_defaults(func=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
