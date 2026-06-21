"""Engine 评估机制 + 优化器 + 设施约束测试。"""
import copy
import json
from pathlib import Path

from conftest import (
    ROOT,
    CONFIG,
    _DB,
    _prof,
    _override,
    _toy_roster,
)
from arknights_base_simulation.engine import (
    Assignment,
    Engine,
    OperatorProfile,
    Schedule,
    dorm_base_recover,
    dorm_base_recover_for_room,
)
from arknights_base_simulation.optimizer import Optimizer, build_profiles
from arknights_base_simulation.roster import Operator
from arknights_base_simulation.skills import SkillDB
from arknights_base_simulation.synergy import (
    build_context,
    dorm_all_recover_for,
    dorm_average_recover_pool,
    dorm_single_recover_for,
    dorm_low_mood_extra_recover_for,
    dorm_target_extra_recover,
    dorm_target_extra_recover_for,
    factions_of,
)

def test_engine_runs():
    prof = build_profiles(_toy_roster(), _DB)
    eng = Engine(CONFIG, prof, Schedule([8, 22]))
    asg = Assignment(manufacture=[("作战记录", ["食铁兽"])], dormitory=[["夜莺"]])
    res = eng.evaluate(asg)
    assert isinstance(res.ap_per_day, float)
    assert "electricity" in res.detail


def test_load_shifts_parses_manual_multishift_json(tmp_path):
    """CLI 多班次 JSON 应稳定解析为 Assignment, 并按宿舍数量补空房。"""
    from arknights_base_simulation.cli import load_shifts

    path = tmp_path / "shifts.json"
    path.write_text(json.dumps({
        "shifts": [
            {
                "control": ["阿米娅"],
                "manufacture": [["赤金", ["能天使"]], {"line": "作战记录", "operators": ["食铁兽"]}],
                "trading": [["夜莺"], {"operators": ["闪灵"]}],
                "power": [["Lancet-2"], [], []],
                "meeting": ["伊内丝"],
                "hire": ["斥罪"],
                "dormitory": [["德克萨斯"]],
                "training": ["星熊"],
            },
            {
                "manufacture": [],
                "trading": [],
                "power": [],
            },
        ],
    }, ensure_ascii=False), encoding="utf-8")
    cfg = copy.deepcopy(CONFIG)
    cfg["dormitory"]["max_rooms"] = 2

    shifts = load_shifts(path, cfg)
    assert len(shifts) == 2
    first = shifts[0]
    assert first.control == ["阿米娅"]
    assert first.manufacture == [("赤金", ["能天使"]), ("作战记录", ["食铁兽"])]
    assert first.trading == [["夜莺"], ["闪灵"]]
    assert first.power == [["Lancet-2"], [], []]
    assert first.meeting == ["伊内丝"]
    assert first.hire == ["斥罪"]
    assert first.training == ["星熊"]
    assert first.dormitory == [["德克萨斯"], []]
    assert shifts[1].dormitory == [[], []]

    invalid = tmp_path / "too_many_shifts.json"
    invalid.write_text(json.dumps([{}, {}, {}, {}, {}]), encoding="utf-8")
    try:
        load_shifts(invalid, cfg)
    except ValueError as exc:
        assert "1~4" in str(exc)
    else:
        raise AssertionError("load_shifts should reject more than 4 shifts")


def test_shift_example_files_reference_known_ops_when_present():
    """随仓库提供的多班次示例不能引用技能表中不存在的干员名。"""
    from arknights_base_simulation.cli import load_shifts

    skill_names = {
        rec["name"]
        for rec in json.loads((ROOT / "data" / "skills.json").read_text(encoding="utf-8"))
    }
    for name in ("shifts_example.json", "shifts_gongsun.json"):
        path = ROOT / name
        if not path.exists():
            continue
        shifts = load_shifts(path, CONFIG)
        used = set()
        for asg in shifts:
            used.update(asg.control)
            used.update(op for _line, ops in asg.manufacture for op in ops)
            used.update(op for ops in asg.trading for op in ops)
            used.update(op for ops in asg.power for op in ops)
            used.update(asg.meeting)
            used.update(asg.hire)
            used.update(asg.training)
            used.update(op for ops in asg.dormitory for op in ops)
        assert used <= skill_names


def test_electricity_forces_power_stations():
    """电力约束: 0 发电站布局应不可行(供电不足), 3 发电站应可行。"""
    prof = build_profiles(_toy_roster(), _DB)
    eng = Engine(CONFIG, prof, Schedule([8, 22]))
    no_power = Assignment(manufacture=[("赤金", ["食铁兽"])])
    assert not eng._electricity_ok(no_power)[0]
    enough = Assignment(manufacture=[("赤金", ["食铁兽"])], power=[[], [], []])
    assert eng._electricity_ok(enough)[0]


def test_electricity_uses_facility_level_tables():
    """PRTS基础: 设施电力应按当前等级取值, 发电站供电也按等级取值。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["manufacture"]["level"] = 2
    cfg["trading"]["level"] = 2
    cfg["power"]["level"] = 2
    cfg["dormitory"]["level"] = 3
    cfg["training"]["level"] = 2
    cfg["workshop"]["level"] = 1
    prof = build_profiles(_toy_roster(), _DB)
    eng = Engine(cfg, prof, Schedule([8, 22]))
    assert eng._electricity_consumption("workshop") == 10.0

    asg = Assignment(
        manufacture=[("赤金", ["食铁兽"])],
        trading=[["夜莺"]],
        dormitory=[["能天使"]],
        power=[[]],
    )
    feasible, supply, demand = eng._electricity_ok(asg)
    assert (feasible, supply, demand) == (False, 130, 250)

    asg.power.append([])
    assert eng._electricity_ok(asg) == (True, 260, 250)

    cfg["meeting"]["level"] = 2
    cfg["hire"]["level"] = 2
    cfg["workshop"]["level"] = 3
    lower_support = Engine(cfg, prof, Schedule([8, 22]))
    assert lower_support._electricity_consumption("workshop") == 10.0
    asg.power = [[]]
    assert lower_support._electricity_ok(asg) == (False, 130, 190)
    asg.power.append([])
    assert lower_support._electricity_ok(asg) == (True, 260, 190)


def test_indexed_facility_levels_apply_per_room_prts_tables():
    """252等布局: 等级列表应按房间索引取 PRTS 容量/订单/供电表。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["manufacture"]["level"] = [3, 2, 1]
    cfg["trading"]["level"] = [3, 1]
    cfg["power"]["level"] = [3, 2]
    prof = build_profiles(_toy_roster(), _DB)
    eng = Engine(cfg, prof, Schedule([8, 22]))

    assert [eng._manufacture_capacity_volume(i) for i in range(3)] == [54.0, 36.0, 24.0]
    assert [eng._trading_order_limit(i) for i in range(2)] == [10.0, 6.0]
    assert [eng._trading_gold_order_profile(i)["lmd_per_order"] for i in range(2)] == [1450, 1000]
    assert [eng._power_supply_per_station(i) for i in range(2)] == [270.0, 130.0]

    asg = Assignment(
        manufacture=[("赤金", []), ("赤金", []), ("赤金", [])],
        trading=[[], []],
        power=[[], []],
    )
    assert eng._electricity_ok(asg) == (True, 400, 360)


def test_electricity_ignores_unbuilt_fixed_facilities():
    """PRTS基础: 未建成的功能设施不应消耗电力。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["layout"]["fixed_rooms"] = {
        "control": 1,
        "meeting": 0,
        "hire": 0,
        "workshop": 0,
        "training": 0,
        "dormitory": 0,
    }
    prof = build_profiles(_toy_roster(), _DB)
    eng = Engine(cfg, prof, Schedule([8, 22]))
    asg = Assignment(manufacture=[("赤金", ["食铁兽"])], power=[[]])
    assert eng._electricity_ok(asg) == (True, 270, 60)


def test_control_center_level_reports_facility_level_cap_violations():
    """PRTS基础: 控制中枢等级同时限制其它设施等级上限。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["control"]["level"] = 2
    cfg["manufacture"]["level"] = 3
    cfg["trading"]["level"] = 2
    cfg["power"]["level"] = 2
    cfg["meeting"]["level"] = 2
    cfg["hire"]["level"] = 2
    cfg["workshop"]["level"] = 2
    cfg["training"]["level"] = 2
    cfg["dormitory"]["level"] = 3
    prof = build_profiles(_toy_roster(), _DB)
    eng = Engine(cfg, prof, Schedule([8, 22]))
    res = eng.evaluate(Assignment(manufacture=[("赤金", ["食铁兽"])], dormitory=[[], []], power=[[], []]))

    violations = res.detail["facility_level_violations"]
    assert {"room": "manufacture", "level": 3, "max_level": 2} in violations
    assert {"room": "dormitory", "index": 0, "level": 3, "max_level": 2} in violations
    assert {"room": "dormitory", "index": 1, "level": 3, "max_level": 2} in violations
    assert any("控制中枢等级限制" in w for w in res.warnings)
    assert res.ap_per_day < -1e8

    cfg["manufacture"]["level"] = 2
    cfg["dormitory"]["level"] = 2
    valid = Engine(cfg, prof, Schedule([8, 22])).evaluate(
        Assignment(manufacture=[("赤金", ["食铁兽"])], dormitory=[[], []], power=[[], []])
    )
    assert valid.detail["facility_level_violations"] == []
    assert not any("控制中枢等级限制" in w for w in valid.warnings)


def test_engine_rejects_facility_operator_slot_overflow():
    """PRTS基础: 手写排班也不能超过设施进驻人数上限。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["manufacture"]["level"] = 2
    cfg["trading"]["level"] = 2
    cfg["control"]["level"] = 3
    cfg["dormitory"]["level"] = 3
    prof = build_profiles(_toy_roster(), _DB)
    eng = Engine(cfg, prof, Schedule([8, 22]))
    res = eng.evaluate(
        Assignment(
            manufacture=[("赤金", ["能天使", "德克萨斯", "缪尔赛思"])],
            trading=[["食铁兽", "夜莺", "空"]],
            control=["能天使", "德克萨斯", "缪尔赛思", "食铁兽"],
            dormitory=[["能天使", "德克萨斯", "缪尔赛思", "食铁兽", "夜莺", "空"]],
            power=[[], []],
        )
    )

    violations = res.detail["facility_capacity_violations"]
    assert {"type": "operator_slots", "room": "manufacture", "index": 0, "count": 3, "max": 2} in violations
    assert {"type": "operator_slots", "room": "trading", "index": 0, "count": 3, "max": 2} in violations
    assert {"type": "operator_slots", "room": "control", "index": 0, "count": 4, "max": 3} in violations
    assert {"type": "operator_slots", "room": "dormitory", "index": 0, "count": 6, "max": 5} in violations
    assert any("设施容量限制" in w for w in res.warnings)
    assert res.ap_per_day < -1e8


def test_engine_rejects_duplicate_operator_assignment():
    """PRTS基础: 同一干员不能同时进驻多个设施或多个位置。"""
    prof = build_profiles(_toy_roster(), _DB)
    res = Engine(CONFIG, prof, Schedule([8, 22])).evaluate(
        Assignment(
            manufacture=[("赤金", ["能天使"])],
            trading=[["能天使"]],
            dormitory=[["夜莺"], ["夜莺"]],
            power=[[], [], []],
        )
    )

    violations = res.detail["facility_capacity_violations"]
    assert {"type": "duplicate_operator", "operator": "能天使", "count": 2} in violations
    assert {"type": "duplicate_operator", "operator": "夜莺", "count": 2} in violations
    assert any("设施容量限制" in w for w in res.warnings)
    assert res.ap_per_day < -1e8


def test_engine_rejects_invalid_manufacture_line_without_crashing():
    """PRTS基础: 制造站只能生产配置/已支持的制造项。"""
    prof = build_profiles(_toy_roster(), _DB)
    res = Engine(CONFIG, prof, Schedule([8, 22])).evaluate(
        Assignment(manufacture=[("不存在的制造项", ["能天使"])], power=[[], [], []])
    )

    violations = res.detail["facility_capacity_violations"]
    assert {
        "type": "invalid_manufacture_line",
        "room": "manufacture",
        "index": 0,
        "line": "不存在的制造项",
    } in violations
    assert res.detail["manufacture"][0]["invalid_line"] is True
    assert any("invalid line" in w for w in res.warnings)
    assert res.ap_per_day < -1e8


def test_engine_rejects_facility_room_count_overflow():
    """PRTS基础: 制造/贸易/发电生产区房间数与宿舍房间数不能超过建造上限。"""
    cfg = copy.deepcopy(CONFIG)
    prof = build_profiles(_toy_roster(), _DB)
    eng = Engine(cfg, prof, Schedule([8, 22]))
    res = eng.evaluate(
        Assignment(
            manufacture=[("赤金", []) for _ in range(6)],
            trading=[[] for _ in range(4)],
            power=[[]],
            dormitory=[[], [], [], [], []],
        )
    )

    violations = res.detail["facility_capacity_violations"]
    assert {"type": "room_count", "room": "manufacture", "count": 6, "max": 5} in violations
    assert {"type": "production_room_count", "room": "production", "count": 11, "max": 9} in violations
    assert {"type": "room_count", "room": "dormitory", "count": 5, "max": 4} in violations
    assert any("设施容量限制" in w for w in res.warnings)
    assert res.ap_per_day < -1e8


def test_engine_rejects_unbuilt_fixed_facility_assignment():
    """PRTS基础: 未建成的固定功能设施不能手写进驻或放置宿舍房间。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["layout"]["fixed_rooms"] = {
        "control": 1,
        "meeting": 0,
        "hire": 0,
        "workshop": 0,
        "training": 0,
        "dormitory": 2,
    }
    prof = build_profiles(_toy_roster(), _DB)
    res = Engine(cfg, prof, Schedule([8, 22])).evaluate(
        Assignment(
            meeting=["能天使"],
            hire=["德克萨斯"],
            workshop=["夜莺"],
            training=["食铁兽"],
            dormitory=[[], [], []],
            power=[[], [], []],
        )
    )

    violations = res.detail["facility_capacity_violations"]
    assert {"type": "room_count", "room": "meeting", "count": 1, "max": 0} in violations
    assert {"type": "room_count", "room": "hire", "count": 1, "max": 0} in violations
    assert {"type": "room_count", "room": "workshop", "count": 1, "max": 0} in violations
    assert {"type": "room_count", "room": "training", "count": 1, "max": 0} in violations
    assert {"type": "room_count", "room": "dormitory", "count": 3, "max": 2} in violations
    assert any("设施容量限制" in w for w in res.warnings)
    assert res.ap_per_day < -1e8


def test_optimizer_enumerates_only_prts_buildable_layouts():
    """布局枚举应遵守 PRTS 建造上限: 制造/贸易最多5, 发电最多3。"""
    opt = Optimizer(CONFIG, {}, Schedule([8, 22]))
    layouts = opt._enumerate_layouts()
    assert layouts
    assert all(m + t + p == CONFIG["layout"]["production_slots"] for m, t, p in layouts)
    assert all(m <= CONFIG["layout"]["max_manufacture"] for m, _t, _p in layouts)
    assert all(t <= CONFIG["layout"]["max_trading"] for _m, t, _p in layouts)
    assert all(p <= CONFIG["layout"]["max_power"] for _m, _t, p in layouts)
    assert (4, 2, 3) in layouts
    assert all(t <= m for m, t, _p in layouts), "贸易站不能超过制造站"
    assert len(layouts) == 7


def test_optimizer_uses_facility_level_operator_slots():
    """PRTS基础: 低等级设施进驻人员上限应按等级, 优化器不能按满级塞站。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["manufacture"]["level"] = 2
    cfg["trading"]["level"] = 2
    cfg["control"]["level"] = 3
    cfg["meeting"]["level"] = 2
    cfg["hire"]["level"] = 2
    prof = build_profiles(
        [
            Operator("能天使", "能天使", 6, 90, 2),
            Operator("德克萨斯", "德克萨斯", 5, 90, 2),
            Operator("食铁兽", "食铁兽", 5, 90, 2),
            Operator("夜莺", "夜莺", 6, 90, 2),
            Operator("空弦", "空弦", 5, 90, 2),
            Operator("可颂", "可颂", 5, 90, 2),
            Operator("阿米娅", "阿米娅", 5, 80, 2),
            Operator("凯尔希", "凯尔希", 6, 90, 2),
            Operator("银灰", "银灰", 6, 90, 2),
            Operator("诗怀雅", "诗怀雅", 5, 80, 2),
        ],
        _DB,
    )
    opt = Optimizer(cfg, prof, Schedule([8, 22]))
    asg = opt._assign(1, 1, 1)

    assert opt._facility_slots("manufacture") == 2
    assert opt._facility_slots("trading") == 2
    assert opt._facility_slots("control") == 3
    assert opt._facility_slots("meeting") == 2
    assert opt._facility_slots("hire") == 1
    assert all(len(ops) <= 2 for _line, ops in asg.manufacture)
    assert all(len(ops) <= 2 for ops in asg.trading)
    assert len(asg.control) <= 3
    assert len(asg.meeting) <= 2
    assert len(asg.hire) <= 1


def test_optimizer_uses_configured_dormitory_room_count():
    """PRTS基础: 优化器填宿舍也应遵守已建成宿舍房间数。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["layout"]["fixed_rooms"]["dormitory"] = 2
    prof = build_profiles(
        [
            Operator("夜莺", "夜莺", 6, 90, 2),
            Operator("闪灵", "闪灵", 6, 90, 2),
            Operator("能天使", "能天使", 6, 90, 2),
            Operator("德克萨斯", "德克萨斯", 5, 90, 2),
            Operator("食铁兽", "食铁兽", 5, 90, 2),
        ],
        _DB,
    )
    opt = Optimizer(cfg, prof, Schedule([8, 22]))
    asg = opt._assign(1, 0, 3)
    res = opt.eng.evaluate(asg)

    assert opt._dorm_room_limit() == 2
    assert len(asg.dormitory) == 2
    assert not any(
        v.get("type") == "room_count" and v.get("room") == "dormitory"
        for v in res.detail["facility_capacity_violations"]
    )


def test_optimizer_skips_unbuilt_fixed_facilities():
    """固定设施未建成时, 优化器不应自动安排对应干员。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["layout"]["fixed_rooms"].update({"control": 0, "meeting": 0, "hire": 0, "dormitory": 0})
    prof = build_profiles(
        [
            Operator("阿米娅", "阿米娅", 5, 80, 2),
            Operator("凯尔希", "凯尔希", 6, 90, 2),
            Operator("银灰", "银灰", 6, 90, 2),
            Operator("寻澜", "寻澜", 5, 80, 2),
            Operator("杰西卡", "杰西卡", 4, 60, 1),
            Operator("锡人", "锡人", 5, 80, 2),
            Operator("夜莺", "夜莺", 6, 90, 2),
            Operator("闪灵", "闪灵", 6, 90, 2),
        ],
        _DB,
    )
    opt = Optimizer(cfg, prof, Schedule([8, 22]))
    asg = opt._assign(1, 0, 1)
    res = opt.eng.evaluate(asg)

    assert asg.control == []
    assert asg.meeting == []
    assert asg.hire == []
    assert asg.dormitory == []
    assert not any(
        v.get("type") == "room_count" and v.get("room") in {"control", "meeting", "hire", "dormitory"}
        for v in res.detail["facility_capacity_violations"]
    )


def test_optimizer_rotation_generates_structured_production_shifts():
    """自动轮换: 各班次独立选最优(允许跨班复用), Assignment 结构和设施约束保持可评估。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["layout"]["production_slots"] = 3
    cfg["layout"]["min_power"] = 1
    cfg["layout"]["max_manufacture"] = 1
    cfg["layout"]["max_trading"] = 1
    cfg["layout"]["max_power"] = 1
    cfg["layout"]["fixed_rooms"].update({"meeting": 0, "hire": 0, "workshop": 0, "training": 0, "dormitory": 1})
    prof = build_profiles(
        [
            Operator("阿米娅", "阿米娅", 5, 80, 2),
            Operator("凯尔希", "凯尔希", 6, 90, 2),
            Operator("能天使", "能天使", 6, 90, 2),
            Operator("德克萨斯", "德克萨斯", 5, 90, 2),
            Operator("食铁兽", "食铁兽", 5, 90, 2),
            Operator("空弦", "空弦", 5, 90, 2),
            Operator("但书", "但书", 5, 80, 2),
            Operator("龙舌兰", "龙舌兰", 5, 80, 2),
            Operator("巫恋", "巫恋", 5, 80, 2),
        ],
        _DB,
    )
    opt = Optimizer(cfg, prof, Schedule([0, 12]))
    shifts, results, layout = opt.optimize_rotation(2, local_search=True)

    assert layout == (1, 1, 1)
    assert len(shifts) == 2
    assert len(results) == 2
    for asg, res in zip(shifts, results):
        assert all(isinstance(room, tuple) for room in asg.manufacture)
        assert not res.detail["facility_capacity_violations"]
        assert res.ap_per_day > 0


def test_config_value_changes_strategy():
    """龙门币估值大幅提高时, 最优布局应出现贸易站(龙门币流)。"""
    from arknights_base_simulation.roster import load_roster

    xlsx = str(Path(__file__).resolve().parent.parent / "data" / "干员练度表.xlsx")
    if not Path(xlsx).exists():
        return  # 没有练度表则跳过
    prof = build_profiles(load_roster(xlsx), _DB)
    cfg = copy.deepcopy(CONFIG)
    cfg["material_values_ap"]["龙门币"] = 0.010
    opt = Optimizer(cfg, prof, Schedule([8, 22]))
    _asg, _res, (m, t, p) = opt.optimize(local_search=False)
    assert t >= 1, "龙门币高估值下应建造贸易站"


def test_conditional_pair_gating():
    """纯条件技能未满足时不计(德克萨斯恩怨需拉普兰德同站); 满足时生效。"""
    prof = _prof("德克萨斯", "拉普兰德", "蕾缪安", "锏")
    eng = Engine(CONFIG, prof, Schedule([8, 22]))

    def teff(ops):
        r = eng.evaluate(Assignment(trading=[ops], power=[[], [], []]))
        return r.detail["trading"][0]["eff%"]
    without = teff(["德克萨斯", "蕾缪安"])
    with_lap = teff(["德克萨斯", "蕾缪安", "拉普兰德"])
    assert with_lap > without + 50, f"恩怨未在拉普兰德同站时激活: {without}->{with_lap}"


def test_conditional_extra_bonus_is_not_unconditional():
    """基础+条件额外技能: 条件未满足时只保留基础, 满足时再加额外。"""
    prof = _prof("蕾缪安", "能天使", "贝洛内", "伺夜", "寻澜", "杰西卡", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(trading=[["蕾缪安"]], power=[[], [], []]))
    assert eng._resolved["蕾缪安"].trade_eff == 20.0
    eng.evaluate(Assignment(trading=[["蕾缪安", "能天使"]], power=[[], [], []]))
    assert eng._resolved["蕾缪安"].trade_eff == 45.0
    with _override(eng, frac={"蕾缪安": 1.0, "能天使": 0.0}):
        eng.evaluate(Assignment(trading=[["蕾缪安", "能天使"]], power=[[], [], []]))
    assert eng._resolved["蕾缪安"].trade_eff == 20.0

    eng.evaluate(Assignment(trading=[["贝洛内"]], power=[[], [], []]))
    assert eng._resolved["贝洛内"].trade_eff == 30.0
    eng.evaluate(Assignment(trading=[["贝洛内"]], meeting=["伺夜"], power=[[], [], []]))
    assert eng._resolved["贝洛内"].trade_eff == 40.0
    with _override(eng, frac={"贝洛内": 1.0, "伺夜": 0.0}):
        eng.evaluate(Assignment(trading=[["贝洛内"]], meeting=["伺夜"], power=[[], [], []]))
    assert eng._resolved["贝洛内"].trade_eff == 30.0

    eng.evaluate(Assignment(meeting=["寻澜", "杰西卡"], power=[[], [], []]))
    assert eng._resolved["寻澜"].clue == 30.0
    eng.evaluate(Assignment(meeting=["寻澜"], power=[[], [], []]))
    assert eng._resolved["寻澜"].clue == 10.0


def test_facility_level_dynamic_skill_formulas():
    """设施等级公式: 宿舍/会客室/训练室每级应按当前满级配置计算。"""
    prof = _prof("维伊", "空弦", "锡人", "伺夜", "渡桥", "娜仁图亚", "菲莱", "瑰盐", "佩佩", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(manufacture=[("作战记录", ["维伊"])], power=[[], [], []]))
    assert eng._resolved["维伊"].prod["all"] == 30.0

    eng.evaluate(Assignment(trading=[["空弦"]], dormitory=[[], [], [], []], power=[[], [], []]))
    assert eng._resolved["空弦"].trade_eff == 40.0

    eng.evaluate(Assignment(hire=["锡人"], dormitory=[[], [], [], []], power=[[], [], []]))
    assert eng._resolved["锡人"].contact == 45.0

    eng.evaluate(Assignment(trading=[["伺夜"], ["渡桥"]], power=[[], [], []]))
    assert eng._resolved["伺夜"].trade_eff == 40.0
    assert eng._resolved["渡桥"].trade_eff == 30.0

    eng.evaluate(Assignment(manufacture=[("赤金", ["娜仁图亚"])], dormitory=[[], [], [], []], power=[[], [], []]))
    assert eng._resolved["娜仁图亚"].prod["gold"] == 20.0

    eng.evaluate(Assignment(power=[["菲莱"]], dormitory=[[], [], [], []]))
    assert eng._resolved["菲莱"].power == 20.0  # 灵河充能10 + 宿舍4*5*0.5

    cfg = copy.deepcopy(CONFIG)
    cfg["training"]["level"] = 2
    cfg["meeting"]["level"] = 2
    cfg["trading"]["level"] = 2
    cfg["power"]["drone_per_hour_base"] = 0.0
    lower = Engine(cfg, prof, Schedule([0, 6, 12, 18]))

    lower.evaluate(Assignment(manufacture=[("作战记录", ["维伊"])], power=[[], [], []]))
    assert lower._resolved["维伊"].prod["all"] == 20.0

    lower.evaluate(Assignment(trading=[["伺夜"], ["渡桥"]], power=[[], [], []]))
    assert lower._resolved["伺夜"].trade_eff == 35.0
    assert lower._resolved["渡桥"].trade_eff == 25.0

    res = lower.evaluate(Assignment(trading=[["瑰盐"], ["佩佩"]], power=[[], [], []]))
    assert lower._resolved["瑰盐"].order_limit == 2.0
    assert lower._resolved["佩佩"].order_limit == 2.0
    assert res.detail["trading"][0]["order_limit"] == 10.0
    assert res.detail["trading"][1]["order_limit"] == 10.0


