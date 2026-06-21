"""瞬态仿真 (simulate) 测试。"""
import copy
import json
from pathlib import Path

from conftest import (
    ROOT,
    CONFIG,
    _DB,
    _prof,
    _override,
)
from arknights_base_simulation.engine import (
    Assignment,
    Engine,
    OperatorProfile,
    Schedule,
    dorm_base_recover,
)
from arknights_base_simulation.optimizer import build_profiles
from arknights_base_simulation.roster import Operator
from arknights_base_simulation.simulate import (
    _dorm_recovery_bonus,
    _fiammetta_swap_pairs,
    _gap_assignment,
    simulate,
)
from arknights_base_simulation.synergy import (
    dorm_all_recover_for,
    dorm_average_recover_pool,
    dorm_single_recover_for,
    dorm_low_mood_extra_recover_for,
    dorm_target_extra_recover,
    dorm_target_extra_recover_for,
)

def test_transient_dorm_low_mood_extra_is_target_specific():
    """simulate: 心情阈值恢复额外只给低心情目标, 不应套给同宿舍所有未满心情干员。"""
    prof = _prof("刺玫", "能天使", "德克萨斯", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(dormitory=[["刺玫", "能天使", "德克萨斯"]], power=[[], [], []])
    mood = {"刺玫": 24.0, "能天使": 18.0, "德克萨斯": 20.0}

    with _override(eng, mood=mood):
        eng.evaluate(asg)

    rose_buff = next(b for b in prof["刺玫"].room_buffs["dormitory"] if b.buff_name == "芬芳疗养·β")
    assert dorm_low_mood_extra_recover_for(rose_buff, "能天使", mood) == 0.1
    assert dorm_low_mood_extra_recover_for(rose_buff, "德克萨斯", mood) == 0.0

    all_bonus, other_by_target, avg_pool, nonfull = _dorm_recovery_bonus(eng, asg, mood)[0]
    assert abs(all_bonus - 0.15) < 1e-9
    assert abs(other_by_target["能天使"] - 0.1) < 1e-9
    assert "德克萨斯" not in other_by_target
    assert avg_pool == 0.0
    assert nonfull == 2


def test_transient_dorm_all_recovery_can_exclude_self():
    """simulate: 赫拉格 挣脱只恢复除自身以外所有干员, 赫拉格自己不能吃这+0.1。"""
    prof = _prof("赫拉格", "能天使", elite=1, rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(dormitory=[["赫拉格", "能天使"]], power=[[], [], []])
    mood = {"赫拉格": 12.0, "能天使": 12.0}

    with _override(eng, mood=mood):
        eng.evaluate(asg)

    all_bonus, other_by_target, avg_pool, nonfull = _dorm_recovery_bonus(eng, asg, mood)[0]
    assert all_bonus == 0.0
    assert "赫拉格" not in other_by_target
    assert abs(other_by_target["能天使"] - 0.1) < 1e-9
    assert avg_pool == 0.0
    assert nonfull == 2


def test_dorm_average_recovery_pool_is_split_in_transient_simulation():
    """冰酿 小酌怡情: 总计+0.8应按休息人数分配, 不是每名休息干员各+0.8。"""
    prof = _prof("冰酿", "能天使", "德克萨斯", rarity=5)
    buff = prof["冰酿"].room_buffs["dormitory"][0]
    assert dorm_average_recover_pool(buff) == 0.8

    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 0.0
    cfg["dormitory"]["level"] = None
    cfg["dormitory"]["base_recover_per_hour"] = 0.0
    cfg["mood"]["base_drain_per_hour"] = 2.0
    cfg["mood"]["cap"] = 4.0
    sch = Schedule([0, 6, 12, 18])

    no_ice = Engine(cfg, prof, sch)
    with_ice = Engine(cfg, prof, sch)
    asg_no_ice = Assignment(
        manufacture=[("作战记录", ["能天使"]), ("作战记录", ["德克萨斯"])],
        dormitory=[[]],
        power=[[], [], []],
    )
    asg_with_ice = Assignment(
        manufacture=[("作战记录", ["能天使"]), ("作战记录", ["德克萨斯"])],
        dormitory=[["冰酿"]],
        power=[[], [], []],
    )

    base = simulate(no_ice, asg_no_ice, sch, days=2, initial_mood=0.0, rest_floor=1.0)
    split = simulate(with_ice, asg_with_ice, sch, days=2, initial_mood=0.0, rest_floor=1.0)

    assert base.days[1].avg_work_fraction == 0.0
    assert 0.0 < split.days[1].avg_work_fraction < 1.0


def test_transient_dorm_self_recovery_adjusts_own_rest_rate_only():
    """simulate: 杜林 嗜睡自身-0.1只修正自己, 同宿舍其他干员仍吃全员+0.25。"""
    prof = _prof("杜林", "能天使", elite=0, level=30, rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(dormitory=[["杜林", "能天使"]], power=[[], [], []])
    mood = {"杜林": 12.0, "能天使": 12.0}

    with _override(eng, mood=mood):
        eng.evaluate(asg)

    all_bonus, other_by_target, avg_pool, nonfull = _dorm_recovery_bonus(eng, asg, mood)[0]
    assert abs(all_bonus - 0.25) < 1e-9
    assert abs(other_by_target["杜林"] + 0.1) < 1e-9
    assert "能天使" not in other_by_target
    assert avg_pool == 0.0
    assert nonfull == 2


def test_transient_simulation():
    """逐日瞬态: 心情爬坡/占空循环, 可持续日产应低于稳态乐观值且为正; 稳态路径不受影响。"""
    prof = _prof("食铁兽", "能天使", "德克萨斯", "夜莺", "闪灵")
    sch = Schedule([8, 22])
    eng = Engine(CONFIG, prof, sch)
    asg = Assignment(manufacture=[("作战记录", ["食铁兽", "能天使"])],
                     dormitory=[["夜莺", "闪灵"]], power=[[], [], []])
    steady = eng.evaluate(asg).ap_per_day
    sim = simulate(eng, asg, sch, days=8)
    assert len(sim.days) == 8
    assert sim.converged_ap > 0
    assert sim.converged_ap <= sim.steady_ap + 1e-6, "瞬态可持续日产不应超过稳态乐观值"
    assert abs(eng.evaluate(asg).ap_per_day - steady) < 1e-6, "瞬态模拟污染了稳态评估"


def test_transient_simulation_multi_shift_recovers_off_shift_ops():
    """simulate(shifts=...): 非当班干员应在宿舍恢复, 下次上岗用恢复后的心情。"""
    prof = _prof("能天使", "德克萨斯", rarity=6)
    cfg = copy.deepcopy(CONFIG)
    cfg["mood"]["base_drain_per_hour"] = 1.0
    eng = Engine(cfg, prof, Schedule([0, 6, 12, 18]))
    shift_a = Assignment(
        manufacture=[("作战记录", ["能天使"])],
        dormitory=[[]],
        power=[[], [], []],
    )
    shift_b = Assignment(
        manufacture=[("作战记录", ["德克萨斯"])],
        dormitory=[[]],
        power=[[], [], []],
    )
    starts = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._mood_override is not None:
            starts.append(dict(eng._mood_override))
        return r

    eng.evaluate = tracking_evaluate
    try:
        simulate(
            eng,
            shift_a,
            Schedule([0, 6, 12, 18]),
            shifts=[shift_a, shift_b],
            days=1,
            initial_mood=6.0,
            rest_floor=1.0,
        )
    finally:
        eng.evaluate = original

    assert [set(s) for s in starts] == [{"能天使"}, {"德克萨斯"}, {"能天使"}, {"德克萨斯"}]
    assert starts[0]["能天使"] == 6.0
    assert starts[2]["能天使"] == cfg["mood"]["cap"]


def test_transient_simulation_multi_shift_steady_reference_is_weighted():
    """simulate(shifts=...): 稳态乐观参考应按各上线间隔加权, 不是只取第一班。"""
    prof = _prof("食铁兽", "能天使", rarity=6)
    cfg = copy.deepcopy(CONFIG)
    cfg["power"]["drone_per_hour_base"] = 0.0
    cfg["mood"]["base_drain_per_hour"] = 0.0
    eng = Engine(cfg, prof, Schedule([0, 12]))
    shift_a = Assignment(manufacture=[("作战记录", ["食铁兽"])], power=[[], [], []])
    shift_b = Assignment(manufacture=[("作战记录", ["能天使"])], power=[[], [], []])
    expected = (eng.evaluate(shift_a).ap_per_day + eng.evaluate(shift_b).ap_per_day) / 2.0

    sim = simulate(
        eng,
        shift_a,
        Schedule([0, 12]),
        shifts=[shift_a, shift_b],
        days=1,
        initial_mood=24.0,
        rest_floor=0.0,
    )

    assert sim.steady_ap == expected
    assert sim.steady_ap != eng.evaluate(shift_a).ap_per_day


def test_transient_simulation_passes_fatigued_clear_to_pools():
    """simulate: 絮雨在 gap 内心情耗尽时, 该窗口记忆碎片/自身感知应清空。"""
    prof = _prof("絮雨", rarity=5)
    cfg = json.loads(json.dumps(CONFIG))
    cfg["mood"]["base_drain_per_hour"] = 2.0
    cfg["dormitory"]["base_recover_per_hour"] = 0.0
    cfg["dormitory"].pop("level", None)
    eng = Engine(cfg, prof, Schedule([0]))
    asg = Assignment(hire=["絮雨"], power=[[], [], []])
    seen = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._frac_override is not None:
            seen.append(dict(eng._ctx.pools))
        return r

    eng.evaluate = tracking_evaluate
    simulate(eng, asg, Schedule([0]), days=1, initial_mood=1.0, rest_floor=1.0)
    assert seen, "simulate should evaluate at least one transient gap"
    assert seen[0].get("记忆碎片", 0) == 0
    assert seen[0].get("感知信息", 0) == 0


def test_transient_simulation_zero_active_fraction_disables_resource_producers():
    """simulate: 窗口内几乎立即涣散的干员不能按默认活跃继续产中间资源。"""
    prof = _prof("灰烬", "战车", "早露", "凛冬", "闪击", rarity=5)
    cfg = copy.deepcopy(CONFIG)
    cfg["mood"]["base_drain_per_hour"] = 1_000_000.0
    eng = Engine(cfg, prof, Schedule([0]))
    asg = Assignment(control=["灰烬", "战车", "早露", "凛冬"], hire=["闪击"], power=[[], [], []])
    seen = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._frac_override is not None:
            seen.append((dict(eng._frac_override), dict(eng._ctx.pools), r.breakdown.get("办公室(公招)", 0.0)))
        return r

    eng.evaluate = tracking_evaluate
    try:
        simulate(eng, asg, Schedule([0]), days=1, initial_mood=1e-6, rest_floor=0.0)
    finally:
        eng.evaluate = original

    assert seen, "simulate should evaluate at least one transient gap"
    _frac, pools, office_ap = seen[0]
    assert pools.get("情报储备", 0) == 0
    assert office_ap == 0.0


def test_transient_simulation_control_other_drain_is_targeted():
    """simulate: 艾拉的控制中枢除自身外增耗应降低其他中枢干员的有效工作占比。"""
    prof = _prof("艾拉", "临光", rarity=5)
    eng = Engine(CONFIG, prof, Schedule([0]))
    asg = Assignment(control=["艾拉", "临光"], power=[[], [], []])
    seen = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._frac_override is not None:
            seen.append(dict(eng._frac_override))
        return r

    eng.evaluate = tracking_evaluate
    try:
        simulate(eng, asg, Schedule([0]), days=1, initial_mood=12.0, rest_floor=0.0)
    finally:
        eng.evaluate = original

    assert seen, "simulate should evaluate at least one transient gap"
    assert seen[0]["艾拉"] > seen[0]["临光"]


def test_transient_simulation_elite_dorm_recovery_is_targeted():
    """simulate: 电弧的宿舍精英恢复只加给休息中的罗德岛精英干员。"""
    prof = _prof("电弧", "迷迭香", "夜莺", "Lancet-2", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0, 2.5, 3.5]))
    asg = Assignment(
        manufacture=[("作战记录", ["迷迭香"])],
        trading=[["夜莺"]],
        power=[["Lancet-2"], [], []],
        control=["电弧"],
        dormitory=[[]],
    )
    starts = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._frac_override is not None:
            starts.append(dict(eng._mood_override or {}))
        return r

    eng.evaluate = tracking_evaluate
    try:
        simulate(eng, asg, Schedule([0, 2.5, 3.5]), days=1, initial_mood=None, rest_floor=20.0)
    finally:
        eng.evaluate = original

    assert len(starts) >= 3
    kal_recovered = starts[2]["迷迭香"] - starts[1]["迷迭香"]
    night_recovered = starts[2]["夜莺"] - starts[1]["夜莺"]
    assert abs((kal_recovered - night_recovered) - 0.1) < 1e-9


def test_transient_simulation_control_named_recovery_is_targeted():
    """simulate: 魔王的指定恢复只提高自身和阿米娅的中枢有效工作占比。"""
    prof = _prof("魔王", "阿米娅", "临光", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0]))
    asg = Assignment(control=["魔王", "阿米娅", "临光"], power=[[], [], []])
    seen = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._frac_override is not None:
            seen.append(dict(eng._frac_override))
        return r

    eng.evaluate = tracking_evaluate
    try:
        simulate(eng, asg, Schedule([0]), days=1, initial_mood=12.0, rest_floor=0.0)
    finally:
        eng.evaluate = original

    assert seen
    assert seen[0]["魔王"] > seen[0]["临光"]
    assert seen[0]["阿米娅"] > seen[0]["临光"]


