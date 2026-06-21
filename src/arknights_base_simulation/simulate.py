"""逐日瞬态模拟 (transient day-by-day simulation)。

与 engine.py 的『稳态日均』不同, 本模块按真实时间轴推进, 跟踪每名干员的心情连续变化
(含初始心情爬坡 / 损耗), 直到收敛到可持续的占空循环。

模型要点:
- 唯一跨日累积的状态是『心情』; 库存每次上线收菜清空, 故按 gap(两次上线之间)结算产量。
- 占空循环(duty-cycle)排班策略: 生产干员满心情时上岗工作, 心情耗尽(<floor)则下班进宿舍恢复,
  恢复至满(cap)再上岗。带迟滞避免抖动。休息期间该工位空缺(无替补 -> 产量真实下降)。
  -> 长期平均工作占比由『工作净消耗 vs 宿舍恢复速度』自然决定, 比稳态的乐观 work_fraction 更接近实战。
- 宿舍干员是恢复位, 不产出、心情维持满; 其恢复技能折算进休息恢复速度。
- 每个 gap: 把休息中的干员从其工位移除, 用单 gap + 每人活动占比覆盖 调 engine 结算该窗口产量。

注意: 这是『固定排班无替补』的瞬态; 真实玩家有富余干员轮换, 故占空循环里工位空缺的损耗是本模型
(及该固定方案)的保守下界。控制中枢/会客室等若驻员需休息, 全局加成会随之短暂掉落(真实但偏保守)。
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from .engine import OTHER_RECOVER_ROOMS, Assignment, Engine, Schedule, dorm_base_recover_for_room
from .synergy import (
    build_context,
    dorm_all_recover_for,
    dorm_average_recover_pool,
    dorm_low_mood_extra_recover_for,
    dorm_low_mood_extra_recover_triggered,
    dorm_single_recover_for,
    dorm_target_extra_recover_for,
    factions_of,
)


@dataclass
class DayRecord:
    day: int
    ap: float                       # 当日折合理智
    breakdown: dict[str, float]     # 当日各项收益
    avg_work_fraction: float        # 当日生产干员平均工作占比
    resting_op_hours: float         # 当日累计"工位因休息空缺"的干员·小时


@dataclass
class SimResult:
    days: list[DayRecord] = field(default_factory=list)
    cumulative_ap: float = 0.0
    steady_ap: float = 0.0          # 稳态引擎日均(乐观参考)
    converged_ap: float = 0.0       # 末日(收敛后)日产
    initial_mood: float = 24.0
    staggered: bool = False         # 初始心情是否错峰(已运行基建)


def _working_rooms(eng: Engine, asg: Assignment):
    """产出 (干员, 设施room, 房间索引) 列表; 仅工作设施(不含宿舍)。"""
    out = []
    for i, (_line, ops) in enumerate(asg.manufacture):
        for nm in ops:
            out.append((nm, "manufacture", i))
    for i, ops in enumerate(asg.trading):
        for nm in ops:
            out.append((nm, "trading", i))
    for i, ops in enumerate(asg.power):
        for nm in ops:
            out.append((nm, "power", i))
    for nm in asg.control:
        out.append((nm, "control", 0))
    for nm in asg.meeting:
        out.append((nm, "meeting", 0))
    for nm in asg.hire:
        out.append((nm, "hire", 0))
    if eng.cfg.get("training", {}).get("active_plan", True) and eng._training_plan_supported():
        for nm in asg.training:
            out.append((nm, "training", 0))
    return out


def _gap_assignment(asg: Assignment, resting: set[str] | list[str], dorm_slots: int) -> Assignment:
    """Copy assignment, remove resting ops from work rooms, and place them into dorm beds."""
    resting_set = set(resting)
    resting_order = list(resting)
    a = copy.deepcopy(asg)
    a.manufacture = [(line, [o for o in ops if o not in resting_set]) for line, ops in a.manufacture]
    a.trading = [[o for o in ops if o not in resting_set] for ops in a.trading]
    a.power = [[o for o in ops if o not in resting_set] for ops in a.power]
    a.control = [o for o in a.control if o not in resting_set]
    a.meeting = [o for o in a.meeting if o not in resting_set]
    a.hire = [o for o in a.hire if o not in resting_set]
    a.training = [o for o in a.training if o not in resting_set]
    already_in_dorm = {nm for room in a.dormitory for nm in room}
    to_place = [nm for nm in resting_order if nm not in already_in_dorm]
    for room in a.dormitory:
        while to_place and len(room) < dorm_slots:
            room.append(to_place.pop(0))
    return a


def _gap_crosses_hour(start: float, gap: float, hour: float) -> bool:
    """Half-open daily interval [start, start+gap) modulo 24; starts exactly at hour count."""
    if gap <= 0:
        return False
    offset = (hour - start) % 24.0
    return 0.0 <= offset < gap


def _self_only_dorm_recovery_rate(eng: Engine, name: str) -> float | None:
    """Dormitory skills that explicitly reject every other mood recovery source."""
    prof = eng.prof.get(name)
    if not prof:
        return None
    for buff in prof.room_buffs.get("dormitory", []):
        if "无法获得其他来源提供的心情恢复效果" in buff.desc:
            st = prof.stat("dormitory")
            return st.dorm_recover_self if st else buff.value / 100.0
    return None


def _fiammetta_swap_pairs(asg: Assignment,
                          mood: dict[str, float] | None = None) -> list[tuple[str, str]]:
    """Return (Fiammetta, swap target) pairs from dormitory.

    With mood data: pair with the lowest-mood roommate (best swap target).
    Without mood: legacy fallback to the operator listed before Fiammetta.
    """
    pairs: list[tuple[str, str]] = []
    for room in asg.dormitory:
        if "菲亚梅塔" not in room:
            continue
        if mood:
            candidates = [(mood.get(nm, 24.0), nm) for nm in room
                          if nm != "菲亚梅塔"]
            if candidates:
                _, best = min(candidates)
                pairs.append(("菲亚梅塔", best))
        else:
            for idx, nm in enumerate(room):
                if nm == "菲亚梅塔" and idx > 0:
                    pairs.append((nm, room[idx - 1]))
    return pairs


def _dorm_recovery_bonus(
    eng: Engine,
    asg: Assignment,
    mood: dict[str, float],
) -> dict[int, tuple[float, dict[str, float], float, int]]:
    """Per dorm: all-member bonus, single-target bonuses, average pool, nonfull count."""
    out: dict[int, tuple[float, dict[str, float], float, int]] = {}
    for idx, room_ops in enumerate(asg.dormitory):
        virtual_members = list(room_ops)
        best_all = best_avg_pool = 0.0
        other_by_target: dict[str, float] = {}
        nonfull_members = [nm for nm in room_ops if mood.get(nm, eng.cap) < eng.cap - 1e-6]
        for nm in room_ops:
            st = eng._stat(nm, "dormitory")
            if not st:
                continue
            prof = eng.prof.get(nm)
            target_buffs = []
            if prof:
                target_buffs = list(prof.room_buffs.get("dormitory", []))
                avg_pool = max(
                    (dorm_average_recover_pool(b) for b in target_buffs),
                    default=0.0,
                )
                best_avg_pool = max(best_avg_pool, avg_pool)
            low_mood_extra = max(
                (dorm_low_mood_extra_recover_triggered(b, room_ops, mood) for b in target_buffs),
                default=0.0,
            )
            self_excluded_all = max(
                (
                    dorm_all_recover_for(b, "__future_rester__")
                    - dorm_all_recover_for(b, nm)
                    for b in target_buffs
                ),
                default=0.0,
            )
            provider_all = max(0.0, st.dorm_recover_all - low_mood_extra - self_excluded_all)
            best_all = max(best_all, provider_all)
            other_recover = st.dorm_recover_other
            if best_avg_pool and abs(other_recover - best_avg_pool) < 1e-9:
                other_recover = 0.0
            candidates: list[tuple[str, float]] = []
            if nm in nonfull_members and st.dorm_recover_self:
                other_by_target[nm] = other_by_target.get(nm, 0.0) + st.dorm_recover_self
            for op in nonfull_members:
                targeted_recover = max(
                    (dorm_single_recover_for(b, op) for b in target_buffs),
                    default=0.0,
                )
                if op != nm:
                    targeted_recover = max(targeted_recover, other_recover)
                target_extra = max(
                    (dorm_target_extra_recover_for(b, op, virtual_members) for b in target_buffs),
                    default=0.0,
                )
                low_mood_target_extra = max(
                    (dorm_low_mood_extra_recover_for(b, op, mood) for b in target_buffs),
                    default=0.0,
                )
                target_all_extra = max(
                    (
                        max(0.0, dorm_all_recover_for(b, op) - dorm_all_recover_for(b, nm))
                        for b in target_buffs
                    ),
                    default=0.0,
                )
                single_bonus = targeted_recover + target_extra + low_mood_target_extra + target_all_extra
                if single_bonus:
                    candidates.append((op, single_bonus))
            if candidates:
                target, single_bonus = min(candidates, key=lambda item: (-item[1], mood.get(item[0], eng.cap)))
                if target is not None:
                    existing = other_by_target.get(target)
                    other_by_target[target] = single_bonus if existing is None else max(existing, single_bonus)
        out[idx] = (best_all, other_by_target, best_avg_pool, len(nonfull_members))
    return out


def _refresh_current_stats(eng: Engine, asg: Assignment, mood: dict[str, float]) -> None:
    """Refresh Engine context/resolved skills for the current gap before mood math."""
    saved_capacity_frac = eng._capacity_frac_override
    eng._capacity_frac_override = dict(saved_capacity_frac or {})
    for _ in range(8):
        eng._ctx = build_context(
            asg,
            mood=mood,
            active_frac=None,
            active_frac_default=1.0,
            fatigued=None,
            active_buffs={nm: prof.room_buffs for nm, prof in eng.prof.items()},
            recruit_slots_noninitial=eng._recruit_slots_noninitial(),
            dorm_levels=eng._dorm_levels(asg),
            facility_levels=eng._facility_levels(),
            drone_cap=eng.cfg.get("power", {}).get("drone_cap", 235),
        )
        eng._resolved = eng._resolve(eng._ctx)
        ctrl_red = eng._control_reduction(asg)
        glob = eng._globals(asg)
        hire_capacity = eng._hire_capacity_overrides(asg, ctrl_red, glob)
        new_capacity = dict(saved_capacity_frac or {})
        new_capacity.update(hire_capacity)
        if eng._frac_maps_close(eng._capacity_frac_override or {}, new_capacity):
            break
        eng._capacity_frac_override = new_capacity
    eng._capacity_frac_override = saved_capacity_frac


def simulate(eng: Engine, asg: Assignment, sched: Schedule, *,
             shifts: list[Assignment] | None = None,
             days: int = 7, initial_mood: float | None = None,
             rest_floor: float = 1.0) -> SimResult:
    """从 initial_mood 起, 逐 gap 推进 days 天, 返回每日产出曲线与累计。

    shifts: 多班次轮换排班, shifts[i] 用于 gaps[i % len(shifts)]。
            未指定时退化为单排班(asg)。指定时 asg 被忽略。
            多班次模式下, 每个 gap 开始时按当班 Assignment 决定工作/休息:
            在当班 Assignment 中的干员上岗(除非心情耗尽), 不在当班的干员自动进宿舍恢复。
    """
    if shifts is None:
        shifts = [asg]
    n_shifts = len(shifts)
    cap = eng.cap
    staggered = initial_mood is None
    init = cap if staggered else float(initial_mood)

    # 1) 收集所有班次的工作干员, 建立全局追踪集合。
    shift_work: list[list[tuple[str, str, int]]] = []
    for s_asg in shifts:
        shift_work.append(_working_rooms(eng, s_asg))
    all_tracked: set[str] = set()
    for sw in shift_work:
        for nm, _r, _i in sw:
            all_tracked.add(nm)
    for s_asg in shifts:
        for fiammetta, target in _fiammetta_swap_pairs(s_asg):
            all_tracked.add(fiammetta)
            all_tracked.add(target)

    # 稳态参考: 多班次按每日 gap 时长加权, 单班次退化为原有评估。
    steady_by_shift = [eng.evaluate(s_asg).ap_per_day for s_asg in shifts]
    steady_ap = sum(
        steady_by_shift[i % n_shifts] * gap / 24.0
        for i, gap in enumerate(sched.gaps)
    )
    dorm_capacity = len(shifts[0].dormitory) * eng.cfg["dormitory"]["slots"]

    # 初始心情: 错峰或统一
    if staggered and all_tracked:
        lo = rest_floor + 1.0
        if n_shifts == 1:
            stagger_list = [(nm, i) for i, (nm, _r, _i) in enumerate(shift_work[0])]
            n = max(1, len(stagger_list) - 1)
            mood = {nm: lo + (cap - lo) * (i / n) for nm, i in stagger_list}
        else:
            stagger_list = sorted(all_tracked)
            n = max(1, len(stagger_list) - 1)
            mood = {nm: lo + (cap - lo) * (i / n) for i, nm in enumerate(stagger_list)}
        for nm in all_tracked:
            mood.setdefault(nm, cap)
    else:
        mood = {nm: init for nm in all_tracked}

    def net_drain(nm, room, working_in_room, control_reduction, control_self_reduction,
                  other_recover, room_drain_adjustment):
        scoped_other = other_recover if room in OTHER_RECOVER_ROOMS else 0.0
        red = (control_self_reduction.get(nm, 0.0) if room == "control"
               else control_reduction + eng._room_reduction(working_in_room) + scoped_other + room_drain_adjustment)
        st = eng._stat(nm, room)
        drain_delta = st.drain_delta if st else 0.0
        return max(0.0, eng.base_drain + drain_delta - red)

    # 占空循环状态: 多班次下每 gap 重置, 单班次下保持迟滞(rest until cap)
    state: dict[str, str] = {}
    if n_shifts == 1:
        for nm, _r, _i in shift_work[0]:
            state[nm] = "work"

    gap_sched = Schedule([0])
    res = SimResult(steady_ap=steady_ap, initial_mood=init, staggered=staggered)

    for day in range(1, days + 1):
        day_bd: dict[str, float] = {}
        day_ap = 0.0
        frac_sum = 0.0
        frac_cnt = 0
        rest_hours = 0.0
        for gap_idx, g in enumerate(sched.gaps):
            cur_asg = shifts[gap_idx % n_shifts]
            work = shift_work[gap_idx % n_shifts]
            work_names = {nm for nm, _r, _i in work}
            room_members: dict[tuple, list[str]] = {}
            for nm, room, idx in work:
                room_members.setdefault((room, idx), []).append(nm)
            ctrl_ops = set(cur_asg.control)

            # (a) 确定谁休息
            if n_shifts == 1:
                want_rest = [nm for nm, _r, _i in work if state.get(nm) == "rest"]
                if len(want_rest) > dorm_capacity:
                    want_rest.sort(key=lambda nm: mood.get(nm, cap))
                    for nm in want_rest[dorm_capacity:]:
                        state[nm] = "work"
                want_rest = want_rest[:dorm_capacity]
            else:
                off_shift = all_tracked - work_names
                exhausted = {nm for nm, _r, _i in work if mood.get(nm, cap) <= rest_floor}
                want_rest = list(off_shift | exhausted)
                if len(want_rest) > dorm_capacity:
                    want_rest.sort(key=lambda nm: mood.get(nm, cap))
                    want_rest = want_rest[:dorm_capacity]
            resting_order = want_rest
            resting: set[str] = set(resting_order)

            asg_resting = _gap_assignment(cur_asg, resting_order, eng.cfg["dormitory"]["slots"])
            _refresh_current_stats(eng, asg_resting, mood)
            dorm_recovery_by_room = _dorm_recovery_bonus(eng, asg_resting, mood)
            dorm_room_of = {
                nm: idx
                for idx, room_ops in enumerate(asg_resting.dormitory)
                for nm in room_ops
            }
            # (b) 各房间在岗人数
            working_in = {rk: sum(1 for nm in mem if nm not in resting)
                          for rk, mem in room_members.items()}
            room_drain_reduction = {}
            for rk, mem in room_members.items():
                room, _idx = rk
                best = 0.0
                inc = 0.0
                for nm in mem:
                    if nm in resting:
                        continue
                    st = eng._stat(nm, room)
                    if st:
                        best = max(best, st.room_drain)
                        inc += st.room_drain_delta
                room_drain_reduction[rk] = best - inc
            control_reduction = eng.ctrl_drain_red * working_in.get(("control", 0), 0)
            control_room_recover = 0.0
            control_room_red = 0.0
            control_room_inc = 0.0
            control_other_delta: dict[str, float] = {}
            control_target_recover: dict[str, float] = {}
            control_target_delta: dict[str, float] = {}
            other_recover = 0.0
            ctrl_recover = 0.0
            ctrl_recover_elite = 0.0
            working_ctrl_ops = {nm for nm in ctrl_ops if nm not in resting}
            for nm in ctrl_ops:
                if nm in resting:
                    continue
                st = eng._stat(nm, "control")
                if st:
                    control_room_recover = max(control_room_recover, st.control_recover)
                    control_room_red = max(control_room_red, st.room_drain)
                    control_room_inc += st.room_drain_delta
                    if st.room_drain_delta_other:
                        for other in ctrl_ops:
                            if other != nm and other not in resting:
                                control_other_delta[other] = control_other_delta.get(other, 0.0) + st.room_drain_delta_other
                    other_recover = max(other_recover, st.other_recover)
                    ctrl_recover = max(ctrl_recover, st.global_recover)
                    ctrl_recover_elite = max(ctrl_recover_elite, st.global_recover_elite)
                prof = eng.prof.get(nm)
                if prof:
                    for b in prof.room_buffs.get("control", []):
                        d = b.desc
                        if "自身和阿米娅心情每小时恢复" in d and "阿米娅" in working_ctrl_ops:
                            m = re.search(r"恢复\+([0-9]+(?:\.[0-9]+)?)", d)
                            if m:
                                amt = float(m.group(1))
                                control_target_recover[nm] = control_target_recover.get(nm, 0.0) + amt
                                control_target_recover["阿米娅"] = control_target_recover.get("阿米娅", 0.0) + amt
                        elif "丰川祥子的心情每小时恢复" in d and "丰川祥子" in working_ctrl_ops:
                            m = re.search(r"恢复\+([0-9]+(?:\.[0-9]+)?)", d)
                            if m:
                                control_target_recover["丰川祥子"] = control_target_recover.get("丰川祥子", 0.0) + float(m.group(1))
                        elif "坚毅随和" in b.buff_name:
                            cnt = sum(
                                1
                                for op in working_ctrl_ops
                                if "鲤氏侦探事务所" in factions_of(op)
                            )
                            for op in working_ctrl_ops:
                                if "鲤氏侦探事务所" in factions_of(op):
                                    control_target_recover[op] = control_target_recover.get(op, 0.0) + 0.2 * cnt
                        elif "丰川祥子心情每小时消耗" in d and "丰川祥子" in working_ctrl_ops:
                            m = re.search(r"消耗\+([0-9]+(?:\.[0-9]+)?)", d)
                            if m:
                                control_target_delta["丰川祥子"] = control_target_delta.get("丰川祥子", 0.0) + float(m.group(1))
                        elif "当魔王进驻中枢时额外+0.1" in d and "魔王" in working_ctrl_ops:
                            other_recover = max(other_recover, 0.2)
            control_room_adjustment = control_room_red - control_room_inc
            control_self_base = control_reduction + control_room_recover + control_room_adjustment
            control_self_reduction = {
                nm: (
                    control_self_base
                    + control_target_recover.get(nm, 0.0)
                    - control_other_delta.get(nm, 0.0)
                    - control_target_delta.get(nm, 0.0)
                )
                for nm in ctrl_ops
                if nm not in resting
            }
            ctrl_working = any(nm in ctrl_ops and nm not in resting for nm in ctrl_ops)

            def rest_rate_for(nm: str) -> float:
                self_only = _self_only_dorm_recovery_rate(eng, nm)
                if self_only is not None:
                    return self_only
                elite_bonus = ctrl_recover_elite if "罗德岛-精英干员" in factions_of(nm) else 0.0
                dorm_bonus = 0.0
                room_idx = dorm_room_of.get(nm)
                if room_idx is not None:
                    base_rec = dorm_base_recover_for_room(eng.cfg, room_idx)
                    all_bonus, other_by_target, avg_pool, nonfull = dorm_recovery_by_room.get(
                        room_idx, (0.0, {}, 0.0, 0)
                    )
                    avg_bonus = avg_pool / max(1, nonfull) if avg_pool else 0.0
                    single_bonus = other_by_target.get(nm, 0.0)
                    dorm_bonus = max(all_bonus + single_bonus, avg_bonus)
                else:
                    base_rec = dorm_base_recover_for_room(eng.cfg, 0)
                return base_rec + dorm_bonus + ((ctrl_recover + elite_bonus) if ctrl_working else 0.0)

            for fiammetta, target in _fiammetta_swap_pairs(asg_resting, mood):
                if target in mood and mood.get(fiammetta, cap) >= cap - 1e-6 and mood[target] < cap - 1e-6:
                    mood[fiammetta], mood[target] = mood[target], cap
            mood_start = dict(mood)

            # (c) 逐干员推进心情
            override: dict[str, float] = {}
            fatigued: set[str] = set()
            for nm, room, idx in work:
                if nm in resting:
                    mood[nm] = min(cap, mood.get(nm, cap) + rest_rate_for(nm) * g)
                    if n_shifts == 1 and mood[nm] >= cap - 1e-6:
                        state[nm] = "work"
                    rest_hours += g
                    continue
                d = net_drain(nm, room, working_in[(room, idx)],
                              control_reduction, control_self_reduction, other_recover,
                              room_drain_reduction.get((room, idx), 0.0))
                if d <= 0:
                    active = 1.0
                else:
                    active = min(1.0, (mood.get(nm, cap) / d) / g if g > 0 else 1.0)
                    mood[nm] = max(0.0, mood.get(nm, cap) - d * g)
                    if mood[nm] <= 1e-9:
                        fatigued.add(nm)
                    if n_shifts == 1 and mood[nm] <= rest_floor:
                        state[nm] = "rest"
                if active <= 1e-9:
                    resting.add(nm)
                    rest_hours += g
                else:
                    override[nm] = active
                    frac_sum += active
                    frac_cnt += 1
            for nm, _room, _idx in work:
                if nm not in resting and nm not in override:
                    override[nm] = 0.0
            # 非当班干员恢复心情
            for nm in all_tracked - work_names:
                mood[nm] = min(cap, mood.get(nm, cap) + rest_rate_for(nm) * g)

            final_resting_order = resting_order + [
                nm for nm, _room, _idx in work
                if nm in resting and nm not in set(resting_order)
            ]
            asg_gap = _gap_assignment(cur_asg, final_resting_order, eng.cfg["dormitory"]["slots"])
            gap_sched.gaps = [g]
            gap_sched.max_gap = g
            eng.sch = gap_sched
            eng._frac_override = override
            eng._mood_override = {nm: mood_start.get(nm, cap) for nm, _room, _idx in work}
            eng._fatigued_override = fatigued
            eng._meeting_daily_bonus = _gap_crosses_hour(sched.hours[gap_idx], g, 4.0)
            eng._day_hours = g
            try:
                r = eng.evaluate(asg_gap)
            finally:
                eng.sch = sched
                eng._frac_override = None
                eng._mood_override = None
                eng._fatigued_override = None
                eng._meeting_daily_bonus = False
                eng._day_hours = 24.0
            day_ap += r.ap_per_day
            for k, v in r.breakdown.items():
                day_bd[k] = day_bd.get(k, 0.0) + v

        res.days.append(DayRecord(day=day, ap=day_ap, breakdown=day_bd,
                                  avg_work_fraction=(frac_sum / frac_cnt if frac_cnt else 0.0),
                                  resting_op_hours=rest_hours))
        res.cumulative_ap += day_ap

    # 可持续日产: 取后半程均值(跳过初期满心情红利与同步振荡的影响), 比末日单点更稳健
    if res.days:
        tail = res.days[max(0, len(res.days) // 2):]
        res.converged_ap = sum(d.ap for d in tail) / len(tail)
    return res