def test_dormitory_base_recovery_can_use_per_room_level_and_ambiance():
    """PRTS基础: 宿舍是逐间升级/布置氛围, 恢复值不能只取第1间宿舍。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["dormitory"]["level"] = [1, 5]
    cfg["dormitory"]["ambiance"] = [0, 5000]
    assert dorm_base_recover_for_room(cfg, 0) == 1.6
    assert dorm_base_recover_for_room(cfg, 1) == 4.0
    assert dorm_base_recover(cfg) == 1.6

    cfg["mood"]["base_drain_per_hour"] = 4.0
    cfg["power"]["drone_per_hour_base"] = 0.0
    prof = _prof("能天使", "夜莺", "闪灵")
    res = Engine(cfg, prof, Schedule([0])).evaluate(
        Assignment(manufacture=[("作战记录", ["能天使"])], dormitory=[["夜莺"], ["闪灵"]], power=[[], [], []])
    )
    assert not any("宿舍恢复" in w for w in res.warnings)


def test_meeting_dorm_ambience_bonus_sums_per_room_capped_ambience():
    """PRTS基础: 会客室宿舍氛围加成按各宿舍有效氛围总和判断。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["dormitory"]["level"] = [1, 2, 3]
    cfg["dormitory"]["ambiance"] = [10000, 1000, 3000]
    cfg["mood"]["base_drain_per_hour"] = 0.0
    prof = _prof("能天使")
    res = Engine(cfg, prof, Schedule([0])).evaluate(
        Assignment(meeting=["能天使"], dormitory=[[], [], []], power=[[], [], []])
    )
    assert res.detail["meeting"][0]["clue%"] == 47.0  # 氛围15 + 会客室等级11 + 非涣散5 + 精二16


def test_resource_pool_chain():
    """迷迭香: 宿舍人数->感知信息->思维链环->生产力(意识实体 每1点+1%)。"""
    prof = _prof("迷迭香", "夜莺", "闪灵", "安哲拉", "斯卡蒂", "幽灵鲨")
    eng = Engine(CONFIG, prof, Schedule([8, 22]))
    a = Assignment(manufacture=[("作战记录", ["迷迭香"])],
                   dormitory=[["夜莺", "闪灵", "安哲拉", "斯卡蒂", "幽灵鲨"]],
                   power=[[], [], []])
    prod = eng.evaluate(a).detail["manufacture"][0]["prod%"]
    assert prod >= 5, f"思维链环未按宿舍人数缩放: {prod}"
    assert eng._ctx.pools["感知信息"] == 5


def test_manufacture_zeroes_other_productivity_only():
    """冬时/自动化: 清同站其他干员生产力, 但保留基础产能和自身数量缩放。"""
    prof = _prof("冬时", "森蚺", "食铁兽", "Castle-3", "阿兰娜", "Lancet-2")
    eng = Engine(CONFIG, prof, Schedule([8, 22]))

    winter = eng.evaluate(Assignment(
        manufacture=[("作战记录", ["冬时", "食铁兽", "Castle-3"])],
        power=[[], [], []],
    ))
    assert winter.detail["manufacture"][0]["prod%"] == 33.0
    with _override(eng, frac={"冬时": 0.0, "食铁兽": 1.0, "Castle-3": 1.0}):
        inactive_winter = eng.evaluate(Assignment(
            manufacture=[("作战记录", ["冬时", "食铁兽", "Castle-3"])],
            power=[[], [], []],
        ))
    assert inactive_winter.detail["manufacture"][0]["prod%"] == 67.0

    eunectes = eng.evaluate(Assignment(
        manufacture=[("作战记录", ["森蚺", "食铁兽", "Castle-3"])],
        power=[[], [], []],
    ))
    assert eunectes.detail["manufacture"][0]["prod%"] == 33.0

    winter_arene = eng.evaluate(Assignment(
        manufacture=[("赤金", ["冬时", "阿兰娜", "食铁兽"])],
        power=[["Lancet-2"], [], []],
    ))
    assert winter_arene.detail["manufacture"][0]["prod%"] == 43.0


def test_eunectes_control_adds_virtual_power_station_count():
    """森蚺控制技能: Lancet-2 在发电站时, 发电站数量额外+2, 影响自动化类技能。"""
    prof = _prof("森蚺", "Lancet-2", "温蒂")
    eng = Engine(CONFIG, prof, Schedule([8, 22]))
    res = eng.evaluate(Assignment(
        control=["森蚺"],
        manufacture=[("作战记录", ["温蒂"])],
        power=[["Lancet-2"], [], []],
    ))
    assert res.detail["manufacture"][0]["prod%"] == 76.0