def test_transient_simulation_lee_agency_recovery_targets_faction_members():
    """simulate: 吽 坚毅随和全员只+0.05/人, 鲤氏干员另吃+0.2/人。"""
    prof = _prof("吽", "阿", "临光", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0]))
    asg = Assignment(control=["吽", "阿", "临光"], power=[[], [], []])
    seen = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._frac_override is not None:
            seen.append(dict(eng._frac_override))
        return r

    eng.evaluate = tracking_evaluate
    try:
        simulate(eng, asg, Schedule([0]), days=1, initial_mood=12.0, rest_floor=0.0)
    finally:
        eng.evaluate = original

    assert seen
    assert seen[0]["吽"] > seen[0]["临光"]
    assert seen[0]["阿"] > seen[0]["临光"]


def test_transient_simulation_control_dorm_recovery_requires_active_provider():
    """simulate: 中枢宿舍全局恢复提供者休息时, 不应由其他中枢干员继续保留该恢复。"""
    prof = _prof("三角初华", "临光", "夜莺", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0]))
    asg = Assignment(
        control=["三角初华", "临光"],
        manufacture=[("作战记录", ["夜莺"])],
        dormitory=[[]],
        power=[[], [], []],
    )
    starts = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._mood_override is not None:
            starts.append(dict(eng._mood_override))
        return r

    eng.evaluate = tracking_evaluate
    try:
        simulate(eng, asg, Schedule([0, 1, 2, 3, 4]), days=1, initial_mood=None, rest_floor=20.0)
    finally:
        eng.evaluate = original

    assert len(starts) >= 5
    assert abs(starts[4]["三角初华"] - (starts[3]["三角初华"] + dorm_base_recover(CONFIG) + 0.05)) < 1e-9


