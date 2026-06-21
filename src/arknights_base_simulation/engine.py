"""基建模拟引擎。

核心模型 (按上线时刻离散结算):
  * 上线时刻把一天分成若干『间隔』gap。两次上线之间干员不可换班。
  * 心情: 工作干员每小时净消耗 d = base_drain + 技能消耗。心情上限 cap。
    某干员在一个长度 g 的间隔里, 有效工作时长 = min(g, cap/d) —— 心情耗尽即停产。
    全天有效工作占比 frac = Σ_gap min(g, cap/d) / 24。频繁上线 + 低消耗 -> frac→1。
  * 产能溢出: 设施产物在间隔内累积, 超过容量(库存/订单上限)即浪费。
    日产量 = Σ_gap min(rate × g, capacity)。
  * 控制中枢全局加成: 所有制造站生产力% / 贸易站订单效率% / 心情恢复, 同种效果取最高。
  * 赤金链: 制造站产赤金 -> 贸易站赤金订单消耗赤金换龙门币; 多余赤金按素材价值计。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .effects import Effect, parse_buffs
from .skills import SkillDB
from .synergy import (
    build_context,
    dorm_all_recover_for,
    dorm_average_recover_pool,
    dorm_target_extra_recover,
    factions_of,
    manufacture_facility_count_scale,
    power_platform_count_scale,
    resolve_buff_effects,
)

OTHER_RECOVER_ROOMS = {"manufacture", "trading", "power", "hire", "meeting", "training"}


# ---------------------------------------------------------------- 干员档案
@dataclass
class RoomStat:
    """某干员在某设施的效果汇总。"""
    prod: dict[str, float] = field(default_factory=dict)   # line-> 生产力%
    capacity: float = 0.0
    trade_eff: float = 0.0
    order_limit: float = 0.0
    power: float = 0.0
    byproduct: float = 0.0
    byproduct_targets: dict[str, float] = field(default_factory=dict)
    byproduct_mood_cost_targets: list[tuple[str, float, float]] = field(default_factory=list)
    clue: float = 0.0
    contact: float = 0.0
    train_speed: float = 0.0
    train_initial_progress: float = 0.0
    dorm_recover: float = 0.0          # =all+other, 给"他人"的恢复(优化器排序用)
    dorm_recover_all: float = 0.0      # 宿舍内"所有干员"恢复 (同种取最高)
    dorm_recover_other: float = 0.0    # 给"某个他人"的恢复 (同种取最高)
    dorm_recover_self: float = 0.0     # 仅恢复自身 (不加速来休息的干员)
    # 仅 control:
    prod_global: dict[str, float] = field(default_factory=dict)
    trade_eff_global: float = 0.0
    clue_global: float = 0.0
    contact_global: float = 0.0
    global_recover: float = 0.0
    global_recover_elite: float = 0.0
    control_recover: float = 0.0
    other_recover: float = 0.0
    room_drain: float = 0.0
    room_drain_delta: float = 0.0
    room_drain_delta_other: float = 0.0
    # 工作净心情消耗增量 (base 之外)
    drain_delta: float = 0.0


class OperatorProfile:
    """缓存一个干员在各设施的 RoomStat。"""

    def __init__(self, name: str, elite: int, level: int, db: SkillDB, rarity: int = 0):
        self.name = name
        self.elite = elite
        self.level = level
        self.rarity = rarity
        self.room_buffs = db.buffs_for(name, elite, level)   # 原始技能, 供动态解析
        self.rooms: dict[str, RoomStat] = {}                 # 静态聚合, 供优化器候选排序
        for room, buffs in self.room_buffs.items():
            self.rooms[room] = _aggregate(parse_buffs(buffs))

    def stat(self, room: str) -> RoomStat | None:
        return self.rooms.get(room)


def _indexed_config_value(raw, idx: int, default=None):
    if isinstance(raw, list):
        if idx < len(raw):
            return raw[idx]
        return default
    return raw if raw is not None else default


def dorm_base_recover_for_room(cfg: dict, idx: int) -> float:
    """宿舍指定房间基础每小时恢复(PRTS公式)。

    Config may use scalar level/ambiance for every dorm, or lists for per-room
    levels/ambiance values. If level is None, use base_recover_per_hour.
    """
    dc = cfg["dormitory"]
    lvl = _indexed_config_value(dc.get("level"), idx)
    if lvl is None:
        return float(dc.get("base_recover_per_hour", 2.0))
    level = int(lvl)
    raw_amb = _indexed_config_value(dc.get("ambiance"), idx, 0)
    amb = min(float(raw_amb or 0), 1000 * level)
    return 1.5 + 0.1 * level + 0.0004 * amb


def dorm_base_recover(cfg: dict) -> float:
    """宿舍空房基础每小时恢复; scalar-compatible wrapper for room 0."""
    return dorm_base_recover_for_room(cfg, 0)


def _rejects_other_mood_recovery(prof: OperatorProfile | None) -> bool:
    if prof is None:
        return False
    return any(
        "无法获得其他来源提供的心情恢复效果" in b.desc
        for b in prof.room_buffs.get("dormitory", [])
    )


def _aggregate(effects: list[Effect]) -> RoomStat:
    s = RoomStat()
    for e in effects:
        if e.kind == "prod":
            s.prod[e.target] = s.prod.get(e.target, 0.0) + e.amount
        elif e.kind == "prod_global":
            s.prod_global[e.target] = max(s.prod_global.get(e.target, 0.0), e.amount)
        elif e.kind == "capacity":
            s.capacity += e.amount
        elif e.kind == "trade_eff":
            s.trade_eff += e.amount
        elif e.kind == "trade_eff_global":
            s.trade_eff_global = max(s.trade_eff_global, e.amount)
        elif e.kind == "clue_global":
            s.clue_global = max(s.clue_global, e.amount)
        elif e.kind == "contact_global":
            s.contact_global = max(s.contact_global, e.amount)
        elif e.kind == "order_limit":
            s.order_limit += e.amount
        elif e.kind == "power":
            s.power += e.amount
        elif e.kind == "byproduct":
            s.byproduct += e.amount
            target = e.target or "any"
            s.byproduct_targets[target] = s.byproduct_targets.get(target, 0.0) + e.amount
        elif e.kind == "byproduct_mood_cost":
            s.byproduct_mood_cost_targets.append((e.target or "any", float(e.mood_cost or 0.0), e.amount))
        elif e.kind == "clue":
            s.clue += e.amount
        elif e.kind == "contact":
            s.contact += e.amount
        elif e.kind == "train_speed":
            s.train_speed += e.amount
        elif e.kind == "train_initial_progress":
            s.train_initial_progress = max(s.train_initial_progress, e.amount)
        elif e.kind == "dorm_recover":            # 兼容旧路径
            s.dorm_recover = max(s.dorm_recover, e.amount)
        elif e.kind == "dorm_recover_all":
            s.dorm_recover_all = max(s.dorm_recover_all, e.amount)
        elif e.kind == "dorm_recover_other":
            s.dorm_recover_other = max(s.dorm_recover_other, e.amount)
        elif e.kind == "dorm_recover_self":
            s.dorm_recover_self += e.amount
        elif e.kind == "global_recover":
            s.global_recover = max(s.global_recover, e.amount)
        elif e.kind == "global_recover_elite":
            s.global_recover_elite = max(s.global_recover_elite, e.amount)
        elif e.kind == "control_recover":
            s.control_recover = max(s.control_recover, e.amount)
        elif e.kind == "other_recover":
            s.other_recover = max(s.other_recover, e.amount)
        elif e.kind == "room_drain":
            s.room_drain = max(s.room_drain, e.amount)
        elif e.kind == "room_drain_delta":
            s.room_drain_delta += e.amount
        elif e.kind == "room_drain_delta_other":
            s.room_drain_delta_other += e.amount
        elif e.kind == "mood_drain":
            s.drain_delta += e.amount
    s.dorm_recover += s.dorm_recover_all + s.dorm_recover_other
    return s


# ---------------------------------------------------------------- 排班/心情
class Schedule:
    """每日上线时刻 -> 间隔列表与最长间隔。"""

    def __init__(self, login_hours: list[float]):
        hrs = sorted(set(float(h) % 24 for h in login_hours))
        if not hrs:
            hrs = [0.0]
        gaps = []
        for i in range(len(hrs)):
            nxt = hrs[(i + 1) % len(hrs)]
            g = (nxt - hrs[i]) % 24
            gaps.append(g if g > 0 else 24.0)
        if len(hrs) == 1:
            gaps = [24.0]
        self.hours = hrs
        self.gaps = gaps
        self.max_gap = max(gaps)

    def work_fraction(self, drain: float, cap: float) -> float:
        """净消耗 drain、心情上限 cap 的干员, 全天有效工作占比。"""
        if drain <= 0:
            return 1.0
        runtime = cap / drain
        worked = sum(min(g, runtime) for g in self.gaps)
        return worked / 24.0

    def daily_capped(self, rate_per_hour: float, capacity: float) -> float:
        """每小时产 rate、容量 capacity 的设施, 计入溢出后的日产量。"""
        if capacity <= 0:
            return rate_per_hour * 24.0
        return sum(min(rate_per_hour * g, capacity) for g in self.gaps)

    def daily_capped_with_bonus(
        self,
        rate_per_hour: float,
        capacity: float,
        bonus: float,
        bonus_hour: float,
    ) -> tuple[float, float, float]:
        """按上线间隔封顶, 并把每日固定入库奖励放进其所在间隔的同一容量池。

        返回 (总入库, 自动搜集入库, 固定奖励入库)。
        """
        if capacity <= 0:
            generated = rate_per_hour * 24.0
            return generated + bonus, generated, bonus

        total = 0.0
        accepted_bonus = 0.0
        for start, gap in zip(self.hours, self.gaps):
            if gap <= 0:
                continue
            offset = (bonus_hour - start) % 24.0
            has_bonus = 0.0 <= offset < gap and bonus > 0.0
            if not has_bonus:
                total += min(rate_per_hour * gap, capacity)
                continue

            before_bonus = min(rate_per_hour * offset, capacity)
            gap_bonus = min(bonus, max(0.0, capacity - before_bonus))
            accepted_bonus += gap_bonus
            after_bonus = min(
                capacity,
                before_bonus + gap_bonus + rate_per_hour * max(0.0, gap - offset),
            )
            total += after_bonus
        return total, max(0.0, total - accepted_bonus), accepted_bonus


# ---------------------------------------------------------------- 装配/结果
@dataclass
class Assignment:
    manufacture: list[tuple[str, list[str]]] = field(default_factory=list)  # (line, ops)
    trading: list[list[str]] = field(default_factory=list)
    power: list[list[str]] = field(default_factory=list)
    control: list[str] = field(default_factory=list)
    meeting: list[str] = field(default_factory=list)
    hire: list[str] = field(default_factory=list)
    workshop: list[str] = field(default_factory=list)
    training: list[str] = field(default_factory=list)
    dormitory: list[list[str]] = field(default_factory=list)


@dataclass
class Result:
    ap_per_day: float
    breakdown: dict[str, float]
    detail: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class Engine:
    def __init__(self, config: dict, profiles: dict[str, OperatorProfile], schedule: Schedule):
        self.cfg = config
        self.prof = profiles
        self.sch = schedule
        self.cap = config["mood"]["cap"]
        self.base_drain = config["mood"]["base_drain_per_hour"]
        self.ctrl_drain_red = config["mood"].get("control_drain_reduction", 0.0)
        self.occ_red = config["mood"].get("occupancy_reduction", [0.0, 0.0, 0.0, 0.0])
        self.val = config["material_values_ap"]
        self._ctx = None
        self._resolved: dict[str, RoomStat] = {}
        self._skip_duplicate_check = False
        # 瞬态模拟钩子(默认 None/24h, 不影响稳态评估):
        #   _frac_override: {干员名: 本结算窗口内的有效工作占比} —— 直接覆盖心情推导
        #   _day_hours:     本次结算窗口时长(小时), 用于无人机/线索/公招等按时长产出的项
        #   _mood_override: {干员名: 本结算窗口开始时心情}, 用于夕/令等阈值型中间产物分支
        #   _fatigued_override: 本窗口内心情耗尽的干员, 用于絮雨等清空型中间产物
        #   _meeting_daily_bonus: 本窗口是否跨过每日4:00会客室线索发放点
        self._frac_override: dict[str, float] | None = None
        self._capacity_frac_override: dict[str, float] | None = None
        self._mood_override: dict[str, float] | None = None
        self._fatigued_override: set[str] | None = None
        self._meeting_daily_bonus: bool = False
        self._day_hours: float = 24.0

    def _stat(self, name: str, room: str) -> RoomStat | None:
        # 动态: 用本次排班上下文解析出的效果(若该干员就排在此设施)
        st = self._resolved.get(name)
        if st is not None and self._ctx.placement.get(name, (None,))[0] == room:
            return st
        return None

    def _resolve(self, ctx) -> dict[str, RoomStat]:
        """对每个已排干员, 在其所在设施按上下文解析效果。"""
        out: dict[str, RoomStat] = {}
        for name, (room, _idx) in ctx.placement.items():
            prof = self.prof.get(name)
            if not prof:
                continue
            if not ctx.active(name):
                out[name] = RoomStat()
                continue
            effs: list[Effect] = []
            for b in prof.room_buffs.get(room, []):
                effs.extend(resolve_buff_effects(b, ctx, name))
            out[name] = _aggregate(effs)
        return out

    def _room_reduction(self, n_ops: int) -> float:
        """同一工作房间内的基础心情消耗减免。控制中枢全局减免另算。"""
        occ = self.occ_red[min(n_ops, len(self.occ_red) - 1)] if self.occ_red else 0.0
        return occ

    def _control_room_recover(self, asg: Assignment, reduction: float = 0.0) -> float:
        """控制中枢内所有干员的心情恢复技能, 同种取最高。"""
        rec = 0.0
        for nm in asg.control:
            st = self._stat(nm, "control")
            if st:
                rec = max(rec, st.control_recover * self._frac(nm, "control", reduction))
        return rec

    def _control_room_drain_adjustment(self, asg: Assignment, reduction: float = 0.0) -> float:
        """控制中枢内全员心情消耗净调整: 减免取最高, 增耗累加。"""
        red = 0.0
        inc = 0.0
        for nm in asg.control:
            st = self._stat(nm, "control")
            if not st:
                continue
            frac = self._frac(nm, "control", reduction)
            if st.room_drain:
                red = max(red, st.room_drain * frac)
            if st.room_drain_delta:
                inc += st.room_drain_delta * frac
        return red - inc

    def _control_other_drain_adjustment_for(self, target: str, asg: Assignment, reduction: float = 0.0) -> float:
        """控制中枢内"除自身外"心情消耗增加, 只作用于目标以外的干员。"""
        inc = 0.0
        for nm in asg.control:
            if nm == target:
                continue
            st = self._stat(nm, "control")
            if st and st.room_drain_delta_other:
                inc += st.room_drain_delta_other * self._frac(nm, "control", reduction)
        return -inc

    def _control_target_recover_for(self, target: str, asg: Assignment, reduction: float = 0.0) -> float:
        """控制中枢内指定干员心情恢复, 如 魔王/阿米娅 或 丰川祥子。"""
        rec = 0.0
        in_control = set(asg.control)
        for provider in asg.control:
            prof = self.prof.get(provider)
            if not prof:
                continue
            frac = self._frac(provider, "control", reduction)
            for b in prof.room_buffs.get("control", []):
                d = b.desc
                if "自身和阿米娅心情每小时恢复" in d and "阿米娅" in in_control and target in {provider, "阿米娅"}:
                    m = re.search(r"恢复\+([0-9]+(?:\.[0-9]+)?)", d)
                    if m:
                        rec += float(m.group(1)) * frac
                elif "丰川祥子的心情每小时恢复" in d and target == "丰川祥子" and "丰川祥子" in in_control:
                    m = re.search(r"恢复\+([0-9]+(?:\.[0-9]+)?)", d)
                    if m:
                        rec += float(m.group(1)) * frac
                elif (
                    "坚毅随和" in b.buff_name
                    and "鲤氏侦探事务所" in factions_of(target)
                    and target in in_control
                ):
                    cnt = sum(
                        1
                        for nm in asg.control
                        if "鲤氏侦探事务所" in factions_of(nm)
                        and self._frac(nm, "control", reduction) > 0.0
                    )
                    rec += 0.2 * cnt * frac
        return rec

    def _control_target_drain_adjustment_for(self, target: str, asg: Assignment, reduction: float = 0.0) -> float:
        """控制中枢内指定干员心情消耗增加, 如 祐天寺若麦→丰川祥子。"""
        inc = 0.0
        if target != "丰川祥子" or "丰川祥子" not in set(asg.control):
            return 0.0
        for provider in asg.control:
            prof = self.prof.get(provider)
            if not prof:
                continue
            frac = self._frac(provider, "control", reduction)
            for b in prof.room_buffs.get("control", []):
                d = b.desc
                if "丰川祥子心情每小时消耗" in d:
                    m = re.search(r"消耗\+([0-9]+(?:\.[0-9]+)?)", d)
                    if m:
                        inc += float(m.group(1)) * frac
        return -inc

    def _control_effective_reduction_for(self, target: str, asg: Assignment) -> float:
        """某个控制中枢干员实际使用的心情减免, 含"除自身外"定向增耗。"""
        red = self._control_self_reduction(asg)
        return (
            red
            + self._control_target_recover_for(target, asg, red)
            + self._control_other_drain_adjustment_for(target, asg, red)
            + self._control_target_drain_adjustment_for(target, asg, red)
        )

    def _control_reduction(self, asg: Assignment) -> float:
        """控制中枢给工作区提供的全局心情消耗减免。

        稳态路径用控制中枢干员的有效工作占比折算; 瞬态路径的 _frac_override 会直接给出
        当前 gap 内是否在岗/有效工作的占比。干员自身技能心情消耗仍由 _net_drain 处理。
        """
        return sum(self.ctrl_drain_red * self._frac(nm, "control", self._control_effective_reduction_for(nm, asg))
                   for nm in asg.control)

    def _control_self_reduction(self, asg: Assignment) -> float:
        """控制中枢自身心情消耗减免: 0.05 * 有效在岗控制中枢人数 + 控制中枢恢复技能。"""
        red = self.ctrl_drain_red * len(asg.control)
        for _ in range(8):
            active_ops = sum(
                self._frac(
                    nm,
                    "control",
                    red
                    + self._control_target_recover_for(nm, asg, red)
                    + self._control_other_drain_adjustment_for(nm, asg, red)
                    + self._control_target_drain_adjustment_for(nm, asg, red),
                )
                for nm in asg.control
            )
            new_red = (
                self.ctrl_drain_red * active_ops
                + self._control_room_recover(asg, red)
                + self._control_room_drain_adjustment(asg, red)
            )
            if abs(new_red - red) < 1e-9:
                return new_red
            red = new_red
        return red

    def _net_drain(self, name: str, room: str, reduction: float = 0.0) -> float:
        st = self._stat(name, room)
        delta = st.drain_delta if st else 0.0
        if room == "training":
            _speed_sub, drain_sub = self._training_condition_subtractions(name)
            delta -= drain_sub
        return max(0.0, self.base_drain + delta - reduction)

    def _training_target_level(self) -> int:
        """当前训练计划的目标专精等级。默认3维持旧的满条件估值口径。"""
        try:
            return int(self.cfg.get("training", {}).get("target_mastery_level", 3))
        except (TypeError, ValueError):
            return 3

    def _training_max_mastery_level(self) -> int:
        """Max mastery level supported by current training-room level; defaults match PRTS L3."""
        trc = self.cfg.get("training", {})
        try:
            level = int(trc.get("level", 3))
        except (TypeError, ValueError):
            level = 3
        by_level = trc.get("max_mastery_by_level", {})
        raw = by_level.get(str(level), by_level.get(level)) if by_level else level
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 3

    def _training_plan_supported(self) -> bool:
        return self._training_target_level() <= self._training_max_mastery_level()

    def _training_target_branch(self) -> str | None:
        branch = self.cfg.get("training", {}).get("target_branch")
        return str(branch) if branch else None

    def _training_target_profession(self) -> str | None:
        profession = self.cfg.get("training", {}).get("target_profession")
        return str(profession) if profession else None

    def _training_base_session_hours(self) -> float:
        """PRTS 基础专精时间: M1=8h, M2=16h, M3=24h; 配置显式给正数时覆盖。"""
        trc = self.cfg.get("training", {})
        try:
            override = float(trc.get("base_session_hours", 0.0))
        except (TypeError, ValueError):
            override = 0.0
        if override > 0.0:
            return override
        return {1: 8.0, 2: 16.0, 3: 24.0}.get(self._training_target_level(), 24.0)

    def _training_condition_subtractions(self, name: str) -> tuple[float, float]:
        """训练技能的条件额外项未满足时, 从 values 表的满条件值中扣除。

        values.json 对不少训练技能记录的是满条件总值, 如 30+65=95。大模拟没有逐次
        训练任务时默认 target_mastery_level=3, 但配置成1/2时需要扣掉不匹配的额外项。
        """
        prof = self.prof.get(name)
        if not prof:
            return 0.0, 0.0
        target_level = self._training_target_level()
        target_branch = self._training_target_branch()
        target_profession = self._training_target_profession()
        speed_sub = 0.0
        drain_sub = 0.0
        num = r"([0-9]+(?:\.[0-9]+)?)"
        professions = ("先锋", "近卫", "重装", "狙击", "术师", "医疗", "辅助", "特种")
        for b in prof.room_buffs.get("training", []):
            d = b.desc
            if target_profession:
                allowed = [p for p in professions if f"{p}干员" in d or f"{p}与" in d or f"与{p}" in d or f"{p}专精" in d]
                if allowed and target_profession not in allowed:
                    speed = float(b.value or 0.0)
                    if speed <= 0.0:
                        for m in re.finditer(rf"(?:训练速度|专精技能训练速度)(?:额外)?(?:提升)?\+{num}%", d):
                            speed += float(m.group(1))
                    speed_sub += speed
                    for m in re.finditer(rf"心情每小时消耗\+{num}", d):
                        drain_sub += float(m.group(1))
                    continue
            for m in re.finditer(rf"如果本次训练专精技能至([123])级.*?(?:训练速度|专精技能训练速度)(?:额外)?(?:提升)?\+{num}%", d):
                if target_level != int(m.group(1)):
                    speed_sub += float(m.group(2))
            for m in re.finditer(rf"如果训练位干员分支为([^\s，。；]+).*?训练速度额外\+{num}%", d):
                if target_branch and target_branch != m.group(1):
                    speed_sub += float(m.group(2))
            for m in re.finditer(rf"训练专精技能至([123])级时，心情每小时消耗\+{num}", d):
                if target_level != int(m.group(1)):
                    drain_sub += float(m.group(2))
        return speed_sub, drain_sub

    def _training_initial_progress(self, name: str, st: RoomStat | None) -> float:
        """Immediate progress at training start, including explicit stateful skill switches."""
        if not st:
            return 0.0
        initial = st.train_initial_progress
        trc = self.cfg.get("training", {})
        if (
            name == "左乐"
            and trc.get("zuo_le_martial_ready", False)
            and self._training_target_level() == 1
            and self._training_target_profession() == "近卫"
        ):
            initial = max(initial, 100.0)
        return initial

    def _frac(self, name: str, room: str, reduction: float = 0.0) -> float:
        # PRTS: 加工站驻员通常为空闲中, 不按小时心情消耗；副产品技能在加工时生效。
        # 但注意力涣散时无法获得副产品。
        if room == "workshop":
            return 0.0 if self._fatigued_override is not None and name in self._fatigued_override else 1.0
        base = self._base_frac(name, room, reduction)
        if self._capacity_frac_override is not None and name in self._capacity_frac_override:
            base = min(base, self._capacity_frac_override[name])
        return base

    def _base_frac(self, name: str, room: str, reduction: float = 0.0) -> float:
        if self._frac_override is not None:
            return self._frac_override.get(name, 0.0)
        return self.sch.work_fraction(self._net_drain(name, room, reduction), self.cap)

    def _context_active_frac(self) -> dict[str, float] | None:
        if self._frac_override is None and not self._capacity_frac_override:
            return None
        out = dict(self._frac_override or {})
        for nm, frac in (self._capacity_frac_override or {}).items():
            out[nm] = min(out.get(nm, 1.0), frac)
        return out

    def _recruit_slots_noninitial(self) -> int:
        try:
            slots = int(
                self._level_value("hire", "recruit_slots_by_level", "recruit_slots", 4.0)
            )
        except (TypeError, ValueError):
            slots = 4
        return max(0, slots - 1)

    def _dorm_levels(self, asg: Assignment) -> dict[int, int]:
        dc = self.cfg.get("dormitory", {})
        raw = dc.get("level", 5)
        if isinstance(raw, list):
            levels = [int(v) for v in raw]
            return {idx: levels[idx] if idx < len(levels) else 5 for idx, _ops in enumerate(asg.dormitory)}
        try:
            level = int(raw)
        except (TypeError, ValueError):
            level = 5
        return {idx: level for idx, _ops in enumerate(asg.dormitory)}

    def _facility_levels(self) -> dict[str, int]:
        defaults = {
            "manufacture": 3,
            "trading": 3,
            "power": 3,
            "meeting": 3,
            "hire": 3,
            "workshop": 3,
            "training": 3,
            "control": 5,
        }
        out = {}
        for room, default in defaults.items():
            try:
                out[room] = int(self.cfg.get(room, {}).get("level", default))
            except (TypeError, ValueError):
                out[room] = default
        return out

    def _facility_level_violations(self, asg: Assignment) -> list[dict[str, int | str]]:
        """PRTS: control center level is also the maximum level for other facilities."""
        levels = self._facility_levels()
        max_level = levels.get("control", 5)
        violations: list[dict[str, int | str]] = []
        for room, level in levels.items():
            if room == "control":
                continue
            if level > max_level:
                violations.append({"room": room, "level": level, "max_level": max_level})

        for idx, level in self._dorm_levels(asg).items():
            if level > max_level:
                violations.append({
                    "room": "dormitory",
                    "index": idx,
                    "level": level,
                    "max_level": max_level,
                })
        return violations

    def _facility_slots(self, section: str, room_idx: int = 0) -> int:
        cfg = self.cfg.get(section, {})
        try:
            fallback = int(cfg.get("slots", 0))
        except (TypeError, ValueError):
            fallback = 0
        level = self._room_level(section, room_idx)
        by_level = cfg.get("slots_by_level", {})
        raw = by_level.get(str(level), by_level.get(level)) if by_level else None
        if raw is not None:
            try:
                return int(raw)
            except (TypeError, ValueError):
                pass
        return fallback

    def _facility_capacity_violations(self, asg: Assignment) -> list[dict[str, int | str]]:
        """PRTS facility and operator-slot limits for manual assignments."""
        violations: list[dict[str, int | str]] = []
        layout = self.cfg.get("layout", {})
        manufacture_lines = set(self.cfg.get("manufacture", {}).get("lines", {}))
        for idx, (line, _ops) in enumerate(asg.manufacture):
            if line not in manufacture_lines:
                violations.append({
                    "type": "invalid_manufacture_line",
                    "room": "manufacture",
                    "index": idx,
                    "line": line,
                })
        room_counts = {
            "manufacture": len(asg.manufacture),
            "trading": len(asg.trading),
            "power": len(asg.power),
        }
        for room, count in room_counts.items():
            max_key = f"max_{room}"
            try:
                max_count = int(layout.get(max_key, count))
            except (TypeError, ValueError):
                max_count = count
            if count > max_count:
                violations.append({
                    "type": "room_count",
                    "room": room,
                    "count": count,
                    "max": max_count,
                })

        try:
            production_slots = int(layout.get("production_slots", sum(room_counts.values())))
        except (TypeError, ValueError):
            production_slots = sum(room_counts.values())
        production_count = sum(room_counts.values())
        if production_count > production_slots:
            violations.append({
                "type": "production_room_count",
                "room": "production",
                "count": production_count,
                "max": production_slots,
            })

        fixed_rooms = layout.get("fixed_rooms", {})
        fixed_room_occupancy = {
            "control": 1 if asg.control else 0,
            "meeting": 1 if asg.meeting else 0,
            "hire": 1 if asg.hire else 0,
            "workshop": 1 if asg.workshop else 0,
            "training": 1 if asg.training else 0,
        }
        for room, count in fixed_room_occupancy.items():
            try:
                max_count = int(fixed_rooms.get(room, 1))
            except (TypeError, ValueError):
                max_count = 1
            if count > max_count:
                violations.append({
                    "type": "room_count",
                    "room": room,
                    "count": count,
                    "max": max_count,
                })

        dorm_cfg = self.cfg.get("dormitory", {})
        try:
            max_dorms = int(dorm_cfg.get("max_rooms", len(asg.dormitory)))
        except (TypeError, ValueError):
            max_dorms = len(asg.dormitory)
        if "dormitory" in fixed_rooms:
            try:
                max_dorms = min(max_dorms, int(fixed_rooms.get("dormitory", max_dorms)))
            except (TypeError, ValueError):
                pass
        if len(asg.dormitory) > max_dorms:
            violations.append({
                "type": "room_count",
                "room": "dormitory",
                "count": len(asg.dormitory),
                "max": max_dorms,
            })

        room_ops = (
            [("manufacture", idx, ops) for idx, (_line, ops) in enumerate(asg.manufacture)]
            + [("trading", idx, ops) for idx, ops in enumerate(asg.trading)]
            + [("power", idx, ops) for idx, ops in enumerate(asg.power)]
            + [("dormitory", idx, ops) for idx, ops in enumerate(asg.dormitory)]
            + [("control", 0, asg.control), ("meeting", 0, asg.meeting), ("hire", 0, asg.hire),
               ("workshop", 0, asg.workshop), ("training", 0, asg.training)]
        )
        for room, idx, ops in room_ops:
            max_ops = self._facility_slots(room, idx)
            if max_ops >= 0 and len(ops) > max_ops:
                violations.append({
                    "type": "operator_slots",
                    "room": room,
                    "index": idx,
                    "count": len(ops),
                    "max": max_ops,
                })
        placements: dict[str, list[str]] = {}
        for room, idx, ops in room_ops:
            for nm in ops:
                placements.setdefault(nm, []).append(f"{room}:{idx}")
        if not self._skip_duplicate_check:
            for nm, places in sorted(placements.items()):
                if len(places) > 1:
                    violations.append({
                        "type": "duplicate_operator",
                        "operator": nm,
                        "count": len(places),
                    })
        return violations

    def _room_level(self, section: str, room_idx: int = 0) -> int:
        cfg = self.cfg.get(section, {})
        default = 5 if section in ("control", "dormitory") else 3
        raw = cfg.get("level", default)
        val = _indexed_config_value(raw, room_idx, default=raw if not isinstance(raw, list) else default)
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _level_value(self, section: str, level_key: str, fallback_key: str, fallback: float,
                     room_idx: int = 0) -> float:
        cfg = self.cfg.get(section, {})
        try:
            fallback_value = float(cfg.get(fallback_key, fallback))
        except (TypeError, ValueError):
            fallback_value = float(fallback)
        if abs(fallback_value - float(fallback)) > 1e-9:
            return fallback_value
        level = self._room_level(section, room_idx)
        by_level = cfg.get(level_key, {})
        if by_level:
            raw = by_level.get(str(level), by_level.get(level))
            if raw is not None:
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
        return fallback_value

    def _manufacture_capacity_volume(self, room_idx: int = 0) -> float:
        """Manufacture storage volume by facility level; defaults match PRTS level 3."""
        return self._level_value("manufacture", "capacity_volume_by_level", "capacity_volume", 54.0, room_idx)

    def _trading_order_limit(self, room_idx: int = 0) -> float:
        """Trading room base order limit by facility level; defaults match PRTS level 3."""
        return self._level_value("trading", "order_limit_by_level", "order_limit", 10.0, room_idx)

    def _trading_gold_order_profile(self, room_idx: int = 0) -> dict:
        tc = self.cfg["trading"]
        base = dict(tc["gold_order"])
        level = self._room_level("trading", room_idx)
        by_level = tc.get("gold_order_by_level", {})
        default = by_level.get("3", by_level.get(3, {})) if by_level else {}
        keys = {"base_minutes_per_order", "gold_per_order", "lmd_per_order", "prob_2_gold", "prob_3_gold", "native_4_gold_probability"}
        if any(abs(float(base.get(k, 0.0)) - float(default.get(k, base.get(k, 0.0)))) > 1e-9 for k in keys):
            return base
        profile = by_level.get(str(level), by_level.get(level)) if by_level else None
        return dict(profile or base)

    def _power_supply_per_station(self, room_idx: int = 0) -> float:
        """Power station supply by facility level; defaults match PRTS level 3."""
        return self._level_value("power", "supply_per_station_by_level", "supply_per_station", 270.0, room_idx)

    def _drone_assist_unlocked(self) -> bool:
        cc = self.cfg.get("control", {})
        try:
            level = int(cc.get("level", 5))
        except (TypeError, ValueError):
            level = 5
        try:
            required = int(cc.get("drone_assist_min_level", 3))
        except (TypeError, ValueError):
            required = 3
        return level >= required

    def _electricity_consumption(self, room: str, room_idx: int = 0) -> float:
        ec = self.cfg["electricity"]
        base = float(ec.get("consumption", {}).get(room, 0.0))
        by_level = ec.get("consumption_by_level", {}).get(room, {})
        if not by_level:
            return base
        level = self._room_level(room, room_idx)
        raw = by_level.get(str(level), by_level.get(level))
        if raw is None:
            return base
        try:
            return float(raw)
        except (TypeError, ValueError):
            return base

    # ---- 全局加成 (控制中枢) ----
    def _globals(self, asg: Assignment) -> dict:
        g = {
            "prod": {}, "trade_eff": 0.0, "clue": 0.0, "contact": 0.0,
            "recover": 0.0, "recover_elite": 0.0, "control_recover": 0.0, "other_recover": 0.0,
        }
        for name in asg.control:
            st = self._stat(name, "control")
            if not st:
                continue
            frac = self._frac(name, "control", self._control_effective_reduction_for(name, asg))
            prof = self.prof.get(name)
            if prof:
                for b in prof.room_buffs.get("control", []):
                    d = b.desc
                    if (
                        "当伊内丝入驻会客室时，会客室线索搜集速度+5%" in d
                        and self._ctx.placement.get("伊内丝", ("",))[0] == "meeting"
                        and self._ctx.active("伊内丝")
                    ):
                        g["clue"] = max(g["clue"], 5.0 * frac)
                    if (
                        "当魔王进驻中枢时额外+0.1" in d
                        and self._ctx.placement.get("魔王", ("",))[0] == "control"
                        and self._ctx.active("魔王")
                    ):
                        g["other_recover"] = max(g["other_recover"], 0.2 * frac)
            for line, amt in st.prod_global.items():
                g["prod"][line] = max(g["prod"].get(line, 0.0), amt * frac)
            g["trade_eff"] = max(g["trade_eff"], st.trade_eff_global * frac)
            g["clue"] = max(g["clue"], st.clue_global * frac)
            g["contact"] = max(g["contact"], st.contact_global * frac)
            g["recover"] = max(g["recover"], st.global_recover * frac)
            g["recover_elite"] = max(g["recover_elite"], st.global_recover_elite * frac)
            g["control_recover"] = max(g["control_recover"], st.control_recover * frac)
            g["other_recover"] = max(g["other_recover"], st.other_recover * frac)
        for name, (room, _idx) in self._ctx.placement.items():
            if room == "control":
                continue
            st = self._stat(name, room)
            if not st:
                continue
            frac = self._frac(name, room, self._control_reduction(asg))
            g["clue"] = max(g["clue"], st.clue_global * frac)
            g["contact"] = max(g["contact"], st.contact_global * frac)
        return g

    def _line_prod(self, prod_map: dict[str, float], line: str) -> float:
        """某生产线得到的生产力% = 该线专属 + 通用(all)。"""
        return prod_map.get(line, 0.0) + prod_map.get("all", 0.0)

    def _warmup_gaps(self) -> list[float]:
        """当前结算窗口内的暖机时长。稳态按上线间隔重置; 单窗口按当前时长。"""
        if abs(self._day_hours - 24.0) > 1e-9:
            return [self._day_hours]
        return list(self.sch.gaps)

    @staticmethod
    def _clue_preference_tag(name: str, desc: str) -> str | None:
        """日常线索搜集的目标/倾向标签。线索交流专用技能不计入。"""
        if "处于线索交流时" in desc:
            return None
        if "更容易获得线索板上尚未拥有的线索" in desc:
            return f"{name}:未拥有线索倾向"
        if "更容易获得线索板上已经拥有的线索" in desc:
            return f"{name}:已拥有线索倾向"
        m = re.search(r"更容易获得(.+?)线索", desc)
        if m:
            return f"{name}:{m.group(1)}线索倾向"
        m = re.search(r"额外增加(.+?)线索的出现概率", desc)
        if m:
            return f"{name}:非目标后提高{m.group(1)}线索概率"
        return None

    def _meeting_clue_tags(self, ops: list[str], control_ops: list[str], reduction: float) -> list[str]:
        """会客室线索倾向/必定线索标签。当前收益仍只估算线索数量。"""
        tags: list[str] = []
        gaps = self._warmup_gaps()

        def add_tag(tag: str | None):
            if tag and tag not in tags:
                tags.append(tag)

        for nm in ops:
            if self._frac(nm, "meeting", reduction) <= 0.0:
                continue
            prof = self.prof.get(nm)
            if not prof:
                continue
            for b in prof.room_buffs.get("meeting", []):
                d = b.desc
                add_tag(self._clue_preference_tag(nm, d))
                if "只有自身处于工作状态" in d:
                    if any(other != nm and self._frac(other, "meeting", reduction) > 0.0 for other in ops):
                        continue
                if "连续消耗超过16点心情" in d and "必定获得" in d:
                    drain = self._net_drain(nm, "meeting", reduction)
                    if drain <= 0.0:
                        continue
                    max_consumed = max(min(gap, self.cap / drain) * drain for gap in gaps)
                    if max_consumed <= 16.0:
                        continue
                    m = re.search(r"必定获得(.+?)的线索", d)
                    target = m.group(1) if m else "指定"
                    add_tag(f"{nm}:必定获得{target}线索")
        for nm in control_ops:
            frac = self._frac(nm, "control", self._control_effective_reduction_for(nm, Assignment(control=control_ops)))
            if frac <= 0.0:
                continue
            prof = self.prof.get(nm)
            if not prof:
                continue
            for b in prof.room_buffs.get("control", []):
                if "线索" in b.desc:
                    add_tag(self._clue_preference_tag(nm, b.desc))
        return tags

    @staticmethod
    def _warmup_average_amount(desc: str, gaps: list[float]) -> float | None:
        """解析进驻后逐小时增长并计算窗口平均值。"""
        if "最终达到" not in desc or not gaps:
            return None
        cap_m = re.search(r"最终达到\+?([0-9]+(?:\.[0-9]+)?)%", desc)
        if not cap_m:
            return None
        rate_m = re.search(r"(?:此后)?每小时(?:提升|\+)([0-9]+(?:\.[0-9]+)?)%", desc)
        if not rate_m:
            return None
        cap = float(cap_m.group(1))
        rate = float(rate_m.group(1))
        if rate <= 0:
            return cap

        first_m = re.search(r"首小时\+?([0-9]+(?:\.[0-9]+)?)%", desc)
        base_m = re.search(r"(?:速度提升|生产力提升|生产力\+)\s*([0-9]+(?:\.[0-9]+)?)%", desc)
        if first_m:
            initial = float(first_m.group(1))
            hold = 1.0
        elif base_m:
            initial = float(base_m.group(1))
            hold = 0.0
        else:
            initial = 0.0
            hold = 0.0

        def integral(gap: float) -> float:
            if gap <= 0:
                return 0.0
            if initial >= cap:
                return cap * gap
            total = initial * min(gap, hold)
            if gap <= hold:
                return total
            ramp_len = min(gap - hold, (cap - initial) / rate)
            total += initial * ramp_len + 0.5 * rate * ramp_len * ramp_len
            total += cap * max(0.0, gap - hold - ramp_len)
            return total

        total_hours = sum(gaps)
        if total_hours <= 0:
            return None
        return sum(integral(g) for g in gaps) / total_hours

    @staticmethod
    def _buff_line_target(desc: str) -> str:
        if "贵金属" in desc or "赤金" in desc:
            return "gold"
        if "作战记录" in desc:
            return "record"
        if "源石" in desc:
            return "shard"
        return "all"

    def _warmup_adjustment(self, name: str, room: str, kind: str, category: str | None = None) -> float:
        """返回暖机技能相对静态满值的修正量(avg - final)。"""
        prof = self.prof.get(name)
        if not prof:
            return 0.0
        adj = 0.0
        gaps = self._warmup_gaps()
        for b in prof.room_buffs.get(room, []):
            avg = self._warmup_average_amount(b.desc, gaps)
            if avg is None:
                continue
            if kind == "prod":
                target = self._buff_line_target(b.desc)
                if category is not None and target not in {"all", category}:
                    continue
            elif kind == "power":
                if "无人机充能速度" not in b.desc:
                    continue
            elif kind == "clue":
                if "线索搜集速度" not in b.desc:
                    continue
            else:
                continue
            adj += avg - (b.value or 0.0)
        return adj

    def _other_recover_for(self, glob: dict, room: str) -> float:
        return glob["other_recover"] if room in OTHER_RECOVER_ROOMS else 0.0

    def _infection_contact_bonus(self, asg: Assignment, contact_pct: float) -> float:
        """琴柳·感染力: 办公室联络速度低于30%(含驻员基础5%)时唯一额外+20%。"""
        if contact_pct >= 30.0:
            return 0.0
        for nm in asg.control:
            prof = self.prof.get(nm)
            if not prof:
                continue
            if not self._frac(nm, "control", self._control_effective_reduction_for(nm, asg)):
                continue
            if any(b.buff_name == "感染力" for b in prof.room_buffs.get("control", [])):
                return 20.0
        return 0.0

    def _hire_capacity_overrides(self, asg: Assignment, ctrl_red: float, glob: dict) -> dict[str, float]:
        """办公室人力资源达到上限后暂停工作, 折算为窗口内工作占比。"""
        if not asg.hire:
            return {}
        hc = self.cfg["hire"]
        red = ctrl_red + self._room_reduction(len(asg.hire)) + self._other_recover_for(glob, "hire")
        contact_pct = glob["contact"]
        active_hire_ops = 0.0
        for nm in asg.hire:
            frac = self._frac(nm, "hire", red)
            active_hire_ops += frac
            contact_pct += hc.get("base_eff_per_operator", 5.0) * frac
            st = self._stat(nm, "hire")
            if st:
                contact_pct += st.contact * frac
        contact_pct += self._infection_contact_bonus(asg, contact_pct)
        if active_hire_ops <= 0.0:
            return {nm: 0.0 for nm in asg.hire}
        contact_rate = hc["contact_per_hour_base"] * (1 + contact_pct / 100.0)
        limit = hc.get("contact_limit", 0.0)
        total_hours = sum(self.sch.gaps)
        if contact_rate <= 0.0 or total_hours <= 0.0:
            facility_frac = 0.0
        elif limit <= 0.0:
            facility_frac = 0.0
        else:
            active_hours = sum(min(g, limit / contact_rate) for g in self.sch.gaps)
            facility_frac = max(0.0, min(1.0, active_hours / total_hours))
        return {
            nm: min(self._base_frac(nm, "hire", red), facility_frac)
            for nm in asg.hire
        }

    @staticmethod
    def _frac_maps_close(a: dict[str, float], b: dict[str, float]) -> bool:
        keys = set(a) | set(b)
        return all(abs(a.get(k, 1.0) - b.get(k, 1.0)) < 1e-6 for k in keys)

    @staticmethod
    def _workshop_byproduct_applies(target: str, craft_category: str | None) -> bool:
        """加工站副产品技能按加工类别生效; 未指定类别时保留旧的通用估值口径。"""
        if not craft_category or craft_category == "any":
            return True
        return target in {"", "any", craft_category}

    def _workshop_mood_limited_crafts(self, ops: list[str], requested_crafts: float, mood_cost: float) -> float:
        """加工站每次加工瞬时消耗配方心情; 涣散或心情不足时不能继续获得副产物。"""
        if requested_crafts <= 0.0 or mood_cost <= 0.0 or not ops:
            return max(0.0, requested_crafts)
        capacity = 0.0
        for nm in ops:
            if self._fatigued_override is not None and nm in self._fatigued_override:
                continue
            mood = self.cap
            if self._mood_override is not None:
                mood = self._mood_override.get(nm, mood)
            capacity += max(0.0, mood) // mood_cost
        return max(0.0, min(requested_crafts, capacity))

    def _workshop_pity_extra_byproducts(
        self,
        ops: list[str],
        crafts: float,
        mood_cost: float,
        byproduct_chance: float,
    ) -> float:
        """九色鹿因果/业报: 失败加工按心情消耗累积, 长期折算为额外副产品数。"""
        if crafts <= 0.0 or mood_cost <= 0.0 or byproduct_chance >= 1.0:
            return 0.0
        extra = 0.0
        failed_crafts = crafts * max(0.0, 1.0 - byproduct_chance)
        failed_mood = failed_crafts * mood_cost
        for nm in ops:
            if self._frac(nm, "workshop") <= 0.0:
                continue
            prof = self.prof.get(nm)
            if not prof:
                continue
            for b in prof.room_buffs.get("workshop", []):
                d = b.desc
                if "累积40点因果" in d and mood_cost <= 4.0:
                    extra += failed_mood / 40.0
                elif "累积80点业报" in d and abs(mood_cost - 8.0) < 1e-9:
                    extra += failed_mood / 80.0
        return extra

    def _workshop_failure_restore_mood_cost(
        self,
        ops: list[str],
        mood_cost: float,
        byproduct_chance: float,
        craft_category: str | None,
    ) -> float:
        """棘刺爆炸艺术: 每2次未出副产品恢复一次配方心情, 按长期期望降低有效消耗。"""
        if mood_cost <= 0.0 or byproduct_chance >= 1.0:
            return max(0.0, mood_cost)
        adjusted = float(mood_cost)
        fail_rate = max(0.0, min(1.0, 1.0 - byproduct_chance))
        for nm in ops:
            if self._frac(nm, "workshop") <= 0.0:
                continue
            prof = self.prof.get(nm)
            if not prof:
                continue
            for b in prof.room_buffs.get("workshop", []):
                if (
                    "每2次加工没有产出副产品" in b.desc
                    and "恢复自身一次心情" in b.desc
                    and self._workshop_desc_category_applies(b.desc, craft_category)
                ):
                    adjusted = min(adjusted, mood_cost * (1.0 - fail_rate / 2.0))
        return max(0.0, adjusted)

    def _workshop_fixed_byproduct_tags(self, ops: list[str], craft_category: str | None) -> list[str]:
        """加工站指定副产品类型标签, 如 T3 副产品必定为固源岩组。"""
        tags: list[str] = []
        for nm in ops:
            if self._frac(nm, "workshop") <= 0.0:
                continue
            prof = self.prof.get(nm)
            if not prof:
                continue
            for b in prof.room_buffs.get("workshop", []):
                if not self._workshop_desc_category_applies(b.desc, craft_category):
                    continue
                m = re.search(r"副产品必定为(.+?组)", b.desc)
                if m:
                    tag = f"{nm}:{m.group(1)}"
                    if tag not in tags:
                        tags.append(tag)
        return tags

    @staticmethod
    def _workshop_desc_category_applies(desc: str, craft_category: str | None) -> bool:
        if not craft_category or craft_category == "any":
            return True
        category_markers = {
            "elite": ("精英材料",),
            "skill": ("技巧概要",),
            "chip": ("芯片",),
            "building": ("基建材料",),
            "alloy": ("炽合金",),
            "polyester": ("聚酸酯",),
            "oriron": ("异铁",),
            "rock": ("源岩",),
            "crystal": ("晶体",),
            "ketone": ("酮凝集",),
            "device": ("装置",),
        }
        if "任意类材料" in desc:
            return True
        return any(marker in desc for marker in category_markers.get(craft_category, ()))

    def _workshop_effective_mood_cost(self, ops: list[str], base_cost: float, craft_category: str | None) -> float:
        """应用加工站配方心情消耗调整技能; 条件中的心情消耗按原始配方消耗判断。"""
        if base_cost <= 0.0 or not ops:
            return max(0.0, base_cost)
        candidates = []
        num = r"([0-9]+(?:\.[0-9]+)?)"
        mood = self._ctx.mood or {}
        low_mood_dorm_cache: dict[float, int] = {}

        def low_mood_dorm_count(threshold: float) -> int:
            if threshold not in low_mood_dorm_cache:
                low_mood_dorm_cache[threshold] = sum(
                    1
                    for dorm_ops in self._ctx.rooms.get("dormitory", {}).values()
                    for dorm_nm in dorm_ops
                    if mood.get(dorm_nm, self.cap) <= threshold
                )
            return low_mood_dorm_cache[threshold]

        for nm in ops:
            if self._frac(nm, "workshop") <= 0.0:
                continue
            prof = self.prof.get(nm)
            if not prof:
                continue
            for b in prof.room_buffs.get("workshop", []):
                d = b.desc
                if "心情消耗" not in d or not self._workshop_desc_category_applies(d, craft_category):
                    continue
                m = re.search(rf"心情消耗为{num}的配方全部-{num}心情消耗", d)
                if m and abs(base_cost - float(m.group(1))) < 1e-9:
                    candidates.append(max(0.0, base_cost - float(m.group(2))))
                m = re.search(rf"心情消耗为{num}以上的配方全部-{num}心情消耗", d)
                if m and base_cost >= float(m.group(1)):
                    candidates.append(max(0.0, base_cost - float(m.group(2))))
                m = re.search(
                    rf"宿舍内每有{num}名心情{num}以下.*?"
                    rf"心情消耗为{num}的配方心情消耗全部-{num}",
                    d,
                )
                if m and abs(base_cost - float(m.group(3))) < 1e-9 and float(m.group(1)) > 0.0:
                    steps = int(low_mood_dorm_count(float(m.group(2))) // float(m.group(1)))
                    if steps > 0:
                        candidates.append(max(0.0, base_cost - steps * float(m.group(4))))
                m = re.search(rf"心情消耗为{num}的配方全部除以{num}心情消耗", d)
                if m and abs(base_cost - float(m.group(1))) < 1e-9 and float(m.group(2)) > 0.0:
                    candidates.append(base_cost / float(m.group(2)))
                m = re.search(rf"心情消耗为{num}以上的配方全部除以{num}心情消耗", d)
                if m and base_cost >= float(m.group(1)) and float(m.group(2)) > 0.0:
                    candidates.append(base_cost / float(m.group(2)))
                m = re.search(rf"全部心情消耗-{num}", d)
                if m:
                    candidates.append(max(0.0, base_cost - float(m.group(1))))
                m = re.search(rf"相应配方(?:的)?心情消耗恒定为{num}", d)
                if m:
                    candidates.append(float(m.group(1)))
                m = re.search(rf"相应配方全部\+{num}心情消耗", d)
                if m:
                    candidates.append(base_cost + float(m.group(1)))
        return min(candidates) if candidates else float(base_cost)

    def _room_drain_reduction(self, room: str, ops: list[str], base_reduction: float) -> float:
        """房间内全体心情消耗净调整: 减免取最高, 增耗累加。返回值加到 reduction 上。"""
        red = 0.0
        inc = 0.0
        for nm in ops:
            st = self._stat(nm, room)
            if not st:
                continue
            frac = self._frac(nm, room, base_reduction)
            if st.room_drain:
                red = max(red, st.room_drain * frac)
            if st.room_drain_delta:
                inc += st.room_drain_delta * frac
        return red - inc

    def _control_trade_room_bonus(self, asg: Assignment, ops: list[str]) -> tuple[float, float]:
        """控制中枢中作用于当前具体贸易站的效率/订单上限加成。"""
        eff = 0.0
        limit = 0.0
        red = self._control_self_reduction(asg)
        cnt = {
            "谢拉格": sum(1 for nm in ops if self._frac(nm, "trading", 0.0) > 0.0 and "谢拉格" in factions_of(nm)),
            "格拉斯哥帮": sum(1 for nm in ops if self._frac(nm, "trading", 0.0) > 0.0 and "格拉斯哥帮" in factions_of(nm)),
            "叙拉古": sum(1 for nm in ops if self._frac(nm, "trading", 0.0) > 0.0 and "叙拉古" in factions_of(nm)),
        }
        for nm in asg.control:
            prof = self.prof.get(nm)
            if not prof:
                continue
            frac = self._frac(nm, "control", red)
            for b in prof.room_buffs.get("control", []):
                d = b.desc
                if "每个存在3名谢拉格干员的贸易站" in d and cnt["谢拉格"] >= 3:
                    eff += 10.0 * frac
                elif "同一贸易站中，每有1名格拉斯哥帮干员" in d:
                    eff += 10.0 * cnt["格拉斯哥帮"] * frac
                elif "每个进驻在贸易站的叙拉古干员" in d:
                    eff += 5.0 * cnt["叙拉古"] * frac
                elif "每个进驻在贸易站的谢拉格干员" in d:
                    eff -= 15.0 * cnt["谢拉格"] * frac
                    limit += 6.0 * cnt["谢拉格"] * frac
                elif (
                    "当赫德雷入驻贸易站时，赫德雷所在贸易站订单上限" in d
                    and "赫德雷" in ops
                    and self._frac("赫德雷", "trading", 0.0) > 0.0
                ):
                    if "同谋·β" in b.buff_name:
                        limit += 2.0 * frac
                    elif "同谋·α" in b.buff_name:
                        limit += 1.0 * frac
        return eff, limit

    def _control_manufacture_room_bonus(self, asg: Assignment, line: str, ops: list[str]) -> float:
        """控制中枢中作用于当前具体制造站的生产力加成。"""
        bonus = 0.0
        red = self._control_self_reduction(asg)
        cnt = {
            "骑士": sum(1 for nm in ops if self._frac(nm, "manufacture", 0.0) > 0.0 and "骑士" in factions_of(nm)),
            "黑钢国际": sum(1 for nm in ops if self._frac(nm, "manufacture", 0.0) > 0.0 and "黑钢国际" in factions_of(nm)),
            "红松骑士团": sum(1 for nm in ops if self._frac(nm, "manufacture", 0.0) > 0.0 and "红松骑士团" in factions_of(nm)),
        }
        for nm in asg.control:
            prof = self.prof.get(nm)
            if not prof:
                continue
            frac = self._frac(nm, "control", red)
            for b in prof.room_buffs.get("control", []):
                d = b.desc
                if "每个进驻在制造站的骑士干员" in d:
                    bonus += 7.0 * cnt["骑士"] * frac
                elif "每个进驻在制造站的黑钢国际干员" in d:
                    bonus += 5.0 * cnt["黑钢国际"] * frac
                elif "每个进驻在制造站的红松骑士团干员" in d:
                    if line == "record":
                        bonus += 10.0 * cnt["红松骑士团"] * frac
                    elif line == "gold":
                        bonus -= 10.0 * cnt["红松骑士团"] * frac
        return bonus + self._abyssal_hunter_manufacture_bonus(asg, ops)

    def _power_manufacture_room_bonus(self, asg: Assignment, ops: list[str]) -> float:
        """发电站中作用于当前具体制造站的生产力加成。"""
        if "野鬃" not in ops or self._frac("野鬃", "manufacture", 0.0) <= 0.0:
            return 0.0
        bonus = 0.0
        red = self._control_reduction(asg)
        for power_ops in asg.power:
            room_red = red + self._room_reduction(len(power_ops))
            if "正义骑士号" not in power_ops or self._frac("正义骑士号", "power", room_red) <= 0.0:
                continue
            prof = self.prof.get("正义骑士号")
            if not prof:
                continue
            if any("野鬃所在的制造站生产力+5%" in b.desc for b in prof.room_buffs.get("power", [])):
                bonus += 5.0 * self._frac("正义骑士号", "power", room_red)
        return bonus

    def _control_training_speed_bonus(self, asg: Assignment) -> float:
        """控制中枢中作用于训练中干员的训练速度加成, 同种效果取最高。"""
        bonus = 0.0
        red = self._control_self_reduction(asg)
        for nm in asg.control:
            prof = self.prof.get(nm)
            if not prof:
                continue
            frac = self._frac(nm, "control", red)
            if frac <= 0.0:
                continue
            for b in prof.room_buffs.get("control", []):
                if "如训练室有干员在进行技能专精" in b.desc and "专精技能训练速度+5%" in b.desc:
                    bonus = max(bonus, 5.0 * frac)
        return bonus

    def _abyssal_hunter_manufacture_bonus(self, asg: Assignment, ops: list[str]) -> float:
        """歌蕾蒂娅·集群狩猎: 给进驻深海猎人的制造站提供特殊生产力加成。"""
        if not any(self._frac(nm, "manufacture", 0.0) > 0.0 and "深海猎人" in factions_of(nm) for nm in ops):
            return 0.0
        if self._manufacture_zero_prod_providers(ops):
            return 0.0
        total_hunters = sum(
            1
            for _line, room_ops in asg.manufacture
            for nm in room_ops
            if self._frac(nm, "manufacture", 0.0) > 0.0 and "深海猎人" in factions_of(nm)
        )
        if total_hunters <= 0:
            return 0.0

        bonus = 0.0
        red = self._control_self_reduction(asg)
        for nm in asg.control:
            if nm != "歌蕾蒂娅":
                continue
            prof = self.prof.get(nm)
            if not prof:
                continue
            frac = self._frac(nm, "control", red)
            for b in prof.room_buffs.get("control", []):
                if "集群狩猎·β" in b.buff_name:
                    bonus += min(90.0, total_hunters * 10.0) * frac
                elif "集群狩猎·α" in b.buff_name:
                    bonus += min(45.0, total_hunters * 5.0) * frac
        return bonus

    def _manufacture_zero_prod_providers(self, ops: list[str], reduction: float = 0.0) -> set[str]:
        """制造站内"其他干员生产力归零"技能持有者。"""
        providers: set[str] = set()
        for nm in ops:
            if self._frac(nm, "manufacture", reduction) <= 0.0:
                continue
            prof = self.prof.get(nm)
            if not prof:
                continue
            for buff in prof.room_buffs.get("manufacture", []):
                if "当前制造站内其他干员提供的生产力全部归零" in buff.desc:
                    providers.add(nm)
                    break
        return providers

    def _manufacture_facility_based_prod_bonus(self, name: str, room_idx: int) -> float:
        """制造站归零技能不清除的按设施数量提供的生产力。"""
        prof = self.prof.get(name)
        if not prof:
            return 0.0
        total = 0.0
        for buff in prof.room_buffs.get("manufacture", []):
            amount = manufacture_facility_count_scale(buff, self._ctx, "manufacture", room_idx)
            if amount is not None:
                total += amount
            amount = power_platform_count_scale(buff, self._ctx)
            if amount is not None:
                total += amount
        return total

    def _manufacture_pair_productivity_bonus(self, line: str, ops: list[str], name: str,
                                             abyssal_bonus_active: bool, reduction: float = 0.0) -> float:
        """槐琥·配合意识: 同站其他干员的普通生产力每5%转换为5%, 上限40%。"""
        if name != "槐琥" or abyssal_bonus_active:
            return 0.0
        prof = self.prof.get(name)
        if not prof or not any(b.buff_name == "配合意识" for b in prof.room_buffs.get("manufacture", [])):
            return 0.0
        other_prod = 0.0
        for other in ops:
            if other == name:
                continue
            frac = self._frac(other, "manufacture", reduction)
            if frac <= 0.0:
                continue
            if not self._manufacture_prod_counts_for_pair(other):
                continue
            st = self._stat(other, "manufacture")
            if not st:
                continue
            other_prod += self._line_prod(st.prod, line) * frac
        return min(40.0, int(other_prod // 5.0) * 5.0)

    def _manufacture_prod_counts_for_pair(self, name: str) -> bool:
        """配合意识不统计根据设施数量提供的生产力。"""
        prof = self.prof.get(name)
        if not prof:
            return True
        for buff in prof.room_buffs.get("manufacture", []):
            d = buff.desc
            if ("不包含根据设施数量提供加成的生产力" in d
                    or "每个发电站为当前制造站" in d
                    or "每个当前制造站内干员为当前制造站" in d
                    or "作业平台进驻发电站" in d):
                return False
        return True

    def _manufacture_capacity_productivity_bonus(self, ops: list[str], reduction: float = 0.0) -> float:
        """红云/泡泡: 当前制造站内干员自身提升的仓库容量换算为生产力。"""
        has_bubble = self._has_manufacture_buff(ops, "泡泡", "大就是好！", reduction)
        has_vermeil = self._has_manufacture_buff(ops, "红云", "回收利用", reduction)
        if not has_bubble and not has_vermeil:
            return 0.0

        total = 0.0
        for nm in ops:
            frac = self._frac(nm, "manufacture", reduction)
            if frac <= 0.0:
                continue
            st = self._stat(nm, "manufacture")
            if not st:
                continue
            cap = max(0.0, st.capacity) * frac
            if has_bubble:
                total += cap * (3.0 if cap > 16.0 else 1.0)
            else:
                total += cap * 2.0
        return total

    def _has_manufacture_buff(self, ops: list[str], name: str, buff_name: str, reduction: float = 0.0) -> bool:
        if name not in ops:
            return False
        if self._frac(name, "manufacture", reduction) <= 0.0:
            return False
        prof = self.prof.get(name)
        return bool(prof and any(b.buff_name == buff_name for b in prof.room_buffs.get("manufacture", [])))

    def evaluate(self, asg: Assignment) -> Result:
        bd: dict[str, float] = {}
        warns: list[str] = []
        # 第一遍: 建上下文(位置/派系计数/资源池); 第二遍: 据此解析每个干员效果
        saved_capacity_frac = self._capacity_frac_override
        self._capacity_frac_override = dict(saved_capacity_frac or {})
        for _ in range(8):
            self._ctx = build_context(
                asg,
                mood=self._mood_override,
                active_frac=self._context_active_frac(),
                active_frac_default=1.0,
                fatigued=self._fatigued_override,
                active_buffs={nm: prof.room_buffs for nm, prof in self.prof.items()},
                recruit_slots_noninitial=self._recruit_slots_noninitial(),
                dorm_levels=self._dorm_levels(asg),
                facility_levels=self._facility_levels(),
                drone_cap=self.cfg.get("power", {}).get("drone_cap", 235),
            )
            self._resolved = self._resolve(self._ctx)
            ctrl_red = self._control_reduction(asg)
            glob = self._globals(asg)
            hire_capacity = self._hire_capacity_overrides(asg, ctrl_red, glob)
            new_capacity = dict(saved_capacity_frac or {})
            new_capacity.update(hire_capacity)
            if self._frac_maps_close(self._capacity_frac_override or {}, new_capacity):
                break
            self._capacity_frac_override = new_capacity
        else:
            self._ctx = build_context(
                asg,
                mood=self._mood_override,
                active_frac=self._context_active_frac(),
                active_frac_default=1.0,
                fatigued=self._fatigued_override,
                active_buffs={nm: prof.room_buffs for nm, prof in self.prof.items()},
                recruit_slots_noninitial=self._recruit_slots_noninitial(),
                dorm_levels=self._dorm_levels(asg),
                facility_levels=self._facility_levels(),
                drone_cap=self.cfg.get("power", {}).get("drone_cap", 235),
            )
            self._resolved = self._resolve(self._ctx)
            ctrl_red = self._control_reduction(asg)
            glob = self._globals(asg)
        mc = self.cfg["manufacture"]

        # ---- 无人机(全基建单一池): 折算为加速生产的分钟数, 砸到收益最高的生产线 ----
        pc = self.cfg["power"]
        charge_pct = 0.0
        power_detail = []
        drone_assist_unlocked = self._drone_assist_unlocked()
        for ops in asg.power:
            room_charge = 0.0
            red = ctrl_red + self._room_reduction(len(ops)) + self._other_recover_for(glob, "power")
            for nm in ops:
                frac = self._frac(nm, "power", red)
                base_charge = pc.get("base_charge_bonus_per_operator", 5.0) * frac
                charge_pct += base_charge
                room_charge += base_charge
                st = self._stat(nm, "power")
                if st:
                    skill_charge = (
                        st.power + self._warmup_adjustment(nm, "power", "power")
                    ) * frac
                    charge_pct += skill_charge
                    room_charge += skill_charge
            power_detail.append({"ops": ops, "charge%": round(room_charge, 1)})
        drone_rate = pc["drone_per_hour_base"] * (1 + charge_pct / 100.0)
        drones_day = self.sch.daily_capped(drone_rate, pc.get("drone_cap", 0.0))
        drone_minutes = drones_day * pc.get("drone_minutes_per_drone", 0.0) if drone_assist_unlocked else 0.0

        def _line_item_value(spec):
            if spec["output"] == "赤金":
                go = self._trading_gold_order_profile()
                return go["lmd_per_order"] / go["gold_per_order"] * self.val.get("龙门币", 0.0)
            if spec["output"] == "源石碎片" and str(self.cfg["trading"].get("strategy", "gold") or "gold") == "orundum":
                oo = self.cfg["trading"].get("orundum_order", {})
                return (
                    oo.get("orundum_per_order", 20.0)
                    * self.val.get("合成玉", 0.0)
                    / max(1e-9, oo.get("source_shard_per_order", 2.0))
                )
            return spec.get("units_per_item", 1) * self.val.get(spec["output"], 0.0)

        # ---- 制造站 ----
        trade_strategy = str(self.cfg["trading"].get("strategy", "gold") or "gold")
        gold_supply = 0.0
        source_shard_supply = 0.0
        man_ap = 0.0
        man_detail = []
        for room_idx, (line, ops) in enumerate(asg.manufacture):
            spec = mc["lines"].get(line)
            if spec is None:
                man_detail.append({
                    "line": line,
                    "ops": ops,
                    "prod%": 0.0,
                    "items/day": 0.0,
                    "active_ops": 0.0,
                    "invalid_line": True,
                })
                continue
            min_level = int(spec.get("min_level", 1) or 1)
            room_level = self._room_level("manufacture", room_idx)
            if room_level < min_level:
                man_detail.append({
                    "line": line,
                    "ops": ops,
                    "prod%": 0.0,
                    "items/day": 0.0,
                    "active_ops": 0.0,
                    "locked_by_level": True,
                    "required_level": min_level,
                    "room_level": room_level,
                })
                continue
            upi = spec.get("units_per_item", 1)
            vpi = spec.get("volume_per_item", 1)
            cap_volume = self._manufacture_capacity_volume(room_idx)
            red = ctrl_red + self._room_reduction(len(ops)) + self._other_recover_for(glob, "manufacture")
            prod_pct = self._line_prod(glob["prod"], spec["category"])
            control_man_bonus = self._control_manufacture_room_bonus(asg, spec["category"], ops)
            abyssal_bonus_active = self._abyssal_hunter_manufacture_bonus(asg, ops) > 0
            prod_pct += control_man_bonus
            prod_pct += self._power_manufacture_room_bonus(asg, ops)
            prod_pct += self._manufacture_capacity_productivity_bonus(ops, red)
            zero_prod_providers = self._manufacture_zero_prod_providers(ops, red)
            active_ops = 0.0
            for nm in ops:
                frac = self._frac(nm, "manufacture", red)
                active_ops += frac
                prod_pct += mc.get("base_prod_per_operator", 1.0) * frac
                st = self._stat(nm, "manufacture")
                if not st:
                    continue
                if not zero_prod_providers or nm in zero_prod_providers:
                    prod_pct += (
                        self._line_prod(st.prod, spec["category"])
                        + self._warmup_adjustment(nm, "manufacture", "prod", spec["category"])
                    ) * frac
                    prod_pct += self._manufacture_pair_productivity_bonus(
                        spec["category"], ops, nm, abyssal_bonus_active, red
                    ) * frac
                else:
                    prod_pct += self._manufacture_facility_based_prod_bonus(nm, room_idx) * frac
                cap_volume += st.capacity * frac        # 库容技能(体积)
            cap_items = cap_volume / vpi
            rate_items = (60.0 / spec["base_minutes_per_item"]) * (1 + prod_pct / 100.0)
            items = self.sch.daily_capped(rate_items, cap_items) if active_ops > 0.0 else 0.0
            if spec["output"] == "赤金":
                gold_supply += items
            elif spec["output"] == "源石碎片" and trade_strategy == "orundum":
                source_shard_supply += items
            else:
                man_ap += items * upi * self.val.get(spec["output"], 0.0)
            man_detail.append({"line": line, "ops": ops, "prod%": round(prod_pct, 1),
                               "items/day": round(items, 1),
                               "active_ops": round(active_ops, 3)})

        gold_supply += float(self.cfg["trading"].get("external_gold_per_day", 0.0) or 0.0)

        # 无人机加速候选: 每架固定减少 3 分钟基础耗时, 可用于制造站或贸易站。
        # 这里先计算制造候选, 贸易候选需等订单效率/赤金余量算出后再决策。
        drone_items = 0.0
        drone_line = None
        drone_kind = None
        drone_trade_idx = None
        best_man_drone = None
        if drone_minutes > 0 and asg.manufacture:
            for (line, _ops), detail in zip(asg.manufacture, man_detail):
                if detail.get("active_ops", 0.0) <= 0.0:
                    continue
                spec = mc["lines"].get(line)
                if spec is None:
                    continue
                # PRTS: 无人机减少的是基础耗时, 不再乘生产力%。
                prod_pct = detail["prod%"]
                items_per_min = 1.0 / spec["base_minutes_per_item"]
                value_per_min = items_per_min * _line_item_value(spec)
                if best_man_drone is None or value_per_min > best_man_drone[0]:
                    best_man_drone = (value_per_min, line, spec, prod_pct, items_per_min)
        bd["制造站(非赤金)"] = man_ap

        # ---- 贸易站 (消耗赤金换龙门币) ----
        tc = self.cfg["trading"]
        trade_ap = 0.0
        trade_lmd = 0.0
        trade_orundum = 0.0
        trade_detail = []
        trade_rooms = []
        sp = tc.get("special_ops", {})

        def _has(name, min_elite):
            p = self.prof.get(name)
            return (
                name in ops
                and p is not None
                and p.elite >= min_elite
                and self._frac(name, "trading", red) > 0.0
            )

        def _has_buff(name: str, buff_name: str) -> bool:
            p = self.prof.get(name)
            return (
                name in ops
                and p is not None
                and self._frac(name, "trading", red) > 0.0
                and any(b.buff_name == buff_name for b in p.room_buffs.get("trading", []))
            )

        def _quality_order_profile() -> tuple[str | None, dict, float]:
            beta_frac = 0.0
            alpha_frac = 0.0
            alpha = {"裁缝·α", "手工艺品·α", "鉴定师的眼光", "懂行"}
            beta = {"裁缝·β", "手工艺品·β", "鉴定师的手段"}
            for nm in ops:
                p = self.prof.get(nm)
                frac = self._frac(nm, "trading", red)
                if p is None or frac <= 0.0:
                    continue
                names = {b.buff_name for b in p.room_buffs.get("trading", [])}
                if names & beta:
                    beta_frac = max(beta_frac, frac)
                elif names & alpha:
                    alpha_frac += min(1.0, frac)
            if beta_frac > 0.0:
                return "beta", tc.get("quality_order_profiles", {}).get("beta", {}), min(1.0, beta_frac)
            if alpha_frac >= 2.0:
                return "alpha2", tc.get("quality_order_profiles", {}).get("alpha2", {}), 1.0
            if alpha_frac > 1.0:
                return "alpha2", tc.get("quality_order_profiles", {}).get("alpha2", {}), alpha_frac / 2.0
            if alpha_frac > 0.0:
                return "alpha", tc.get("quality_order_profiles", {}).get("alpha", {}), min(1.0, alpha_frac)
            return None, {}, 0.0

        def _quality_ramp_ratio(tier: str | None, active_ratio: float) -> float:
            if not tier:
                return 0.0
            hours = tc.get("quality_order_profiles", {}).get(tier, {}).get("warmup_hours", 0.0)
            gaps = self._warmup_gaps()
            total = sum(gaps)
            if hours <= 0 or total <= 0:
                return max(0.0, min(1.0, active_ratio))

            def integral(gap: float) -> float:
                ramp = min(gap, hours)
                return 0.5 * ramp * ramp / hours + max(0.0, gap - hours)

            return max(0.0, min(1.0, sum(integral(g) for g in gaps) / total * active_ratio))

        def _blended_quality_profile(tier: str | None, peak: dict, base: dict, active_ratio: float) -> dict:
            ratio = _quality_ramp_ratio(tier, active_ratio)
            if not peak or ratio <= 0.0:
                return base
            keys = (
                "base_minutes_per_order",
                "gold_per_order",
                "lmd_per_order",
                "prob_2_gold",
                "prob_3_gold",
                "native_4_gold_probability",
            )
            out = dict(base)
            for k in keys:
                out[k] = base.get(k, 0.0) + (peak.get(k, base.get(k, 0.0)) - base.get(k, 0.0)) * ratio
            out["warmup_ratio"] = ratio
            return out

        for trade_idx, ops in enumerate(asg.trading):
            base_red = ctrl_red + self._room_reduction(len(ops)) + self._other_recover_for(glob, "trading")
            red = base_red + self._room_drain_reduction("trading", ops, base_red)
            active_ops = sum(self._frac(nm, "trading", red) for nm in ops)
            # 特殊赤金订单干员仅作用于 gold 策略。
            gold_strategy = trade_strategy == "gold"
            has_dorothy = gold_strategy and _has("但书", 0)
            dorothy_e2 = gold_strategy and _has("但书", 2)
            has_tequila = gold_strategy and _has("龙舌兰", 0)
            tequila_e2 = gold_strategy and _has("龙舌兰", 2)
            has_shamare = gold_strategy and _has("巫恋", 2)  # 低语为精2技能
            has_closur = gold_strategy and _has_buff("可露希尔", "特别订单")
            has_pepe = gold_strategy and _has_buff("佩佩", "慧眼独到")
            has_uofficial = gold_strategy and _has_buff("U-Official", "天真的谈判者")

            # ---- 订单获取效率 ----
            eff = glob["trade_eff"]
            limit = self._trading_order_limit(trade_idx)
            room_ctrl_eff, room_ctrl_limit = self._control_trade_room_bonus(asg, ops)
            eff += room_ctrl_eff
            limit += room_ctrl_limit
            if has_shamare:
                # 巫恋·低语: 站内"其他干员"效率归零, 且这些其他干员每人自身 +45%。
                # "每人"承接"其他干员", 不含巫恋自身 -> 三人站 = 其他2人×45% = +90%。
                # PRTS 将每名进驻干员 +1% 记为贸易站基础效率, 不属于被低语归零的干员技能效率。
                self_eff = sp.get("巫恋", {}).get("self_eff", 45.0)
                for nm in ops:
                    frac = self._frac(nm, "trading", red)
                    eff += tc.get("base_eff_per_operator", 1.0) * frac
                    if nm != "巫恋":
                        eff += self_eff * frac
                    st = self._stat(nm, "trading")
                    if st:
                        limit += st.order_limit * frac
            else:
                for nm in ops:
                    frac = self._frac(nm, "trading", red)
                    eff += tc.get("base_eff_per_operator", 1.0) * frac
                    st = self._stat(nm, "trading")
                    if not st:
                        continue
                    eff += st.trade_eff * frac
                    limit += st.order_limit * frac

            limit = max(1.0, limit)
            tags = []
            if trade_strategy == "orundum":
                oo = tc.get("orundum_order", {})
                order_profile = {
                    "base_minutes_per_order": oo.get("base_minutes_per_order", 120.0),
                    "source_shard_per_order": oo.get("source_shard_per_order", 2.0),
                    "orundum_per_order": oo.get("orundum_per_order", 20.0),
                }
                order_minutes = order_profile["base_minutes_per_order"]
                order_eff_applies = True
                if self._room_level("trading", trade_idx) < int(oo.get("min_level", 3) or 3):
                    order_minutes = 0.0
                    tags.append("开采协力未解锁")
            else:
                go = self._trading_gold_order_profile(trade_idx)
                quality_tier, quality_profile, quality_active_ratio = _quality_order_profile()
                order_profile = (
                    _blended_quality_profile(quality_tier, quality_profile, go, quality_active_ratio)
                    if quality_profile and self._room_level("trading", trade_idx) == 3 and not (has_closur or has_pepe) else go
                )
                order_minutes = order_profile.get("base_minutes_per_order", go["base_minutes_per_order"])
                order_eff_applies = True
                if has_pepe:
                    order_minutes = sp.get("佩佩", {}).get("base_minutes_per_order", 270.0)
                    order_eff_applies = False
                elif has_closur:
                    order_minutes = sp.get("可露希尔", {}).get("base_minutes_per_order", 144.0)
            rate = (
                (60.0 / order_minutes) * ((1 + eff / 100.0) if order_eff_applies else 1.0)
                if active_ops > 0.0 and order_minutes > 0.0 else 0.0
            )
            orders_possible = self.sch.daily_capped(rate, limit) if active_ops > 0.0 else 0.0

            # ---- 每单赤金交付 / 龙门币 (特殊干员改写) ----
            gold_per = order_profile.get("gold_per_order", 0.0)
            lmd_per = order_profile.get("lmd_per_order", 0.0)
            source_shard_per = order_profile.get("source_shard_per_order", 0.0)
            orundum_per = order_profile.get("orundum_per_order", 0.0)
            value_per_order = lmd_per * self.val.get("龙门币", 0.0) + orundum_per * self.val.get("合成玉", 0.0)
            if trade_strategy == "gold" and quality_tier and order_profile is not go:
                tags.append(f"高品质订单{quality_tier}@{order_profile.get('warmup_ratio', 1.0):.2f}")
            if has_pepe:
                d = sp.get("佩佩", {})
                gold_per = d.get("gold_per_order", 0.0)
                lmd_per = d.get("lmd_per_order", 1000.0)
                tags.append("佩佩特别独占订单")
            elif has_closur:
                d = sp.get("可露希尔", {})
                gold_per = d.get("gold_per_order", 2.0)
                lmd_per = d.get("lmd_per_order", 1200.0)
                tags.append("可露希尔特别订单")
            elif has_uofficial:
                d = sp.get("U-Official", {})
                gold_per = d.get("gold_per_order", 2.0)
                lmd_per = d.get("lmd_per_order", 1000.0)
                tags.append("U-Official固定2赤金单")
            # 龙舌兰·投资: 原生≥4赤金单 +500/+250 龙门币, 不额外耗赤金。
            # 但书影响 2/3 赤金单(违约), 龙舌兰影响 4 赤金单(投资), 互不干涉可同时生效。
            if has_tequila and not has_closur and not has_pepe and not has_uofficial:
                d = sp.get("龙舌兰", {})
                lmd_per += (
                    d.get("lmd_bonus_e2" if tequila_e2 else "lmd_bonus_e0", 0.0)
                    * order_profile.get("native_4_gold_probability", go.get("native_4_gold_probability", 1.0))
                )
                tags.append("龙舌兰投资")
            # 但书·违约单: 继承原订单, 对原生<4赤金单追加赤金与同步报酬。
            if has_dorothy and not has_closur and not has_pepe and not has_uofficial:
                d = sp.get("但书", {})
                bonus_gold = d.get("gold_bonus_e2" if dorothy_e2 else "gold_bonus_e0", 0.0)
                affected_prob = order_profile.get("prob_2_gold", 0.0) + order_profile.get("prob_3_gold", 0.0)
                gold_per += affected_prob * bonus_gold
                lmd_per += affected_prob * bonus_gold * 500.0
                tags.append(f"但书违约单+{bonus_gold:g}")
            if has_shamare:
                tags.append("巫恋低语")

            trade_rooms.append({
                "kind": trade_strategy,
                "ops": ops,
                "eff": eff,
                "limit": limit,
                "orders_possible": orders_possible,
                "gold_per": gold_per,
                "lmd_per": lmd_per,
                "source_shard_per": source_shard_per,
                "orundum_per": orundum_per,
                "value_per_order": value_per_order,
                "order_minutes": order_minutes,
                "order_eff_applies": order_eff_applies,
                "active_ops": active_ops,
                "tags": tags,
            })

        # 在制造/贸易之间选择无人机投放目标。贸易加速只在基础出单后仍有赤金可卖时计入。
        # 无人机减少的是订单基础耗时, 不受订单获取效率二次放大。
        base_gold_need = sum(r["orders_possible"] * r["gold_per"] for r in trade_rooms if r.get("kind") == "gold")
        base_source_need = sum(
            r["orders_possible"] * r["source_shard_per"]
            for r in trade_rooms
            if r.get("kind") == "orundum"
        )
        base_gold_surplus = max(0.0, gold_supply - base_gold_need)
        base_source_surplus = max(0.0, source_shard_supply - base_source_need)
        best_trade_drone = None
        if drone_minutes > 0 and trade_rooms and (base_gold_surplus > 0 or base_source_surplus > 0):
            go = self._trading_gold_order_profile()
            for idx, r in enumerate(trade_rooms):
                if r.get("active_ops", 0.0) <= 0.0:
                    continue
                if r.get("order_minutes", 0.0) <= 0.0:
                    continue
                orders_per_min = 1.0 / r.get("order_minutes", go["base_minutes_per_order"])
                if r.get("kind") == "orundum":
                    input_per = r["source_shard_per"]
                    max_orders_by_input = base_source_surplus / input_per if input_per else float("inf")
                else:
                    input_per = r["gold_per"]
                    max_orders_by_input = base_gold_surplus / input_per if input_per else float("inf")
                extra_orders = min(drone_minutes * orders_per_min, max_orders_by_input)
                if extra_orders <= 0:
                    continue
                value_per_min = orders_per_min * r.get("value_per_order", 0.0)
                if best_trade_drone is None or value_per_min > best_trade_drone[0]:
                    best_trade_drone = (value_per_min, idx, extra_orders)

        if best_man_drone and (best_trade_drone is None or best_man_drone[0] >= best_trade_drone[0]):
            _vpm, line, spec, _best_prod, items_per_min = best_man_drone
            drone_items = drone_minutes * items_per_min
            drone_line = line
            drone_kind = "manufacture"
            if spec["output"] == "赤金":
                gold_supply += drone_items
            elif spec["output"] == "源石碎片" and trade_strategy == "orundum":
                source_shard_supply += drone_items
            else:
                man_ap += drone_items * spec.get("units_per_item", 1) * self.val.get(spec["output"], 0.0)
                bd["制造站(非赤金)"] = man_ap
        elif best_trade_drone:
            _vpm, drone_trade_idx, drone_items = best_trade_drone
            drone_kind = "trading"
            drone_line = f"贸易站{drone_trade_idx + 1}"

        gold_left = gold_supply
        source_left = source_shard_supply
        for idx, r in enumerate(trade_rooms):
            orders_possible = r["orders_possible"]
            tags = list(r["tags"])
            if drone_kind == "trading" and idx == drone_trade_idx:
                orders_possible += drone_items
                tags.append("无人机加速")
            if r.get("kind") == "orundum":
                shard_orders = orders_possible
                shard_per = r["source_shard_per"]
                need = shard_orders * shard_per
                if need > source_left:
                    shard_orders = source_left / shard_per if shard_per else 0.0
                    source_left = 0.0
                else:
                    source_left -= need
                orundum = shard_orders * r["orundum_per"]
                idle = max(0.0, orders_possible - shard_orders)
                trade_orundum += orundum
                trade_detail.append({"ops": r["ops"], "strategy": "orundum",
                                     "eff%": round(r["eff"], 1),
                                     "order_limit": round(r["limit"], 1),
                                     "合成玉单/day": round(shard_orders, 1),
                                     "缺源石碎片空转单/day": round(idle, 1),
                                     "合成玉/day": round(orundum, 1),
                                     "active_ops": round(r.get("active_ops", 0.0), 3),
                                     "特殊": tags})
            else:
                # 赤金订单, 受赤金供应限制 (没有赤金就出不了单 -> 制造↔贸易必须平衡)
                gold_orders = orders_possible
                gold_per = r["gold_per"]
                lmd_per = r["lmd_per"]
                need = gold_orders * gold_per
                if need > gold_left:
                    gold_orders = gold_left / gold_per if gold_per else 0.0
                    gold_left = 0.0
                else:
                    gold_left -= need
                lmd = gold_orders * lmd_per
                idle = max(0.0, orders_possible - gold_orders)
                trade_lmd += lmd
                trade_detail.append({"ops": r["ops"], "strategy": "gold",
                                     "eff%": round(r["eff"], 1),
                                     "order_limit": round(r["limit"], 1),
                                     "赤金单/day": round(gold_orders, 1),
                                     "缺赤金空转单/day": round(idle, 1),
                                     "龙门币/day": round(lmd, 0),
                                     "active_ops": round(r.get("active_ops", 0.0), 3),
                                     "特殊": tags})
        trade_ap = trade_lmd * self.val.get("龙门币", 0.0)
        bd["贸易站(龙门币)"] = trade_ap
        bd["贸易站(合成玉)"] = trade_orundum * self.val.get("合成玉", 0.0)
        # 多余赤金: 中间产物, 未卖出默认不计入收益(系数0), 迫使制造↔贸易闭环
        factor = tc.get("unsold_gold_value_factor", 0.0)
        bd["余量赤金(未卖出)"] = gold_left * self.val.get("赤金", 0.0) * factor

        # ---- 发电站/无人机: 收益已通过上面的"无人机加速"计入生产线, 此处不再按素材重复估值 ----
        bd["无人机加速"] = 0.0  # 占位(价值已并入制造站/赤金)

        # ---- 会客室 (线索) ----
        mec = self.cfg["meeting"]
        clue_pct = 0.0
        meeting_detail = []
        meeting_red = ctrl_red + self._room_reduction(len(asg.meeting)) + self._other_recover_for(glob, "meeting")
        active_meeting_ops = sum(self._frac(nm, "meeting", meeting_red) for nm in asg.meeting)
        if asg.meeting:
            dc = self.cfg.get("dormitory", {})
            amb = 0.0
            for idx, _ops in enumerate(asg.dormitory):
                lvl = _indexed_config_value(dc.get("level"), idx)
                raw_amb = _indexed_config_value(dc.get("ambiance"), idx, 0)
                if lvl is None:
                    amb += float(raw_amb or 0)
                else:
                    amb += min(float(raw_amb or 0), 1000 * int(lvl))
            for threshold, bonus in mec.get("dorm_ambience_bonus", []):
                if amb >= threshold:
                    clue_pct = max(clue_pct, bonus)
            clue_pct += glob["clue"]
            clue_pct += mec.get("level_bonus", {}).get(str(mec.get("level", 3)), 0.0)
            for nm in asg.meeting:
                frac = self._frac(nm, "meeting", meeting_red)
                if frac <= 0:
                    continue
                clue_pct += mec.get("non_fatigued_bonus", 5.0) * frac
                prof = self.prof.get(nm)
                if prof:
                    clue_pct += mec.get("rarity_bonus", {}).get(str(prof.rarity), 0.0) * frac
                    clue_pct += mec.get("elite_bonus", {}).get(str(prof.elite), 0.0) * frac
                st = self._stat(nm, "meeting")
                if st:
                    clue_pct += (
                        st.clue + self._warmup_adjustment(nm, "meeting", "clue")
                    ) * frac
        meeting_active = active_meeting_ops > 0.0
        clue_rate = mec["clue_per_hour_base"] * (1 + clue_pct / 100.0) if meeting_active else 0.0
        collected_clues = 0.0
        daily_clues = 0.0
        clues = 0.0
        if meeting_active:
            clue_limit = mec.get("clue_limit", 0.0)
            daily_bonus = mec.get("daily_clue_if_staffed", 0.0)
            if self._day_hours >= 24.0:
                clues, collected_clues, daily_clues = self.sch.daily_capped_with_bonus(
                    clue_rate, clue_limit, daily_bonus, 4.0
                )
            else:
                collected_clues = self.sch.daily_capped(clue_rate, clue_limit)
                if self._meeting_daily_bonus:
                    daily_clues = min(daily_bonus, max(0.0, clue_limit - collected_clues))
                clues = min(clue_limit, collected_clues + daily_clues) if clue_limit > 0 else collected_clues + daily_clues
        if asg.meeting:
            clue_tags = self._meeting_clue_tags(asg.meeting, asg.control, meeting_red)
            meeting_detail.append({"ops": asg.meeting, "clue%": round(clue_pct, 1),
                                   "collected/day": round(collected_clues, 2),
                                   "daily_bonus/day": round(daily_clues, 2),
                                   "clues/day": round(clues, 2),
                                   "active_ops": round(active_meeting_ops, 3),
                                   "limit": mec.get("clue_limit", 0.0),
                                   "clue_tags": clue_tags})
        bd["会客室(线索)"] = clues * self.val.get("线索", 0.0)

        # ---- 办公室 (公招联络) ----
        # 产物是"公招刷新", 并非直接产出招募许可; 按每次刷新的保守期望理智(ap_per_refresh)估值。
        hc = self.cfg["hire"]
        hire_detail = []
        contact_pct = glob["contact"]
        red = ctrl_red + self._room_reduction(len(asg.hire)) + self._other_recover_for(glob, "hire")
        active_hire_ops = 0.0
        for nm in asg.hire:
            frac = self._frac(nm, "hire", red)
            active_hire_ops += frac
            contact_pct += hc.get("base_eff_per_operator", 5.0) * frac
            st = self._stat(nm, "hire")
            if st:
                contact_pct += st.contact * frac
        contact_pct += self._infection_contact_bonus(asg, contact_pct)
        hire_active = active_hire_ops > 0.0
        contact_rate = hc["contact_per_hour_base"] * (1 + contact_pct / 100.0) if hire_active else 0.0
        contacts = self.sch.daily_capped(contact_rate, hc.get("contact_limit", 0.0)) if hire_active else 0.0
        if asg.hire:
            hire_detail.append({"ops": asg.hire, "contact%": round(contact_pct, 1),
                                "refreshes/day": round(contacts, 2),
                                "active_ops": round(active_hire_ops, 3),
                                "limit": hc.get("contact_limit", 0.0)})
        bd["办公室(公招)"] = contacts * hc.get("ap_per_refresh", 0.0)

        # ---- 训练室 ----
        # 训练室不产素材; 若用户给 ap_value_per_hour, 按等效训练小时估值。PRTS 注释中
        # 艾丽妮/逻各斯的 -50% 实际是下次训练开始立即完成50%进度, 单独折成一次训练基准时长。
        trc = self.cfg["training"]
        train_detail = []
        train_equiv_hours = 0.0
        training_supported = self._training_plan_supported()
        training_active = trc.get("active_plan", True) and training_supported
        if asg.training and trc.get("active_plan", True) and not training_supported:
            warns.append(
                f"训练室等级{trc.get('level', 3)}不支持专精{self._training_target_level()}, 训练计划未执行。"
            )
        base_session_hours = self._training_base_session_hours()
        train_red = ctrl_red + self._room_reduction(len(asg.training)) + self._other_recover_for(glob, "training")
        control_train_bonus = self._control_training_speed_bonus(asg) if training_active and asg.training else 0.0
        for nm in asg.training:
            frac = self._frac(nm, "training", train_red) if training_active else 0.0
            st = self._stat(nm, "training")
            speed = trc.get("base_speed_per_operator", 5.0) if training_active and frac > 0.0 else 0.0
            if st and training_active and frac > 0.0:
                speed_sub, _drain_sub = self._training_condition_subtractions(nm)
                speed += max(0.0, st.train_speed - speed_sub)
            if training_active and frac > 0.0:
                speed += control_train_bonus
            initial = self._training_initial_progress(nm, st) if training_active and frac > 0.0 else 0.0
            work_hours = self._day_hours * frac
            instant_hours = base_session_hours * initial / 100.0 * frac
            equiv = work_hours * (1 + speed / 100.0) + instant_hours
            train_equiv_hours += equiv
            train_detail.append({
                "ops": [nm],
                "active_plan": training_active,
                "max_mastery_level": self._training_max_mastery_level(),
                "speed%": round(speed, 1),
                "initial_progress%": round(initial, 1),
                "base_session_hours": round(base_session_hours, 2),
                "equiv_hours/day": round(equiv, 2),
            })
        bd["训练室"] = train_equiv_hours * trc.get("ap_value_per_hour", 0.0)

        # ---- 加工站 ----
        # 副产品概率只在有加工任务时变现；默认配置为0, 有任务估值时按期望副产品理智计入。
        wc = self.cfg.get("workshop", {})
        workshop_detail = []
        workshop_pct = 0.0
        craft_category = wc.get("craft_category")
        base_craft_mood_cost = wc.get("craft_mood_cost", 0.0)
        red = ctrl_red + self._room_reduction(len(asg.workshop)) + self._other_recover_for(glob, "workshop")
        for nm in asg.workshop:
            frac = self._frac(nm, "workshop", red)
            st = self._stat(nm, "workshop")
            if st:
                if not craft_category or craft_category == "any":
                    workshop_pct += st.byproduct * frac
                else:
                    workshop_pct += sum(
                        amount for target, amount in st.byproduct_targets.items()
                        if self._workshop_byproduct_applies(target, craft_category)
                    ) * frac
                workshop_pct += sum(
                    amount for target, required_cost, amount in st.byproduct_mood_cost_targets
                    if self._workshop_byproduct_applies(target, craft_category)
                    and abs(float(base_craft_mood_cost) - required_cost) < 1e-9
                ) * frac
        requested_crafts = wc.get("crafts_per_day", 0.0) * (self._day_hours / 24.0)
        effective_craft_mood_cost = self._workshop_effective_mood_cost(
            asg.workshop,
            base_craft_mood_cost,
            craft_category,
        )
        if asg.workshop:
            byproduct_chance = max(0.0, min(1.0, wc.get("base_byproduct_chance", 0.0) + workshop_pct / 100.0))
        else:
            byproduct_chance = 0.0
        net_craft_mood_cost = self._workshop_failure_restore_mood_cost(
            asg.workshop,
            effective_craft_mood_cost,
            byproduct_chance,
            craft_category,
        )
        crafts = self._workshop_mood_limited_crafts(
            asg.workshop,
            requested_crafts,
            net_craft_mood_cost,
        )
        pity_extra_byproducts = self._workshop_pity_extra_byproducts(
            asg.workshop,
            crafts,
            effective_craft_mood_cost,
            byproduct_chance,
        )
        expected_byproducts = crafts * byproduct_chance + pity_extra_byproducts
        fixed_byproduct = self._workshop_fixed_byproduct_tags(asg.workshop, craft_category)
        lmd_saving_ap = 0.0
        craft_lmd_cost = float(wc.get("craft_lmd_cost", 0.0) or 0.0)
        if craft_lmd_cost > 0.0:
            for nm in asg.workshop:
                if self._frac(nm, "workshop") <= 0.0:
                    continue
                prof = self.prof.get(nm)
                if not prof:
                    continue
                if any(
                    "减免龙门币消耗" in b.desc
                    and self._workshop_desc_category_applies(b.desc, craft_category)
                    for b in prof.room_buffs.get("workshop", [])
                ):
                    lmd_saving_ap = crafts * craft_lmd_cost * self.val.get("龙门币", 0.0)
                    break
        byproduct_ap = expected_byproducts * wc.get("ap_per_byproduct", 0.0)
        workshop_ap = byproduct_ap + lmd_saving_ap
        if asg.workshop:
            workshop_detail.append({"ops": asg.workshop, "byproduct%": round(workshop_pct, 1),
                                    "byproduct_chance%": round(byproduct_chance * 100.0, 1),
                                    "pity_byproducts/day": round(pity_extra_byproducts, 3),
                                    "byproducts/day": round(expected_byproducts, 3),
                                    "requested_crafts/day": round(requested_crafts, 2),
                                    "crafts/day": round(crafts, 2),
                                    "base_mood_cost": round(base_craft_mood_cost, 2),
                                    "mood_cost": round(effective_craft_mood_cost, 2),
                                    "net_mood_cost": round(net_craft_mood_cost, 2),
                                    "fixed_byproduct": fixed_byproduct,
                                    "byproduct_ap/day": round(byproduct_ap, 3),
                                    "lmd_saving_ap/day": round(lmd_saving_ap, 3),
                                    "workshop_ap/day": round(workshop_ap, 3)})
        bd["加工站"] = workshop_ap

        # ---- 心情可持续性检查 (宿舍恢复 vs 工作消耗) ----
        # 工作消耗: 计入所有工作设施, 不只制造贸易。
        total_drain = 0.0
        work_rooms = ([("manufacture", ops) for _l, ops in asg.manufacture]
                      + [("trading", ops) for ops in asg.trading]
                      + [("power", ops) for ops in asg.power]
                      + [("control", asg.control), ("meeting", asg.meeting), ("hire", asg.hire),
                         ("training", asg.training if training_active else [])])
        for room, ops in work_rooms:
            red = None if room == "control" else ctrl_red + self._room_reduction(len(ops)) + self._other_recover_for(glob, room)
            if room != "control":
                red += self._room_drain_reduction(room, ops, red)
                total_drain += sum(self._net_drain(nm, room, red) * self._frac(nm, room, red) for nm in ops)
            else:
                total_drain += sum(
                    self._net_drain(nm, room, self._control_effective_reduction_for(nm, asg))
                    * self._frac(nm, room, self._control_effective_reduction_for(nm, asg))
                    for nm in ops
                )
        # 宿舍恢复能力: 每间 = 基础×人数 + 全员型(同种取最高)×人数 + 单体他人型(取最高, 仅1人)。
        # 菲亚梅塔等明确拒绝其他来源恢复的干员不能把占用的宿舍格当作普通恢复容量。
        dorm_recover = 0.0
        dorm_slots = int(self.cfg.get("dormitory", {}).get("slots", 5))
        dorm_names = {nm for room in asg.dormitory for nm in room}
        elite_rest_candidates = {
            nm
            for _room, ops in work_rooms
            for nm in ops
            if nm not in dorm_names and "罗德岛-精英干员" in factions_of(nm)
        }
        remaining_elite_resters = len(elite_rest_candidates)
        for ridx, room in enumerate(asg.dormitory):
            base_rec = dorm_base_recover_for_room(self.cfg, ridx)
            n = 0
            best_other = best_avg_pool = 0.0
            elite_n = 0
            ordinary_blocked = 0
            ordinary_targets: list[str] = []
            all_recover_providers: list[tuple[str, RoomStat, OperatorProfile | None]] = []
            for nm in room:
                prof = self.prof.get(nm)
                if _rejects_other_mood_recovery(prof):
                    ordinary_blocked += 1
                    continue
                n += 1
                ordinary_targets.append(nm)
                if "罗德岛-精英干员" in factions_of(nm):
                    elite_n += 1
                st = self._stat(nm, "dormitory")
                if st:
                    all_recover_providers.append((nm, st, prof))
                    target_extra = 0.0
                    if prof:
                        target_extra = max(
                            (dorm_target_extra_recover(b, room) for b in prof.room_buffs.get("dormitory", [])),
                            default=0.0,
                        )
                        avg_pool = max(
                            (dorm_average_recover_pool(b) for b in prof.room_buffs.get("dormitory", [])),
                            default=0.0,
                        )
                        best_avg_pool = max(best_avg_pool, avg_pool)
                    other_recover = st.dorm_recover_other
                    if best_avg_pool and abs(other_recover - best_avg_pool) < 1e-9:
                        other_recover = 0.0
                    best_other = max(best_other, other_recover + target_extra)
            current_ordinary_n = n
            n = max(n, max(0, dorm_slots - ordinary_blocked))
            empty_ordinary_slots = max(0, n - current_ordinary_n)
            future_elite_n = min(empty_ordinary_slots, remaining_elite_resters)
            remaining_elite_resters -= future_elite_n
            future_targets = [f"__future_rester_{i}" for i in range(empty_ordinary_slots)]

            def best_all_for_target(target: str) -> float:
                best = 0.0
                for provider, st, prof in all_recover_providers:
                    target_buffs = list(prof.room_buffs.get("dormitory", [])) if prof else []
                    self_excluded_all = max(
                        (
                            dorm_all_recover_for(b, "__future_rester__")
                            - dorm_all_recover_for(b, provider)
                            for b in target_buffs
                        ),
                        default=0.0,
                    )
                    provider_all = max(0.0, st.dorm_recover_all - self_excluded_all)
                    target_all_extra = max(
                        (
                            max(0.0, dorm_all_recover_for(b, target) - dorm_all_recover_for(b, provider))
                            for b in target_buffs
                        ),
                        default=0.0,
                    )
                    best = max(best, provider_all + target_all_extra)
                return best

            target_recover = sum(
                base_rec + glob["recover"] + best_all_for_target(target)
                for target in ordinary_targets + future_targets
            )
            dorm_recover += (
                target_recover
                + glob["recover_elite"] * (elite_n + future_elite_n)
                + (max(best_other, best_avg_pool) if n else 0.0)
            )
        if dorm_recover + 1e-9 < total_drain:
            warns.append(f"宿舍恢复({dorm_recover:.1f}/h) < 工作心情消耗({total_drain:.1f}/h), "
                         f"长期不可持续, 收益已按比例下调。")
            scale = dorm_recover / total_drain if total_drain else 1.0
            for k in bd:
                bd[k] *= scale

        facility_level_violations = self._facility_level_violations(asg)
        if facility_level_violations:
            joined = ", ".join(
                f"{v['room']} Lv{v['level']} > Lv{v['max_level']}"
                for v in facility_level_violations
            )
            warns.append(f"控制中枢等级限制: {joined}, 布局不可行。")
        facility_capacity_violations = self._facility_capacity_violations(asg)
        if facility_capacity_violations:
            def _capacity_warning_part(v: dict[str, int | str]) -> str:
                if v.get("type") == "duplicate_operator":
                    return f"{v['operator']} duplicate x{v['count']}"
                if v.get("type") == "invalid_manufacture_line":
                    return f"manufacture:{v['index']} invalid line {v['line']}"
                return f"{v['room']} {v['count']}>{v['max']}"

            joined = ", ".join(
                _capacity_warning_part(v)
                for v in facility_capacity_violations
            )
            warns.append(f"设施容量限制: {joined}, 布局不可行。")

        # ---- 电力可行性 (发电站供电 >= 总消耗) ----
        feasible, supply, demand = self._electricity_ok(asg)
        ap = sum(bd.values())
        if facility_level_violations or facility_capacity_violations:
            ap = -1e9 + ap
        if not feasible:
            warns.append(f"电力不足: 供电 {supply} < 消耗 {demand}, 布局不可行。")
            ap = -1e9 + ap   # 标记不可行, 优化器据此剔除

        result = Result(
            ap_per_day=ap,
            breakdown=bd,
            detail={"globals": glob, "manufacture": man_detail, "trading": trade_detail,
                    "power": power_detail, "meeting": meeting_detail, "hire": hire_detail,
                    "training": train_detail, "workshop": workshop_detail,
                    "gold_supply/day": round(gold_supply, 1),
                    "drones": {"per_day": round(drones_day, 0), "line": drone_line,
                               "kind": drone_kind,
                               "assist_unlocked": drone_assist_unlocked,
                               "minutes/day": round(drone_minutes, 1),
                               "extra_items/day": round(drone_items, 1)},
                    "facility_level_violations": facility_level_violations,
                    "facility_capacity_violations": facility_capacity_violations,
                    "electricity": {"supply": supply, "demand": demand}},
            warnings=warns,
        )
        self._capacity_frac_override = saved_capacity_frac
        return result

    def _electricity_ok(self, asg: Assignment) -> tuple[bool, int, int]:
        """发电站供电 vs 全设施电力消耗。"""
        ec = self.cfg["electricity"]["consumption"]
        fixed_rooms = self.cfg.get("layout", {}).get("fixed_rooms", {})

        def fixed_count(room: str) -> int:
            try:
                return int(fixed_rooms.get(room, 1))
            except (TypeError, ValueError):
                return 1

        supply = sum(self._power_supply_per_station(i) for i in range(len(asg.power)))
        demand = (
            sum(self._electricity_consumption("manufacture", i) for i in range(len(asg.manufacture)))
            + sum(self._electricity_consumption("trading", i) for i in range(len(asg.trading)))
            + sum(self._electricity_consumption("dormitory", i) for i in range(len(asg.dormitory)))
            + fixed_count("meeting") * self._electricity_consumption("meeting")
            + fixed_count("hire") * self._electricity_consumption("hire")
            + fixed_count("training") * self._electricity_consumption("training")
            + fixed_count("workshop") * self._electricity_consumption("workshop")
            + fixed_count("control") * ec["control"]
        )
        return supply >= demand, int(supply), int(demand)