def test_zero_mood_operator_is_not_active_for_base_skills():
    """PRTS基建基础: 心情为0的注意力涣散干员不视为工作状态, 后勤技能失效。"""
    prof = _prof("阿米娅", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(control=["阿米娅"], power=[[], [], []])

    eng.evaluate(asg)
    assert eng._resolved["阿米娅"].trade_eff_global == 7.0

    with _override(eng, mood={"阿米娅": 0.0}):
        eng.evaluate(asg)
    assert eng._resolved["阿米娅"].trade_eff_global == 0.0
    assert eng._ctx.active("阿米娅") is False


def test_greyy_lightningbearer_dawn_adds_virtual_power_station_count():
    """承曦格雷伊·晨曦: 其他发电站无作业平台时发电站额外+1, 仅影响设施数量。"""
    prof = _prof("承曦格雷伊", "温蒂", "Lancet-2", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([8, 22]))

    dawn = eng.evaluate(Assignment(
        manufacture=[("作战记录", ["温蒂"])],
        power=[["承曦格雷伊"], []],
    ))
    assert dawn.detail["manufacture"][0]["prod%"] == 46.0  # 基础1 + (2真实+1晨曦)*15

    blocked = eng.evaluate(Assignment(
        manufacture=[("作战记录", ["温蒂"])],
        power=[["承曦格雷伊"], ["Lancet-2"]],
    ))
    assert blocked.detail["manufacture"][0]["prod%"] == 31.0  # 其他发电站存在作业平台, 不触发晨曦


def test_waai_fu_coordination_is_dynamic_not_static():
    """槐琥 配合意识: 同站其他干员普通生产力每5%转5%, 上限40%。"""
    prof = _prof("槐琥", "食铁兽", "Castle-3")
    eng = Engine(CONFIG, prof, Schedule([8, 22]))
    res = eng.evaluate(Assignment(
        manufacture=[("作战记录", ["槐琥", "食铁兽", "Castle-3"])],
        power=[[], [], []],
    ))
    assert res.detail["manufacture"][0]["prod%"] == 108.0
    with _override(eng, frac={"槐琥": 1.0, "食铁兽": 0.0, "Castle-3": 1.0}):
        inactive_other = eng.evaluate(Assignment(
            manufacture=[("作战记录", ["槐琥", "食铁兽", "Castle-3"])],
            power=[[], [], []],
        ))
    assert inactive_other.detail["manufacture"][0]["prod%"] == 62.0


def test_waai_fu_team_spirit_cancels_manufacture_self_mood_drain():
    """槐琥 团队精神: 消除当前制造站内所有干员自身心情消耗影响。"""
    prof = _prof("槐琥", "雪猎", "泡普卡", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(manufacture=[("作战记录", ["雪猎"])], power=[[], [], []]))
    assert eng._resolved["雪猎"].drain_delta == 0.25

    eng.evaluate(Assignment(manufacture=[("作战记录", ["槐琥", "雪猎", "泡普卡"])], power=[[], [], []]))
    assert eng._resolved["槐琥"].drain_delta == 0.0
    assert eng._resolved["雪猎"].drain_delta == 0.0
    assert eng._resolved["泡普卡"].drain_delta == 0.0


def test_gladiia_abyssal_hunter_control_bonus_and_zero_priority():
    """歌蕾蒂娅 集群狩猎: 深海制造站加成；自动化/仿生海龙清零优先且不叠加。"""
    prof = _prof("歌蕾蒂娅", "安哲拉", "斯卡蒂", "温蒂", "夜莺")
    eng = Engine(CONFIG, prof, Schedule([8, 22]))

    with _override(eng, frac={"歌蕾蒂娅": 1.0, "安哲拉": 1.0, "斯卡蒂": 1.0}):
        abyssal = eng.evaluate(Assignment(
            control=["歌蕾蒂娅"],
            manufacture=[("作战记录", ["安哲拉"]), ("作战记录", ["斯卡蒂"])],
            power=[[], [], []],
        ))
    assert abyssal.detail["manufacture"][0]["prod%"] == 21.0
    assert abyssal.detail["manufacture"][1]["prod%"] == 21.0
    with _override(eng, frac={"歌蕾蒂娅": 1.0, "安哲拉": 1.0, "斯卡蒂": 0.0}):
        one_active_hunter = eng.evaluate(Assignment(
            control=["歌蕾蒂娅"],
            manufacture=[("作战记录", ["安哲拉"]), ("作战记录", ["斯卡蒂"])],
            power=[[], [], []],
        ))
    assert one_active_hunter.detail["manufacture"][0]["prod%"] == 11.0
    assert one_active_hunter.detail["manufacture"][1]["prod%"] == 0.0
    with _override(eng, frac={"歌蕾蒂娅": 1.0, "安哲拉": 1.0, "斯卡蒂": 0.0, "夜莺": 1.0}):
        inactive_hunter_room = eng.evaluate(Assignment(
            control=["歌蕾蒂娅"],
            manufacture=[("作战记录", ["安哲拉"]), ("作战记录", ["夜莺", "斯卡蒂"])],
            power=[[], [], []],
    ))
    assert inactive_hunter_room.detail["manufacture"][0]["prod%"] == 11.0
    assert inactive_hunter_room.detail["manufacture"][1]["prod%"] == 1.0

    with _override(eng, frac={"歌蕾蒂娅": 1.0, "温蒂": 1.0, "安哲拉": 1.0}):
        zero_priority = eng.evaluate(Assignment(
            control=["歌蕾蒂娅"],
            manufacture=[("作战记录", ["温蒂", "安哲拉"])],
            power=[[], [], []],
        ))
    assert zero_priority.detail["manufacture"][0]["prod%"] == 47.0


def test_gladiia_abyssal_hunter_alpha_control_bonus():
    """歌蕾蒂娅 集群狩猎·α: 每名制造站深海猎人提供5%制造站加成。"""
    prof = _prof("歌蕾蒂娅", "安哲拉", "斯卡蒂", elite=0, level=1)
    eng = Engine(CONFIG, prof, Schedule([0]))

    with _override(eng, frac={"歌蕾蒂娅": 1.0, "安哲拉": 1.0, "斯卡蒂": 1.0}):
        res = eng.evaluate(Assignment(
            control=["歌蕾蒂娅"],
            manufacture=[("作战记录", ["安哲拉"]), ("作战记录", ["斯卡蒂"])],
            power=[[], [], []],
        ))

    assert res.detail["manufacture"][0]["prod%"] == 11.0
    assert res.detail["manufacture"][1]["prod%"] == 11.0


def test_manufacture_skill_class_counting():
    """制造站技能类别计数: 莱茵/标准化/金属工艺技能按当前站内已解锁技能数动态生效。"""
    prof = _prof(
        "多萝西", "白面鸮", "水月", "香草", "罗比菈塔", "海沫",
        "苍苔", "砾", "溯光星源", "历阵锐枪芬", "克洛丝", "米格鲁",
    )
    eng = Engine(CONFIG, prof, Schedule([8, 22]))

    eng.evaluate(Assignment(manufacture=[("作战记录", ["多萝西", "白面鸮"])], power=[[], [], []]))
    assert eng._resolved["多萝西"].prod["all"] == 35.0  # 莱茵科技25 + 当前站2个莱茵科技技能*5

    eng.evaluate(Assignment(manufacture=[("作战记录", ["水月", "香草", "罗比菈塔"])], power=[[], [], []]))
    assert eng._resolved["水月"].prod["all"] == 40.0  # 标准化25 + 当前站3个标准化技能*5

    eng.evaluate(Assignment(manufacture=[("作战记录", ["水月", "海沫", "白面鸮"])], power=[[], [], []]))
    assert eng._resolved["水月"].prod["all"] == 40.0  # 海沫使莱茵科技技能也视作标准化类

    eng.evaluate(Assignment(manufacture=[("赤金", ["苍苔", "砾"])], power=[[], [], []]))
    assert eng._resolved["苍苔"].prod["gold"] == 30.0
    assert eng._resolved["苍苔"].prod["all"] == 10.0  # 当前站2个金属工艺技能*5

    eng.evaluate(Assignment(manufacture=[("作战记录", ["溯光星源", "多萝西", "白面鸮"])], power=[[], [], []]))
    assert eng._resolved["溯光星源"].capacity == 15.0

    eng.evaluate(Assignment(manufacture=[("作战记录", ["历阵锐枪芬", "克洛丝", "米格鲁"])], power=[[], [], []]))
    assert eng._resolved["历阵锐枪芬"].prod["all"] == 35.0  # 标准化15 + 同站2名A1小队干员*10


def test_manufacture_trading_station_count_productivity():
    """清流/引星棘刺: 贵金属生产力按贸易站数量动态缩放。"""
    prof = _prof("清流", "引星棘刺", "能天使", "德克萨斯")
    eng = Engine(CONFIG, prof, Schedule([8, 22]))

    eng.evaluate(Assignment(manufacture=[("赤金", ["清流"])], power=[[], [], []]))
    assert eng._resolved["清流"].prod["gold"] == 0.0

    eng.evaluate(Assignment(
        manufacture=[("赤金", ["清流"])],
        trading=[["能天使"], ["德克萨斯"]],
        power=[[], [], []],
    ))
    assert eng._resolved["清流"].prod["gold"] == 40.0

    eng.evaluate(Assignment(
        manufacture=[("赤金", ["引星棘刺"])],
        trading=[["能天使"], ["德克萨斯"]],
        power=[[], [], []],
    ))
    assert eng._resolved["引星棘刺"].prod["gold"] == 36.0


def test_manufacture_room_presence_condition_is_gated():
    """烈夏 患难拍档: 只有古米在贸易站时, 作战记录生产力+35%。"""
    prof = _prof("烈夏", "古米", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(manufacture=[("作战记录", ["烈夏"])], power=[[], [], []]))
    assert eng._resolved["烈夏"].prod == {}
    eng.evaluate(Assignment(manufacture=[("作战记录", ["烈夏"])], dormitory=[["古米"]], power=[[], [], []]))
    assert eng._resolved["烈夏"].prod == {}
    eng.evaluate(Assignment(manufacture=[("作战记录", ["烈夏"])], trading=[["古米"]], power=[[], [], []]))
    assert eng._resolved["烈夏"].prod["record"] == 35.0


def test_manufacture_capacity_to_productivity_rules():
    """红云/泡泡: 站内干员自身提升的仓库容量换生产力; 泡泡优先且不与红云叠加。"""
    prof = _prof("红云", "泡泡", "火神")
    eng = Engine(CONFIG, prof, Schedule([8, 22]))

    vermeil = eng.evaluate(Assignment(manufacture=[("作战记录", ["红云", "火神"])], power=[[], [], []]))
    assert eng._resolved["红云"].prod == {}
    assert vermeil.detail["manufacture"][0]["prod%"] == 51.0  # base2 + 火神-5 + (8+19)*2
    with _override(eng, frac={"红云": 0.0, "火神": 1.0}):
        inactive_vermeil = eng.evaluate(Assignment(manufacture=[("作战记录", ["红云", "火神"])], power=[[], [], []]))
    assert inactive_vermeil.detail["manufacture"][0]["prod%"] == -4.0

    bubble = eng.evaluate(Assignment(manufacture=[("作战记录", ["泡泡", "红云", "火神"])], power=[[], [], []]))
    assert eng._resolved["泡泡"].prod == {}
    assert bubble.detail["manufacture"][0]["prod%"] == 73.0  # base3 + 火神-5 + 10 + 8 + 19*3


def test_totter_mood_gap_manufacture_skills_are_dynamic():
    """铅踝: 模糊视线按心情落差扣生产力, 窗外雪啸只在落差>12时触发。"""
    prof = _prof("铅踝", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(manufacture=[("作战记录", ["铅踝"])], power=[[], [], []])

    eng.evaluate(asg)
    assert eng._resolved["铅踝"].prod["all"] == 30.0
    assert eng._resolved["铅踝"].capacity == 0.0

    with _override(eng, mood={"铅踝": 12.0}):
        eng.evaluate(asg)
    assert eng._resolved["铅踝"].prod["all"] == 15.0
    assert eng._resolved["铅踝"].capacity == 0.0

    with _override(eng, mood={"铅踝": 11.9}):
        eng.evaluate(asg)
    assert eng._resolved["铅踝"].prod["all"] == 25.0
    assert eng._resolved["铅踝"].capacity == 6.0


def test_stainless_workshop_counts_low_mood_dorm_operators():
    """白铁: 宿舍内每名心情12以下干员为加工站副产品概率+5%。"""
    prof = _prof("白铁", "夜莺", "临光", "阿米娅", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(workshop=["白铁"], dormitory=[["夜莺", "临光"], ["阿米娅"]], power=[[], [], []])

    eng.evaluate(asg)
    assert eng._resolved["白铁"].byproduct == 0.0

    with _override(eng, mood={"夜莺": 24.0, "临光": 24.0, "阿米娅": 24.0}):
        eng.evaluate(asg)
    assert eng._resolved["白铁"].byproduct == 0.0

    with _override(eng, mood={"夜莺": 11.9, "临光": 12.0, "阿米娅": 12.1}):
        eng.evaluate(asg)
    assert eng._resolved["白铁"].byproduct == 10.0

    cfg = copy.deepcopy(CONFIG)
    cfg["workshop"]["craft_category"] = "skill"
    cfg["workshop"]["base_byproduct_chance"] = 0.0
    cfg["workshop"]["crafts_per_day"] = 1.0
    cfg["workshop"]["ap_per_byproduct"] = 1.0
    skill_eng = Engine(cfg, prof, Schedule([0, 6, 12, 18]))
    with _override(skill_eng, mood={"夜莺": 11.9, "临光": 12.0, "阿米娅": 12.1}):
        skill = skill_eng.evaluate(asg)
    assert skill.detail["workshop"][0]["byproduct%"] == 0.0


def test_fontaine_workshop_counts_low_mood_dorm_operators():
    """芳汀: 宿舍低心情人数副产品技能按阈值和加工类别生效。"""
    names = ["芳汀", "夜莺", "临光", "阿米娅"]
    prof = _prof(*names, rarity=5)
    cfg = copy.deepcopy(CONFIG)
    cfg["workshop"]["craft_category"] = "elite"
    cfg["workshop"]["base_byproduct_chance"] = 0.0
    cfg["workshop"]["crafts_per_day"] = 1.0
    cfg["workshop"]["ap_per_byproduct"] = 1.0
    eng = Engine(cfg, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(workshop=["芳汀"], dormitory=[["夜莺", "临光", "阿米娅"]], power=[[], [], []])

    with _override(eng, mood={"夜莺": 19.9, "临光": 4.0, "阿米娅": 20.1}):
        elite = eng.evaluate(asg)
    assert elite.detail["workshop"][0]["byproduct%"] == 10.0

    cfg["workshop"]["craft_category"] = "skill"
    skill_eng = Engine(cfg, prof, Schedule([0, 6, 12, 18]))
    with _override(skill_eng, mood={"夜莺": 19.9, "临光": 4.0, "阿米娅": 20.1}):
        skill = skill_eng.evaluate(asg)
    assert skill.detail["workshop"][0]["byproduct%"] == 0.0

    low_phase_prof = _prof(*names, elite=0, level=1, rarity=5)
    cfg["workshop"]["craft_category"] = "elite"
    low_phase_eng = Engine(cfg, low_phase_prof, Schedule([0, 6, 12, 18]))
    with _override(low_phase_eng, mood={"夜莺": 3.9, "临光": 4.0, "阿米娅": 4.1}):
        low_phase = low_phase_eng.evaluate(asg)
    assert low_phase.detail["workshop"][0]["byproduct%"] == 10.0


def test_catherine_workshop_bonus_requires_stainless_in_dormitory():
    """凯瑟琳工效模范β: 白铁进驻宿舍时任意类材料副产品额外+10%。"""
    prof = _prof("凯瑟琳", "白铁", rarity=5)
    cfg = copy.deepcopy(CONFIG)
    cfg["workshop"]["craft_category"] = "skill"
    cfg["workshop"]["base_byproduct_chance"] = 0.0
    cfg["workshop"]["crafts_per_day"] = 1.0
    cfg["workshop"]["ap_per_byproduct"] = 1.0
    eng = Engine(cfg, prof, Schedule([0, 6, 12, 18]))

    solo = eng.evaluate(Assignment(workshop=["凯瑟琳"], power=[[], [], []]))
    assert solo.detail["workshop"][0]["byproduct%"] == 50.0

    with_stainless = eng.evaluate(Assignment(workshop=["凯瑟琳"], dormitory=[["白铁"]], power=[[], [], []]))
    assert with_stainless.detail["workshop"][0]["byproduct%"] == 60.0

    with _override(eng, frac={"凯瑟琳": 1.0, "白铁": 0.0}):
        inactive_stainless = eng.evaluate(
            Assignment(workshop=["凯瑟琳"], dormitory=[["白铁"]], power=[[], [], []])
        )
    assert inactive_stainless.detail["workshop"][0]["byproduct%"] == 50.0


def test_perfumer_alter_dorm_recovery_only_for_low_mood_room_members():
    """撷英调香师: 净化呼吸只在本宿舍存在心情20以下干员时提供恢复额外项。"""
    prof = _prof("撷英调香师", "夜莺", "临光", rarity=5)
    assert prof["撷英调香师"].stat("dormitory").dorm_recover_all == 0.15

    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(dormitory=[["撷英调香师", "夜莺"], ["临光"]], power=[[], [], []])

    eng.evaluate(asg)
    assert eng._resolved["撷英调香师"].dorm_recover_all == 0.15

    with _override(eng, mood={"夜莺": 20.0, "临光": 10.0}):
        eng.evaluate(asg)
    assert eng._resolved["撷英调香师"].dorm_recover_all == 0.25

    with _override(eng, mood={"夜莺": 20.1, "临光": 10.0}):
        eng.evaluate(asg)
    assert eng._resolved["撷英调香师"].dorm_recover_all == 0.15


def test_dorm_low_mood_recovery_extra_in_same_skill_keeps_base_recovery():
    """波卜/刺玫: 同一技能内的基础宿舍恢复和低心情额外项都应保留。"""
    prof = _prof("波卜", "刺玫", "夜莺", "临光", "阿米娅", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    popu_asg = Assignment(dormitory=[["波卜", "夜莺", "阿米娅"], ["临光"]], power=[[], [], []])
    eng.evaluate(popu_asg)
    assert abs(eng._resolved["波卜"].dorm_recover_all - 0.20) < 1e-9

    with _override(eng, mood={"夜莺": 23.0, "阿米娅": 24.0, "临光": 10.0}):
        eng.evaluate(popu_asg)
    assert abs(eng._resolved["波卜"].dorm_recover_all - 0.21) < 1e-9

    rosemary_asg = Assignment(dormitory=[["刺玫", "夜莺"], ["临光"]], power=[[], [], []])
    with _override(eng, mood={"夜莺": 18.1, "临光": 10.0}):
        eng.evaluate(rosemary_asg)
    assert abs(eng._resolved["刺玫"].dorm_recover_all - 0.15) < 1e-9

    with _override(eng, mood={"夜莺": 18.0, "临光": 24.0}):
        eng.evaluate(rosemary_asg)
    assert abs(eng._resolved["刺玫"].dorm_recover_all - 0.25) < 1e-9


def test_dorm_target_extra_recovery_affects_sustainability():
    """新约能天使: 同宿舍存在拉特兰目标时, 单体恢复应从0.55提高到1.0。"""
    prof = _prof(
        "新约能天使", "CONFESS-47", "德克萨斯", "能天使",
        "摩根", "推进之王", "达格达", "夜莺",
        rarity=5,
    )
    exu_buff = prof["新约能天使"].room_buffs["dormitory"][0]
    assert dorm_target_extra_recover(exu_buff, ["新约能天使", "德克萨斯"]) == 0.0
    assert dorm_target_extra_recover(exu_buff, ["新约能天使", "CONFESS-47"]) == 0.45
    assert dorm_target_extra_recover_for(exu_buff, "德克萨斯", ["新约能天使", "CONFESS-47", "德克萨斯"]) == 0.0
    assert dorm_target_extra_recover_for(exu_buff, "CONFESS-47", ["新约能天使", "CONFESS-47", "德克萨斯"]) == 0.45
    morgan_buff = prof["摩根"].room_buffs["dormitory"][0]
    assert dorm_target_extra_recover(morgan_buff, ["摩根", "达格达"]) == 0.0
    assert dorm_target_extra_recover(morgan_buff, ["摩根", "推进之王", "夜莺"]) == 0.3
    assert dorm_target_extra_recover(morgan_buff, ["摩根", "推进之王", "达格达"]) == 0.3
    assert dorm_target_extra_recover_for(morgan_buff, "夜莺", ["摩根", "推进之王", "夜莺"]) == 0.0
    assert dorm_target_extra_recover_for(morgan_buff, "推进之王", ["摩根", "推进之王", "达格达"]) == 0.3

    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 0.0
    cfg["dormitory"]["level"] = None
    cfg["dormitory"]["base_recover_per_hour"] = 0.0
    cfg["mood"]["base_drain_per_hour"] = 0.9
    eng = Engine(cfg, prof, Schedule([0, 6, 12, 18]))
    base_asg = Assignment(
        manufacture=[("作战记录", ["能天使"])],
        dormitory=[["新约能天使", "德克萨斯"]],
        power=[[], [], []],
    )
    target_asg = Assignment(
        manufacture=[("作战记录", ["能天使"])],
        dormitory=[["新约能天使", "CONFESS-47"]],
        power=[[], [], []],
    )

    base = eng.evaluate(base_asg)
    target = eng.evaluate(target_asg)

    assert base.warnings
    assert target.warnings == []
    assert target.ap_per_day > base.ap_per_day


def test_perception_pool_is_shared_between_rosmontis_and_ebenholz():
    """迷迭香/黑键各自产生的感知信息应进共享池, 再分别转思维链环/无声共鸣。"""
    prof = _prof("迷迭香", "黑键", "夜莺", "闪灵", "安哲拉")
    eng = Engine(CONFIG, prof, Schedule([8, 22]))
    asg = Assignment(
        manufacture=[("作战记录", ["迷迭香"])],
        trading=[["黑键"]],
        dormitory=[["夜莺", "闪灵", "安哲拉"]],
        power=[[], [], []],
    )
    eng.evaluate(asg)
    assert eng._ctx.pools["感知信息"] == 6
    assert eng._ctx.pools["思维链环"] == 6
    assert eng._ctx.pools["无声共鸣"] == 6


def test_dorm_level_intermediate_products_follow_configured_room_level():
    """爱丽丝/车尔尼/森西的当前宿舍每级资源应按配置宿舍等级, 不是固定满级5。"""
    prof = _prof("爱丽丝", "车尔尼", "森西", rarity=5)
    cfg = copy.deepcopy(CONFIG)
    cfg["dormitory"]["level"] = 3
    eng = Engine(cfg, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(dormitory=[["爱丽丝"], ["车尔尼"], ["森西"]], power=[[], [], []]))

    assert eng._ctx.pools["梦境"] == 3
    assert eng._ctx.pools["小节"] == 3
    assert eng._ctx.pools["魔物料理"] == 3
    assert eng._ctx.pools["感知信息"] == 6


def test_special_trading_ops():
    """但书违约单(2/3赤金单) / 龙舌兰投资(4赤金单) / 巫恋低语(清零他人+每人45%)。
    但书和龙舌兰作用于不同订单类型, 可同时生效。"""
    prof = _prof("但书", "龙舌兰", "巫恋", "空弦", "能天使", "德克萨斯", "火哨")
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 0.0
    eng = Engine(cfg, prof, Schedule([8, 22]))

    def lmd(trio):
        a = Assignment(manufacture=[("赤金", ["能天使"]), ("赤金", ["德克萨斯"])],
                       trading=[trio], power=[[], [], []])
        return eng.evaluate(a).detail["trading"][0]["龙门币/day"]

    base = lmd(["空弦"])
    dorothy = lmd(["但书"])
    tequila = lmd(["龙舌兰"])
    combo = lmd(["但书", "龙舌兰"])
    assert dorothy > base * 1.4, f"但书违约单未提升吞吐: {base}->{dorothy}"
    assert tequila > base, f"龙舌兰投资未生效: {base}->{tequila}"
    assert combo > dorothy, f"但书+龙舌兰应同时生效(不同订单类型): 但书{dorothy} 合{combo}"
    eff = eng.evaluate(Assignment(trading=[["巫恋", "空弦", "德克萨斯"]],
                                  power=[[], [], []])).detail["trading"][0]["eff%"]
    assert eff == 93.0, f"巫恋低语应保留贸易站基础效率: eff={eff}"
    assert eng._resolved["巫恋"].drain_delta == -0.25  # 裁缝·α自身减耗; 低语+0.25应是全站增耗
    assert eng._resolved["巫恋"].room_drain_delta == 0.25
    red = eng._room_reduction(3) + eng._room_drain_reduction("trading", ["巫恋", "空弦", "德克萨斯"], 0.0)
    assert abs(red - (-0.15)) < 1e-9
    assert abs(eng._net_drain("空弦", "trading", red) - 1.15) < 1e-9
    with _override(eng, frac={"巫恋": 0.0, "能天使": 1.0}):
        inactive_shamare = eng.evaluate(
            Assignment(trading=[["巫恋", "能天使"]], power=[[], [], []])
        ).detail["trading"][0]["eff%"]
    assert inactive_shamare == 36.0
    assert eng._resolved["巫恋"].drain_delta == 0.0
    assert eng._resolved["巫恋"].room_drain_delta == 0.0
    eng.evaluate(Assignment(trading=[["火哨", "能天使"]], power=[[], [], []]))
    assert eng._resolved["火哨"].drain_delta == 0.0
    assert eng._resolved["火哨"].room_drain == 0.1
    red = eng._room_drain_reduction("trading", ["火哨", "能天使"], 0.0)
    assert abs(eng._net_drain("能天使", "trading", red) - 0.9) < 1e-9


def test_shu_manufacture_room_mood_drain_reduction_applies_to_roommates():
    """黍 春雷响，万物长: 当前制造站内所有干员心情消耗-0.1, 不是只给自身。"""
    prof = _prof("黍", "能天使", rarity=6)
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 0.0
    eng = Engine(cfg, prof, Schedule([0]))
    eng.evaluate(Assignment(manufacture=[("赤金", ["黍", "能天使"])], power=[[], [], []]))

    assert eng._resolved["黍"].room_drain == 0.1
    red = eng._room_drain_reduction("manufacture", ["黍", "能天使"], 0.0)
    assert abs(red - 0.1) < 1e-9
    assert abs(eng._net_drain("能天使", "manufacture", red) - 0.9) < 1e-9


def test_prts_level3_trade_order_distribution_expectation():
    """3级贸易站普通赤金订单按 PRTS 2/3/4赤金单概率取长期期望, 不是固定4赤金单。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 0.0
    cfg["trading"]["order_limit"] = 100
    cfg["manufacture"]["capacity_volume"] = 10000
    cfg["manufacture"]["lines"]["赤金"]["base_minutes_per_item"] = 1.0
    cfg["mood"]["base_drain_per_hour"] = 0.0
    prof = _prof("能天使", "夜莺", "龙舌兰", rarity=6)
    eng = Engine(cfg, prof, Schedule([0]))

    go = cfg["trading"]["gold_order"]
    assert go["base_minutes_per_order"] == 203.4
    assert go["gold_per_order"] == 2.9
    assert go["lmd_per_order"] == 1450
    assert go["native_4_gold_probability"] == 0.2

    base = eng.evaluate(Assignment(
        manufacture=[("赤金", ["能天使"])],
        trading=[["夜莺"]],
        power=[[], [], []],
    ))
    expected_orders = 24 * 60 / 203.4 * 1.01  # one non-fatigued trading operator gives +1%
    expected_lmd = expected_orders * 1450
    assert abs(base.detail["trading"][0]["龙门币/day"] - round(expected_lmd, 0)) < 1e-9

    tequila = eng.evaluate(Assignment(
        manufacture=[("赤金", ["能天使"])],
        trading=[["龙舌兰"]],
        power=[[], [], []],
    ))
    expected_tequila_lmd = expected_orders * (1450 + 500 * 0.2)
    assert abs(tequila.detail["trading"][0]["龙门币/day"] - round(expected_tequila_lmd, 0)) < 1e-9


def test_prts_lower_level_trade_order_distribution_expectation():
    """1/2级贸易站普通赤金订单应按对应等级的 PRTS 订单分布取期望。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 0.0
    cfg["trading"]["order_limit"] = 100
    cfg["manufacture"]["capacity_volume"] = 10000
    cfg["manufacture"]["lines"]["赤金"]["base_minutes_per_item"] = 1.0
    cfg["mood"]["base_drain_per_hour"] = 0.0
    prof = _prof("能天使", "夜莺", "龙舌兰", rarity=6)

    cfg["trading"]["level"] = 1
    lv1 = Engine(cfg, prof, Schedule([0]))
    lv1_res = lv1.evaluate(Assignment(
        manufacture=[("赤金", ["能天使"])],
        trading=[["夜莺"]],
        power=[[], [], []],
    ))
    expected_lv1_orders = 24 * 60 / 144.0 * 1.01
    assert abs(lv1_res.detail["trading"][0]["龙门币/day"] - round(expected_lv1_orders * 1000, 0)) < 1e-9
    assert lv1._trading_gold_order_profile()["native_4_gold_probability"] == 0.0

    cfg["trading"]["level"] = 2
    lv2 = Engine(cfg, prof, Schedule([0]))
    lv2_res = lv2.evaluate(Assignment(
        manufacture=[("赤金", ["能天使"])],
        trading=[["夜莺"]],
        power=[[], [], []],
    ))
    expected_lv2_orders = 24 * 60 / 170.4 * 1.01
    assert abs(lv2_res.detail["trading"][0]["龙门币/day"] - round(expected_lv2_orders * 1200, 0)) < 1e-9

    tequila_lv2 = lv2.evaluate(Assignment(
        manufacture=[("赤金", ["能天使"])],
        trading=[["龙舌兰"]],
        power=[[], [], []],
    ))
    assert "龙舌兰投资" in tequila_lv2.detail["trading"][0]["特殊"]
    assert abs(tequila_lv2.detail["trading"][0]["龙门币/day"] - round(expected_lv2_orders * 1200, 0)) < 1e-9


def test_prts_trade_orundum_strategy_consumes_source_shards():
    """PRTS 开采协力: Lv3贸易站可用 2源石碎片->20合成玉, 源石碎片作为中间产物不重复计收益。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 0.0
    cfg["trading"]["strategy"] = "orundum"
    cfg["trading"]["order_limit"] = 100
    cfg["manufacture"]["lines"] = {
        "源石碎片": {
            "output": "源石碎片",
            "units_per_item": 1,
            "base_minutes_per_item": 60,
            "volume_per_item": 1,
            "category": "originium",
            "min_level": 3,
        }
    }
    prof = _prof("夜莺", "闪灵", rarity=6)
    eng = Engine(cfg, prof, Schedule([0]))

    res = eng.evaluate(Assignment(
        manufacture=[("源石碎片", ["夜莺"])],
        trading=[["闪灵"]],
        dormitory=[[], [], [], []],
        power=[[], [], []],
    ))

    assert res.breakdown["制造站(非赤金)"] == 0.0
    assert res.detail["manufacture"][0]["items/day"] == 24.2
    assert res.detail["trading"][0]["strategy"] == "orundum"
    assert res.detail["trading"][0]["合成玉单/day"] == 12.1
    assert res.detail["trading"][0]["合成玉/day"] == 242.4
    assert abs(res.breakdown["贸易站(合成玉)"] - 242.4 * cfg["material_values_ap"]["合成玉"]) < 1e-9

    cfg["trading"]["level"] = 2
    locked = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(
        manufacture=[("源石碎片", ["夜莺"])],
        trading=[["闪灵"]],
        dormitory=[[], [], [], []],
        power=[[], [], []],
    ))
    assert "开采协力未解锁" in locked.detail["trading"][0]["特殊"]
    assert locked.detail["trading"][0]["合成玉/day"] == 0.0


def test_prts_high_quality_trade_order_distribution_expectation():
    """单个α/β提质技能按 PRTS 3级站订单概率期望改写普通赤金订单。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 0.0
    cfg["trading"]["order_limit"] = 100
    cfg["manufacture"]["capacity_volume"] = 10000
    cfg["manufacture"]["lines"]["赤金"]["base_minutes_per_item"] = 1.0
    cfg["mood"]["base_drain_per_hour"] = 0.0
    prof = _prof("能天使", "柏喙", "龙舌兰", rarity=5)
    eng = Engine(cfg, prof, Schedule([0]))

    beta = eng.evaluate(Assignment(
        manufacture=[("赤金", ["能天使"])],
        trading=[["柏喙"]],
        power=[[], [], []],
    )).detail["trading"][0]
    beta_ratio = (0.5 * 5.0 + 19.0) / 24.0
    beta_minutes = 203.4 + (262.8 - 203.4) * beta_ratio
    beta_lmd = 1450 + (1900 - 1450) * beta_ratio
    beta_p4 = 0.2 + (0.85 - 0.2) * beta_ratio
    expected_beta_orders = 24 * 60 / beta_minutes * 1.01
    assert "高品质订单beta@0.90" in beta["特殊"]
    assert beta["赤金单/day"] == round(expected_beta_orders, 1)
    assert beta["龙门币/day"] == round(expected_beta_orders * beta_lmd, 0)

    with _override(eng, frac={"柏喙": 0.5, "能天使": 1.0}):
        half_beta = eng.evaluate(Assignment(
            manufacture=[("赤金", ["能天使"])],
            trading=[["柏喙"]],
            power=[[], [], []],
        )).detail["trading"][0]
    half_beta_ratio = beta_ratio * 0.5
    half_beta_minutes = 203.4 + (262.8 - 203.4) * half_beta_ratio
    half_beta_lmd = 1450 + (1900 - 1450) * half_beta_ratio
    half_beta_orders = 24 * 60 / half_beta_minutes * 1.005
    assert "高品质订单beta@0.45" in half_beta["特殊"]
    assert half_beta["赤金单/day"] == round(half_beta_orders, 1)
    assert half_beta["龙门币/day"] == round(half_beta_orders * half_beta_lmd, 0)

    beta_tequila = eng.evaluate(Assignment(
        manufacture=[("赤金", ["能天使"])],
        trading=[["柏喙", "龙舌兰"]],
        power=[[], [], []],
    )).detail["trading"][0]
    expected_combo_orders = 24 * 60 / beta_minutes * 1.02
    expected_combo_lmd = expected_combo_orders * (beta_lmd + 500 * beta_p4)
    assert "高品质订单beta@0.90" in beta_tequila["特殊"]
    assert "龙舌兰投资" in beta_tequila["特殊"]
    assert beta_tequila["龙门币/day"] == round(expected_combo_lmd, 0)

    alpha_prof = {
        "能天使": OperatorProfile("能天使", 2, 90, _DB, rarity=6),
        "柏喙": OperatorProfile("柏喙", 0, 90, _DB, rarity=5),
        "卡夫卡": OperatorProfile("卡夫卡", 0, 90, _DB, rarity=5),
    }
    alpha_eng = Engine(cfg, alpha_prof, Schedule([0]))
    alpha2 = alpha_eng.evaluate(Assignment(
        manufacture=[("赤金", ["能天使"])],
        trading=[["柏喙", "卡夫卡"]],
        power=[[], [], []],
    )).detail["trading"][0]
    alpha_ratio = (0.5 * 3.0 + 21.0) / 24.0
    alpha2_minutes = 203.4 + (244.32 - 203.4) * alpha_ratio
    alpha2_lmd = 1450 + (1760 - 1450) * alpha_ratio
    expected_alpha2_orders = 24 * 60 / alpha2_minutes * 1.02
    assert "高品质订单alpha2@0.94" in alpha2["特殊"]
    assert alpha2["赤金单/day"] == round(expected_alpha2_orders, 1)
    assert alpha2["龙门币/day"] == round(expected_alpha2_orders * alpha2_lmd, 0)

    dorothy_prof = {
        "能天使": OperatorProfile("能天使", 2, 90, _DB, rarity=6),
        "柏喙": OperatorProfile("柏喙", 2, 90, _DB, rarity=5),
        "但书": OperatorProfile("但书", 2, 90, _DB, rarity=5),
    }
    dorothy_eng = Engine(cfg, dorothy_prof, Schedule([0]))
    beta_dorothy = dorothy_eng.evaluate(Assignment(
        manufacture=[("赤金", ["能天使"])],
        trading=[["柏喙", "但书"]],
        power=[[], [], []],
    )).detail["trading"][0]
    beta_ratio = (0.5 * 5.0 + 19.0) / 24.0
    beta_minutes = 203.4 + (262.8 - 203.4) * beta_ratio
    beta_lmd = 1450 + (1900 - 1450) * beta_ratio
    beta_p2 = 0.30 + (0.05 - 0.30) * beta_ratio
    beta_p3 = 0.50 + (0.10 - 0.50) * beta_ratio
    expected_beta_dorothy_orders = 24 * 60 / beta_minutes * 1.02
    expected_beta_dorothy_lmd = expected_beta_dorothy_orders * (
        beta_lmd + (beta_p2 + beta_p3) * 2.0 * 500.0
    )
    assert "高品质订单beta@0.90" in beta_dorothy["特殊"]
    assert "但书违约单+2" in beta_dorothy["特殊"]
    assert beta_dorothy["赤金单/day"] == round(expected_beta_dorothy_orders, 1)
    assert beta_dorothy["龙门币/day"] == round(expected_beta_dorothy_lmd, 0)


def test_inactive_trade_order_limit_skill_does_not_apply():
    """贸易站订单上限技能应随干员有效工作占比折算, inactive 时不生效。"""
    prof = _prof("焰狐龙梓兰", "能天使")
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    active = eng.evaluate(Assignment(trading=[["焰狐龙梓兰", "能天使"]], power=[[], [], []]))
    assert active.detail["trading"][0]["order_limit"] == 13.0

    with _override(eng, frac={"焰狐龙梓兰": 0.0, "能天使": 1.0}):
        inactive = eng.evaluate(Assignment(trading=[["焰狐龙梓兰", "能天使"]], power=[[], [], []]))
    assert inactive.detail["trading"][0]["order_limit"] == 10.0


def test_fixed_special_trading_orders_from_prts_notes():
    """可露希尔/佩佩固定特殊订单与 U-Official 继承原订单属性。"""
    prof = _prof("可露希尔", "佩佩", "但书", "龙舌兰", "U-Official", "柏喙", "能天使", "夜莺", rarity=6)
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 0.0
    cfg["trading"]["order_limit"] = 100
    cfg["manufacture"]["capacity_volume"] = 10000
    cfg["manufacture"]["lines"]["赤金"]["base_minutes_per_item"] = 1.0
    eng = Engine(cfg, prof, Schedule([0]))

    closur = eng.evaluate(Assignment(
        manufacture=[("赤金", ["夜莺"])],
        trading=[["可露希尔"]],
        power=[[], [], []],
    )).detail["trading"][0]
    assert closur["特殊"] == ["可露希尔特别订单"]
    assert closur["赤金单/day"] == 11.1  # 24h * 60 / 144min * (1 + 11%)
    assert closur["龙门币/day"] == 13320.0

    pepe = eng.evaluate(Assignment(trading=[["佩佩"]], power=[[], [], []])).detail["trading"][0]
    assert pepe["特殊"] == ["佩佩特别独占订单"]
    assert pepe["赤金单/day"] == 5.3  # 24h * 60 / 270min, 0 赤金也应成交
    assert pepe["龙门币/day"] == 5333.0

    pepe_with_eff = eng.evaluate(Assignment(trading=[["佩佩", "能天使"]], power=[[], [], []])).detail["trading"][0]
    assert pepe_with_eff["eff%"] > pepe["eff%"]
    assert pepe_with_eff["龙门币/day"] == pepe["龙门币/day"]  # 特别独占订单不受订单效率影响

    priority = eng.evaluate(Assignment(
        manufacture=[("赤金", ["夜莺"])],
        trading=[["佩佩", "可露希尔", "但书"]],
        power=[[], [], []],
    )).detail["trading"][0]
    assert priority["特殊"] == ["佩佩特别独占订单"]
    assert priority["龙门币/day"] == 5333.0

    uoff = eng.evaluate(Assignment(
        manufacture=[("赤金", ["夜莺"])],
        trading=[["U-Official"]],
        power=[[], [], []],
    )).detail["trading"][0]
    assert uoff["特殊"] == ["U-Official固定2赤金单"]
    expected_uoff_orders = 24 * 60 / 203.4 * 1.11
    assert uoff["赤金单/day"] == round(expected_uoff_orders, 1)
    assert uoff["龙门币/day"] == round(expected_uoff_orders * 1000, 0)

    uoff_quality = eng.evaluate(Assignment(
        manufacture=[("赤金", ["夜莺"])],
        trading=[["U-Official", "柏喙"]],
        power=[[], [], []],
    )).detail["trading"][0]
    beta_ratio = (0.5 * 5.0 + 19.0) / 24.0
    beta_minutes = 203.4 + (262.8 - 203.4) * beta_ratio
    expected_uoff_quality_orders = 24 * 60 / beta_minutes * 1.12
    assert uoff_quality["特殊"] == ["高品质订单beta@0.90", "U-Official固定2赤金单"]
    assert uoff_quality["赤金单/day"] == round(expected_uoff_quality_orders, 1)
    assert uoff_quality["龙门币/day"] == round(expected_uoff_quality_orders * 1000, 0)

    uoff_no_breach = eng.evaluate(Assignment(
        manufacture=[("赤金", ["夜莺"])],
        trading=[["U-Official", "但书", "龙舌兰"]],
        power=[[], [], []],
    )).detail["trading"][0]
    assert uoff_no_breach["特殊"] == ["U-Official固定2赤金单"]

    dorothy_e2 = eng.evaluate(Assignment(
        manufacture=[("赤金", ["夜莺"])],
        trading=[["但书"]],
        power=[[], [], []],
    )).detail["trading"][0]
    assert dorothy_e2["特殊"] == ["但书违约单+2"]
    assert dorothy_e2["赤金单/day"] == 7.2
    assert dorothy_e2["龙门币/day"] == 16088.0

    e0_prof = {
        "但书": OperatorProfile("但书", 0, 90, _DB, rarity=6),
        "夜莺": OperatorProfile("夜莺", 2, 90, _DB, rarity=6),
    }
    e0_eng = Engine(cfg, e0_prof, Schedule([0]))
    dorothy_e0 = e0_eng.evaluate(Assignment(
        manufacture=[("赤金", ["夜莺"])],
        trading=[["但书"]],
        power=[[], [], []],
    )).detail["trading"][0]
    assert dorothy_e0["特殊"] == ["但书违约单+1"]
    assert dorothy_e0["赤金单/day"] == 7.2
    assert dorothy_e0["龙门币/day"] == 13228.0


def test_trading_counting_skill_keeps_conditional_extra():
    """摩根 帮派指南针: 格拉斯哥帮人数加成应与推进之王条件额外叠加。"""
    prof = _prof("摩根", "达格达", "推进之王", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(trading=[["摩根"]], power=[[], [], []]))
    assert eng._resolved["摩根"].trade_eff == 0.0

    eng.evaluate(Assignment(trading=[["摩根", "达格达"]], power=[[], [], []]))
    assert eng._resolved["摩根"].trade_eff == 20.0
    with _override(eng, frac={"摩根": 1.0, "达格达": 0.0}):
        eng.evaluate(Assignment(trading=[["摩根", "达格达"]], power=[[], [], []]))
    assert eng._resolved["摩根"].trade_eff == 0.0

    eng.evaluate(Assignment(trading=[["摩根", "推进之王"]], power=[[], [], []]))
    assert eng._resolved["摩根"].trade_eff == 55.0
    with _override(eng, frac={"摩根": 1.0, "推进之王": 0.0}):
        eng.evaluate(Assignment(trading=[["摩根", "推进之王"]], power=[[], [], []]))
    assert eng._resolved["摩根"].trade_eff == 0.0


def test_trading_workplace_extra_requires_active_working_operator():
    """赫德雷 白手起家: 伊内丝/W 须在非宿舍工作场所且处于工作状态才分别+5%。"""
    prof = _prof("赫德雷", "伊内丝", "W", "夜莺", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(trading=[["赫德雷"]], power=[[], [], []]))
    assert eng._resolved["赫德雷"].trade_eff == 30.0

    eng.evaluate(Assignment(
        manufacture=[("赤金", ["W"])],
        trading=[["赫德雷"]],
        meeting=["伊内丝"],
        power=[[], [], []],
    ))
    assert eng._resolved["赫德雷"].trade_eff == 40.0

    eng.evaluate(Assignment(
        manufacture=[("赤金", ["W"])],
        trading=[["赫德雷"]],
        dormitory=[["伊内丝"]],
        power=[[], [], []],
    ))
    assert eng._resolved["赫德雷"].trade_eff == 35.0

    with _override(eng, frac={"赫德雷": 1.0, "伊内丝": 0.0, "W": 1.0}):
        eng.evaluate(Assignment(
            manufacture=[("赤金", ["W"])],
            trading=[["赫德雷"]],
            meeting=["伊内丝"],
            power=[[], [], []],
        ))
    assert eng._resolved["赫德雷"].trade_eff == 35.0


def test_bubble_hunter_trade_faction_counts_current_trading_room():
    """焰狐龙梓兰 队长的自觉: 按当前贸易站内泡影国狩猎小队人数+20%, 含自身。"""
    prof = _prof("焰狐龙梓兰", "雷狼龙S空爆", "罗德岛隐秘队", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(trading=[["焰狐龙梓兰"]], power=[[], [], []]))
    assert eng._resolved["焰狐龙梓兰"].order_limit == 3.0
    assert eng._resolved["焰狐龙梓兰"].trade_eff == 20.0

    eng.evaluate(Assignment(trading=[["焰狐龙梓兰", "雷狼龙S空爆"]], power=[[], [], []]))
    assert eng._resolved["焰狐龙梓兰"].trade_eff == 40.0
    with _override(eng, frac={"焰狐龙梓兰": 1.0, "雷狼龙S空爆": 0.0}):
        eng.evaluate(Assignment(trading=[["焰狐龙梓兰", "雷狼龙S空爆"]], power=[[], [], []]))
    assert eng._resolved["焰狐龙梓兰"].trade_eff == 20.0

    eng.evaluate(Assignment(trading=[["焰狐龙梓兰"], ["雷狼龙S空爆"]], power=[[], [], []]))
    assert eng._resolved["焰狐龙梓兰"].trade_eff == 20.0


def test_trading_text_eff_without_value_is_parsed_without_static_count_pollution():
    """雷狼龙S空爆 气氛组的效率写在文本里; 焰狐龙梓兰仍按人数动态计算。"""
    prof = _prof("焰狐龙梓兰", "雷狼龙S空爆", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    assert prof["雷狼龙S空爆"].stat("trading").trade_eff == 35.0

    eng.evaluate(Assignment(trading=[["雷狼龙S空爆"]], power=[[], [], []]))
    assert eng._resolved["雷狼龙S空爆"].trade_eff == 35.0

    eng.evaluate(Assignment(trading=[["焰狐龙梓兰"]], power=[[], [], []]))
    assert eng._resolved["焰狐龙梓兰"].trade_eff == 20.0

    eng.evaluate(Assignment(trading=[["焰狐龙梓兰", "雷狼龙S空爆"]], power=[[], [], []]))
    assert eng._resolved["焰狐龙梓兰"].trade_eff == 40.0
    assert eng._resolved["雷狼龙S空爆"].trade_eff == 35.0


def test_trade_room_faction_presence_requires_active_member():
    """维娜·维多利亚: 当前贸易站内存在工作中的格拉斯哥帮干员时才给额外效率。"""
    prof = _prof("维娜·维多利亚", "摩根", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(trading=[["维娜·维多利亚"]], power=[[], [], []]))
    assert eng._resolved["维娜·维多利亚"].trade_eff == 30.0

    eng.evaluate(Assignment(trading=[["维娜·维多利亚", "摩根"]], power=[[], [], []]))
    assert eng._resolved["维娜·维多利亚"].trade_eff == 40.0

    with _override(eng, frac={"维娜·维多利亚": 1.0, "摩根": 0.0}):
        eng.evaluate(Assignment(trading=[["维娜·维多利亚", "摩根"]], power=[[], [], []]))
    assert eng._resolved["维娜·维多利亚"].trade_eff == 30.0


def test_gold_production_line_chain():
    """赤金生产线: 赤金制造站 + 鸿雪/绮良产线, 鸿雪/图耶按总线数提高贸易效率。"""
    prof = _prof("鸿雪", "图耶", "绮良", "至简", "桃金娘", "褐果", "杜林", "夜莺", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(
        manufacture=[("赤金", ["夜莺"])],
        trading=[["鸿雪", "图耶", "绮良"]],
        dormitory=[["至简", "桃金娘", "褐果", "杜林"]],
        power=[[], [], []],
    )
    eng.evaluate(asg)
    assert eng._resolved["绮良"].trade_eff == 5.0
    assert eng._resolved["鸿雪"].trade_eff == 45.0
    assert eng._resolved["图耶"].trade_eff == 65.0
    with _override(eng, frac={
        "鸿雪": 1.0,
        "图耶": 1.0,
        "绮良": 1.0,
        "至简": 0.0,
        "桃金娘": 0.0,
        "褐果": 1.0,
        "杜林": 1.0,
        "夜莺": 1.0,
    }):
        eng.evaluate(asg)
    assert eng._resolved["鸿雪"].trade_eff == 25.0
    assert eng._resolved["图耶"].trade_eff == 35.0

    with _override(eng, frac={"鸿雪": 0.0, "图耶": 1.0, "绮良": 1.0, "夜莺": 1.0}):
        eng.evaluate(asg)
    assert eng._resolved["图耶"].trade_eff == 5.0

    with _override(eng, frac={"鸿雪": 1.0, "图耶": 1.0, "绮良": 0.0, "夜莺": 1.0}):
        eng.evaluate(asg)
    assert eng._resolved["鸿雪"].trade_eff == 25.0
    assert eng._resolved["图耶"].trade_eff == 35.0


def test_order_limit_driven_trade_efficiency():
    """订单上限动态: 贸易站等级上限与锏/琳琅诗怀雅按站内上限提升量换效率。"""
    prof = _prof("锏", "琳琅诗怀雅", "佩佩", "瑰盐", "银灰", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(trading=[["锏", "琳琅诗怀雅", "佩佩"]], power=[[], [], []]))
    assert eng._resolved["佩佩"].order_limit == 3.0
    assert eng._resolved["锏"].trade_eff == 25.0      # 佩佩+3, 不足每5上限一档; 只剩不怒自威+25
    assert eng._resolved["琳琅诗怀雅"].trade_eff == 32.0  # 订单分发20 + 3*4

    eng.evaluate(Assignment(trading=[["锏", "琳琅诗怀雅", "瑰盐"]], power=[[], [], []]))
    assert eng._resolved["瑰盐"].order_limit == 3.0
    assert eng._resolved["锏"].trade_eff == 25.0
    assert eng._resolved["琳琅诗怀雅"].trade_eff == 32.0

    eng.evaluate(Assignment(trading=[["锏", "银灰", "佩佩"]], power=[[], [], []]))
    assert eng._resolved["锏"].trade_eff == 50.0      # 银灰+4 + 佩佩+3 -> 一档冠军风采+25
    with _override(eng, frac={"锏": 1.0, "银灰": 0.5, "佩佩": 1.0}):
        eng.evaluate(Assignment(trading=[["锏", "银灰", "佩佩"]], power=[[], [], []]))
    assert eng._resolved["锏"].trade_eff == 50.0      # 银灰+2 + 佩佩+3 -> 仍只有一档
    with _override(eng, frac={"锏": 1.0, "银灰": 0.5, "佩佩": 0.5}):
        eng.evaluate(Assignment(trading=[["锏", "银灰", "佩佩"]], power=[[], [], []]))
    assert eng._resolved["锏"].trade_eff == 25.0      # 2 + 1.5 不足一档


def test_trade_eff_driven_trade_efficiency_is_dynamic():
    """雪雉 天道酬勤: 按当前贸易站内干员已提供订单效率换算, 不应静态满值。"""
    prof = _prof("雪雉", "远山", "梓兰", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(trading=[["雪雉"]], power=[[], [], []]))
    assert eng._resolved["雪雉"].trade_eff == 0.0

    eng.evaluate(Assignment(trading=[["雪雉", "远山"]], power=[[], [], []]))
    assert eng._resolved["雪雉"].trade_eff == 25.0

    eng.evaluate(Assignment(trading=[["雪雉", "远山", "梓兰"]], power=[[], [], []]))
    assert eng._resolved["雪雉"].trade_eff == 35.0
    with _override(eng, frac={"雪雉": 1.0, "远山": 0.5}):
        eng.evaluate(Assignment(trading=[["雪雉", "远山"]], power=[[], [], []]))
    assert eng._resolved["雪雉"].trade_eff == 10.0


def test_jaye_order_limit_penalty_depends_on_other_trade_efficiency():
    """孑 市井之道: 订单上限降低应按同站其他干员提供的订单效率动态计算。"""
    prof = _prof("孑", "远山", "梓兰", "能天使", "空", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(trading=[["孑"]], power=[[], [], []]))
    assert eng._resolved["孑"].order_limit == 0.0

    eng.evaluate(Assignment(trading=[["孑", "远山"]], power=[[], [], []]))
    assert eng._resolved["孑"].order_limit == -2.0

    eng.evaluate(Assignment(trading=[["孑", "远山", "梓兰"]], power=[[], [], []]))
    assert eng._resolved["孑"].order_limit == -5.0
    with _override(eng, frac={"孑": 1.0, "远山": 0.5}):
        eng.evaluate(Assignment(trading=[["孑", "远山"]], power=[[], [], []]))
    assert eng._resolved["孑"].order_limit == -1.0

    cfg = copy.deepcopy(CONFIG)
    cfg["trading"]["order_limit"] = 3
    cfg["power"]["drone_per_hour_base"] = 0.0
    low_limit = Engine(cfg, prof, Schedule([0, 6, 12, 18])).evaluate(
        Assignment(trading=[["孑", "能天使", "空"]], power=[[], [], []])
    )
    assert low_limit.detail["trading"][0]["order_limit"] == 1.0


def test_manufacture_recipe_type_trade_efficiency():
    """石英 精准排期: 制造站每有1类配方加工, 当前贸易站效率额外+2%。"""
    prof = {
        "石英": OperatorProfile("石英", 2, 90, _DB, rarity=5),
        "夜莺": OperatorProfile("夜莺", 2, 90, _DB, rarity=6),
        "能天使": OperatorProfile("能天使", 2, 90, _DB, rarity=6),
    }
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(manufacture=[("赤金", ["夜莺"])], trading=[["石英"]], power=[[], [], []]))
    assert eng._resolved["石英"].trade_eff == 32.0

    eng.evaluate(Assignment(manufacture=[("赤金", ["夜莺"]), ("作战记录", ["能天使"])],
                            trading=[["石英"]], power=[[], [], []]))
    assert eng._resolved["石英"].trade_eff == 34.0


def test_faction_facility_count_scaling():
    """基建内每有一间进驻某类干员的设施: 按房间数而非人数/静态值缩放。"""
    prof = _prof("真言", "迷迭香", "煌", "凯尔希·思衡托", "风絮", "夕", "令", "黍", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(
        trading=[["真言"]],
        manufacture=[("作战记录", ["迷迭香"])],
        dormitory=[["煌"]],
        hire=["凯尔希·思衡托"],
        power=[[], [], []],
    )
    eng.evaluate(asg)
    assert eng._resolved["真言"].trade_eff == 31.0
    assert eng._resolved["凯尔希·思衡托"].contact == 42.0
    with _override(eng, frac={
        "真言": 1.0,
        "迷迭香": 1.0,
        "煌": 0.0,
        "凯尔希·思衡托": 1.0,
    }):
        eng.evaluate(asg)
    assert eng._resolved["真言"].trade_eff == 29.0
    assert eng._resolved["凯尔希·思衡托"].contact == 38.0

    eng.evaluate(Assignment(
        trading=[["风絮"]],
        control=["夕"],
        manufacture=[("作战记录", ["令"])],
        dormitory=[["黍"]],
        power=[[], [], []],
    ))
    assert eng._resolved["风絮"].trade_eff == 32.0


def test_layout_branch_control_skill_wang():
    """望·权变: 外势(贸易+发电) >= 实地(制造) 给贸易+7, 否则制造+2。"""
    prof = _prof("望", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    trade_branch = Assignment(
        control=["望"],
        manufacture=[("作战记录", []) for _ in range(4)],
        trading=[[] for _ in range(2)],
        power=[[], [], []],
    )
    prod_branch = Assignment(
        control=["望"],
        manufacture=[("作战记录", []) for _ in range(6)],
        trading=[[]],
        power=[[], [], []],
    )

    res = eng.evaluate(trade_branch)
    assert res.detail["globals"]["trade_eff"] == 7.0
    assert res.detail["globals"]["prod"] == {}

    res = eng.evaluate(prod_branch)
    assert res.detail["globals"]["trade_eff"] == 0.0
    assert res.detail["globals"]["prod"]["all"] == 2.0

    with _override(eng, frac={"望": 0.0}):
        inactive_trade = eng.evaluate(trade_branch)
        inactive_prod = eng.evaluate(prod_branch)
    assert inactive_trade.detail["globals"]["trade_eff"] == 0.0
    assert inactive_prod.detail["globals"]["prod"].get("all", 0.0) == 0.0


def test_control_faction_pair_global_bonuses_are_gated_by_active_coworker():
    """PRTS: 同中枢派系协作技能必须有对应派系干员一起工作才触发。"""
    prof = _prof("斩业星熊", "陈", "麒麟R夜刀", "火龙S黑角", "泰拉大陆调查团", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    solo_lgd = eng.evaluate(Assignment(
        control=["斩业星熊"],
        manufacture=[("赤金", [])],
        trading=[[]],
        power=[[], [], []],
    ))
    assert solo_lgd.detail["globals"]["prod"] == {}

    paired_lgd = eng.evaluate(Assignment(
        control=["斩业星熊", "陈"],
        manufacture=[("赤金", [])],
        trading=[[]],
        power=[[], [], []],
    ))
    assert paired_lgd.detail["globals"]["prod"]["all"] == 3.0

    with _override(eng, frac={"斩业星熊": 1.0, "陈": 0.0}):
        inactive_coworker = eng.evaluate(Assignment(
            control=["斩业星熊", "陈"],
            manufacture=[("赤金", [])],
            trading=[[]],
            power=[[], [], []],
        ))
    assert inactive_coworker.detail["globals"]["prod"] == {}

    solo_hunter = eng.evaluate(Assignment(
        control=["麒麟R夜刀"],
        manufacture=[("赤金", [])],
        trading=[[]],
        power=[[], [], []],
    ))
    assert solo_hunter.detail["globals"]["prod"] == {}
    assert solo_hunter.detail["globals"]["trade_eff"] == 0.0

    paired_hunter = eng.evaluate(Assignment(
        control=["麒麟R夜刀", "火龙S黑角"],
        manufacture=[("赤金", [])],
        trading=[[]],
        power=[[], [], []],
    ))
    assert paired_hunter.detail["globals"]["prod"]["all"] == 2.0
    assert paired_hunter.detail["globals"]["trade_eff"] == 7.0

    blackhorn_with_member = eng.evaluate(Assignment(
        control=["火龙S黑角", "泰拉大陆调查团"],
        manufacture=[("赤金", [])],
        trading=[[]],
        power=[[], [], []],
    ))
    assert blackhorn_with_member.detail["globals"]["trade_eff"] == 7.0


def test_control_trade_room_scoped_faction_bonuses():
    """控制中枢贸易站派系技能应作用于具体贸易站, 不应变成全局贸易效率。"""
    prof = _prof("凛御银灰", "灵知", "银灰", "讯使", "角峰", "戴菲恩", "摩根", "推进之王", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    res = eng.evaluate(Assignment(
        control=["凛御银灰"],
        trading=[["银灰", "讯使", "角峰"], []],
        power=[[], [], []],
    ))
    assert res.detail["globals"]["trade_eff"] == 0.0
    assert res.detail["trading"][0]["eff%"] == 63.0  # 本站3人基础+银灰20+讯使15+角峰15+商业版图10
    assert res.detail["trading"][1]["eff%"] == 0.0
    with _override(eng, frac={"凛御银灰": 1.0, "银灰": 1.0, "讯使": 1.0, "角峰": 0.0}):
        inactive_third_kjerag = eng.evaluate(Assignment(
            control=["凛御银灰"],
            trading=[["银灰", "讯使", "角峰"], []],
            power=[[], [], []],
        ))
    assert inactive_third_kjerag.detail["trading"][0]["eff%"] == 37.0

    res = eng.evaluate(Assignment(
        control=["灵知"],
        trading=[["银灰", "讯使"], []],
        power=[[], [], []],
    ))
    assert res.detail["globals"]["trade_eff"] == 0.0
    assert res.detail["trading"][0]["eff%"] == 7.0  # 2人基础+20+15-2*15
    assert res.detail["trading"][1]["eff%"] == 0.0
    with _override(eng, frac={"灵知": 1.0, "银灰": 0.0, "讯使": 1.0}):
        inactive_silverash = eng.evaluate(Assignment(
            control=["灵知"],
            trading=[["银灰", "讯使"], []],
            power=[[], [], []],
        ))
    assert inactive_silverash.detail["trading"][0]["eff%"] == 1.0
    assert inactive_silverash.detail["trading"][0]["order_limit"] == 18.0

    res = eng.evaluate(Assignment(
        control=["戴菲恩"],
        trading=[["摩根"], ["推进之王"]],
        power=[[], [], []],
    ))
    assert res.detail["globals"]["trade_eff"] == 0.0
    assert res.detail["trading"][0]["eff%"] == 11.0
    assert res.detail["trading"][1]["eff%"] == 11.0

    res = eng.evaluate(Assignment(
        control=["戴菲恩"],
        trading=[["摩根", "推进之王"], []],
        power=[[], [], []],
    ))
    assert res.detail["trading"][0]["eff%"] == 77.0  # 2基础 + 摩根55 + 戴菲恩2*10
    assert res.detail["trading"][1]["eff%"] == 0.0

    siracusa_prof = _prof("八幡海铃", "红云", "贾维", "夜莺", rarity=5)
    siracusa_eng = Engine(CONFIG, siracusa_prof, Schedule([0, 6, 12, 18]))
    res = siracusa_eng.evaluate(Assignment(
        control=["八幡海铃"],
        trading=[["红云", "贾维"], ["夜莺"]],
        power=[[], [], []],
    ))
    assert res.detail["globals"]["trade_eff"] == 0.0
    assert res.detail["trading"][0]["eff%"] == 12.0  # 2基础 + 2名叙拉古*5
    assert res.detail["trading"][1]["eff%"] == 1.0
    with _override(siracusa_eng, frac={"八幡海铃": 1.0, "红云": 1.0, "贾维": 0.0, "夜莺": 1.0}):
        inactive_siracusan = siracusa_eng.evaluate(Assignment(
            control=["八幡海铃"],
            trading=[["红云", "贾维"], ["夜莺"]],
            power=[[], [], []],
        ))
    assert inactive_siracusan.detail["trading"][0]["eff%"] == 6.0
    assert inactive_siracusan.detail["trading"][1]["eff%"] == 1.0


def test_wisadel_conspiracy_targets_meeting_and_hederer_trade_room():
    """维什戴尔·同谋: 伊内丝在会客室时+线索, 赫德雷所在贸易站订单上限+2。"""
    prof = _prof("维什戴尔", "伊内丝", "赫德雷", "夜莺", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    res = eng.evaluate(Assignment(
        control=["维什戴尔"],
        meeting=["伊内丝"],
        trading=[["赫德雷"], ["夜莺"]],
        power=[[], [], []],
    ))

    assert res.detail["globals"]["clue"] == 5.0
    assert res.detail["globals"]["trade_eff"] == 0.0
    assert res.detail["trading"][0]["order_limit"] == 12.0
    assert res.detail["trading"][1]["order_limit"] == 10.0
    with _override(eng, frac={"维什戴尔": 1.0, "伊内丝": 0.0, "赫德雷": 0.0, "夜莺": 1.0}):
        inactive_targets = eng.evaluate(Assignment(
            control=["维什戴尔"],
            meeting=["伊内丝"],
            trading=[["赫德雷"], ["夜莺"]],
            power=[[], [], []],
        ))
    assert inactive_targets.detail["globals"]["clue"] == 0.0
    assert inactive_targets.detail["trading"][0]["order_limit"] == 10.0


def test_winter_control_clue_counts_only_ursus_students_in_meeting_room():
    """怒潮凛冬: 只按会客室内乌萨斯学生自治团人数加线索, 不应计入其他设施。"""
    prof = _prof("怒潮凛冬", "真理", "凛冬", "夜莺", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    res = eng.evaluate(Assignment(
        control=["怒潮凛冬"],
        meeting=["真理"],
        hire=["凛冬"],
        power=[[], [], []],
    ))

    assert res.detail["globals"]["clue"] == 10.0
    assert eng._resolved["怒潮凛冬"].drain_delta == 0.5


def test_control_manufacture_room_scoped_faction_bonuses():
    """控制中枢制造站派系技能应作用于具体制造站内对应派系干员。"""
    prof = _prof("薇薇安娜", "涤火杰西卡", "焰尾", "砾", "杰西卡", "夜刀", "野鬃", "灰毫", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    res = eng.evaluate(Assignment(
        control=["薇薇安娜", "涤火杰西卡"],
        manufacture=[("赤金", ["砾"]), ("作战记录", ["杰西卡"]), ("作战记录", ["夜刀"])],
        power=[[], [], []],
    ))
    assert res.detail["globals"]["prod"] == {}
    assert res.detail["manufacture"][0]["prod%"] == 43.0  # 砾基础1 + 金属工艺35 + 骑士7
    assert res.detail["manufacture"][1]["prod%"] == 31.0  # 杰西卡基础1 + 标准化25 + 黑钢5
    assert res.detail["manufacture"][2]["prod%"] == 16.0  # 夜刀不吃上述控制加成
    with _override(eng, frac={"薇薇安娜": 1.0, "涤火杰西卡": 1.0, "砾": 0.0}):
        inactive_gravel = eng.evaluate(Assignment(
            control=["薇薇安娜", "涤火杰西卡"],
            manufacture=[("赤金", ["砾"])],
            power=[[], [], []],
        ))
    assert inactive_gravel.detail["manufacture"][0]["prod%"] == 0.0

    res = eng.evaluate(Assignment(
        control=["焰尾"],
        manufacture=[("作战记录", ["野鬃"]), ("赤金", ["灰毫"]), ("作战记录", ["夜刀"])],
        power=[[], [], []],
    ))
    assert res.detail["manufacture"][0]["prod%"] == 36.0  # 野鬃基础1 + 自身25 + 红松作战记录+10
    assert res.detail["manufacture"][1]["prod%"] == 16.0  # 灰毫基础1 + 自身25 + 红松贵金属-10
    assert res.detail["manufacture"][2]["prod%"] == 16.0  # 非红松只吃自身标准化


def test_justice_knight_targets_wild_mane_manufacture_not_power_charge():
    """正义骑士号·滴滴启动: 给野鬃所在制造站+5%, 不是无人机充能+5%。"""
    prof = _prof("正义骑士号", "野鬃", "夜莺", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    assert prof["正义骑士号"].stat("power").power == 10.0

    base = eng.evaluate(Assignment(
        manufacture=[("作战记录", ["野鬃"]), ("作战记录", ["夜莺"])],
        power=[[], [], []],
    ))
    assert base.detail["manufacture"][0]["prod%"] == 26.0
    assert base.detail["manufacture"][1]["prod%"] == 1.0

    targeted = eng.evaluate(Assignment(
        manufacture=[("作战记录", ["野鬃"]), ("作战记录", ["夜莺"])],
        power=[["正义骑士号"], [], []],
    ))
    assert targeted.detail["power"][0]["charge%"] == 15.0  # 发电站基础5 + 备用能源10
    assert targeted.detail["manufacture"][0]["prod%"] == 31.0
    assert targeted.detail["manufacture"][1]["prod%"] == 1.0

    with _override(eng, frac={"正义骑士号": 0.0, "野鬃": 1.0, "夜莺": 1.0}):
        inactive = eng.evaluate(Assignment(
            manufacture=[("作战记录", ["野鬃"]), ("作战记录", ["夜莺"])],
            power=[["正义骑士号"], [], []],
        ))
    assert inactive.detail["manufacture"][0]["prod%"] == 26.0


def test_manufacture_power_platform_count_scales_productivity():
    """阿兰娜 机械精通: 按发电站内工作中的作业平台数量提供贵金属生产力。"""
    prof = _prof("阿兰娜", "Lancet-2", "Castle-3", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    no_platform = eng.evaluate(Assignment(manufacture=[("赤金", ["阿兰娜"])], power=[[], [], []]))
    assert no_platform.detail["manufacture"][0]["prod%"] == 1.0

    one_platform = eng.evaluate(Assignment(manufacture=[("赤金", ["阿兰娜"])], power=[["Lancet-2"], [], []]))
    assert one_platform.detail["manufacture"][0]["prod%"] == 11.0

    two_platforms = eng.evaluate(Assignment(
        manufacture=[("赤金", ["阿兰娜"])],
        power=[["Lancet-2"], ["Castle-3"], []],
    ))
    assert two_platforms.detail["manufacture"][0]["prod%"] == 21.0

    with _override(eng, frac={"阿兰娜": 1.0, "Lancet-2": 1.0, "Castle-3": 0.0}):
        one_active_platform = eng.evaluate(Assignment(
            manufacture=[("赤金", ["阿兰娜"])],
            power=[["Lancet-2"], ["Castle-3"], []],
        ))
    assert one_active_platform.detail["manufacture"][0]["prod%"] == 11.0


def test_office_recruit_slot_clue_global_is_separate_from_contact():
    """办公室每个非初始招募位提供会客室线索速度, 不应混入办公室联络速度。"""
    prof = _prof("乌有", "隐现", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    res = eng.evaluate(Assignment(hire=["乌有"], power=[[], [], []]))
    assert eng._resolved["乌有"].contact == 35.0
    assert res.detail["globals"]["clue"] == 15.0

    res = eng.evaluate(Assignment(hire=["隐现"], power=[[], [], []]))
    assert eng._resolved["隐现"].contact == 30.0
    assert res.detail["globals"]["clue"] == 15.0


def test_meeting_recruit_slot_clue_scales_with_noninitial_slots():
    """骋风·广交义友: 会客室自身线索速度按非初始招募位数量缩放。"""
    prof = _prof("骋风", "夜莺", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    res = eng.evaluate(Assignment(meeting=["骋风"], dormitory=[["夜莺"]], power=[[], [], []]))

    assert eng._resolved["骋风"].clue == 15.0
    assert abs(res.breakdown["会客室(线索)"] - 4.9368) < 1e-9


def test_office_recruit_slot_mood_reduction_scales_with_noninitial_slots():
    """雪绒: 每个非初始招募位心情消耗-0.1, 满级办公室应为-0.3。"""
    prof = _prof("雪绒", rarity=5)
    assert prof["雪绒"].stat("hire").drain_delta == -0.1

    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    eng.evaluate(Assignment(hire=["雪绒"], power=[[], [], []]))

    assert eng._resolved["雪绒"].contact == 35.0
    assert abs(eng._resolved["雪绒"].drain_delta + 0.3) < 1e-9


def test_office_recruit_slot_contact_scales_with_noninitial_slots():
    """林·用人唯才: 每个非初始招募位+10%联络速度, 满级办公室应为+30%。"""
    prof = _prof("林", rarity=6)
    assert prof["林"].stat("hire").contact == 30.0  # 静态: 特殊渠道20 + 用人唯才单位值10

    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    eng.evaluate(Assignment(hire=["林"], power=[[], [], []]))

    assert eng._resolved["林"].contact == 50.0  # 特殊渠道20 + 3个非初始招募位*10

    cfg_by_level = copy.deepcopy(CONFIG)
    cfg_by_level["hire"]["level"] = 2
    level2 = Engine(cfg_by_level, prof, Schedule([0, 6, 12, 18]))
    level2.evaluate(Assignment(hire=["林"], power=[[], [], []]))
    assert level2._recruit_slots_noninitial() == 2
    assert level2._resolved["林"].contact == 40.0  # Lv2办公室3招募位 -> 2个非初始

    cfg = copy.deepcopy(CONFIG)
    cfg["hire"]["recruit_slots"] = 3
    lower_level = Engine(cfg, prof, Schedule([0, 6, 12, 18]))
    lower_level.evaluate(Assignment(hire=["林"], power=[[], [], []]))
    assert lower_level._resolved["林"].contact == 40.0  # 特殊渠道20 + 2个非初始招募位*10

    prof_sang = _prof("桑葚", rarity=5)
    lower_level = Engine(cfg, prof_sang, Schedule([0, 6, 12, 18]))
    lower_level.evaluate(Assignment(hire=["桑葚"], power=[[], [], []]))
    assert lower_level._ctx.pools["人间烟火"] == 20


def test_clue_exchange_skill_is_not_clue_search_speed():
    """处于线索交流时的会客技能不应计入日常线索搜集速度估值。"""
    prof = _prof("跃跃", "响石", "伊内丝", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(meeting=["跃跃"], power=[[], [], []]))
    assert eng._resolved["跃跃"].clue == 10.0

    eng.evaluate(Assignment(meeting=["响石"], power=[[], [], []]))
    assert eng._resolved["响石"].clue == 0.0

    eng.evaluate(Assignment(meeting=["伊内丝"], power=[[], [], []]))
    assert eng._resolved["伊内丝"].clue == 30.0


def test_meeting_named_room_condition_requires_active_target_room():
    """信仰搅拌机: 菲亚梅塔在宿舍时才提供会客室线索速度额外+10%。"""
    prof = _prof("信仰搅拌机", "菲亚梅塔", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(meeting=["信仰搅拌机"], power=[[], [], []]))
    assert eng._resolved["信仰搅拌机"].clue == 20.0

    eng.evaluate(Assignment(meeting=["信仰搅拌机"], trading=[["菲亚梅塔"]], power=[[], [], []]))
    assert eng._resolved["信仰搅拌机"].clue == 20.0

    eng.evaluate(Assignment(meeting=["信仰搅拌机"], dormitory=[["菲亚梅塔"]], power=[[], [], []]))
    assert eng._resolved["信仰搅拌机"].clue == 30.0

    with _override(eng, frac={"信仰搅拌机": 1.0, "菲亚梅塔": 0.0}):
        eng.evaluate(Assignment(meeting=["信仰搅拌机"], dormitory=[["菲亚梅塔"]], power=[[], [], []]))
    assert eng._resolved["信仰搅拌机"].clue == 20.0


def test_warmup_skills_use_window_average_not_static_cap():
    """进驻后逐小时增长的技能按结算窗口平均值生效, _resolved 仍保留最终满值。"""
    prof = _prof("克洛丝", "空构", "伊内丝", "阿罗玛", "夜莺", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0]))

    man = eng.evaluate(Assignment(manufacture=[("作战记录", ["克洛丝"])], power=[[], [], []]))
    assert eng._resolved["克洛丝"].prod["all"] == 25.0
    assert man.detail["manufacture"][0]["prod%"] == 24.5

    power = eng.evaluate(Assignment(power=[["空构"]]))
    assert eng._resolved["空构"].power == 20.0
    assert power.detail["power"][0]["charge%"] == 24.3
    assert power.detail["drones"]["per_day"] == 235.0

    meeting = eng.evaluate(Assignment(meeting=["伊内丝"], dormitory=[["夜莺"]], power=[[], [], []]))
    assert eng._resolved["伊内丝"].clue == 30.0
    assert 5.19 < meeting.breakdown["会客室(线索)"] < 5.23

    aroma = eng.evaluate(Assignment(manufacture=[("赤金", ["阿罗玛"])], power=[[], [], []]))
    assert eng._resolved["阿罗玛"].prod["all"] == 20.0
    assert eng._resolved["阿罗玛"].prod["gold"] == 25.0
    assert aroma.detail["manufacture"][0]["prod%"] == 33.5


def test_only_self_working_meeting_skill_is_gated():
    """复奏: 会客室只有自身处于工作状态时才提供线索速度和额外心情消耗。"""
    prof = _prof("复奏", "风丸", "伊内丝", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(meeting=["复奏"], power=[[], [], []]))
    assert eng._resolved["复奏"].clue == 35.0
    assert eng._resolved["复奏"].drain_delta == 1.0

    eng.evaluate(Assignment(meeting=["复奏", "伊内丝"], power=[[], [], []]))
    assert eng._resolved["复奏"].clue == 0.0
    assert eng._resolved["复奏"].drain_delta == 0.0
    assert eng._resolved["伊内丝"].clue == 30.0

    eng.evaluate(Assignment(meeting=["风丸"], power=[[], [], []]))
    assert eng._resolved["风丸"].clue == 50.0  # 化影15 + 得心应手35

    eng.evaluate(Assignment(meeting=["风丸", "伊内丝"], power=[[], [], []]))
    assert eng._resolved["风丸"].clue == 15.0


def test_meeting_continuous_mood_clue_guarantee_tags():
    """奥达/霍尔海雅: 只有自身工作且连续消耗>16心情时标记下一次必定线索。"""
    prof = _prof("奥达", "霍尔海雅", "伊内丝", rarity=6)
    long_shift = Engine(CONFIG, prof, Schedule([0]))

    odda = long_shift.evaluate(Assignment(meeting=["奥达"], power=[[], [], []]))
    assert "奥达:必定获得罗德岛制药线索" in odda.detail["meeting"][0]["clue_tags"]

    hoho = long_shift.evaluate(Assignment(meeting=["霍尔海雅"], power=[[], [], []]))
    assert "霍尔海雅:必定获得莱茵生命线索" in hoho.detail["meeting"][0]["clue_tags"]

    blocked_by_coworker = long_shift.evaluate(Assignment(meeting=["奥达", "伊内丝"], power=[[], [], []]))
    assert blocked_by_coworker.detail["meeting"][0]["clue_tags"] == []

    frequent_login = Engine(CONFIG, prof, Schedule([0, 6, 12, 18])).evaluate(
        Assignment(meeting=["奥达"], power=[[], [], []])
    )
    assert frequent_login.detail["meeting"][0]["clue_tags"] == []


def test_meeting_clue_preference_tags():
    """会客室线索倾向只作为 detail 标签输出, 不改变线索数量估值。"""
    prof = _prof(
        "U-Official", "晓歌", "夏栎", "山", "苦艾", "极境", "耶拉",
        "海蒂", "艾拉", "骋风", "夜莺",
        rarity=6,
    )
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    owned = eng.evaluate(Assignment(meeting=["U-Official"], power=[[], [], []]))
    assert "U-Official:已拥有线索倾向" in owned.detail["meeting"][0]["clue_tags"]

    missing = eng.evaluate(Assignment(meeting=["晓歌"], power=[[], [], []]))
    assert "晓歌:未拥有线索倾向" in missing.detail["meeting"][0]["clue_tags"]

    faction = eng.evaluate(Assignment(meeting=["夏栎"], power=[[], [], []]))
    assert "夏栎:格拉斯哥帮线索倾向" in faction.detail["meeting"][0]["clue_tags"]
    assert "夏栎:非目标后提高格拉斯哥帮线索概率" in faction.detail["meeting"][0]["clue_tags"]

    for name, target in (
        ("山", "莱茵生命"),
        ("苦艾", "乌萨斯学生自治团"),
        ("极境", "罗德岛制药"),
        ("耶拉", "喀兰贸易"),
    ):
        res = eng.evaluate(Assignment(meeting=[name], power=[[], [], []]))
        assert f"{name}:非目标后提高{target}线索概率" in res.detail["meeting"][0]["clue_tags"]

    control_pref = eng.evaluate(Assignment(control=["海蒂"], meeting=["夜莺"], power=[[], [], []]))
    assert "海蒂:格拉斯哥帮线索倾向" in control_pref.detail["meeting"][0]["clue_tags"]

    control_missing_pref = eng.evaluate(Assignment(control=["艾拉"], meeting=["夜莺"], power=[[], [], []]))
    assert "艾拉:未拥有线索倾向" in control_missing_pref.detail["meeting"][0]["clue_tags"]

    exchange_only = eng.evaluate(Assignment(meeting=["骋风"], power=[[], [], []]))
    assert exchange_only.detail["meeting"][0]["clue_tags"] == []


def test_meeting_pair_working_extra_clue_is_gated():
    """会客室"与X进驻一起工作"额外线索速度只在同会客室时生效。"""
    prof = _prof("忍冬", "铃兰", "凛视", "提丰", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(meeting=["忍冬"], power=[[], [], []]))
    assert eng._resolved["忍冬"].clue == 20.0
    eng.evaluate(Assignment(meeting=["忍冬", "铃兰"], power=[[], [], []]))
    assert eng._resolved["忍冬"].clue == 50.0
    with _override(eng, frac={"忍冬": 1.0, "铃兰": 0.0}):
        eng.evaluate(Assignment(meeting=["忍冬", "铃兰"], power=[[], [], []]))
    assert eng._resolved["忍冬"].clue == 20.0

    eng.evaluate(Assignment(meeting=["凛视"], power=[[], [], []]))
    assert eng._resolved["凛视"].clue == 10.0
    eng.evaluate(Assignment(meeting=["凛视", "提丰"], power=[[], [], []]))
    assert eng._resolved["凛视"].clue == 25.0
    with _override(eng, frac={"凛视": 1.0, "提丰": 0.0}):
        eng.evaluate(Assignment(meeting=["凛视", "提丰"], power=[[], [], []]))
    assert eng._resolved["凛视"].clue == 10.0


def test_meeting_conditional_control_operator_clue_is_gated():
    """罗德岛隐秘队: 焰狐龙梓兰在控制中枢时才提供线索速度。"""
    prof = _prof("罗德岛隐秘队", "焰狐龙梓兰", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(meeting=["罗德岛隐秘队"], power=[[], [], []]))
    assert eng._resolved["罗德岛隐秘队"].clue == 0.0
    eng.evaluate(Assignment(meeting=["罗德岛隐秘队"], trading=[["焰狐龙梓兰"]], power=[[], [], []]))
    assert eng._resolved["罗德岛隐秘队"].clue == 0.0
    eng.evaluate(Assignment(meeting=["罗德岛隐秘队"], control=["焰狐龙梓兰"], power=[[], [], []]))
    assert eng._resolved["罗德岛隐秘队"].clue == 10.0
    with _override(eng, frac={"罗德岛隐秘队": 1.0, "焰狐龙梓兰": 0.0}):
        eng.evaluate(Assignment(meeting=["罗德岛隐秘队"], control=["焰狐龙梓兰"], power=[[], [], []]))
    assert eng._resolved["罗德岛隐秘队"].clue == 0.0


def test_dorm_recovery_classification():
    """宿舍恢复三类: 自身型不帮他人; 全员/单体型才加速来休息的干员。"""
    assert OperatorProfile("斯卡蒂", 2, 90, _DB).stat("dormitory").dorm_recover_self == 1.0
    assert OperatorProfile("斯卡蒂", 2, 90, _DB).stat("dormitory").dorm_recover == 0.0  # 不帮他人
    assert OperatorProfile("杜林", 0, 30, _DB).stat("dormitory").dorm_recover_self == -0.1
    assert OperatorProfile("安比尔", 0, 1, _DB).stat("dormitory").dorm_recover_self == -0.1
    assert OperatorProfile("夜莺", 2, 90, _DB).stat("dormitory").dorm_recover_all == 0.2
    assert OperatorProfile("闪灵", 2, 90, _DB).stat("dormitory").dorm_recover_other == 0.75
    shining = OperatorProfile("闪灵", 2, 90, _DB).room_buffs["dormitory"][0]
    indigo = OperatorProfile("深靛", 2, 90, _DB).room_buffs["dormitory"][0]
    assert dorm_single_recover_for(shining, "闪灵") == 0.0
    assert dorm_single_recover_for(shining, "夜莺") == 0.75
    assert dorm_single_recover_for(indigo, "深靛") == 0.55
    hellagur = OperatorProfile("赫拉格", 1, 90, _DB).room_buffs["dormitory"][0]
    assert dorm_all_recover_for(hellagur, "赫拉格") == 0.0
    assert dorm_all_recover_for(hellagur, "夜莺") == 0.1


def test_fiammetta_self_recovery_does_not_create_static_dorm_capacity():
    """菲亚梅塔·自律: 静态可持续估算中只排除她占用的普通恢复床位。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 0.0
    prof = _prof("菲亚梅塔", "夜莺", rarity=6)
    eng = Engine(cfg, prof, Schedule([0]))

    fia = eng.evaluate(Assignment(
        manufacture=[("作战记录", ["夜莺"])],
        dormitory=[["菲亚梅塔"]],
        power=[[], [], []],
    ))
    assert fia.breakdown["制造站(非赤金)"] > 0.0
    assert not fia.warnings

    normal = eng.evaluate(Assignment(
        manufacture=[("作战记录", ["菲亚梅塔"])],
        dormitory=[["夜莺"]],
        power=[[], [], []],
    ))
    assert normal.breakdown["制造站(非赤金)"] > 0.0
    assert not normal.warnings


def test_static_sustainability_counts_empty_dorm_beds():
    """静态可持续估算: 已建宿舍的空床位也能恢复休息干员, 不是只有宿舍常驻干员才算容量。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 0.0
    cfg["mood"]["base_drain_per_hour"] = 1.0
    prof = _prof("夜莺", rarity=6)
    eng = Engine(cfg, prof, Schedule([0]))

    no_dorm = eng.evaluate(Assignment(manufacture=[("作战记录", ["夜莺"])], power=[[], [], []]))
    assert no_dorm.breakdown["制造站(非赤金)"] == 0.0
    assert any("宿舍恢复(0.0/h)" in w for w in no_dorm.warnings)

    empty_dorm = eng.evaluate(Assignment(manufacture=[("作战记录", ["夜莺"])], dormitory=[[]], power=[[], [], []]))
    assert empty_dorm.breakdown["制造站(非赤金)"] > 0.0
    assert not empty_dorm.warnings


def test_static_sustainability_dorm_all_recovery_can_exclude_self():
    """静态可持续估算: 赫拉格 挣脱不能让自身床位获得除自身以外全员恢复。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 0.0
    cfg["dormitory"]["level"] = None
    cfg["dormitory"]["base_recover_per_hour"] = 0.0
    cfg["dormitory"]["slots"] = 1
    cfg["mood"]["base_drain_per_hour"] = 0.05
    prof = _prof("赫拉格", "能天使", elite=1, rarity=6)
    eng = Engine(cfg, prof, Schedule([0]))

    res = eng.evaluate(
        Assignment(
            manufacture=[("作战记录", ["能天使"])],
            dormitory=[["赫拉格"]],
            power=[[], [], []],
        )
    )

    assert any("宿舍恢复(0.0/h)" in w for w in res.warnings)
    assert res.breakdown["制造站(非赤金)"] == 0.0


def test_static_sustainability_elite_recovery_counts_future_resters():
    """静态可持续估算: 宿舍精英恢复应作用于未来进入空床休息的精英工作干员。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 0.0
    cfg["dormitory"]["level"] = None
    cfg["dormitory"]["base_recover_per_hour"] = 0.0
    cfg["mood"]["base_drain_per_hour"] = 0.05
    prof = _prof("电弧", "迷迭香", rarity=6)
    eng = Engine(cfg, prof, Schedule([0]))
    asg = Assignment(
        control=["电弧"],
        manufacture=[("作战记录", ["迷迭香"])],
        dormitory=[[]],
        power=[[], [], []],
    )

    res = eng.evaluate(asg)

    assert res.detail["globals"]["recover_elite"] == 0.1
    assert res.breakdown["制造站(非赤金)"] > 0.0
    assert not res.warnings


def test_dorm_self_recovery_scales_with_roommates():
    """协律/芳汀: 每名同宿舍其他干员额外为自身恢复+0.05。"""
    prof = _prof("协律", "芳汀", "夜莺", "闪灵", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(dormitory=[["协律"]], power=[[], [], []]))
    assert eng._resolved["协律"].dorm_recover_self == 0.7

    eng.evaluate(Assignment(dormitory=[["协律", "夜莺", "闪灵"]], power=[[], [], []]))
    assert abs(eng._resolved["协律"].dorm_recover_self - 0.8) < 1e-9

    eng.evaluate(Assignment(dormitory=[["芳汀", "夜莺"]], power=[[], [], []]))
    assert eng._resolved["芳汀"].dorm_recover_self == 0.75


def test_dorm_level_recovery_bonus():
    """响石: 当前宿舍每级为恢复效果额外+0.02, 满级宿舍应为0.15+5*0.02。"""
    prof = _prof("响石", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    eng.evaluate(Assignment(dormitory=[["响石"]], power=[[], [], []]))
    assert abs(eng._resolved["响石"].dorm_recover_all - 0.25) < 1e-9
    assert abs(eng._resolved["响石"].dorm_recover - 0.25) < 1e-9

    cfg = copy.deepcopy(CONFIG)
    cfg["dormitory"]["level"] = 3
    lower = Engine(cfg, prof, Schedule([0, 6, 12, 18]))
    lower.evaluate(Assignment(dormitory=[["响石"]], power=[[], [], []]))
    assert abs(lower._resolved["响石"].dorm_recover_all - 0.21) < 1e-9


def test_dorm_power_station_count_recovery_bonus():
    """流明: 柔和微光按有效发电站数量额外恢复, 含仅影响设施数量的虚拟发电站。"""
    prof = {
        "流明": OperatorProfile("流明", 2, 90, _DB, rarity=6),
        "承曦格雷伊": OperatorProfile("承曦格雷伊", 2, 90, _DB, rarity=5),
        "Lancet-2": OperatorProfile("Lancet-2", 2, 90, _DB, rarity=1),
    }
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(dormitory=[["流明"]], power=[[]]))
    assert abs(eng._resolved["流明"].dorm_recover_all - 0.20) < 1e-9

    eng.evaluate(Assignment(dormitory=[["流明"]], power=[[], [], []]))
    assert abs(eng._resolved["流明"].dorm_recover_all - 0.30) < 1e-9
    assert abs(eng._resolved["流明"].dorm_recover - 0.30) < 1e-9

    eng.evaluate(Assignment(dormitory=[["流明"]], power=[["承曦格雷伊"], []]))
    assert abs(eng._resolved["流明"].dorm_recover_all - 0.30) < 1e-9

    eng.evaluate(Assignment(dormitory=[["流明"]], power=[["承曦格雷伊"], ["Lancet-2"]]))
    assert abs(eng._resolved["流明"].dorm_recover_all - 0.25) < 1e-9


def test_dorm_recruit_slot_recovery_bonus():
    """隐德来希/斥罪: 宿舍恢复按非初始招募位额外叠加, 满级办公室取3个。"""
    prof = _prof("隐德来希", "斥罪", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(dormitory=[["隐德来希"], ["斥罪"]], power=[[], [], []]))

    assert abs(eng._resolved["隐德来希"].dorm_recover_all - 0.40) < 1e-9
    assert abs(eng._resolved["隐德来希"].dorm_recover - 0.40) < 1e-9
    assert abs(eng._resolved["斥罪"].dorm_recover_all - 0.30) < 1e-9
    assert abs(eng._resolved["斥罪"].dorm_recover - 0.30) < 1e-9


def test_dorm_faction_branch_recovery_bonus():
    """余/纯烬艾雅法拉: 宿舍恢复按基建内岁/行医人数缩放, 最多4名。"""
    prof = _prof("余", "年", "夕", "令", "重岳", "纯烬艾雅法拉", "蜜莓", "桑葚", "褐果", "哈洛德", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(dormitory=[["余"], ["年"], ["夕"], ["令", "重岳"]], power=[[], [], []]))
    assert abs(eng._resolved["余"].dorm_recover_all - 0.24) < 1e-9
    assert abs(eng._resolved["余"].dorm_recover - 0.24) < 1e-9
    with _override(eng, frac={"余": 1.0, "年": 1.0, "夕": 0.0, "令": 0.0, "重岳": 1.0}):
        eng.evaluate(Assignment(dormitory=[["余"], ["年"], ["夕"], ["令", "重岳"]], power=[[], [], []]))
    assert abs(eng._resolved["余"].dorm_recover_all - 0.18) < 1e-9
    assert abs(eng._resolved["余"].dorm_recover - 0.18) < 1e-9

    eng.evaluate(Assignment(
        dormitory=[["纯烬艾雅法拉"], ["蜜莓"], ["桑葚"], ["褐果", "哈洛德"]],
        power=[[], [], []],
    ))
    assert abs(eng._resolved["纯烬艾雅法拉"].dorm_recover_all - 0.24) < 1e-9
    assert abs(eng._resolved["纯烬艾雅法拉"].dorm_recover - 0.24) < 1e-9
    with _override(eng, frac={"纯烬艾雅法拉": 1.0, "蜜莓": 1.0, "桑葚": 0.0, "褐果": 1.0, "哈洛德": 0.0}):
        eng.evaluate(Assignment(
            dormitory=[["纯烬艾雅法拉"], ["蜜莓"], ["桑葚"], ["褐果", "哈洛德"]],
            power=[[], [], []],
        ))
    assert abs(eng._resolved["纯烬艾雅法拉"].dorm_recover_all - 0.18) < 1e-9
    assert abs(eng._resolved["纯烬艾雅法拉"].dorm_recover - 0.18) < 1e-9


def test_synergy_scaling_fixes():
    """人数缩放(吉星 每名工作干员+x%)与 塑心 仅本宿舍间人数。"""
    prof = _prof("吉星", "能天使", "德克萨斯")
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    solo = eng.evaluate(Assignment(trading=[["吉星"]], power=[[], [], []])).detail["trading"][0]["eff%"]
    trio = eng.evaluate(Assignment(trading=[["吉星", "能天使", "德克萨斯"]], power=[[], [], []])).detail["trading"][0]["eff%"]
    assert solo == 1.0, f"吉星单独仅应有PRTS基础效率+1%: {solo}"
    assert trio > solo + 30, f"吉星按每名其他干员+20%未生效: {solo}->{trio}"
    with _override(eng, frac={"吉星": 1.0, "能天使": 1.0, "德克萨斯": 0.0}):
        partial = eng.evaluate(Assignment(trading=[["吉星", "能天使", "德克萨斯"]], power=[[], [], []]))
    assert eng._resolved["吉星"].trade_eff == 20.0
    assert partial.detail["trading"][0]["eff%"] == 57.0
    # 塑心 无声共鸣 = 本宿舍间人数(2), 非全宿舍(3)
    prof2 = _prof("塑心", "夜莺", "闪灵")
    ctx = build_context(Assignment(dormitory=[["塑心", "夜莺"], ["闪灵"]]))
    assert ctx.pools.get("无声共鸣", 0) == 2, f"塑心应按本宿舍2人: {ctx.pools.get('无声共鸣')}"


def test_workshop_is_idle_not_hourly_work_state():
    """PRTS基础: 加工站通常为空闲中, 不按小时心情消耗, 瞬态覆盖也不应关闭副产品技能。"""
    prof = _prof("霜华", rarity=5)
    cfg = json.loads(json.dumps(CONFIG))
    cfg["workshop"]["crafts_per_day"] = 10.0
    cfg["workshop"]["base_byproduct_chance"] = 0.0
    cfg["workshop"]["ap_per_byproduct"] = 2.0
    cfg["dormitory"]["base_recover_per_hour"] = 0.0
    cfg["dormitory"].pop("level", None)
    eng = Engine(cfg, prof, Schedule([0]))
    asg = Assignment(workshop=["霜华"], power=[[], [], []])

    res = eng.evaluate(asg)
    assert res.detail["workshop"][0]["byproduct%"] == 50.0
    assert res.breakdown["加工站"] == 10.0

    no_op_cfg = json.loads(json.dumps(cfg))
    no_op_cfg["workshop"]["base_byproduct_chance"] = 0.10
    no_op = Engine(no_op_cfg, prof, Schedule([0]))
    no_op_res = no_op.evaluate(Assignment(workshop=[], power=[[], [], []]))
    assert no_op_res.breakdown["加工站"] == 0.0
    assert no_op_res.detail["workshop"] == []
    assert res.warnings == []

    with _override(eng, frac={}):
        res = eng.evaluate(asg)
    assert res.detail["workshop"][0]["byproduct%"] == 50.0
    assert res.breakdown["加工站"] == 10.0

    with _override(eng, fatigued={"霜华"}):
        fatigued = eng.evaluate(asg)
    assert fatigued.detail["workshop"][0]["byproduct%"] == 0.0
    assert fatigued.breakdown["加工站"] == 0.0


def test_workshop_byproduct_respects_craft_category():
    """加工站副产品技能按加工类别生效: 精英材料技能不能加到技巧概要加工上。"""
    prof = _prof("末药", "伯塔尼", "霜华", rarity=5)
    cfg = copy.deepcopy(CONFIG)
    cfg["workshop"]["crafts_per_day"] = 10.0
    cfg["workshop"]["base_byproduct_chance"] = 0.0
    cfg["workshop"]["ap_per_byproduct"] = 2.0
    cfg["workshop"]["craft_category"] = "skill"
    eng = Engine(cfg, prof, Schedule([0]))

    elite_on_skill = eng.evaluate(Assignment(workshop=["末药"], power=[[], [], []]))
    assert elite_on_skill.detail["workshop"][0]["byproduct%"] == 0.0
    assert elite_on_skill.breakdown["加工站"] == 0.0

    skill_on_skill = eng.evaluate(Assignment(workshop=["伯塔尼"], power=[[], [], []]))
    assert skill_on_skill.detail["workshop"][0]["byproduct%"] == 75.0
    assert skill_on_skill.breakdown["加工站"] == 15.0

    cfg["workshop"]["craft_category"] = "elite"
    eng = Engine(cfg, prof, Schedule([0]))
    elite_on_elite = eng.evaluate(Assignment(workshop=["末药"], power=[[], [], []]))
    assert elite_on_elite.detail["workshop"][0]["byproduct%"] == 75.0

    any_skill = eng.evaluate(Assignment(workshop=["霜华"], power=[[], [], []]))
    assert any_skill.detail["workshop"][0]["byproduct%"] == 50.0


def test_workshop_crafts_are_limited_by_instant_mood_cost():
    """PRTS: 加工每次瞬时消耗配方心情, 心情不足/涣散后不能继续获得副产物。"""
    prof = _prof("霜华", rarity=5)
    cfg = copy.deepcopy(CONFIG)
    cfg["workshop"]["crafts_per_day"] = 10.0
    cfg["workshop"]["craft_mood_cost"] = 4.0
    cfg["workshop"]["base_byproduct_chance"] = 0.0
    cfg["workshop"]["ap_per_byproduct"] = 2.0
    eng = Engine(cfg, prof, Schedule([0]))

    full_mood = eng.evaluate(Assignment(workshop=["霜华"], power=[[], [], []]))
    assert full_mood.detail["workshop"][0]["requested_crafts/day"] == 10.0
    assert full_mood.detail["workshop"][0]["crafts/day"] == 6.0
    assert full_mood.breakdown["加工站"] == 6.0

    with _override(eng, mood={"霜华": 8.0}):
        low_mood = eng.evaluate(Assignment(workshop=["霜华"], power=[[], [], []]))
    assert low_mood.detail["workshop"][0]["crafts/day"] == 2.0
    assert low_mood.breakdown["加工站"] == 2.0


def test_jiuse_lu_workshop_pity_byproducts_follow_mood_cost():
    """九色鹿: 因果/业报按未出副产品的加工心情消耗长期折算额外副产品。"""
    prof = _prof("九色鹿", rarity=5)
    cfg = copy.deepcopy(CONFIG)
    cfg["workshop"]["crafts_per_day"] = 10.0
    cfg["workshop"]["base_byproduct_chance"] = 0.0
    cfg["workshop"]["ap_per_byproduct"] = 2.0
    eng = Engine(cfg, prof, Schedule([0]))

    cfg["workshop"]["craft_mood_cost"] = 4.0
    cost_4 = eng.evaluate(Assignment(workshop=["九色鹿"], power=[[], [], []]))
    assert cost_4.detail["workshop"][0]["crafts/day"] == 6.0
    assert cost_4.detail["workshop"][0]["pity_byproducts/day"] == 0.6
    assert cost_4.detail["workshop"][0]["byproducts/day"] == 0.6
    assert cost_4.breakdown["加工站"] == 1.2

    cfg["workshop"]["craft_mood_cost"] = 2.0
    cost_2 = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(workshop=["九色鹿"], power=[[], [], []]))
    assert cost_2.detail["workshop"][0]["crafts/day"] == 10.0
    assert cost_2.detail["workshop"][0]["pity_byproducts/day"] == 0.5
    assert cost_2.breakdown["加工站"] == 1.0

    cfg["workshop"]["craft_mood_cost"] = 8.0
    cost_8 = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(workshop=["九色鹿"], power=[[], [], []]))
    assert cost_8.detail["workshop"][0]["crafts/day"] == 3.0
    assert cost_8.detail["workshop"][0]["pity_byproducts/day"] == 0.3
    assert cost_8.breakdown["加工站"] == 0.6

    cfg["workshop"]["base_byproduct_chance"] = 1.0
    guaranteed = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(workshop=["九色鹿"], power=[[], [], []]))
    assert guaranteed.detail["workshop"][0]["pity_byproducts/day"] == 0.0


def test_thorns_workshop_failure_restore_increases_craft_capacity():
    """棘刺 爆炸艺术: 未出副产品每2次返还一次心情, 按长期期望提高可加工次数。"""
    prof = _prof("棘刺", "霜华", rarity=6)
    cfg = copy.deepcopy(CONFIG)
    cfg["workshop"]["crafts_per_day"] = 10.0
    cfg["workshop"]["craft_mood_cost"] = 4.0
    cfg["workshop"]["base_byproduct_chance"] = 0.0
    cfg["workshop"]["ap_per_byproduct"] = 2.0
    eng = Engine(cfg, prof, Schedule([0]))

    regular = eng.evaluate(Assignment(workshop=["霜华"], power=[[], [], []]))
    assert regular.detail["workshop"][0]["byproduct_chance%"] == 50.0
    assert regular.detail["workshop"][0]["mood_cost"] == 4.0
    assert regular.detail["workshop"][0]["net_mood_cost"] == 4.0
    assert regular.detail["workshop"][0]["crafts/day"] == 6.0

    thorns = eng.evaluate(Assignment(workshop=["棘刺"], power=[[], [], []]))
    assert thorns.detail["workshop"][0]["byproduct_chance%"] == 50.0
    assert thorns.detail["workshop"][0]["mood_cost"] == 4.0
    assert thorns.detail["workshop"][0]["net_mood_cost"] == 3.0
    assert thorns.detail["workshop"][0]["crafts/day"] == 8.0

    with _override(eng, fatigued={"棘刺"}):
        fatigued = eng.evaluate(Assignment(workshop=["棘刺"], power=[[], [], []]))
    assert fatigued.detail["workshop"][0]["net_mood_cost"] == 4.0
    assert fatigued.detail["workshop"][0]["crafts/day"] == 0.0


def test_workshop_mood_cost_skills_modify_instant_craft_cost():
    """加工站心情消耗调整技能应改变可加工次数。"""
    prof = _prof("缇缇", "罗小黑", "年", "伯塔尼", "蒂比", rarity=6)
    cfg = copy.deepcopy(CONFIG)
    cfg["workshop"]["crafts_per_day"] = 10.0
    cfg["workshop"]["craft_category"] = "elite"
    cfg["workshop"]["craft_mood_cost"] = 8.0
    cfg["workshop"]["ap_per_byproduct"] = 0.0
    eng = Engine(cfg, prof, Schedule([0]))

    titti = eng.evaluate(Assignment(workshop=["缇缇"], power=[[], [], []]))
    assert titti.detail["workshop"][0]["mood_cost"] == 4.0
    assert titti.detail["workshop"][0]["crafts/day"] == 6.0

    luoxiaohei = eng.evaluate(Assignment(workshop=["罗小黑"], power=[[], [], []]))
    assert luoxiaohei.detail["workshop"][0]["mood_cost"] == 2.0
    assert luoxiaohei.detail["workshop"][0]["crafts/day"] == 10.0

    nian = eng.evaluate(Assignment(workshop=["年"], power=[[], [], []]))
    assert nian.detail["workshop"][0]["mood_cost"] == 10.0
    assert nian.detail["workshop"][0]["crafts/day"] == 2.0

    cfg["workshop"]["crafts_per_day"] = 20.0
    cfg["workshop"]["craft_category"] = "skill"
    cfg["workshop"]["craft_mood_cost"] = 2.0
    low_cost = Engine(cfg, prof, Schedule([0]))
    botany = low_cost.evaluate(Assignment(workshop=["伯塔尼"], power=[[], [], []]))
    assert botany.detail["workshop"][0]["mood_cost"] == 1.0
    assert botany.detail["workshop"][0]["crafts/day"] == 20.0

    tippi = low_cost.evaluate(Assignment(workshop=["蒂比"], power=[[], [], []]))
    assert tippi.detail["workshop"][0]["mood_cost"] == 1.0
    assert tippi.detail["workshop"][0]["crafts/day"] == 20.0

    cfg["workshop"]["craft_category"] = "elite"
    off_category = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(workshop=["伯塔尼"], power=[[], [], []]))
    assert off_category.detail["workshop"][0]["mood_cost"] == 2.0
    assert off_category.detail["workshop"][0]["crafts/day"] == 12.0


def test_workshop_threshold_mood_cost_subtraction():
    """矩: 心情消耗为N以上的配方全部-X — 走 >= 阈值分支而非等值匹配。"""
    prof = _prof("矩", rarity=5)
    cfg = copy.deepcopy(CONFIG)
    cfg["workshop"]["crafts_per_day"] = 10.0
    cfg["workshop"]["craft_category"] = "elite"
    cfg["workshop"]["ap_per_byproduct"] = 0.0

    cfg["workshop"]["craft_mood_cost"] = 4.0
    eng = Engine(cfg, prof, Schedule([0]))
    cost_4 = eng.evaluate(Assignment(workshop=["矩"], power=[[], [], []]))
    assert cost_4.detail["workshop"][0]["mood_cost"] == 2.0
    assert cost_4.detail["workshop"][0]["crafts/day"] == 10.0

    cfg["workshop"]["craft_mood_cost"] = 8.0
    eng = Engine(cfg, prof, Schedule([0]))
    cost_8 = eng.evaluate(Assignment(workshop=["矩"], power=[[], [], []]))
    assert cost_8.detail["workshop"][0]["mood_cost"] == 6.0
    assert cost_8.detail["workshop"][0]["crafts/day"] == 4.0

    cfg["workshop"]["craft_mood_cost"] = 3.0
    eng = Engine(cfg, prof, Schedule([0]))
    cost_3 = eng.evaluate(Assignment(workshop=["矩"], power=[[], [], []]))
    assert cost_3.detail["workshop"][0]["mood_cost"] == 3.0
    assert cost_3.detail["workshop"][0]["crafts/day"] == 8.0


def test_workshop_fixed_mood_cost_skills_override_recipe_cost():
    """止颂/泥岩: 精英材料相应配方心情消耗按 PRTS 固定为指定值。"""
    prof = {
        "止颂": OperatorProfile("止颂", 2, 90, _DB, rarity=5),
        "泥岩": OperatorProfile("泥岩", 2, 90, _DB, rarity=6),
    }
    cfg = copy.deepcopy(CONFIG)
    cfg["workshop"]["crafts_per_day"] = 10.0
    cfg["workshop"]["craft_category"] = "elite"
    cfg["workshop"]["ap_per_byproduct"] = 0.0

    cfg["workshop"]["craft_mood_cost"] = 2.0
    zhison_low = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(workshop=["止颂"], power=[[], [], []]))
    assert zhison_low.detail["workshop"][0]["mood_cost"] == 3.0
    assert zhison_low.detail["workshop"][0]["crafts/day"] == 8.0

    cfg["workshop"]["craft_mood_cost"] = 8.0
    zhison_high = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(workshop=["止颂"], power=[[], [], []]))
    assert zhison_high.detail["workshop"][0]["mood_cost"] == 3.0
    assert zhison_high.detail["workshop"][0]["crafts/day"] == 8.0

    mudrock = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(workshop=["泥岩"], power=[[], [], []]))
    assert mudrock.detail["workshop"][0]["mood_cost"] == 2.0
    assert mudrock.detail["workshop"][0]["crafts/day"] == 10.0

    cfg["workshop"]["craft_category"] = "skill"
    off_category = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(workshop=["泥岩"], power=[[], [], []]))
    assert off_category.detail["workshop"][0]["mood_cost"] == 8.0
    assert off_category.detail["workshop"][0]["crafts/day"] == 3.0


def test_stainless_workshop_low_mood_dorms_reduce_mood_cost():
    """白铁心相连: 每10名心情12以下宿舍干员使4心情精英配方-1心情消耗。"""
    dorm_ops = ["夜莺", "临光", "阿米娅", "能天使", "德克萨斯", "闪灵", "星熊", "银灰", "推进之王", "艾雅法拉"]
    prof = _prof("白铁", *dorm_ops, rarity=6)
    cfg = copy.deepcopy(CONFIG)
    cfg["workshop"]["crafts_per_day"] = 10.0
    cfg["workshop"]["craft_category"] = "elite"
    cfg["workshop"]["craft_mood_cost"] = 4.0
    cfg["workshop"]["ap_per_byproduct"] = 0.0
    eng = Engine(cfg, prof, Schedule([0]))
    asg = Assignment(
        workshop=["白铁"],
        dormitory=[dorm_ops[0:3], dorm_ops[3:6], dorm_ops[6:8], dorm_ops[8:10]],
        power=[[], [], []],
    )

    with _override(eng, mood={nm: 11.9 for nm in dorm_ops[:9]}):
        nine_low = eng.evaluate(asg)
    assert nine_low.detail["workshop"][0]["mood_cost"] == 4.0
    assert nine_low.detail["workshop"][0]["crafts/day"] == 6.0

    with _override(eng, mood={nm: 12.0 for nm in dorm_ops}):
        ten_low = eng.evaluate(asg)
    assert ten_low.detail["workshop"][0]["mood_cost"] == 3.0
    assert ten_low.detail["workshop"][0]["crafts/day"] == 8.0


def test_workshop_byproduct_original_mood_cost_condition():
    """原始心情消耗为N的副产品技能只按原始配方消耗触发。"""
    prof = {
        "罗小黑": OperatorProfile("罗小黑", 2, 90, _DB, rarity=5),
        "休谟斯": OperatorProfile("休谟斯", 2, 90, _DB, rarity=4),
    }
    cfg = copy.deepcopy(CONFIG)
    cfg["workshop"]["crafts_per_day"] = 10.0
    cfg["workshop"]["craft_category"] = "elite"
    cfg["workshop"]["base_byproduct_chance"] = 0.0
    cfg["workshop"]["ap_per_byproduct"] = 1.0
    eng = Engine(cfg, prof, Schedule([0]))

    cfg["workshop"]["craft_mood_cost"] = 8.0
    cost_8 = eng.evaluate(Assignment(workshop=["罗小黑"], power=[[], [], []]))
    assert cost_8.detail["workshop"][0]["byproduct%"] == 50.0
    assert cost_8.detail["workshop"][0]["mood_cost"] == 2.0

    cfg["workshop"]["craft_mood_cost"] = 4.0
    cost_4 = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(workshop=["罗小黑"], power=[[], [], []]))
    assert cost_4.detail["workshop"][0]["byproduct%"] == 0.0
    assert cost_4.detail["workshop"][0]["mood_cost"] == 1.0

    cfg["workshop"]["craft_mood_cost"] = 2.0
    humus_cost_2 = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(workshop=["休谟斯"], power=[[], [], []]))
    assert humus_cost_2.detail["workshop"][0]["byproduct%"] == 90.0

    cfg["workshop"]["craft_mood_cost"] = 4.0
    humus_cost_4 = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(workshop=["休谟斯"], power=[[], [], []]))
    assert humus_cost_4.detail["workshop"][0]["byproduct%"] == 50.0

    cfg["workshop"]["craft_mood_cost"] = 2.0
    cfg["workshop"]["craft_category"] = "skill"
    humus_skill = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(workshop=["休谟斯"], power=[[], [], []]))
    assert humus_skill.detail["workshop"][0]["byproduct%"] == 50.0


def test_workshop_fixed_byproduct_type_is_reported_for_t3_elite_skills():
    """莱伊/洛洛/蚀清: T3 精英材料副产品指定类型应在 detail 中显式标出。"""
    prof = _prof("莱伊", "洛洛", "蚀清", rarity=5)
    cfg = copy.deepcopy(CONFIG)
    cfg["workshop"]["crafts_per_day"] = 1.0
    cfg["workshop"]["craft_category"] = "elite"
    cfg["workshop"]["base_byproduct_chance"] = 0.0
    cfg["workshop"]["ap_per_byproduct"] = 0.0
    eng = Engine(cfg, prof, Schedule([0]))

    assert eng.evaluate(Assignment(workshop=["莱伊"], power=[[], [], []])).detail["workshop"][0]["fixed_byproduct"] == ["莱伊:固源岩组"]
    assert eng.evaluate(Assignment(workshop=["洛洛"], power=[[], [], []])).detail["workshop"][0]["fixed_byproduct"] == ["洛洛:聚酸酯组"]
    assert eng.evaluate(Assignment(workshop=["蚀清"], power=[[], [], []])).detail["workshop"][0]["fixed_byproduct"] == ["蚀清:异铁组"]

    cfg["workshop"]["craft_category"] = "skill"
    off_category = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(workshop=["莱伊"], power=[[], [], []]))
    assert off_category.detail["workshop"][0]["fixed_byproduct"] == []

    with _override(eng, fatigued={"莱伊"}):
        fatigued = eng.evaluate(Assignment(workshop=["莱伊"], power=[[], [], []]))
    assert fatigued.detail["workshop"][0]["fixed_byproduct"] == []


def test_blemishine_workshop_lmd_cost_reduction_is_optional_and_category_gated():
    """瑕光 精打细算: 配置加工龙门币成本后, 精英材料按实际加工次数折算节省。"""
    prof = _prof("瑕光", rarity=6)
    cfg = copy.deepcopy(CONFIG)
    cfg["workshop"]["crafts_per_day"] = 10.0
    cfg["workshop"]["craft_category"] = "elite"
    cfg["workshop"]["craft_mood_cost"] = 8.0
    cfg["workshop"]["craft_lmd_cost"] = 400.0
    cfg["workshop"]["base_byproduct_chance"] = 0.0
    cfg["workshop"]["ap_per_byproduct"] = 0.0
    eng = Engine(cfg, prof, Schedule([0]))

    elite = eng.evaluate(Assignment(workshop=["瑕光"], power=[[], [], []]))
    assert elite.detail["workshop"][0]["mood_cost"] == 4.0
    assert elite.detail["workshop"][0]["crafts/day"] == 6.0
    assert elite.detail["workshop"][0]["lmd_saving_ap/day"] == 8.64
    assert abs(elite.breakdown["加工站"] - 8.64) < 1e-9

    cfg["workshop"]["craft_category"] = "skill"
    skill = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(workshop=["瑕光"], power=[[], [], []]))
    assert skill.detail["workshop"][0]["lmd_saving_ap/day"] == 0.0
    assert skill.breakdown["加工站"] == 0.0


def test_prts_baseline_mechanics():
    """PRTS基础项: 工作位+1%/人、发电+5%、控制中枢每人-0.05、会客/办公室基础加成。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 0.0
    prof = _prof("能天使", "夜莺", rarity=6)
    eng = Engine(cfg, prof, Schedule([0, 6, 12, 18]))

    man = eng.evaluate(Assignment(manufacture=[("作战记录", ["能天使"])], power=[[], [], []]))
    assert man.detail["manufacture"][0]["prod%"] == 1.0
    assert cfg["manufacture"]["capacity_volume"] == 54
    assert cfg["manufacture"]["lines"]["作战记录"]["volume_per_item"] == 5
    cfg["manufacture"]["lines"]["作战记录"]["base_minutes_per_item"] = 60
    capped_record = Engine(cfg, prof, Schedule([0])).evaluate(
        Assignment(manufacture=[("作战记录", ["能天使"])], power=[[], [], []])
    )
    assert capped_record.detail["manufacture"][0]["items/day"] == 10.8
    cfg_level2 = copy.deepcopy(CONFIG)
    cfg_level2["power"]["drone_per_hour_base"] = 0.0
    cfg_level2["manufacture"]["level"] = 2
    cfg_level2["manufacture"]["lines"]["作战记录"]["base_minutes_per_item"] = 60
    locked_level2_record = Engine(cfg_level2, prof, Schedule([0])).evaluate(
        Assignment(manufacture=[("作战记录", ["能天使"])], power=[[], [], []])
    )
    assert locked_level2_record.detail["manufacture"][0]["locked_by_level"] is True
    assert locked_level2_record.detail["manufacture"][0]["required_level"] == 3
    cfg_level2["manufacture"]["lines"]["赤金"]["base_minutes_per_item"] = 60
    capped_level2_gold = Engine(cfg_level2, prof, Schedule([0])).evaluate(
        Assignment(manufacture=[("赤金", ["能天使"])], power=[[], [], []])
    )
    assert capped_level2_gold.detail["manufacture"][0]["items/day"] == 18.0

    trade = eng.evaluate(Assignment(trading=[["夜莺"]], power=[[], [], []]))
    assert trade.detail["trading"][0]["eff%"] == 1.0

    cfg2 = copy.deepcopy(CONFIG)
    eng2 = Engine(cfg2, prof, Schedule([0, 6, 12, 18]))
    power = eng2.evaluate(Assignment(power=[["能天使"]]))
    assert power.detail["drones"]["per_day"] == 252  # 10/h * 24h * 1.05
    assert power.detail["drones"]["minutes/day"] == 756.0  # PRTS: 3 min accelerated per drone

    eng3 = Engine(CONFIG, prof, Schedule([0]))
    assert eng3._room_reduction(1) == 0.0
    assert eng3._room_reduction(2) == 0.05
    assert eng3._room_reduction(3) == 0.1
    ctrl_asg = Assignment(control=["能天使", "夜莺"])
    eng3.evaluate(ctrl_asg)
    assert eng3._control_reduction(ctrl_asg) == 0.10
    with _override(eng3, frac={"能天使": 1.0, "夜莺": 0.0}):
        assert eng3._control_reduction(ctrl_asg) == 0.05

    meeting = eng.evaluate(Assignment(meeting=["能天使"], dormitory=[["夜莺"]], power=[[], [], []]))
    expected_clue_ap = (0.05 * 24 * (1 + (15 + 11 + 5 + 5 + 16) / 100) + 1) * CONFIG["material_values_ap"]["线索"]
    assert abs(meeting.breakdown["会客室(线索)"] - expected_clue_ap) < 1e-6
    low_amb_cfg = copy.deepcopy(CONFIG)
    low_amb_cfg["dormitory"]["ambiance"] = 1000
    low_amb = Engine(low_amb_cfg, prof, Schedule([0, 6, 12, 18]))
    one_dorm = low_amb.evaluate(Assignment(meeting=["能天使"], dormitory=[["夜莺"]], power=[[], [], []]))
    one_expected = (0.05 * 24 * (1 + (11 + 5 + 5 + 16) / 100) + 1) * CONFIG["material_values_ap"]["线索"]
    assert abs(one_dorm.breakdown["会客室(线索)"] - one_expected) < 1e-6
    four_dorms = low_amb.evaluate(Assignment(meeting=["能天使"], dormitory=[["夜莺"], [], [], []], power=[[], [], []]))
    four_expected = (0.05 * 24 * (1 + (15 + 11 + 5 + 5 + 16) / 100) + 1) * CONFIG["material_values_ap"]["线索"]
    assert abs(four_dorms.breakdown["会客室(线索)"] - four_expected) < 1e-6

    hire = eng.evaluate(Assignment(hire=["能天使"], dormitory=[["夜莺"]], power=[[], [], []]))
    assert cfg["hire"]["contact_per_hour_base"] == 1.0 / 12.0
    expected_hire_ap = cfg["hire"]["contact_per_hour_base"] * 24 * 1.05 * cfg["hire"]["ap_per_refresh"]
    assert abs(hire.breakdown["办公室(公招)"] - expected_hire_ap) < 1e-6


def test_manufacture_requires_active_staff_to_produce():
    """PRTS: 制造站只有指定制造方案且进驻干员后才会开始制造。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 10.0
    cfg["material_values_ap"]["赤金"] = 1.0
    cfg["trading"]["external_gold_per_day"] = 0
    prof = _prof("夜莺", rarity=6)

    empty = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(manufacture=[("赤金", [])], power=[[]]))
    assert empty.detail["manufacture"][0]["active_ops"] == 0.0
    assert empty.detail["manufacture"][0]["items/day"] == 0.0
    assert empty.detail["gold_supply/day"] == 0.0
    assert empty.detail["drones"]["line"] is None

    staffed = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(manufacture=[("赤金", ["夜莺"])], power=[[]]))
    assert staffed.detail["manufacture"][0]["active_ops"] > 0.0
    assert staffed.detail["manufacture"][0]["items/day"] > 0.0