def test_transient_simulation_dorm_low_mood_recovery_updates_per_gap():
    """simulate: 休息干员低心情进入宿舍时, 触发当前宿舍低心情恢复额外项。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["dormitory"]["level"] = None
    cfg["dormitory"]["base_recover_per_hour"] = 0.0
    cfg["mood"]["base_drain_per_hour"] = 10.0
    prof = _prof("撷英调香师", "夜莺", rarity=6)
    eng = Engine(cfg, prof, Schedule([0, 1, 2]))
    starts = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._mood_override is not None:
            starts.append(dict(eng._mood_override))
        return r

    eng.evaluate = tracking_evaluate
    try:
        simulate(
            eng,
            Assignment(manufacture=[("作战记录", ["夜莺"])], dormitory=[["撷英调香师"]], power=[[], [], []]),
            Schedule([0, 1, 2]),
            days=1,
            initial_mood=0.0,
            rest_floor=1.0,
        )
    finally:
        eng.evaluate = original

    assert len(starts) >= 3
    assert abs(starts[2]["夜莺"] - 0.25) < 1e-9


def test_transient_simulation_dorm_recovery_does_not_cross_rooms():
    """simulate: 该宿舍内恢复只作用于同一宿舍, 不能跨宿舍给休息干员。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["dormitory"]["level"] = None
    cfg["dormitory"]["base_recover_per_hour"] = 0.0
    cfg["mood"]["base_drain_per_hour"] = 10.0
    prof = _prof("夜莺", "能天使", "德克萨斯", "阿米娅", "星熊", "银灰", rarity=6)
    sch = Schedule([0, 1, 2])

    def starts_for(dormitory):
        eng = Engine(cfg, prof, sch)
        starts = []
        original = eng.evaluate

        def tracking_evaluate(a):
            r = original(a)
            if eng._mood_override is not None:
                starts.append(dict(eng._mood_override))
            return r

        eng.evaluate = tracking_evaluate
        try:
            simulate(
                eng,
                Assignment(manufacture=[("作战记录", ["能天使"])], dormitory=dormitory, power=[[], [], []]),
                sch,
                days=1,
                initial_mood=0.0,
                rest_floor=1.0,
            )
        finally:
            eng.evaluate = original
        return starts

    same_room = starts_for([["夜莺"]])
    other_room = starts_for([["夜莺", "德克萨斯", "阿米娅", "星熊", "银灰"], []])

    assert len(same_room) >= 3
    assert len(other_room) >= 3
    assert same_room[2]["能天使"] > 0.0
    assert abs(other_room[2]["能天使"]) < 1e-9


