"""命令行入口。

用法:
  python -m arknights_base_simulation.cli 干员练度表.xlsx --logins 8,22 --config config.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import Assignment, Schedule
from .optimizer import Optimizer, build_profiles
from .roster import load_roster
from .skills import ROOM_CN, SkillDB

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent.parent / "config.json"
DEFAULT_XLSX = Path(__file__).resolve().parent.parent.parent / "data" / "干员练度表.xlsx"

LINE_TO_MAA_PRODUCT = {
    "作战记录": "Battle Record",
    "赤金": "Pure Gold",
    "源石碎片": "Originium Shard",
}


def export_maa(asg: Assignment, layout: tuple[int, int, int]) -> dict:
    """将 Assignment 导出为 MAA 自定义基建排班表 JSON (base-scheduling-schema)。"""
    rooms: dict[str, list] = {}

    rooms["manufacture"] = [
        {"operators": list(ops), "product": LINE_TO_MAA_PRODUCT.get(line, "Pure Gold"),
         "sort": False, "autofill": False}
        for line, ops in asg.manufacture
    ]

    rooms["trading"] = [
        {"operators": list(ops), "sort": False, "autofill": False}
        for ops in asg.trading
    ]

    rooms["control"] = [{"operators": list(asg.control), "sort": False, "autofill": False}]

    rooms["power"] = [
        {"operators": list(ops), "sort": False, "autofill": False}
        if ops else {"skip": True}
        for ops in asg.power
    ]

    rooms["meeting"] = [{"operators": list(asg.meeting), "sort": False, "autofill": False}]

    rooms["hire"] = [{"operators": list(asg.hire), "sort": False, "autofill": False}]

    rooms["dormitory"] = [
        {"operators": list(ops), "sort": False, "autofill": True}
        for ops in asg.dormitory
    ]

    if asg.workshop:
        rooms["processing"] = [{"operators": list(asg.workshop), "sort": False, "autofill": False}]

    return {
        "plans": [{
            "name": f"arknights_base_simulation {layout[0]}-{layout[1]}-{layout[2]}",
            "rooms": rooms,
        }]
    }


def _fmt_ops(ops: list[str]) -> str:
    return "、".join(ops) if ops else "(空)"


def render(asg: Assignment, res, layout, schedule: Schedule) -> str:
    m, t, p = layout
    L: list[str] = []
    L.append("=" * 60)
    L.append("  明日方舟基建 · 最优循环生产方案")
    L.append("=" * 60)
    L.append(f"上线时刻: {', '.join(f'{int(h):02d}:00' for h in schedule.hours)}  "
             f"(最长间隔 {schedule.max_gap:.0f}h)")
    L.append(f"布局: 制造站×{m}  贸易站×{t}  发电站×{p}  "
             f"+ 控制中枢×1 会客室×1 办公室×1 加工站×1 训练室×1 宿舍×{len(asg.dormitory)}")
    elec = res.detail.get("electricity", {})
    if elec:
        L.append(f"电力: 供电 {elec['supply']} / 消耗 {elec['demand']}")
    L.append("")
    L.append(f"  ★ 日均收益 ≈ {res.ap_per_day:.1f} 理智/天")
    L.append("")
    L.append("── 收益拆分 (理智/天) ──")
    for k, v in sorted(res.breakdown.items(), key=lambda kv: -kv[1]):
        if abs(v) > 1e-6:
            L.append(f"   {k:<18} {v:8.1f}")
    L.append("")
    L.append("── 控制中枢 (全局加成) ──")
    g = res.detail["globals"]
    gp = ", ".join(f"{k}+{v:.1f}%" for k, v in g["prod"].items()) or "无"
    L.append(f"   驻员: {_fmt_ops(asg.control)}")
    L.append(f"   全局: 制造生产力[{gp}]  贸易订单效率+{g['trade_eff']:.1f}%  宿舍恢复+{g['recover']:.2f}/h")
    L.append("")
    L.append("── 制造站 ──")
    for d in res.detail["manufacture"]:
        L.append(f"   [{d['line']}] 生产力+{d['prod%']}%  日产{d['items/day']}件  | {_fmt_ops(d['ops'])}")
    dr = res.detail.get("drones", {})
    if dr:
        unit = "单" if dr.get("kind") == "trading" else "件"
        L.append(f"   无人机加速: {dr['per_day']:.0f}架/天 → {dr['line']} +{dr['extra_items/day']}{unit}")
    L.append(f"   赤金总供应: {res.detail['gold_supply/day']}件/天")
    L.append("")
    L.append("── 贸易站 ──")
    for d in res.detail["trading"]:
        idle = f" 空转{d['缺赤金空转单/day']}" if d["缺赤金空转单/day"] > 0.5 else ""
        sp = f" [{', '.join(d['特殊'])}]" if d.get("特殊") else ""
        L.append(f"   订单效率+{d['eff%']}%  赤金单{d['赤金单/day']}/天{idle}  "
                 f"龙门币{d['龙门币/day']:.0f}/天{sp}  | {_fmt_ops(d['ops'])}")
    L.append("")
    L.append("── 其他设施 ──")
    L.append(f"   发电站: {'; '.join(_fmt_ops(o) for o in asg.power)}")
    L.append(f"   会客室: {_fmt_ops(asg.meeting)}")
    L.append(f"   办公室: {_fmt_ops(asg.hire)}")
    for i, room in enumerate(asg.dormitory, 1):
        L.append(f"   宿舍{i}: {_fmt_ops(room)}")
    if res.warnings:
        L.append("")
        L.append("── 注意 ──")
        for w in res.warnings:
            L.append(f"   ⚠ {w}")
    L.append("=" * 60)
    return "\n".join(L)


def render_sim(sim, days: int) -> str:
    L: list[str] = []
    L.append("")
    L.append("=" * 60)
    mood_desc = "错峰(已运行基建)" if sim.staggered else f"全员 {sim.initial_mood:.0f}(新部署)"
    L.append(f"  逐日瞬态模拟 · {days}天 (初始心情 {mood_desc}, 占空循环)")
    L.append("=" * 60)
    L.append(f"稳态日均(乐观参考): {sim.steady_ap:.1f} 理智/天")
    L.append("")
    L.append("  日次   日产理智   平均工作占比   工位休息空缺(人·时)")
    peak = max((d.ap for d in sim.days), default=1.0) or 1.0
    for d in sim.days:
        bar = "█" * int(round(28 * d.ap / peak))
        L.append(f"   D{d.day:<3}  {d.ap:8.1f}      {d.avg_work_fraction*100:5.1f}%        "
                 f"{d.resting_op_hours:6.0f}   {bar}")
    L.append("")
    L.append(f"  {days}天累计: {sim.cumulative_ap:.0f} 理智  (日均 {sim.cumulative_ap / days:.1f}/天)")
    L.append(f"  可持续日产(后半程均值) ≈ {sim.converged_ap:.1f}/天, 为稳态乐观值的 "
             f"{sim.converged_ap / sim.steady_ap * 100 if sim.steady_ap else 0:.0f}%")
    L.append("  说明: 初期心情满 -> 产出高; 干员下班恢复后回落到可持续占空循环。")
    L.append("        曲线呈周期波动是『全员同时满心情部署』的同步效应(整站同步下班 -> 空缺坑);")
    L.append("        真实运行中各干员心情错峰, 实际更平滑, 取后半程均值作可持续日产。")
    L.append("        固定排班无替补, 工位休息空缺为该方案保守下界(玩家有富余干员轮换可填)。")
    L.append("=" * 60)
    return "\n".join(L)


def load_shifts(path: str | Path, config: dict) -> list[Assignment]:
    """从 JSON 文件加载排班清单 (最多4组)。

    每组格式:
    {
      "control": ["干员A", ...],
      "manufacture": [["赤金", ["干员B", ...]], ["作战记录", ["干员C", ...]]],
      "trading": [["干员D", ...], ...],
      "power": [["干员E"], [], []],
      "meeting": ["干员F", ...],
      "hire": ["干员G"],
      "dormitory": [["干员H", ...], ...],
      "training": ["干员I"]
    }
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "shifts" in raw:
        raw = raw["shifts"]
    if not isinstance(raw, list) or not (1 <= len(raw) <= 4):
        raise ValueError(f"排班清单需要 1~4 组, 实际 {len(raw) if isinstance(raw, list) else type(raw)}")

    dorm_count = config.get("dormitory", {}).get("max_rooms", 4)
    shifts: list[Assignment] = []
    for i, s in enumerate(raw):
        asg = Assignment()
        asg.control = list(s.get("control", []))
        for room in s.get("manufacture", []):
            if isinstance(room, list) and len(room) == 2:
                asg.manufacture.append((room[0], list(room[1])))
            elif isinstance(room, dict):
                asg.manufacture.append((room["line"], list(room.get("operators", []))))
        for room in s.get("trading", []):
            asg.trading.append(list(room) if isinstance(room, list) else list(room.get("operators", [])))
        for room in s.get("power", []):
            asg.power.append(list(room))
        asg.meeting = list(s.get("meeting", []))
        asg.hire = list(s.get("hire", []))
        asg.training = list(s.get("training", []))
        for room in s.get("dormitory", []):
            asg.dormitory.append(list(room))
        while len(asg.dormitory) < dorm_count:
            asg.dormitory.append([])
        shifts.append(asg)
    return shifts