def test_staffed_facility_output_requires_active_staff():
    """贸易/会客/办公室: 空房或有效工作占比为0时不推进基础产出。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 0.0
    cfg["manufacture"]["capacity_volume"] = 10000
    cfg["manufacture"]["lines"]["赤金"]["base_minutes_per_item"] = 1.0
    cfg["meeting"]["clue_per_hour_base"] = 10.0
    cfg["hire"]["contact_per_hour_base"] = 1.0
    cfg["material_values_ap"]["线索"] = 1.0
    cfg["hire"]["ap_per_refresh"] = 1.0
    prof = _prof("能天使", "夜莺", "闪灵", "安哲拉", rarity=6)
    eng = Engine(cfg, prof, Schedule([0]))

    empty_trade = eng.evaluate(Assignment(
        manufacture=[("赤金", ["能天使"])],
        trading=[[]],
        power=[[], [], []],
    ))
    assert empty_trade.detail["trading"][0]["active_ops"] == 0.0
    assert empty_trade.detail["trading"][0]["赤金单/day"] == 0.0
    assert empty_trade.breakdown["贸易站(龙门币)"] == 0.0

    with _override(eng, frac={"能天使": 1.0, "夜莺": 0.0, "闪灵": 0.0, "安哲拉": 0.0}):
        inactive = eng.evaluate(Assignment(
            manufacture=[("赤金", ["能天使"])],
            trading=[["夜莺"]],
            meeting=["闪灵"],
            hire=["安哲拉"],
            power=[[], [], []],
        ))

    assert inactive.detail["trading"][0]["active_ops"] == 0.0
    assert inactive.detail["trading"][0]["赤金单/day"] == 0.0
    assert inactive.detail["meeting"][0]["active_ops"] == 0.0
    assert inactive.detail["meeting"][0]["clues/day"] == 0.0
    assert inactive.detail["hire"][0]["active_ops"] == 0.0
    assert inactive.detail["hire"][0]["refreshes/day"] == 0.0


def test_drones_are_capped_per_collection_window_by_storage_limit():
    """无人机持有上限235: 充能超出上限会丢弃, 但每次上线消耗后下一窗口重新累计。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 10.0
    cfg["power"]["base_charge_bonus_per_operator"] = 0.0
    prof = {}

    one_login = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(power=[[]]))
    assert one_login.detail["drones"]["per_day"] == 235.0
    assert one_login.detail["drones"]["minutes/day"] == 705.0

    four_logins = Engine(cfg, prof, Schedule([0, 6, 12, 18])).evaluate(Assignment(power=[[]]))
    assert four_logins.detail["drones"]["per_day"] == 240.0
    assert four_logins.detail["drones"]["minutes/day"] == 720.0