def test_transient_simulation_dorm_single_target_recovery_only_hits_one_operator():
    """simulate: 宿舍内某个他人恢复只选一个未满心情目标, 不能全员套用。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["dormitory"]["level"] = None
    cfg["dormitory"]["base_recover_per_hour"] = 0.0
    cfg["mood"]["base_drain_per_hour"] = 10.0
    prof = _prof("闪灵", "能天使", "德克萨斯", rarity=6)
    sch = Schedule([0, 1, 2])
    eng = Engine(cfg, prof, sch)
    starts = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._mood_override is not None:
            starts.append(dict(eng._mood_override))
        return r

    eng.evaluate = tracking_evaluate
    try:
        simulate(
            eng,
            Assignment(
                manufacture=[("作战记录", ["能天使"]), ("作战记录", ["德克萨斯"])],
                dormitory=[["闪灵"]],
                power=[[], [], []],
            ),
            sch,
            days=1,
            initial_mood=0.0,
            rest_floor=1.0,
        )
    finally:
        eng.evaluate = original

    assert len(starts) >= 3
    assert abs(starts[2]["能天使"] - 0.75) < 1e-9
    assert abs(starts[2]["德克萨斯"]) < 1e-9


def test_transient_simulation_dorm_single_target_can_include_self_when_text_allows():
    """simulate: 深靛 毒剂师之友未写除自身以外, 休息时可以把单体恢复给自己。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["dormitory"]["level"] = None
    cfg["dormitory"]["base_recover_per_hour"] = 0.0
    cfg["mood"]["base_drain_per_hour"] = 1.0
    cfg["mood"]["cap"] = 10.0
    prof = _prof("深靛", rarity=6)
    sch = Schedule([0, 1, 2])
    eng = Engine(cfg, prof, sch)
    starts = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._mood_override is not None:
            starts.append(dict(eng._mood_override))
        return r

    eng.evaluate = tracking_evaluate
    try:
        simulate(
            eng,
            Assignment(manufacture=[("作战记录", ["深靛"])], dormitory=[[]], power=[[], [], []]),
            sch,
            days=1,
            initial_mood=2.0,
            rest_floor=1.5,
        )
    finally:
        eng.evaluate = original

    assert len(starts) >= 3
    assert abs(starts[1]["深靛"] - 1.0) < 1e-9
    assert abs(starts[2]["深靛"] - 1.55) < 1e-9