def render_shifts(shifts: list[Assignment], results: list, schedule: Schedule) -> str:
    L: list[str] = []
    L.append("=" * 60)
    L.append("  明日方舟基建 · 多班次排班")
    L.append("=" * 60)
    L.append(f"上线时刻: {', '.join(f'{int(h):02d}:00' for h in schedule.hours)}  "
             f"({len(shifts)} 组排班, 每组覆盖一个上线间隔)")
    for i, (asg, res) in enumerate(zip(shifts, results)):
        gap_idx = i % len(schedule.gaps)
        start = schedule.hours[gap_idx]
        end = schedule.hours[(gap_idx + 1) % len(schedule.hours)]
        L.append("")
        L.append(f"── 班次 {i + 1} ({int(start):02d}:00 ~ {int(end):02d}:00)  "
                 f"稳态 {res.ap_per_day:.1f} 理智/天 ──")
        L.append(f"   中枢: {_fmt_ops(asg.control)}")
        for d in res.detail.get("manufacture", []):
            L.append(f"   [{d['line']}] +{d['prod%']}%  {d['items/day']:.1f}件/天  | {_fmt_ops(d['ops'])}")
        for d in res.detail.get("trading", []):
            sp = f" [{', '.join(d['特殊'])}]" if d.get("特殊") else ""
            L.append(f"   贸易 +{d['eff%']}%  {d['龙门币/day']:.0f}龙门币/天{sp}  | {_fmt_ops(d['ops'])}")
        L.append(f"   发电: {'; '.join(_fmt_ops(o) for o in asg.power)}")
        L.append(f"   会客: {_fmt_ops(asg.meeting)}  办公: {_fmt_ops(asg.hire)}")
    L.append("=" * 60)
    return "\n".join(L)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="明日方舟基建运转大模拟 / 最优循环生产方案")
    ap.add_argument("xlsx", nargs="?", default=DEFAULT_XLSX, help="干员练度表.xlsx 路径")
    ap.add_argument("--logins", default=None, help="每日上线时刻(24h), 逗号分隔, 1~4个, 如 8,22")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG), help="config.json 路径")
    ap.add_argument("--values-xlsx", default=None,
                    help="物品价值表.xlsx 路径; 按物品id实时载入等效理智, 覆盖 config 里的 material_values_ap")
    ap.add_argument("--no-local-search", action="store_true", help="关闭局部搜索(更快)")
    ap.add_argument("--days", type=int, default=None,
                    help="逐日瞬态模拟天数: 按真实时间轴推进心情(含初始爬坡/占空循环), 输出每日产出曲线")
    ap.add_argument("--initial-mood", type=float, default=None,
                    help="瞬态模拟的初始心情; 默认错峰模拟已运行基建。设低值(如 12)可看新部署爬坡")
    ap.add_argument("--json", dest="as_json", action="store_true", help="输出 JSON")
    ap.add_argument("--export-maa", default=None, metavar="PATH",
                    help="导出 MAA 自定义基建排班表 JSON 到指定路径")
    ap.add_argument("--shifts", default=None, metavar="PATH",
                    help="多班次排班 JSON 路径 (1~4组); 跳过优化器, 直接用指定排班进行瞬态模拟")
    ap.add_argument("--n-shifts", type=int, default=None, metavar="N",
                    help="自动生成 N 组轮换排班 (1~4); 中枢/制造/贸易分班轮换, 发电/会客/办公固定")
    ap.add_argument("--layout", default=None, metavar="M,T,P",
                    help="指定布局(制造,贸易,发电), 如 4,2,3 表示243; 不指定则枚举所有合法布局取最优")
    ap.add_argument("--lock", default=None, action="append", metavar="ROOM:OP1,OP2,...",
                    help="锁定干员到指定设施, 优化器填充剩余位。可多次使用。"
                         "如 --lock control:令,夕,诗怀雅 --lock trading:但书,巫恋,龙舌兰")
    args = ap.parse_args(argv)

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.values_xlsx:
        from .valuetable import load_value_table
        vals = load_value_table(args.values_xlsx)
        config["material_values_ap"].update(vals)
        print(f"已从物品价值表载入 {len(vals)} 项素材理智价值: "
              + ", ".join(f"{k}={v:.4g}" for k, v in vals.items()), file=sys.stderr)
    if args.logins:
        hours = [float(x) for x in args.logins.split(",") if x.strip() != ""]
        if not (1 <= len(hours) <= 4):
            print("错误: 上线时刻需 1~4 个", file=sys.stderr)
            return 2
        config["schedule"]["login_hours"] = hours
    schedule = Schedule(config["schedule"]["login_hours"])

    roster = load_roster(args.xlsx)
    db = SkillDB()
    profiles = build_profiles(roster, db)
    print(f"已招募 {len(roster)} 名干员, 其中 {len(profiles)} 名有基建技能数据。", file=sys.stderr)

    from .engine import Engine
    if args.shifts:
        shifts = load_shifts(args.shifts, config)
        asg0 = shifts[0]
        m = len(asg0.manufacture)
        t = len(asg0.trading)
        p = len(asg0.power)
        config["layout"]["production_slots"] = m + t + p
        config["layout"]["min_power"] = p
        config["layout"]["max_manufacture"] = m
        config["layout"]["max_trading"] = t
        config["layout"]["max_power"] = p
        eng = Engine(config, profiles, schedule)
        eng._skip_duplicate_check = True
        shift_results = []
        for s_asg in shifts:
            shift_results.append(eng.evaluate(s_asg))
        print(render_shifts(shifts, shift_results, schedule))

        days = args.days or 7
        from .simulate import simulate
        sim = simulate(eng, shifts[0], schedule, shifts=shifts, days=days,
                       initial_mood=args.initial_mood)
        print(render_sim(sim, days))
        return 0

    layout_fixed = None
    if args.layout:
        parts = [int(x) for x in args.layout.split(",")]
        if len(parts) != 3:
            print("错误: --layout 需要 M,T,P 三个数字, 如 4,2,3", file=sys.stderr)
            return 2
        layout_fixed = tuple(parts)
        config["layout"]["production_slots"] = sum(parts)
        config["layout"]["min_power"] = parts[2]

    lock_seed = {}
    if args.lock:
        for spec in args.lock:
            if ":" not in spec:
                print(f"错误: --lock 格式应为 ROOM:OP1,OP2,...  实际: {spec}", file=sys.stderr)
                return 2
            room, ops_str = spec.split(":", 1)
            for op in ops_str.split(","):
                op = op.strip()
                if op:
                    lock_seed[op] = room
        if lock_seed:
            print(f"已锁定: {', '.join(f'{nm}→{room}' for nm, room in lock_seed.items())}", file=sys.stderr)

    opt = Optimizer(config, profiles, schedule)

    if args.n_shifts and args.n_shifts > 1:
        if not (2 <= args.n_shifts <= 4):
            print("错误: --n-shifts 需要 2~4", file=sys.stderr)
            return 2

        def progress_rot(i, n, layout, ap):
            print(f"\r  评估布局 {i}/{n}  制{layout[0]}/贸{layout[1]}/电{layout[2]}  "
                  f"当前最佳 {ap:.0f}    ", end="", file=sys.stderr)

        shifts, shift_results, layout, perpetual = opt.optimize_rotation(
            args.n_shifts, local_search=not args.no_local_search, progress=progress_rot,
            layout=layout_fixed, user_seed=lock_seed or None)
        print("", file=sys.stderr)
        print(render_shifts(shifts, shift_results, schedule))

        days = args.days or 7
        from .simulate import simulate
        sim = simulate(opt.eng, shifts[0], schedule, shifts=shifts, days=days,
                       initial_mood=args.initial_mood, perpetual=perpetual)
        print(render_sim(sim, days))
        return 0

    def progress(i, n, layout, ap):
        print(f"\r  评估布局 {i}/{n}  制{layout[0]}/贸{layout[1]}/电{layout[2]}  "
              f"当前最佳 {ap:.0f}    ", end="", file=sys.stderr)

    asg, res, layout = opt.optimize(local_search=not args.no_local_search, progress=progress,
                                     layout=layout_fixed, user_seed=lock_seed or None)
    print("", file=sys.stderr)

    if args.export_maa:
        maa_plan = export_maa(asg, layout)
        Path(args.export_maa).write_text(
            json.dumps(maa_plan, ensure_ascii=False, indent=4) + "\n",
            encoding="utf-8",
        )
        print(f"已导出 MAA 排班表: {args.export_maa}", file=sys.stderr)

    if args.as_json:
        out = {
            "ap_per_day": res.ap_per_day,
            "layout": {"manufacture": layout[0], "trading": layout[1], "power": layout[2]},
            "breakdown": res.breakdown,
            "assignment": {
                "control": asg.control, "manufacture": asg.manufacture, "trading": asg.trading,
                "power": asg.power, "meeting": asg.meeting, "hire": asg.hire,
                "dormitory": asg.dormitory,
            },
            "warnings": res.warnings,
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(render(asg, res, layout, schedule))
        if args.days:
            from .simulate import simulate
            sim = simulate(opt.eng, asg, schedule, days=args.days,
                           initial_mood=args.initial_mood)
            print(render_sim(sim, args.days))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