def test_drone_assist_requires_control_center_level_three():
    """PRTS: 控制中枢Lv3才解锁制造/贸易的无人机协助。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["control"]["level"] = 2
    cfg["power"]["drone_per_hour_base"] = 10.0
    cfg["mood"]["base_drain_per_hour"] = 0.0
    cfg["manufacture"]["capacity_volume"] = 10000
    cfg["manufacture"]["lines"]["作战记录"]["base_minutes_per_item"] = 60.0
    prof = _prof("夜莺", rarity=6)
    asg = Assignment(manufacture=[("作战记录", ["夜莺"])], power=[[], []])

    locked = Engine(cfg, prof, Schedule([0])).evaluate(asg)
    assert locked.detail["drones"]["per_day"] == 235.0
    assert locked.detail["drones"]["assist_unlocked"] is False
    assert locked.detail["drones"]["minutes/day"] == 0.0
    assert locked.detail["drones"]["line"] is None
    assert locked.detail["manufacture"][0]["items/day"] == 24.2

    cfg["control"]["level"] = 3
    unlocked = Engine(cfg, prof, Schedule([0])).evaluate(asg)
    assert unlocked.detail["drones"]["assist_unlocked"] is True
    assert unlocked.detail["drones"]["minutes/day"] == 705.0
    assert unlocked.detail["drones"]["line"] == "作战记录"
    assert unlocked.detail["drones"]["extra_items/day"] == 11.8


def test_meeting_clues_require_staff_and_are_capped_by_own_storage_limit():
    """会客室: 自有线索库上限按上线收取间隔生效, 不是全天总上限。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["meeting"]["clue_per_hour_base"] = 10.0
    cfg["mood"]["base_drain_per_hour"] = 0.0
    cfg["material_values_ap"]["线索"] = 1.0
    prof = _prof("夜莺", rarity=6)

    no_staff = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(power=[[], [], []]))
    assert no_staff.breakdown["会客室(线索)"] == 0.0

    one_login = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(meeting=["夜莺"], power=[[], [], []]))
    assert one_login.detail["meeting"][0]["collected/day"] == 10.0
    assert one_login.detail["meeting"][0]["daily_bonus/day"] == 0.0
    assert one_login.detail["meeting"][0]["clues/day"] == 10.0
    assert one_login.breakdown["会客室(线索)"] == 10.0

    four_logins = Engine(cfg, prof, Schedule([0, 6, 12, 18])).evaluate(
        Assignment(meeting=["夜莺"], power=[[], [], []])
    )
    assert four_logins.detail["meeting"][0]["collected/day"] == 40.0
    assert four_logins.detail["meeting"][0]["daily_bonus/day"] == 0.0
    assert four_logins.detail["meeting"][0]["clues/day"] == 40.0