def test_transient_simulation_dorm_target_extra_recovery_stays_on_valid_target():
    """simulate: 如果目标是X的宿舍恢复额外项只能给合规目标, 不能给同房低心情非目标。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["dormitory"]["level"] = None
    cfg["dormitory"]["base_recover_per_hour"] = 0.0
    cfg["mood"]["base_drain_per_hour"] = 10.0
    prof = _prof("新约能天使", "德克萨斯", "CONFESS-47", rarity=6)
    sch = Schedule([0, 1, 2])
    eng = Engine(cfg, prof, sch)
    starts = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._mood_override is not None:
            starts.append(dict(eng._mood_override))
        return r

    eng.evaluate = tracking_evaluate
    try:
        simulate(
            eng,
            Assignment(
                manufacture=[("作战记录", ["德克萨斯", "CONFESS-47"])],
                dormitory=[["新约能天使"]],
                power=[[], [], []],
            ),
            sch,
            days=1,
            initial_mood=0.0,
            rest_floor=1.0,
        )
    finally:
        eng.evaluate = original

    assert len(starts) >= 2
    assert abs(starts[2]["CONFESS-47"] - 1.0) < 1e-9
    assert abs(starts[2]["德克萨斯"]) < 1e-9


def test_transient_gap_assignment_preserves_dorm_placement_order():
    """simulate: 休息干员进宿舍的顺序必须稳定, 菲亚梅塔前一位按当前gap宿舍排布判断。"""
    asg = Assignment(
        manufacture=[("作战记录", ["菲亚梅塔", "夜莺"])],
        dormitory=[["阿米娅"]],
        power=[[], [], []],
    )

    gap_asg = _gap_assignment(asg, ["菲亚梅塔", "夜莺"], dorm_slots=5)

    assert gap_asg.manufacture == [("作战记录", [])]
    assert gap_asg.dormitory == [["阿米娅", "菲亚梅塔", "夜莺"]]
    assert _fiammetta_swap_pairs(gap_asg) == [("菲亚梅塔", "阿米娅")]


def test_transient_simulation_rest_capacity_requires_assigned_dorm_rooms():
    """simulate: 当前方案没有宿舍房间时, 不能凭配置最大宿舍数获得休息床位。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["dormitory"]["level"] = None
    cfg["dormitory"]["base_recover_per_hour"] = 10.0
    cfg["mood"]["base_drain_per_hour"] = 10.0
    prof = _prof("夜莺", rarity=6)
    sch = Schedule([0, 1, 2])

    no_dorm = simulate(
        Engine(cfg, prof, sch),
        Assignment(manufacture=[("作战记录", ["夜莺"])], power=[[], [], []]),
        sch,
        days=2,
        initial_mood=0.0,
        rest_floor=1.0,
    )
    assert no_dorm.days[0].avg_work_fraction == 0.0
    assert no_dorm.days[1].avg_work_fraction == 0.0

    empty_dorm = simulate(
        Engine(cfg, prof, sch),
        Assignment(manufacture=[("作战记录", ["夜莺"])], dormitory=[[]], power=[[], [], []]),
        sch,
        days=2,
        initial_mood=0.0,
        rest_floor=1.0,
    )
    assert empty_dorm.days[1].avg_work_fraction > 0.0