def test_meeting_daily_clue_shares_own_storage_capacity():
    """会客室每日线索和自动搜集共用自有线索库容量, 不能越过上限入库。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["meeting"]["clue_limit"] = 10.0
    cfg["meeting"]["daily_clue_if_staffed"] = 1.0
    cfg["mood"]["base_drain_per_hour"] = 0.0
    cfg["material_values_ap"]["线索"] = 1.0
    prof = _prof("夜莺", rarity=6)
    asg = Assignment(meeting=["夜莺"], power=[[], [], []])

    cfg["meeting"]["clue_per_hour_base"] = 3.0
    full_before_bonus = Engine(cfg, prof, Schedule([0])).evaluate(asg)
    assert full_before_bonus.detail["meeting"][0]["collected/day"] == 10.0
    assert full_before_bonus.detail["meeting"][0]["daily_bonus/day"] == 0.0
    assert full_before_bonus.detail["meeting"][0]["clues/day"] == 10.0

    cfg["meeting"]["clue_per_hour_base"] = 1.5
    bonus_before_full = Engine(cfg, prof, Schedule([0])).evaluate(asg)
    assert bonus_before_full.detail["meeting"][0]["collected/day"] == 9.0
    assert bonus_before_full.detail["meeting"][0]["daily_bonus/day"] == 1.0
    assert bonus_before_full.detail["meeting"][0]["clues/day"] == 10.0


def test_hire_refreshes_are_capped_by_resource_limit():
    """办公室人力资源余量上限为3: 达到上限后直到上线收取前暂停工作。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["hire"]["contact_per_hour_base"] = 1.0
    cfg["hire"]["base_eff_per_operator"] = 0.0
    cfg["hire"]["ap_per_refresh"] = 1.0
    cfg["mood"]["base_drain_per_hour"] = 0.0
    prof = _prof("夜莺", rarity=6)

    no_staff = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(power=[[], [], []]))
    assert no_staff.detail["hire"] == []
    assert no_staff.breakdown["办公室(公招)"] == 0.0

    one_login = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(hire=["夜莺"], power=[[], [], []]))
    assert one_login.detail["hire"][0]["refreshes/day"] == 3.0
    assert one_login.detail["hire"][0]["active_ops"] == 0.125
    assert one_login.breakdown["办公室(公招)"] == 3.0

    four_logins = Engine(cfg, prof, Schedule([0, 6, 12, 18])).evaluate(Assignment(hire=["夜莺"], power=[[], [], []]))
    assert four_logins.detail["hire"][0]["refreshes/day"] == 12.0
    assert four_logins.detail["hire"][0]["active_ops"] == 0.5
    assert four_logins.breakdown["办公室(公招)"] == 12.0


def test_conditional_power_station_charge_skills_are_gated():
    """发电站条件充能: 条件 +5% 不应无条件生效, 条件对象也必须有效工作。"""
    cfg = copy.deepcopy(CONFIG)
    prof = _prof(
        "GALLUS²", "Lancet-2", "Friston-3", "凯尔希",
        "CONFESS-47", "空弦", "PhonoR-0", "逻各斯",
        rarity=6,
    )
    eng = Engine(cfg, prof, Schedule([0]))

    gallus_solo = eng.evaluate(Assignment(power=[["GALLUS²"]]))
    assert gallus_solo.detail["power"][0]["charge%"] == 15.0
    assert gallus_solo.detail["drones"]["per_day"] == 235

    gallus_pair = eng.evaluate(Assignment(power=[["GALLUS²"], ["Lancet-2"]]))
    assert sum(p["charge%"] for p in gallus_pair.detail["power"]) == 35.0
    assert gallus_pair.detail["drones"]["per_day"] == 235

    with _override(eng, frac={"GALLUS²": 1.0, "Lancet-2": 0.0}):
        inactive_platform = eng.evaluate(Assignment(power=[["GALLUS²"], ["Lancet-2"]]))
    assert sum(p["charge%"] for p in inactive_platform.detail["power"]) == 15.0
    assert inactive_platform.detail["drones"]["per_day"] == 235

    friston_solo = eng.evaluate(Assignment(power=[["Friston-3"]]))
    assert friston_solo.detail["power"][0]["charge%"] == 15.0
    assert friston_solo.detail["drones"]["per_day"] == 235

    friston_kal = eng.evaluate(Assignment(control=["凯尔希"], power=[["Friston-3"]]))
    assert friston_kal.detail["power"][0]["charge%"] == 20.0
    assert friston_kal.detail["drones"]["per_day"] == 235

    with _override(eng, frac={"Friston-3": 1.0, "凯尔希": 0.0}):
        inactive_kal = eng.evaluate(Assignment(control=["凯尔希"], power=[["Friston-3"]]))
    assert inactive_kal.detail["power"][0]["charge%"] == 15.0
    assert inactive_kal.detail["drones"]["per_day"] == 235

    confess_solo = eng.evaluate(Assignment(power=[["CONFESS-47"]]))
    assert confess_solo.detail["power"][0]["charge%"] == 15.0

    confess_laterano = eng.evaluate(Assignment(power=[["CONFESS-47"], ["空弦"]]))
    assert confess_laterano.detail["power"][0]["charge%"] == 20.0
    assert confess_laterano.detail["power"][1]["charge%"] == 5.0

    with _override(eng, frac={"CONFESS-47": 1.0, "空弦": 0.0}):
        inactive_laterano = eng.evaluate(Assignment(power=[["CONFESS-47"], ["空弦"]]))
    assert inactive_laterano.detail["power"][0]["charge%"] == 15.0

    phonor_solo = eng.evaluate(Assignment(power=[["PhonoR-0"]]))
    assert phonor_solo.detail["power"][0]["charge%"] == 15.0

    phonor_logos = eng.evaluate(Assignment(training=["逻各斯"], power=[["PhonoR-0"]]))
    assert phonor_logos.detail["power"][0]["charge%"] == 20.0

    with _override(eng, frac={"PhonoR-0": 1.0, "逻各斯": 0.0}):
        inactive_logos = eng.evaluate(Assignment(training=["逻各斯"], power=[["PhonoR-0"]]))
    assert inactive_logos.detail["power"][0]["charge%"] == 15.0