def test_transient_simulation_fiammetta_self_recovery_ignores_other_buffs():
    """菲亚梅塔·自律: 大模拟休息时只吃自身+2/h, 不吃基础宿舍/同宿舍/中枢宿舍恢复。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["mood"]["cap"] = 100.0
    cfg["mood"]["base_drain_per_hour"] = 1.0
    cfg["manufacture"]["lines"]["作战记录"]["base_minutes_per_item"] = 1.0
    prof = _prof("菲亚梅塔", "夜莺", "三角初华", rarity=6)
    eng = Engine(cfg, prof, Schedule([0, 6, 12, 18]))
    asg = Assignment(
        manufacture=[("作战记录", ["菲亚梅塔"])],
        dormitory=[["夜莺"]],
        control=["三角初华"],
        power=[[], [], []],
    )
    starts = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._mood_override is not None:
            starts.append(dict(eng._mood_override))
        return r

    eng.evaluate = tracking_evaluate
    try:
        simulate(eng, asg, Schedule([0, 6, 12, 18]), days=2, initial_mood=0.0, rest_floor=1.0)
    finally:
        eng.evaluate = original

    assert len(starts) >= 4
    assert starts[2]["菲亚梅塔"] - starts[1]["菲亚梅塔"] == 12.0
    assert starts[2]["三角初华"] - starts[1]["三角初华"] > 12.0


def test_transient_simulation_fiammetta_swaps_with_previous_dorm_operator():
    """菲亚梅塔·患难之交: 目标严格取当前宿舍中前一位进驻干员。"""
    assert _fiammetta_swap_pairs(Assignment(dormitory=[["夜莺", "菲亚梅塔"]])) == [("菲亚梅塔", "夜莺")]
    assert _fiammetta_swap_pairs(Assignment(dormitory=[["菲亚梅塔", "夜莺"]])) == []


def test_transient_simulation_control_named_drain_is_targeted():
    """simulate: 祐天寺若麦的成效优先只增加丰川祥子的中枢心情消耗。"""
    prof = _prof("祐天寺若麦", "丰川祥子", "夜莺", rarity=6)
    eng = Engine(CONFIG, prof, Schedule([0]))
    asg = Assignment(control=["祐天寺若麦", "丰川祥子", "夜莺"], power=[[], [], []])
    seen = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._frac_override is not None:
            seen.append(dict(eng._frac_override))
        return r

    eng.evaluate = tracking_evaluate
    try:
        simulate(eng, asg, Schedule([0]), days=1, initial_mood=12.0, rest_floor=0.0)
    finally:
        eng.evaluate = original

    assert seen
    assert seen[0]["丰川祥子"] < seen[0]["祐天寺若麦"]


def test_transient_simulation_meeting_daily_clue_is_awarded_once_per_day():
    """simulate: 会客室每日4:00发放线索, 不能因按gap结算而漏掉。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["meeting"]["clue_per_hour_base"] = 0.0
    cfg["meeting"]["daily_clue_if_staffed"] = 1.0
    cfg["material_values_ap"]["线索"] = 1.0
    cfg["mood"]["base_drain_per_hour"] = 0.0
    prof = _prof("夜莺", rarity=6)
    asg = Assignment(meeting=["夜莺"], power=[[], [], []])

    four_login_sch = Schedule([0, 6, 12, 18])
    eng = Engine(cfg, prof, four_login_sch)
    four_login = simulate(eng, asg, four_login_sch, days=1, initial_mood=24.0, rest_floor=0.0)
    assert four_login.days[0].breakdown["会客室(线索)"] == 1.0

    wrap_sch = Schedule([8, 22])
    eng = Engine(cfg, prof, wrap_sch)
    wrap = simulate(eng, asg, wrap_sch, days=1, initial_mood=24.0, rest_floor=0.0)
    assert wrap.days[0].breakdown["会客室(线索)"] == 1.0


def test_transient_simulation_training_assistant_idle_without_active_plan():
    """simulate: 无训练计划时训练室协助位不进入占空心情循环。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["training"]["active_plan"] = False
    cfg["training"]["ap_value_per_hour"] = 1.0
    cfg["mood"]["base_drain_per_hour"] = 10.0
    prof = _prof("星熊", rarity=6)
    eng = Engine(cfg, prof, Schedule([0, 6, 12, 18]))
    seen = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._mood_override is not None:
            seen.append(dict(eng._mood_override))
        return r

    eng.evaluate = tracking_evaluate
    try:
        result = simulate(
            eng,
            Assignment(training=["星熊"], power=[[], [], []]),
            Schedule([0, 6, 12, 18]),
            days=1,
            initial_mood=24.0,
        )
    finally:
        eng.evaluate = original

    assert result.days[0].resting_op_hours == 0.0
    assert seen
    assert all("星熊" not in mood for mood in seen)


def test_control_other_facility_recovery_applies_to_active_training():
    """重岳 孤光共照: 训练计划进行中时, 训练室协助位属于其他工作设施。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["training"]["ap_value_per_hour"] = 1.0
    cfg["mood"]["base_drain_per_hour"] = 2.0
    cfg["dormitory"]["base_recover_per_hour"] = 20.0
    prof = _prof("重岳", "夜莺", rarity=6)
    eng = Engine(cfg, prof, Schedule([0]))

    base = eng.evaluate(Assignment(training=["夜莺"], dormitory=[[]], power=[[], [], []]))
    with_recover = eng.evaluate(Assignment(control=["重岳"], training=["夜莺"], dormitory=[[]], power=[[], [], []]))
    assert with_recover.detail["training"][0]["equiv_hours/day"] > base.detail["training"][0]["equiv_hours/day"]

    cfg["training"]["active_plan"] = False
    idle = Engine(cfg, prof, Schedule([0])).evaluate(
        Assignment(control=["重岳"], training=["夜莺"], dormitory=[[]], power=[[], [], []])
    )
    assert idle.detail["training"][0]["equiv_hours/day"] == 0.0