def test_muelsyse_power_charge_counts_active_other_rhine_lab_members():
    """缪尔赛思·生态科主任: 只按基建内仍有效工作的其他莱茵生命干员额外充能。"""
    prof = _prof("缪尔赛思", "多萝西", "白面鸮", "夜莺", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0]))

    solo = eng.evaluate(Assignment(power=[["缪尔赛思"]]))
    assert solo.detail["power"][0]["charge%"] == 15.0

    one_rhine = eng.evaluate(Assignment(power=[["缪尔赛思"]], manufacture=[("赤金", ["多萝西"])]))
    assert one_rhine.detail["power"][0]["charge%"] == 18.0

    two_rhine = eng.evaluate(Assignment(
        power=[["缪尔赛思"]],
        manufacture=[("赤金", ["多萝西"])],
        hire=["白面鸮"],
        meeting=["夜莺"],
    ))
    assert two_rhine.detail["power"][0]["charge%"] == 21.0

    with _override(eng, frac={"缪尔赛思": 1.0, "多萝西": 1.0, "白面鸮": 0.0, "夜莺": 1.0}):
        inactive_rhine = eng.evaluate(Assignment(
            power=[["缪尔赛思"]],
            manufacture=[("赤金", ["多萝西"])],
            hire=["白面鸮"],
            meeting=["夜莺"],
        ))
    assert inactive_rhine.detail["power"][0]["charge%"] == 18.0


def test_greyy_lightningbearer_drone_cap_charge_uses_base_drone_limit():
    """承曦格雷伊·巡线框架按配置无人机上限换算: floor(cap/10)*1%。"""
    cfg = copy.deepcopy(CONFIG)
    prof = _prof("承曦格雷伊", rarity=5)
    eng = Engine(cfg, prof, Schedule([0]))

    power = eng.evaluate(Assignment(power=[["承曦格雷伊"]]))

    assert eng._resolved["承曦格雷伊"].power == 23.0
    assert power.detail["power"][0]["charge%"] == 28.0
    assert power.detail["drones"]["per_day"] == 235.0
    assert power.detail["drones"]["minutes/day"] == 705.0  # 每架固定折算3分钟加速

    cfg["power"]["drone_cap"] = 120
    lower_cap = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(power=[["承曦格雷伊"]]))
    assert lower_cap.detail["power"][0]["charge%"] == 17.0
    assert lower_cap.detail["drones"]["per_day"] == 120.0
    assert lower_cap.detail["drones"]["minutes/day"] == 360.0


def test_drones_can_accelerate_trading_orders():
    """无人机协助可用于贸易站: 每架固定减少3分钟基础耗时, 不受订单效率放大。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 1.0
    cfg["power"]["base_charge_bonus_per_operator"] = 0.0
    cfg["manufacture"]["lines"]["赤金"]["base_minutes_per_item"] = 1000.0
    cfg["trading"]["gold_order"]["base_minutes_per_order"] = 1.0
    cfg["trading"]["gold_order"]["gold_per_order"] = 0.002
    cfg["trading"]["gold_order"]["lmd_per_order"] = 100000.0
    cfg["trading"]["base_eff_per_operator"] = 100.0
    cfg["trading"]["order_limit"] = 1
    prof = {
        "夜莺": OperatorProfile("夜莺", 2, 90, _DB),
        "能天使": OperatorProfile("能天使", 2, 90, _DB, rarity=6),
    }
    eng = Engine(cfg, prof, Schedule([0]))

    res = eng.evaluate(Assignment(manufacture=[("赤金", ["能天使"])], trading=[["夜莺"]], power=[[]]))

    assert res.detail["drones"]["minutes/day"] == 72.0
    assert res.detail["drones"]["line"] == "贸易站1"
    assert res.detail["drones"]["extra_items/day"] == 72.0
    assert "无人机加速" in res.detail["trading"][0]["特殊"]


def test_drones_choose_manufacture_room_by_base_time_value():
    """无人机制造候选按基础耗时价值选, 不能被当前站生产力%二次放大。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 1.0
    cfg["power"]["base_charge_bonus_per_operator"] = 0.0
    prof = _prof("食铁兽", "Castle-3", "夜莺", rarity=5)
    eng = Engine(cfg, prof, Schedule([0]))

    res = eng.evaluate(Assignment(
        manufacture=[("作战记录", ["食铁兽", "Castle-3"]), ("赤金", ["夜莺"])],
        power=[[], [], []],
    ))

    assert res.detail["manufacture"][0]["prod%"] > 60.0
    assert res.detail["drones"]["line"] == "赤金"