def test_transient_simulation_refreshes_resource_mood_thresholds_each_gap():
    """simulate: 资源池/心情阈值技能必须按当前gap重新解析, 不能沿用上一gap状态。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["mood"]["cap"] = 24.0
    cfg["mood"]["base_drain_per_hour"] = 1.0
    prof = _prof("重岳", "令", "夜莺", rarity=6)
    eng = Engine(cfg, prof, Schedule([0, 1, 2, 3, 4]))
    asg = Assignment(control=["重岳", "令"], hire=["夜莺"], power=[[], [], []])
    starts = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._mood_override is not None:
            starts.append(dict(eng._mood_override))
        return r

    eng.evaluate = tracking_evaluate
    try:
        simulate(eng, asg, Schedule([0, 1, 2, 3, 4]), days=1, initial_mood=13.0, rest_floor=-100.0)
    finally:
        eng.evaluate = original

    assert len(starts) == 5
    assert starts[1]["令"] > 12.0
    assert starts[2]["令"] < 12.0
    before_threshold = starts[1]["夜莺"] - starts[2]["夜莺"]
    after_threshold = starts[2]["夜莺"] - starts[3]["夜莺"]
    assert abs(before_threshold - 0.8) < 1e-9
    assert abs(after_threshold - 0.85) < 1e-9


def test_transient_simulation_refreshes_self_drain_delta_each_gap():
    """simulate: 自身心情消耗增量也要按当前在岗技能重算, 不能缓存初始delta。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["mood"]["cap"] = 24.0
    cfg["mood"]["base_drain_per_hour"] = 1.0
    cfg["dormitory"]["base_recover_per_hour"] = 0.0
    prof = _prof("夕", "令", "重岳", rarity=6)
    hours = list(range(24))
    eng = Engine(cfg, prof, Schedule(hours))
    asg = Assignment(control=["夕", "令", "重岳"], dormitory=[[]], power=[[], [], []])
    starts = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._mood_override is not None:
            starts.append(dict(eng._mood_override))
        return r

    eng.evaluate = tracking_evaluate
    try:
        simulate(eng, asg, Schedule(hours), days=1, initial_mood=None, rest_floor=1.0)
    finally:
        eng.evaluate = original

    assert len(starts) == 24
    while_ling_working = starts[14]["重岳"] - starts[15]["重岳"]
    after_ling_rests = starts[15]["重岳"] - starts[16]["重岳"]
    assert abs(while_ling_working - 0.8) < 1e-9
    assert abs(after_ling_rests - 1.35) < 1e-9


def test_transient_simulation_places_resters_in_dorm_context():
    """simulate: 休息干员进入宿舍后, 宿舍人数/宿舍成员类资源应在gap结算中生效。"""
    cfg = copy.deepcopy(CONFIG)
    cfg["mood"]["base_drain_per_hour"] = 10.0
    prof = _prof("乌有", "夜莺", rarity=6)
    eng = Engine(cfg, prof, Schedule([0, 1]))
    asg = Assignment(
        trading=[["乌有"]],
        manufacture=[("作战记录", ["夜莺"])],
        dormitory=[[]],
        power=[[], [], []],
    )
    pools = []
    original = eng.evaluate

    def tracking_evaluate(a):
        r = original(a)
        if eng._mood_override is not None:
            pools.append(eng._ctx.pools.get("人间烟火", 0.0))
        return r

    eng.evaluate = tracking_evaluate
    try:
        simulate(eng, asg, Schedule([0, 1]), days=1, initial_mood=None, rest_floor=1.0)
    finally:
        eng.evaluate = original

    assert pools == [0.0, 1.0]