def test_control_center_recovery_scope():
    """临光 左膀右臂: 控制中枢内恢复, 不应算作宿舍全局恢复。"""
    prof = _prof("临光", "夜莺", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(control=["临光"], dormitory=[["夜莺"]], power=[[], [], []])
    res = eng.evaluate(asg)
    assert res.detail["globals"]["control_recover"] == 0.05
    assert res.detail["globals"]["recover"] == 0.0
    assert abs(eng._control_self_reduction(asg) - 0.10) < 1e-9


def test_control_center_reduction_uses_active_staff_plus_control_skills():
    """控制中枢自身减耗 = 0.05 * 活跃中枢干员数 + 中枢内恢复技能。"""
    prof = _prof("临光", "夜莺", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(control=["临光", "夜莺"], power=[[], [], []])
    eng.evaluate(asg)

    assert abs(eng._control_self_reduction(asg) - 0.15) < 1e-9

    with _override(eng, frac={"临光": 1.0, "夜莺": 0.0}):
        eng.evaluate(asg)
        assert abs(eng._control_self_reduction(asg) - 0.10) < 1e-9


def test_mlynar_other_facility_recovery_is_not_control_recovery():
    """玛恩纳 公事公办: 只给部分其他设施恢复, PRTS脚注额外+0.05不重复叠加。"""
    prof = _prof("玛恩纳", "临光", "夜莺", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(control=["玛恩纳"], manufacture=[("作战记录", ["夜莺"])], power=[[], [], []])
    res = eng.evaluate(asg)

    assert abs(res.detail["globals"]["other_recover"] - 0.15) < 1e-9
    assert res.detail["globals"]["control_recover"] == 0.05  # only 独善其身
    assert abs(eng._control_self_reduction(asg) - 0.10) < 1e-9

    with_nearl = eng.evaluate(
        Assignment(control=["玛恩纳", "临光"], manufacture=[("作战记录", ["夜莺"])], power=[[], [], []])
    )
    assert abs(with_nearl.detail["globals"]["other_recover"] - 0.15) < 1e-9


def test_wisadel_babel_banner_extra_other_recovery_requires_doctor():
    """维什戴尔 巴别塔之帜: 魔王进驻中枢时其他设施恢复从+0.1提升到+0.2。"""
    prof = _prof("维什戴尔", "魔王", "夜莺", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    solo = eng.evaluate(Assignment(control=["维什戴尔"], manufacture=[("作战记录", ["夜莺"])], power=[[], [], []]))
    assert solo.detail["globals"]["other_recover"] == 0.1

    with_doctor = eng.evaluate(
        Assignment(control=["维什戴尔", "魔王"], manufacture=[("作战记录", ["夜莺"])], power=[[], [], []])
    )
    assert with_doctor.detail["globals"]["other_recover"] == 0.2


def test_control_center_room_drain_delta_affects_control_staff():
    """阿 神经质: 控制中枢内全员心情消耗增加应影响中枢驻员自身持续性。"""
    prof = _prof("阿", "临光", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(control=["阿", "临光"], power=[[], [], []])
    eng.evaluate(asg)

    red = eng._control_self_reduction(asg)
    assert red < -1.3
    assert eng._net_drain("临光", "control", red) > 2.3


def test_control_center_other_staff_drain_delta_excludes_provider():
    """艾拉 反抗者: 只增加控制中枢内除自身外干员的心情消耗。"""
    prof = _prof("艾拉", "临光", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(control=["艾拉", "临光"], power=[[], [], []])
    eng.evaluate(asg)

    assert eng._resolved["艾拉"].drain_delta == 0.0
    assert eng._resolved["艾拉"].room_drain_delta_other == 0.25
    ella_red = eng._control_effective_reduction_for("艾拉", asg)
    nearl_red = eng._control_effective_reduction_for("临光", asg)
    assert abs((ella_red - nearl_red) - 0.25) < 1e-9
    assert abs(
        eng._net_drain("临光", "control", nearl_red)
        - eng._net_drain("艾拉", "control", ella_red)
        - 0.25
    ) < 1e-9


def test_control_center_named_recovery_targets_only_named_ops():
    """魔王 未完的故事: 只恢复自身和阿米娅, 不应变成控制中枢全员恢复。"""
    prof = _prof("魔王", "阿米娅", "临光", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(control=["魔王", "阿米娅", "临光"], power=[[], [], []])
    res = eng.evaluate(asg)

    assert res.detail["globals"]["control_recover"] == 0.05  # only 临光的全员恢复
    mon_red = eng._control_effective_reduction_for("魔王", asg)
    amiya_red = eng._control_effective_reduction_for("阿米娅", asg)
    nearl_red = eng._control_effective_reduction_for("临光", asg)
    assert abs(mon_red - nearl_red - 0.1) < 1e-9
    assert abs(amiya_red - nearl_red - 0.1) < 1e-9


def test_control_center_lee_agency_recovery_has_prts_note_values():
    """吽 坚毅随和: PRTS脚注为全员+0.05/人, 鲤氏干员定向额外+0.2/人。"""
    prof = _prof("吽", "阿", "临光", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(control=["吽", "阿", "临光"], power=[[], [], []])
    res = eng.evaluate(asg)

    assert abs(res.detail["globals"]["control_recover"] - 0.1) < 1e-9
    hung_red = eng._control_effective_reduction_for("吽", asg)
    a_red = eng._control_effective_reduction_for("阿", asg)
    nearl_red = eng._control_effective_reduction_for("临光", asg)
    assert abs(hung_red - nearl_red - 0.4) < 1e-9
    assert abs(a_red - nearl_red - 0.4) < 1e-9


def test_control_center_named_drain_targets_only_named_op():
    """祐天寺若麦 成效优先: 只增加丰川祥子心情消耗, 线索加成同种取最高。"""
    prof = _prof("祐天寺若麦", "丰川祥子", "夜莺", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    no_pair = eng.evaluate(Assignment(control=["祐天寺若麦", "夜莺"], power=[[], [], []]))
    assert no_pair.detail["globals"]["clue"] == 5.0  # 勤学苦练
    assert eng._resolved["祐天寺若麦"].drain_delta == 0.0

    asg = Assignment(control=["祐天寺若麦", "丰川祥子", "夜莺"], power=[[], [], []])
    res = eng.evaluate(asg)

    assert res.detail["globals"]["clue"] == 5.0  # 成效优先同种取最高, 不与勤学苦练叠加
    assert eng._resolved["祐天寺若麦"].drain_delta == 0.0
    wakamugi_red = eng._control_effective_reduction_for("祐天寺若麦", asg)
    sakiko_red = eng._control_effective_reduction_for("丰川祥子", asg)
    assert abs((wakamugi_red - sakiko_red) - 0.05) < 1e-9


def test_control_elite_dorm_recovery_scope():
    """电弧 无言的慈爱: 只恢复宿舍内罗德岛精英干员, 不应作为全员宿舍恢复。"""
    prof = _prof("电弧", "凯尔希", "夜莺", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    res = eng.evaluate(Assignment(control=["电弧"], dormitory=[["凯尔希", "夜莺"]], power=[[], [], []]))

    assert res.detail["globals"]["recover"] == 0.0
    assert res.detail["globals"]["recover_elite"] == 0.1


def test_control_named_work_together_and_power_platform_conditions_are_gated():
    """老鲤/布丁: 纯条件控制中枢技能不应在条件未满足时无条件生效。"""
    prof = _prof("老鲤", "阿", "布丁", "Lancet-2", "Castle-3", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    solo_li = eng.evaluate(Assignment(control=["老鲤"], power=[[], [], []]))
    assert solo_li.detail["globals"]["control_recover"] == 0.0

    a_elsewhere = eng.evaluate(Assignment(control=["老鲤"], trading=[["阿"]], power=[[], [], []]))
    assert a_elsewhere.detail["globals"]["control_recover"] == 0.0

    with_a = eng.evaluate(Assignment(control=["老鲤", "阿"], power=[[], [], []]))
    assert with_a.detail["globals"]["control_recover"] == 0.25
    with _override(eng, frac={"老鲤": 1.0, "阿": 0.0}):
        inactive_a = eng.evaluate(Assignment(control=["老鲤", "阿"], power=[[], [], []]))
    assert inactive_a.detail["globals"]["control_recover"] == 0.0

    no_platform = eng.evaluate(Assignment(control=["布丁"], power=[[], [], []]))
    assert no_platform.detail["globals"]["prod"] == {}

    one_platform = eng.evaluate(Assignment(control=["布丁"], power=[["Lancet-2"], [], []]))
    assert one_platform.detail["globals"]["prod"] == {}

    two_platforms = eng.evaluate(Assignment(control=["布丁"], power=[["Lancet-2"], ["Castle-3"], []]))
    assert two_platforms.detail["globals"]["prod"]["all"] == 2.0

    with _override(eng, frac={"布丁": 1.0, "Lancet-2": 1.0, "Castle-3": 0.0}):
        one_active_platform = eng.evaluate(Assignment(control=["布丁"], power=[["Lancet-2"], ["Castle-3"], []]))
    assert one_active_platform.detail["globals"]["prod"] == {}


def test_control_global_dorm_recovery_requires_explicit_dorm_target():
    """控制中枢宿舍恢复: 只把明确写"宿舍内...恢复"的技能算作宿舍全局恢复。"""
    prof = _prof("三角初华", "歌蕾蒂娅", "安哲拉", "夜莺", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    positive = eng.evaluate(Assignment(control=["三角初华"], dormitory=[["夜莺"]], power=[[], [], []]))
    assert positive.detail["globals"]["recover"] == 0.05

    gladiia = eng.evaluate(Assignment(control=["歌蕾蒂娅"], dormitory=[["安哲拉", "夜莺"]], power=[[], [], []]))
    assert gladiia.detail["globals"]["recover"] == 0.0
    assert gladiia.detail["globals"]["control_recover"] == 0.05
    assert eng._resolved["歌蕾蒂娅"].global_recover == 0.0
    assert eng._resolved["歌蕾蒂娅"].drain_delta == -0.5


def test_gladiia_tidal_watch_counts_dorm_abyssal_hunter_mood():
    """歌蕾蒂娅·潮汐守望: 宿舍内深海猎人提供自身恢复, 满心情时额外恢复。"""
    prof = _prof("歌蕾蒂娅", "斯卡蒂", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(control=["歌蕾蒂娅"], dormitory=[["斯卡蒂"]], power=[[], [], []])

    eng.evaluate(asg)
    assert eng._resolved["歌蕾蒂娅"].drain_delta == -0.5

    with _override(eng, mood={"歌蕾蒂娅": 24.0, "斯卡蒂": 23.0}):
        eng.evaluate(asg)
    assert eng._resolved["歌蕾蒂娅"].drain_delta == 0.0

    no_dorm_hunter = eng.evaluate(Assignment(control=["歌蕾蒂娅"], power=[[], [], []]))
    assert eng._resolved["歌蕾蒂娅"].drain_delta == 0.5
    assert no_dorm_hunter.detail["globals"]["control_recover"] == 0.05


def test_intermediate_product_pools_and_multi_resource_scale():
    """梦境/记忆碎片应保留池并转感知; 闪击应同时吃情报储备和乌萨斯特饮。"""
    prof = _prof("爱丽丝", "絮雨", "夜莺", "闪击", "灰烬", "战车", "霜华", "早露", "凛冬", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    pools_asg = Assignment(hire=["絮雨"], dormitory=[["爱丽丝", "夜莺"]], power=[[], [], []])
    eng.evaluate(pools_asg)
    assert eng._ctx.pools["梦境"] == 5
    assert eng._ctx.pools["记忆碎片"] == 30
    assert eng._ctx.pools["感知信息"] == 35

    blitz_asg = Assignment(
        control=["灰烬", "战车", "霜华", "早露", "凛冬"],
        hire=["闪击"],
        power=[[], [], []],
    )
    eng.evaluate(blitz_asg)
    assert eng._ctx.pools["情报储备"] == 3
    assert eng._ctx.pools["乌萨斯特饮"] == 2
    assert eng._resolved["闪击"].contact == 45.0  # 基底20 + 3*5 + 2*5
    with _override(eng, frac={
        "灰烬": 1.0,
        "战车": 1.0,
        "霜华": 0.0,
        "早露": 1.0,
        "凛冬": 0.0,
        "闪击": 1.0,
    }):
        eng.evaluate(blitz_asg)
    assert eng._ctx.pools["情报储备"] == 2
    assert eng._ctx.pools["乌萨斯特饮"] == 1
    assert eng._resolved["闪击"].contact == 35.0  # 基底20 + 2*5 + 1*5


def test_prts_intermediate_products_fuller_chains():
    """补齐 PRTS 中间产物主链: 小节/工程机器人/热情值/其他设施恢复/彩虹恢复缩放。"""
    # 车尔尼: 小节 -> 感知信息
    prof = _prof("车尔尼", "夜莺", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    eng.evaluate(Assignment(dormitory=[["车尔尼", "夜莺"]], power=[[], [], []]))
    assert eng._ctx.pools["小节"] == 5
    assert eng._ctx.pools["感知信息"] == 5

    # 至简: 工程机器人 -> 制造站生产力
    prof = _prof("至简", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    robot_asg = Assignment(
        manufacture=[("作战记录", ["至简"])],
        power=[[], [], []],
        dormitory=[[], [], [], []],
    )
    res = eng.evaluate(robot_asg)
    assert eng._ctx.pools["工程机器人"] == 49
    assert res.detail["manufacture"][0]["prod%"] == 31.0  # PRTS基础1 + floor(49/8)*5

    cfg = copy.deepcopy(CONFIG)
    for room in ("manufacture", "power", "meeting", "hire", "workshop", "training"):
        cfg.setdefault(room, {})["level"] = 2
    cfg["control"]["level"] = 4
    cfg["dormitory"]["level"] = 3
    lower = Engine(cfg, prof, Schedule([0, 6, 12, 18]))
    lower_res = lower.evaluate(Assignment(
        manufacture=[("赤金", ["至简"])],
        power=[[], [], []],
        dormitory=[[], [], [], []],
    ))
    assert lower._ctx.pools["工程机器人"] == 32
    assert lower_res.detail["manufacture"][0]["prod%"] == 21.0  # PRTS基础1 + floor(32/8)*5

    # 热情值消费者: 丰川祥子给贵金属制造全局生产力, 若叶睦给贸易全局效率；
    # 若叶睦与丰川祥子同中枢时, 互为半身消除自身心情消耗影响。
    prof = _prof("丰川祥子", "八幡海铃", "三角初华", "祐天寺若麦", "若叶睦", "夜莺", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    passion_asg = Assignment(
        control=["丰川祥子", "八幡海铃", "三角初华", "祐天寺若麦", "若叶睦"],
        dormitory=[["夜莺"]],
        power=[[], [], []],
    )
    res = eng.evaluate(passion_asg)
    assert eng._ctx.pools["热情值"] >= 41
    assert res.detail["globals"]["prod"]["gold"] >= 3.0
    assert res.detail["globals"]["trade_eff"] == 5.0
    assert abs(eng._resolved["丰川祥子"].drain_delta - 0.05) < 1e-9
    assert eng._resolved["若叶睦"].drain_delta == 0.0

    no_sakiko_asg = Assignment(
        control=["八幡海铃", "三角初华", "祐天寺若麦", "若叶睦"],
        dormitory=[["夜莺"]],
        power=[[], [], []],
    )
    res = eng.evaluate(no_sakiko_asg)
    assert eng._ctx.pools["热情值"] >= 41
    assert res.detail["globals"]["trade_eff"] == 5.0
    assert abs(eng._resolved["若叶睦"].drain_delta - 0.05) < 1e-9

    low_passion_prof = _prof("丰川祥子", rarity=5)
    low_passion_eng = Engine(CONFIG, low_passion_prof, Schedule([0, 6, 12, 18]))
    low_passion_eng.evaluate(Assignment(control=["丰川祥子"], power=[[], [], []]))
    assert low_passion_eng._ctx.pools.get("热情值", 0) == 0
    assert low_passion_eng._resolved["丰川祥子"].drain_delta == 0.0

    # 重岳: 人间烟火提高其他设施恢复; 彩虹小队: 控制中枢恢复按彩虹人数缩放
    prof = _prof("重岳", "桑葚", "灰烬", "战车", "霜华", "早露", "凛冬", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    other_asg = Assignment(control=["重岳"], hire=["桑葚"], power=[[], [], []])
    res = eng.evaluate(other_asg)
    assert eng._ctx.pools["人间烟火"] >= 35
    assert abs(res.detail["globals"]["other_recover"] - 0.10) < 1e-9

    rainbow_asg = Assignment(control=["灰烬", "战车", "霜华"], power=[[], [], []])
    res = eng.evaluate(rainbow_asg)
    assert abs(res.detail["globals"]["control_recover"] - 0.15) < 1e-9

    prof = _prof("灵知", "银灰", "圣聆初雪", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    kjerag_asg = Assignment(control=["灵知", "银灰", "圣聆初雪"], power=[[], [], []])
    res = eng.evaluate(kjerag_asg)
    assert abs(res.detail["globals"]["control_recover"] - 0.15) < 1e-9

    # 导火索/霜华: 非百分比资源消费者与达到阈值
    prof = _prof("导火索", "霜华", "战车", "早露", "凛冬", "真理", "古米", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    drink_asg = Assignment(
        control=["战车", "早露", "凛冬", "真理", "古米"],
        manufacture=[("作战记录", ["导火索"])],
        workshop=["霜华"],
        power=[[], [], []],
    )
    res = eng.evaluate(drink_asg)
    assert eng._ctx.pools["乌萨斯特饮"] == 4
    assert eng._resolved["导火索"].capacity == 8
    assert res.detail["workshop"][0]["byproduct%"] == 65.0  # 基础50 + 达到4瓶额外15
    assert res.breakdown["加工站"] == 0.0

    cfg = copy.deepcopy(CONFIG)
    cfg["workshop"]["crafts_per_day"] = 10.0
    cfg["workshop"]["base_byproduct_chance"] = 0.10
    cfg["workshop"]["ap_per_byproduct"] = 2.0
    cfg["mood"]["base_drain_per_hour"] = 0.0
    eng = Engine(cfg, prof, Schedule([0, 6, 12, 18]))
    res = eng.evaluate(drink_asg)
    assert abs(res.breakdown["加工站"] - 15.0) < 1e-9
    assert res.detail["workshop"][0]["byproduct_chance%"] == 75.0

    # 火龙S黑角: 泰拉大陆调查团应计入怪物猎人小队人数; 新联动狩猎队不应混入木天蓼队伍。
    prof = _prof("火龙S黑角", "麒麟R夜刀", "泰拉大陆调查团", "雷狼龙S空爆", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    res = eng.evaluate(Assignment(control=["火龙S黑角"], power=[[], [], []]))
    assert eng._ctx.pools["木天蓼"] == 2
    assert res.detail["globals"]["trade_eff"] == 0.0

    res = eng.evaluate(Assignment(control=["火龙S黑角", "麒麟R夜刀"], power=[[], [], []]))
    assert eng._ctx.pools["木天蓼"] == 12
    assert res.detail["globals"]["trade_eff"] == 7.0
    assert res.detail["globals"]["prod"]["all"] == 2.0

    eng.evaluate(Assignment(control=["火龙S黑角", "泰拉大陆调查团"], power=[[], [], []]))
    assert eng._ctx.pools["木天蓼"] == 4
    with _override(eng, frac={"火龙S黑角": 1.0, "泰拉大陆调查团": 0.0}):
        eng.evaluate(Assignment(control=["火龙S黑角", "泰拉大陆调查团"], power=[[], [], []]))
    assert eng._ctx.pools["木天蓼"] == 2
    eng.evaluate(Assignment(control=["火龙S黑角", "雷狼龙S空爆"], power=[[], [], []]))
    assert eng._ctx.pools["木天蓼"] == 2

    # 塑心: 无声共鸣提高宿舍全员恢复; 铎铃: 人间烟火降低贸易站全体心情消耗
    prof = _prof("塑心", "夜莺", "闪灵", "安哲拉", "斯卡蒂", "铎铃", "桑葚", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    mental_asg = Assignment(
        dormitory=[["塑心", "夜莺", "闪灵", "安哲拉", "斯卡蒂"]],
        trading=[["铎铃"]],
        hire=["桑葚"],
        power=[[], [], []],
    )
    eng.evaluate(mental_asg)
    assert eng._ctx.pools["无声共鸣"] == 5
    assert abs(eng._resolved["塑心"].dorm_recover_all - 0.21) < 1e-9
    assert eng._ctx.pools["人间烟火"] == 30
    assert abs(eng._resolved["铎铃"].room_drain - 0.16) < 1e-9


def test_prts_intermediate_product_generic_consumers():
    """通用资源消费者: 会客/训练/制造/贸易都能吃到中间产物缩放。"""
    # 无声共鸣生产者: 深律每个非初始招募位+15; 黑键每2点无声共鸣+1%订单效率。
    prof = _prof("深律", "黑键", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    res = eng.evaluate(Assignment(hire=["深律"], trading=[["黑键"]], power=[[], [], []]))
    assert eng._ctx.pools["无声共鸣"] == 45
    assert eng._resolved["黑键"].trade_eff == 22.0
    assert res.detail["trading"][0]["eff%"] == 23.0

    # 人间烟火消费者: 赤刃陈会客每10点+1%; 余训练每1点+1%。
    prof = _prof("赤刃明霄陈", "余", "桑葚", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    eng.evaluate(Assignment(meeting=["赤刃明霄陈"], training=["余"], hire=["桑葚"], power=[[], [], []]))
    assert eng._ctx.pools["人间烟火"] == 30
    assert eng._resolved["赤刃明霄陈"].clue == 23.0  # 基础20 + floor(30/10)*1
    assert eng._resolved["余"].train_speed == 30.0

    # 情报储备消费者: 双月会客基础应按PRTS描述5%, 而不是 values 表名义10%。
    prof = _prof("双月", "灰烬", "战车", "霜华", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    eng.evaluate(Assignment(control=["灰烬", "战车", "霜华"], meeting=["双月"], power=[[], [], []]))
    assert eng._ctx.pools["情报储备"] == 3
    assert eng._resolved["双月"].clue == 20.0  # 基础5 + 3*5

    # 人间烟火 -> 巫术结晶 -> 截云制造生产力。
    prof = _prof("截云", "桑葚", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    eng.evaluate(Assignment(manufacture=[("作战记录", ["截云"])], hire=["桑葚"], power=[[], [], []]))
    assert eng._ctx.pools["巫术结晶"] == 6
    assert eng._resolved["截云"].prod["all"] == 12.0

    # 人间烟火消费者: 黍无无条件基底, 30点人间烟火按每3点+1%=10%。
    prof = _prof("黍", "桑葚", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    eng.evaluate(Assignment(manufacture=[("赤金", ["黍"])], hire=["桑葚"], power=[[], [], []]))
    assert eng._ctx.pools["人间烟火"] == 30
    assert eng._resolved["黍"].prod["all"] == 10.0

    # 木天蓼消费者: 泰拉大陆调查团贸易/制造技能。
    prof = _prof("火龙S黑角", "麒麟R夜刀", "泰拉大陆调查团", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    eng.evaluate(Assignment(control=["火龙S黑角", "麒麟R夜刀"], trading=[["泰拉大陆调查团"]], power=[[], [], []]))
    assert eng._ctx.pools["木天蓼"] == 12
    assert eng._resolved["泰拉大陆调查团"].trade_eff == 41.0  # 基础5 + 12*3

    eng.evaluate(Assignment(control=["火龙S黑角", "麒麟R夜刀"],
                            manufacture=[("作战记录", ["泰拉大陆调查团"])], power=[[], [], []]))
    assert eng._ctx.pools["木天蓼"] == 12
    assert eng._resolved["泰拉大陆调查团"].prod["all"] == 17.0  # 基础5 + 12*1
    assert eng._resolved["泰拉大陆调查团"].capacity == 8.0


def test_monster_food_chain_consumers_and_target_recovery():
    """莱欧斯小队: 森西产魔物料理, 莱欧斯/齐尔查克/玛露西尔分别按资源缩放。"""
    prof = _prof("森西", "莱欧斯", "齐尔查克", "玛露西尔", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(
        dormitory=[["森西"]],
        meeting=["莱欧斯"],
        trading=[["齐尔查克"]],
        manufacture=[("作战记录", ["玛露西尔"])],
        power=[[], [], []],
    ))

    assert eng._ctx.pools["魔物料理"] == 5.0
    assert eng._resolved["莱欧斯"].clue == 30.0  # 好奇心20 + 5层*2
    assert eng._resolved["齐尔查克"].trade_eff == 35.0  # 半身人公会代表30 + 5层*1
    assert eng._resolved["齐尔查克"].order_limit == 1.0
    assert eng._resolved["玛露西尔"].prod["all"] == 35.0  # 差遣使魔30 + 5层*1

    sensi_buff = next(b for b in prof["森西"].room_buffs["dormitory"] if b.buff_name == "资深料理人")
    assert dorm_target_extra_recover(sensi_buff, ["森西", "莱欧斯"]) == 0.15


def test_bellone_vigil_trade_pair_conditions_are_separate():
    """贝洛内: 伺夜在基建内只给效率额外; 同贸易站才给未偿还的债务订单上限/心情。"""
    prof = _prof("贝洛内", "伺夜", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(trading=[["贝洛内"]], power=[[], [], []]))
    assert eng._resolved["贝洛内"].trade_eff == 30.0
    assert eng._resolved["贝洛内"].order_limit == 0.0
    assert eng._resolved["贝洛内"].drain_delta == 0.0

    eng.evaluate(Assignment(trading=[["贝洛内"], ["伺夜"]], power=[[], [], []]))
    assert eng._resolved["贝洛内"].trade_eff == 40.0
    assert eng._resolved["贝洛内"].order_limit == 0.0
    assert eng._resolved["贝洛内"].drain_delta == 0.0

    eng.evaluate(Assignment(trading=[["贝洛内", "伺夜"]], power=[[], [], []]))
    assert eng._resolved["贝洛内"].trade_eff == 40.0
    assert eng._resolved["贝洛内"].order_limit == 2.0
    assert eng._resolved["贝洛内"].drain_delta == -0.1

    with _override(eng, frac={"贝洛内": 1.0, "伺夜": 0.0}):
        eng.evaluate(Assignment(trading=[["贝洛内", "伺夜"]], power=[[], [], []]))
    assert eng._resolved["贝洛内"].trade_eff == 30.0
    assert eng._resolved["贝洛内"].order_limit == 0.0
    assert eng._resolved["贝洛内"].drain_delta == 0.0


def test_training_initial_progress_is_not_plain_speed():
    """PRTS 注释: 艾丽妮/逻各斯 -50% 实际为下次训练开始立即完成50%进度。"""
    prof = _prof("艾丽妮", "逻各斯", "星熊", "夜莺", rarity=6)

    assert prof["艾丽妮"].stat("training").train_speed == 30.0
    assert prof["艾丽妮"].stat("training").train_initial_progress == 50.0
    assert prof["逻各斯"].stat("training").train_initial_progress == 50.0
    assert prof["星熊"].stat("training").train_speed == 60.0
    assert prof["星熊"].stat("training").train_initial_progress == 0.0

    cfg = copy.deepcopy(CONFIG)
    cfg["training"]["ap_value_per_hour"] = 1.0
    cfg["training"]["base_session_hours"] = 8.0
    cfg["mood"]["base_drain_per_hour"] = 0.0
    eng = Engine(cfg, prof, Schedule([0]))

    irene = eng.evaluate(Assignment(training=["艾丽妮"], power=[[], [], []]))
    assert abs(irene.breakdown["训练室"] - 36.4) < 1e-9  # 24h * (100%+5%基础+30%) + 8h训练的50%初始进度
    assert irene.detail["training"][0]["speed%"] == 35.0
    assert irene.detail["training"][0]["initial_progress%"] == 50.0
    assert irene.detail["training"][0]["base_session_hours"] == 8.0

    hoshiguma = eng.evaluate(Assignment(training=["星熊"], power=[[], [], []]))
    assert abs(hoshiguma.breakdown["训练室"] - 39.6) < 1e-9  # 24h * (100%+5%基础+60%)
    assert hoshiguma.detail["training"][0]["speed%"] == 65.0

    no_skill = eng.evaluate(Assignment(training=["夜莺"], power=[[], [], []]))
    assert abs(no_skill.breakdown["训练室"] - 25.2) < 1e-9  # 24h * (100%+5%基础)
    assert no_skill.detail["training"][0]["speed%"] == 5.0

    with _override(eng, frac={"星熊": 0.0}):
        inactive = eng.evaluate(Assignment(training=["星熊"], power=[[], [], []]))
    assert inactive.breakdown["训练室"] == 0.0
    assert inactive.detail["training"][0]["equiv_hours/day"] == 0.0


def test_zuo_le_martial_ready_instantly_completes_guard_m1_only():
    """左乐·思而后行: 武道满时仅下一次近卫M1立即完成, 默认不无条件触发。"""
    prof = _prof("左乐", rarity=6)
    cfg = copy.deepcopy(CONFIG)
    cfg["training"]["ap_value_per_hour"] = 1.0
    cfg["training"]["target_mastery_level"] = 1
    cfg["training"]["target_profession"] = "近卫"

    default = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(training=["左乐"], power=[[], [], []]))
    assert default.detail["training"][0]["initial_progress%"] == 0.0
    assert default.detail["training"][0]["speed%"] == 30.0

    cfg["training"]["zuo_le_martial_ready"] = True
    ready = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(training=["左乐"], power=[[], [], []]))
    assert ready.detail["training"][0]["initial_progress%"] == 100.0

    cfg["training"]["target_mastery_level"] = 2
    wrong_level = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(training=["左乐"], power=[[], [], []]))
    assert wrong_level.detail["training"][0]["initial_progress%"] == 0.0

    cfg["training"]["target_mastery_level"] = 1
    cfg["training"]["target_profession"] = "狙击"
    wrong_profession = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(training=["左乐"], power=[[], [], []]))
    assert wrong_profession.detail["training"][0]["initial_progress%"] == 0.0


def test_training_base_session_hours_default_to_prts_mastery_times():
    """PRTS 基础专精训练时长: M1/M2/M3 分别为 8/16/24 小时。"""
    prof = _prof("艾丽妮", rarity=6)
    cfg = copy.deepcopy(CONFIG)
    cfg["training"]["ap_value_per_hour"] = 1.0
    cfg["mood"]["base_drain_per_hour"] = 0.0
    eng = Engine(cfg, prof, Schedule([0]))

    m3 = eng.evaluate(Assignment(training=["艾丽妮"], power=[[], [], []]))
    assert m3.detail["training"][0]["base_session_hours"] == 24.0
    assert abs(m3.breakdown["训练室"] - 44.4) < 1e-9

    cfg["training"]["target_mastery_level"] = 1
    eng = Engine(cfg, prof, Schedule([0]))
    m1 = eng.evaluate(Assignment(training=["艾丽妮"], power=[[], [], []]))
    assert m1.detail["training"][0]["base_session_hours"] == 8.0
    assert abs(m1.breakdown["训练室"] - 36.4) < 1e-9


def test_training_room_level_caps_supported_mastery():
    """PRTS 训练室等级: Lv1/Lv2/Lv3 分别支持专精一/二/三。"""
    prof = _prof("星熊", rarity=6)
    cfg = copy.deepcopy(CONFIG)
    cfg["training"]["level"] = 2
    cfg["training"]["target_mastery_level"] = 3
    cfg["training"]["ap_value_per_hour"] = 1.0
    cfg["mood"]["base_drain_per_hour"] = 0.0

    blocked = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(training=["星熊"], power=[[], [], []]))
    assert blocked.breakdown["训练室"] == 0.0
    assert blocked.detail["training"][0]["active_plan"] is False
    assert blocked.detail["training"][0]["max_mastery_level"] == 2
    assert any("不支持专精3" in w for w in blocked.warnings)

    cfg["training"]["target_mastery_level"] = 2
    supported = Engine(cfg, prof, Schedule([0])).evaluate(Assignment(training=["星熊"], power=[[], [], []]))
    assert supported.detail["training"][0]["active_plan"] is True
    assert supported.detail["training"][0]["base_session_hours"] == 16.0
    assert abs(supported.breakdown["训练室"] - 39.6) < 1e-9
    assert supported.warnings == []


def test_training_assistant_is_idle_without_active_training_plan():
    """PRTS: 无训练计划时协助位干员不视为工作状态, 不提供训练加速且无心情消耗。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["training"]["active_plan"] = False
    cfg["training"]["ap_value_per_hour"] = 1.0
    cfg["training"]["base_session_hours"] = 8.0
    prof = _prof("星熊", rarity=6)
    eng = Engine(cfg, prof, Schedule([0]))

    res = eng.evaluate(Assignment(training=["星熊"], power=[[], [], []]))

    assert res.breakdown["训练室"] == 0.0
    assert res.detail["training"][0]["active_plan"] is False
    assert res.detail["training"][0]["speed%"] == 0.0
    assert res.detail["training"][0]["equiv_hours/day"] == 0.0
    assert res.warnings == []


def test_control_center_training_speed_bonus_targets_active_training_once():
    """控制中枢训练加速: 有训练计划时给训练中干员+5%, 同种效果取最高。"""
    prof = _prof("阿斯卡纶", "烛煌", "斩业星熊", "夜莺", rarity=6)

    cfg = copy.deepcopy(CONFIG)
    cfg["training"]["ap_value_per_hour"] = 1.0
    cfg["mood"]["base_drain_per_hour"] = 0.0
    eng = Engine(cfg, prof, Schedule([0]))

    base = eng.evaluate(Assignment(training=["夜莺"], power=[[], [], []]))
    assert base.detail["training"][0]["speed%"] == 5.0

    one = eng.evaluate(Assignment(control=["阿斯卡纶"], training=["夜莺"], power=[[], [], []]))
    assert one.detail["training"][0]["speed%"] == 10.0

    multiple_same_kind = eng.evaluate(Assignment(
        control=["阿斯卡纶", "烛煌", "斩业星熊"],
        training=["夜莺"],
        power=[[], [], []],
    ))
    assert multiple_same_kind.detail["training"][0]["speed%"] == 10.0

    with _override(eng, frac={"阿斯卡纶": 0.0, "夜莺": 1.0}):
        inactive_control = eng.evaluate(Assignment(control=["阿斯卡纶"], training=["夜莺"], power=[[], [], []]))
    assert inactive_control.detail["training"][0]["speed%"] == 5.0

    cfg["training"]["active_plan"] = False
    idle = Engine(cfg, prof, Schedule([0]))
    idle_res = idle.evaluate(Assignment(control=["阿斯卡纶"], training=["夜莺"], power=[[], [], []]))
    assert idle_res.detail["training"][0]["speed%"] == 0.0


def test_control_conditional_self_drain_requires_active_faction_coworker():
    """摆渡人 英雄的骄傲: 萨尔贡同中枢时才有自身+0.02心情消耗。"""
    prof = _prof("摆渡人", "缇缇", "夜莺", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    solo = eng.evaluate(Assignment(control=["摆渡人"], power=[[], [], []]))
    assert solo.detail["globals"]["clue"] == 5.0
    assert eng._resolved["摆渡人"].drain_delta == 0.0

    with_sargon = eng.evaluate(Assignment(control=["摆渡人", "缇缇"], power=[[], [], []]))
    assert with_sargon.detail["globals"]["clue"] == 5.0
    assert eng._resolved["摆渡人"].drain_delta == 0.02

    with _override(eng, frac={"摆渡人": 1.0, "缇缇": 0.0}):
        inactive_sargon = eng.evaluate(Assignment(control=["摆渡人", "缇缇"], power=[[], [], []]))
    assert inactive_sargon.detail["globals"]["clue"] == 5.0
    assert eng._resolved["摆渡人"].drain_delta == 0.0


def test_control_alter_recover_counts_active_alter_operators():
    """异格者: 控制中枢内每个仍有效工作的异格干员提供0.05/h恢复。"""
    prof = _prof("濯尘芙蓉", "寒芒克洛丝", "炎狱炎熔", "夜莺", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    all_active = eng.evaluate(Assignment(control=["濯尘芙蓉", "寒芒克洛丝", "炎狱炎熔"], power=[[], [], []]))
    assert abs(eng._resolved["濯尘芙蓉"].control_recover - 0.15) < 1e-9
    assert abs(all_active.detail["globals"]["control_recover"] - 0.15) < 1e-9

    with _override(eng, frac={"濯尘芙蓉": 1.0, "寒芒克洛丝": 0.0, "炎狱炎熔": 1.0}):
        one_inactive = eng.evaluate(Assignment(control=["濯尘芙蓉", "寒芒克洛丝", "炎狱炎熔"], power=[[], [], []]))
    assert abs(eng._resolved["濯尘芙蓉"].control_recover - 0.10) < 1e-9
    assert abs(one_inactive.detail["globals"]["control_recover"] - 0.10) < 1e-9

    non_alter = eng.evaluate(Assignment(control=["濯尘芙蓉", "夜莺"], power=[[], [], []]))
    assert eng._resolved["濯尘芙蓉"].control_recover == 0.05
    assert non_alter.detail["globals"]["control_recover"] == 0.05


def test_training_target_mastery_level_gates_conditional_speed_and_drain():
    """训练室"专精至N级"额外速度/心情消耗应按当前训练目标等级门控。"""
    prof = _prof("W", "假日威龙陈", "仇白", rarity=6)

    cfg = copy.deepcopy(CONFIG)
    cfg["training"]["target_mastery_level"] = 1
    cfg["training"]["ap_value_per_hour"] = 1.0
    cfg["mood"]["base_drain_per_hour"] = 1.0
    eng = Engine(cfg, prof, Schedule([0]))

    w_m1 = eng.evaluate(Assignment(training=["W"], power=[[], [], []]))
    assert w_m1.detail["training"][0]["speed%"] == 35.0  # 基础5 + 狙击专精30, 不吃M3额外65
    assert eng._net_drain("W", "training") == 1.0

    chen_m1 = eng.evaluate(Assignment(training=["假日威龙陈"], power=[[], [], []]))
    assert chen_m1.detail["training"][0]["speed%"] == 100.0  # 基础5 + 30 + M1额外65
    assert eng._net_drain("假日威龙陈", "training") == 2.0

    cfg["training"]["target_mastery_level"] = 3
    eng = Engine(cfg, prof, Schedule([0]))

    w_m3 = eng.evaluate(Assignment(training=["W"], power=[[], [], []]))
    assert w_m3.detail["training"][0]["speed%"] == 100.0
    assert eng._net_drain("W", "training") == 2.0

    chen_m3 = eng.evaluate(Assignment(training=["假日威龙陈"], power=[[], [], []]))
    assert chen_m3.detail["training"][0]["speed%"] == 35.0
    assert eng._net_drain("假日威龙陈", "training") == 1.0

    cfg["training"]["target_branch"] = "斗士"
    eng = Engine(cfg, prof, Schedule([0]))
    qiubai_wrong_branch = eng.evaluate(Assignment(training=["仇白"], power=[[], [], []]))
    assert qiubai_wrong_branch.detail["training"][0]["speed%"] == 35.0

    cfg["training"]["target_branch"] = "领主"
    eng = Engine(cfg, prof, Schedule([0]))
    qiubai_matching_branch = eng.evaluate(Assignment(training=["仇白"], power=[[], [], []]))
    assert qiubai_matching_branch.detail["training"][0]["speed%"] == 80.0


def test_training_target_profession_gates_speed_and_conditional_drain():
    """训练室职业限定技能只应作用于匹配职业的训练对象。"""
    prof = _prof("W", rarity=6)

    cfg = copy.deepcopy(CONFIG)
    cfg["training"]["target_mastery_level"] = 3
    cfg["training"]["target_profession"] = "狙击"
    cfg["training"]["ap_value_per_hour"] = 1.0
    cfg["mood"]["base_drain_per_hour"] = 1.0
    eng = Engine(cfg, prof, Schedule([0]))

    matching = eng.evaluate(Assignment(training=["W"], power=[[], [], []]))
    assert matching.detail["training"][0]["speed%"] == 100.0  # 基础5 + 狙击专精30 + M3额外65
    assert eng._net_drain("W", "training") == 2.0

    cfg["training"]["target_profession"] = "医疗"
    eng = Engine(cfg, prof, Schedule([0]))
    mismatched = eng.evaluate(Assignment(training=["W"], power=[[], [], []]))
    assert mismatched.detail["training"][0]["speed%"] == 5.0
    assert eng._net_drain("W", "training") == 1.0


def test_training_text_speed_without_value_is_parsed():
    """雷狼龙S空爆的训练速度写在技能文本里, 数据 value 只给了心情消耗。"""
    prof = _prof("雷狼龙S空爆", rarity=5)

    assert prof["雷狼龙S空爆"].stat("training").train_speed == 50.0
    assert prof["雷狼龙S空爆"].stat("training").drain_delta == 1.0

    cfg = copy.deepcopy(CONFIG)
    cfg["training"]["ap_value_per_hour"] = 1.0
    cfg["mood"]["base_drain_per_hour"] = 0.0
    eng = Engine(cfg, prof, Schedule([0]))

    res = eng.evaluate(Assignment(training=["雷狼龙S空爆"], power=[[], [], []]))
    assert res.detail["training"][0]["speed%"] == 55.0
    assert eng._net_drain("雷狼龙S空爆", "training") == 1.0


def test_training_faction_count_skills_use_active_base_members_and_include_self():
    """战术指导按基建内仍工作的进攻方/防守方计数, 训练助手本人也属于基建内成员。"""
    prof = _prof("双月", "灰烬", "闪击", "导火索", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    eng.evaluate(Assignment(training=["双月"], power=[[], [], []]))
    assert eng._resolved["双月"].train_speed == 10.0

    eng.evaluate(Assignment(
        training=["双月"],
        control=["灰烬"],
        hire=["闪击"],
        manufacture=[("赤金", ["导火索"])],
        power=[[], [], []],
    ))
    assert eng._resolved["双月"].train_speed == 40.0

    with _override(eng, frac={"双月": 1.0, "灰烬": 1.0, "闪击": 0.0, "导火索": 1.0}):
        eng.evaluate(Assignment(
            training=["双月"],
            control=["灰烬"],
            hire=["闪击"],
            manufacture=[("赤金", ["导火索"])],
            power=[[], [], []],
        ))
    assert eng._resolved["双月"].train_speed == 30.0

    defense_prof = _prof("艾拉", "战车", "霜华", "医生", rarity=5)
    defense_eng = Engine(CONFIG, defense_prof, Schedule([0, 6, 12, 18]))

    defense_eng.evaluate(Assignment(training=["艾拉"], power=[[], [], []]))
    assert defense_eng._resolved["艾拉"].train_speed == 10.0

    defense_asg = Assignment(
        training=["艾拉"],
        control=["战车", "霜华"],
        hire=["医生"],
        power=[[], [], []],
    )
    defense_eng.evaluate(defense_asg)
    assert defense_eng._resolved["艾拉"].train_speed == 40.0

    with _override(defense_eng, frac={"艾拉": 1.0, "战车": 1.0, "霜华": 0.0, "医生": 1.0}):
        defense_eng.evaluate(defense_asg)
    assert defense_eng._resolved["艾拉"].train_speed == 30.0


def test_mood_threshold_intermediate_product_branches():
    """夕/令阈值技能: 按心情 >12 / <=12 切换感知信息与人间烟火分支。"""
    prof = _prof("夕", "令", "重岳", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(control=["夕", "令"], power=[[], [], []])

    eng.evaluate(asg)
    assert eng._ctx.pools.get("感知信息", 0) == 10  # 夕 >12
    assert eng._ctx.pools.get("人间烟火", 0) == 15  # 令 >12
    assert eng._resolved["夕"].drain_delta == 0.0  # 令 杯莫停

    with _override(eng, mood={"夕": 12.0, "令": 24.0}):
        eng.evaluate(asg)
    assert eng._ctx.pools.get("感知信息", 0) == 0
    assert eng._ctx.pools.get("人间烟火", 0) == 30  # 夕 <=12 + 令 >12

    with _override(eng, mood={"夕": 24.0, "令": 12.0}):
        eng.evaluate(asg)
    assert eng._ctx.pools.get("感知信息", 0) == 20  # 夕 >12 + 令 <=12
    assert eng._ctx.pools.get("人间烟火", 0) == 0

    eng.evaluate(Assignment(control=["夕", "重岳"], power=[[], [], []]))
    assert eng._resolved["夕"].drain_delta == 0.5
    assert eng._resolved["重岳"].drain_delta == 0.5
    eng.evaluate(Assignment(control=["夕", "令", "重岳"], power=[[], [], []]))
    assert eng._resolved["夕"].drain_delta == 0.0
    assert eng._resolved["重岳"].drain_delta == 0.0
    assert eng._ctx.pools.get("人间烟火", 0) == 30
    with _override(eng, frac={"夕": 0.0, "令": 1.0, "重岳": 1.0}):
        eng.evaluate(Assignment(control=["夕", "令", "重岳"], power=[[], [], []]))
    assert eng._ctx.pools.get("人间烟火", 0) == 25


def test_intermediate_product_active_gate_no_square_decay():
    """瞬态窗口: 涣散/休息的资源生产者不产资源; 自产自用技能不被 active_frac 平方衰减。"""
    prof = _prof("灰烬", "战车", "早露", "凛冬", "闪击", "双月", "乌有", "夜莺", "絮雨", "爱丽丝", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))

    with _override(eng, frac={"灰烬": 0.0, "战车": 1.0, "早露": 1.0, "凛冬": 1.0, "闪击": 1.0, "双月": 1.0}):
        eng.evaluate(Assignment(control=["灰烬", "战车", "早露", "凛冬"], hire=["闪击"], meeting=["双月"], power=[[], [], []]))
    assert eng._ctx.pools.get("情报储备", 0) == 0
    assert eng._ctx.pools.get("乌萨斯特饮", 0) == 2
    assert eng._resolved["闪击"].contact == 30.0  # 基底20 + 乌萨斯特饮2*5, 不吃涣散灰烬的情报储备
    assert eng._resolved["双月"].clue == 5.0

    with _override(eng, frac={"乌有": 0.5}):
        eng.evaluate(Assignment(trading=[["乌有"]], dormitory=[["夜莺"]], power=[[], [], []]))
    assert eng._ctx.pools.get("人间烟火", 0) == 1
    assert eng._resolved["乌有"].trade_eff == 1.0

    with _override(eng, frac={"絮雨": 0.5, "爱丽丝": 1.0}):
        eng.evaluate(Assignment(hire=["絮雨"], dormitory=[["爱丽丝"]], power=[[], [], []]))
    assert eng._ctx.pools.get("梦境", 0) == 5
    assert eng._ctx.pools.get("记忆碎片", 0) == 30
    assert eng._ctx.pools.get("感知信息", 0) == 35

    with _override(eng, frac={"絮雨": 0.5, "爱丽丝": 1.0}, fatigued={"絮雨"}):
        eng.evaluate(Assignment(hire=["絮雨"], dormitory=[["爱丽丝"]], power=[[], [], []]))
    assert eng._ctx.pools.get("梦境", 0) == 5
    assert eng._ctx.pools.get("记忆碎片", 0) == 0
    assert eng._ctx.pools.get("感知信息", 0) == 5


def test_control_contact_global_without_office_keyword():
    """八幡海铃 可靠伙伴: 文本只有'人脉资源联络速度', 也应算办公室全局加成。"""
    prof = _prof("八幡海铃", "焰狐龙梓兰", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    res = eng.evaluate(Assignment(control=["八幡海铃"], power=[[], [], []]))
    assert eng._ctx.pools["热情值"] == 10
    assert res.detail["globals"]["contact"] == 10.0
    orch = eng.evaluate(Assignment(control=["焰狐龙梓兰"], power=[[], [], []]))
    assert orch.detail["globals"]["contact"] == 10.0


def test_sailach_infection_contact_bonus_is_conditional():
    """琴柳 感染力: 联络速度低于30%(含基础5%)时才唯一额外+20%, 不是静态+50。"""
    prof = _prof("琴柳", "夜莺", "艾雅法拉", rarity=6)
    cfg = copy.deepcopy(CONFIG)
    cfg["mood"]["base_drain_per_hour"] = 0.0
    eng = Engine(cfg, prof, Schedule([0]))

    assert prof["琴柳"].stat("control").contact_global == 0.0

    low = eng.evaluate(Assignment(control=["琴柳"], hire=["夜莺"], power=[[], [], []]))
    base_refreshes = cfg["hire"]["contact_per_hour_base"] * 24
    assert abs(low.breakdown["办公室(公招)"] - base_refreshes * 1.25 * cfg["hire"]["ap_per_refresh"]) < 1e-9

    high = eng.evaluate(Assignment(control=["琴柳"], hire=["艾雅法拉"], power=[[], [], []]))
    assert abs(high.breakdown["办公室(公招)"] - base_refreshes * 1.50 * cfg["hire"]["ap_per_refresh"]) < 1e-9

