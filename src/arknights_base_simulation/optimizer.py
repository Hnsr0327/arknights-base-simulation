"""布局 + 排班优化器。

策略 (启发式, 兼顾速度与质量):
  1. 枚举布局: 制造:贸易:发电 三者瓜分 production_slots(默认9), 发电≥min_power。
  2. 每种布局做贪心排班:
       控制中枢(全局加成最大化) -> 制造/贸易(边际收益贪心) -> 发电/会客室/办公室
       -> 制造站逐房间选最优生产线(用真实引擎评估) -> 宿舍(补足心情可持续)。
  3. 轻量局部搜索: 尝试用未用干员替换生产位以提升日均理智。
  4. 取所有布局里日均理智最高者。
"""
from __future__ import annotations

from dataclasses import dataclass

from .engine import Assignment, Engine, OperatorProfile, Result, Schedule, dorm_base_recover_for_room
from .roster import Operator
from .skills import SkillDB


# 特殊贸易干员的"等效订单效率"(仅用于优化器候选排序; 引擎按真实机制结算)。
# 但书违约单≈+55%吞吐; 龙舌兰投资只作用于3级站20%的原生4赤金单,
# 期望收益约 +100/1450 = +6.9%。巫恋静态已带 trade_eff=45, 无需补。
SPECIAL_TRADE_EFF = {"但书": 55.0, "龙舌兰": 7.0}
# 特殊贸易组合的协同伙伴(无"当与X"描述, 硬编码): 巫恋低语解放席位 -> 配龙舌兰/柏喙/卡夫卡。
SPECIAL_TRADE_PARTNERS = {"巫恋": {"龙舌兰", "柏喙", "卡夫卡"}}
QUALITY_TRADE_ALPHA = {"裁缝·α", "手工艺品·α", "鉴定师的眼光", "懂行"}
QUALITY_TRADE_BETA = {"裁缝·β", "手工艺品·β", "鉴定师的手段"}


def build_profiles(roster: list[Operator], db: SkillDB) -> dict[str, OperatorProfile]:
    profiles: dict[str, OperatorProfile] = {}
    for op in roster:
        if not db.has_operator(op.name):
            continue
        # 同名(如阿米娅两分支)取练度更高者
        prev = profiles.get(op.name)
        if prev and (prev.elite, prev.level) >= (op.elite, op.level):
            continue
        profiles[op.name] = OperatorProfile(op.name, op.elite, op.level, db, op.rarity)
    return profiles


class Optimizer:
    def __init__(self, config: dict, profiles: dict[str, OperatorProfile], schedule: Schedule):
        self.cfg = config
        self.prof = profiles
        self.sch = schedule
        self.eng = Engine(config, profiles, schedule)
        self.lines = list(config["manufacture"]["lines"].keys())
        self._partner_cache = None

    # ---- 各设施可用候选 (有对应技能且数值>0) ----
    def _candidates(self, room: str, key) -> list[str]:
        out = []
        for nm, p in self.prof.items():
            st = p.stat(room)
            if st and key(st) > 0:
                out.append(nm)
        out.sort(key=lambda nm: key(self.prof[nm].stat(room)), reverse=True)
        return out

    def _enabled_categories(self) -> set[str]:
        """当前配置中启用的制造线类别 (record/gold/shard)。"""
        cats: set[str] = set()
        for spec in self.cfg["manufacture"]["lines"].values():
            cat = spec.get("category", "")
            if cat:
                cats.add(cat)
        return cats

    def _best_prod(self, st) -> float:
        if not st.prod:
            return 0.0
        enabled = self._enabled_categories()
        candidates = [c for c in ("record", "gold", "shard") if c in enabled]
        if not candidates:
            return st.prod.get("all", 0.0)
        return max(st.prod.get(c, 0.0) + st.prod.get("all", 0.0) for c in candidates)

    _DISABLED_CAT_KEYWORDS = {
        "record": "作战记录类",
        "gold": "贵金属类",
        "shard": "源石类",
    }

    def _effective_prod(self, nm: str, m: int, t: int, p: int) -> float:
        """考虑设施数量缩放、条件型技能、禁用类别的实际估计产值。"""
        import re
        prof = self.prof.get(nm)
        if not prof:
            return 0.0
        ms = prof.stat("manufacture")
        if not ms or not ms.prod:
            return 0.0
        enabled = self._enabled_categories()
        disabled_kw = {kw for cat, kw in self._DISABLED_CAT_KEYWORDS.items() if cat not in enabled}
        buffs = prof.room_buffs.get("manufacture", [])
        adjusted = 0.0
        has_zero_others = False
        has_unconditional = False
        for b in buffs:
            d = b.desc
            if "生产力" not in d:
                continue
            if "当与" in d:
                continue
            if any(kw in d for kw in disabled_kw):
                continue
            fm = re.search(r"每个发电站为当前制造站\+(\d+(?:\.\d+)?)%", d)
            if fm:
                adjusted += p * float(fm.group(1))
                has_unconditional = True
                if "其他干员提供的生产力全部归零" in d:
                    has_zero_others = True
                continue
            fm = re.search(r"每个贸易站为当前制造站.*?\+(\d+(?:\.\d+)?)%", d)
            if fm:
                adjusted += t * float(fm.group(1))
                has_unconditional = True
                continue
            fm = re.search(r"每个当前制造站内干员为当前制造站\+(\d+(?:\.\d+)?)%", d)
            if fm:
                adjusted += 3 * float(fm.group(1))
                has_unconditional = True
                if "其他干员提供的生产力全部归零" in d:
                    has_zero_others = True
                continue
            fm = re.search(r"每(\d+)%生产力.*?额外.*?(\d+(?:\.\d+)?)%.*?最多.*?(\d+(?:\.\d+)?)%", d)
            if fm:
                adjusted += float(fm.group(3)) * 0.75
                has_unconditional = True
                continue
            if "心情落差" in d and "生产力" in d:
                fm = re.search(r"生产力\+(\d+(?:\.\d+)?)%", d)
                penalty = re.search(r"生产力-(\d+(?:\.\d+)?)%", d)
                if fm:
                    adjusted += float(fm.group(1))
                    has_unconditional = True
                continue
            fm = re.search(r"生产力\+(\d+(?:\.\d+)?)%", d)
            if fm:
                adjusted += float(fm.group(1))
                has_unconditional = True
                continue
        if not has_unconditional and adjusted <= 0:
            return 0.0
        if adjusted > 0:
            return adjusted
        return self._best_prod(ms)

    def _prod_category(self, nm: str) -> str:
        """干员最强制造类别 (仅限启用的生产线)。

        仅有未启用类别技能(如源石碎片未启用时的shard专精)的干员返回 'none'。
        """
        prof = self.prof.get(nm)
        if not prof:
            return "all"
        ms = prof.stat("manufacture")
        if not ms or not ms.prod:
            return "all"
        enabled = self._enabled_categories()
        has_all = ms.prod.get("all", 0.0) > 0
        best_cat, best_val = "all" if has_all else "none", ms.prod.get("all", 0.0)
        for c in ("record", "gold", "shard"):
            if c not in enabled:
                continue
            v = ms.prod.get(c, 0.0) + ms.prod.get("all", 0.0)
            if v > best_val:
                best_cat, best_val = c, v
        return best_cat

    def _quality_trade_eff(self, nm: str) -> float:
        prof = self.prof.get(nm)
        if not prof:
            return 0.0
        names = {b.buff_name for b in prof.room_buffs.get("trading", [])}
        if names & QUALITY_TRADE_BETA:
            return 1.5
        if names & QUALITY_TRADE_ALPHA:
            return 1.0
        return 0.0

    def _enumerate_layouts(self) -> list[tuple[int, int, int]]:
        total = self.cfg["layout"]["production_slots"]
        layout_cfg = self.cfg["layout"]
        pmin = layout_cfg["min_power"]
        max_m = layout_cfg.get("max_manufacture", total)
        max_t = layout_cfg.get("max_trading", total)
        max_p = layout_cfg.get("max_power", total)
        layouts = []
        for p in range(pmin, min(max_p, total) + 1):  # 发电
            for m in range(0, min(max_m, total - p) + 1):  # 制造
                t = total - p - m                  # 贸易
                if t < 0 or t > max_t or t > m:
                    continue
                layouts.append((m, t, p))
        return layouts

    def _facility_slots(self, section: str, room_idx: int = 0) -> int:
        return self.eng._facility_slots(section, room_idx)

    def _room_slots_list(self, section: str, count: int) -> list[int]:
        return [self._facility_slots(section, i) for i in range(count)]

    def _fixed_room_count(self, section: str, default: int = 1) -> int:
        fixed_rooms = self.cfg.get("layout", {}).get("fixed_rooms", {})
        raw = fixed_rooms.get(section, default)
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return max(0, default)

    def _fixed_facility_slots(self, section: str) -> int:
        return self._facility_slots(section) if self._fixed_room_count(section) > 0 else 0

    def _trading_gold_order_profile(self, room_idx: int = 0) -> dict:
        return self.eng._trading_gold_order_profile(room_idx)

    def _dorm_room_limit(self) -> int:
        dc = self.cfg["dormitory"]
        try:
            max_rooms = int(dc.get("max_rooms", 0))
        except (TypeError, ValueError):
            max_rooms = 0
        fixed_rooms = self.cfg.get("layout", {}).get("fixed_rooms", {})
        if "dormitory" in fixed_rooms:
            try:
                max_rooms = min(max_rooms, int(fixed_rooms["dormitory"]))
            except (TypeError, ValueError):
                pass
        return max(0, max_rooms)

    def _assign(self, m: int, t: int, p: int, exclude: set[str] | None = None,
                seed: dict[str, str] | None = None) -> Assignment:
        cfg = self.cfg
        seed = seed or {}
        used: set[str] = set(exclude or ())
        asg = Assignment()

        def take(name):
            used.add(name)

        # --- Pre-place seed control operators ---
        for nm, target in seed.items():
            if target == "control" and nm in self.prof and nm not in used:
                asg.control.append(nm)
                take(nm)

        # --- 控制中枢: 全局加成最大化 ---
        ctrl = self._candidates(
            "control",
            lambda st: max(st.prod_global.values(), default=0.0) * 3
            + st.trade_eff_global + st.global_recover * 5 + st.global_recover_elite * 3,
        )
        for nm in ctrl:
            if nm in used:
                continue
            if len(asg.control) >= self._fixed_facility_slots("control"):
                break
            asg.control.append(nm)
            take(nm)

        # --- 生产位: 制造 vs 贸易 边际贪心 ---
        ms_per_room = self._room_slots_list("manufacture", m)
        ts_per_room = self._room_slots_list("trading", t)
        man_slots = sum(ms_per_room)
        trade_slots = sum(ts_per_room)
        # 估每点 prod% / eff% 的 ap 价值, 用于跨设施比较 (基础产速 = 60/基础耗时)
        mc = cfg["manufacture"]
        best_line_rate = max(
            (60.0 / spec["base_minutes_per_item"]) * spec.get("units_per_item", 1)
            * self._material_chain_value(spec)
            for spec in mc["lines"].values()
        )
        v_per_prod = best_line_rate / 100.0 * 24.0
        to = self._trading_gold_order_profile()
        base_orders_ph = 60.0 / to["base_minutes_per_order"]
        v_per_eff = (base_orders_ph / 100.0
                     * to["lmd_per_order"] * self.cfg["material_values_ap"]["龙门币"] * 24.0)

        cand = []  # (value, name, role)
        for nm, prof in self.prof.items():
            if nm in used:
                continue
            ms = prof.stat("manufacture")
            ts = prof.stat("trading")
            eff_prod = self._effective_prod(nm, m, t, p)
            if eff_prod > 0:
                cand.append((eff_prod * v_per_prod, nm, "manufacture"))
            eff_rank = (ts.trade_eff if ts else 0.0) + SPECIAL_TRADE_EFF.get(nm, 0.0) + self._quality_trade_eff(nm)
            if eff_rank > 0:
                cand.append((eff_rank * v_per_eff, nm, "trading"))
        cand.sort(reverse=True)

        man_ops: list[str] = []
        trade_ops: list[str] = []
        for nm, target in seed.items():
            if target == "manufacture" and nm in self.prof and nm not in used:
                man_ops.append(nm); take(nm)
            elif target == "trading" and nm in self.prof and nm not in used:
                trade_ops.append(nm); take(nm)
        for _v, nm, role in cand:
            if nm in used:
                continue
            if role == "manufacture" and len(man_ops) < man_slots:
                man_ops.append(nm); take(nm)
            elif role == "trading" and len(trade_ops) < trade_slots:
                trade_ops.append(nm); take(nm)
            if len(man_ops) >= man_slots and len(trade_ops) >= trade_slots:
                break

        # 分配到房间: 同类别严格不混编(gold 和 record 不进同一房间, all 填补空位)
        by_cat: dict[str, list[str]] = {"gold": [], "record": [], "all": []}
        for nm in man_ops:
            by_cat.setdefault(self._prod_category(nm), by_cat["all"]).append(nm)
        fillers = list(by_cat["all"])
        man_rooms: list[list[str]] = []
        for cat in ("gold", "record"):
            pool = by_cat[cat]
            pos = 0
            while pos < len(pool) and len(man_rooms) < m:
                n = ms_per_room[len(man_rooms)]
                room = pool[pos:pos + n]
                pos += len(room)
                while len(room) < n and fillers:
                    room.append(fillers.pop(0))
                man_rooms.append(room)
        while len(man_rooms) < m:
            n = ms_per_room[len(man_rooms)]
            man_rooms.append(fillers[:n])
            fillers = fillers[n:]
        for room_ops in man_rooms:
            asg.manufacture.append([self.lines[0], room_ops])
        pos = 0
        for i in range(t):
            n = ts_per_room[i]
            asg.trading.append(trade_ops[pos:pos + n])
            pos += n

        # --- 发电站 ---
        for nm in self._candidates("power", lambda st: st.power):
            if nm in used:
                continue
            if len(asg.power) >= p:
                break
            asg.power.append([nm]); take(nm)
        while len(asg.power) < p:
            asg.power.append([])

        # --- 会客室 / 办公室 ---
        meeting_slots = self._fixed_facility_slots("meeting")
        for nm in self._candidates("meeting", lambda st: st.clue):
            if nm in used or len(asg.meeting) >= meeting_slots:
                continue
            asg.meeting.append(nm); take(nm)
        hire_slots = self._fixed_facility_slots("hire")
        for nm in self._candidates("hire", lambda st: st.contact):
            if nm in used or len(asg.hire) >= hire_slots:
                continue
            asg.hire.append(nm); take(nm)

        # --- 宿舍: 先补足心情可持续 (否则可持续性惩罚会把各生产线收益都压到0, 无法比较) ---
        self._fill_dorms(asg, used, seed=seed)

        # --- 生产线优化 (逐房间挑最优线) ---
        asg.manufacture = [(line, ops) for line, ops in asg.manufacture]
        self._optimize_lines(asg)
        return asg

    def _material_chain_value(self, spec: dict) -> float:
        """单位产物的 ap 价值; 赤金按贸易链折算(取与素材价值的较大者)。"""
        vals = self.cfg["material_values_ap"]
        if spec["output"] == "赤金":
            to = self._trading_gold_order_profile()
            chain = (to["lmd_per_order"] * vals["龙门币"]) / max(1, to["gold_per_order"])
            return max(vals.get("赤金", 0.0), chain)
        return vals.get(spec["output"], 0.0)

    # Resource pool producers: (operator, target_facility) pairs from synergy.py's
    # build_resource_pools. Used to generate synergy seeds — the greedy filler misses
    # these cross-station chains because each operator's individual value is low.
    _POOL_PRODUCERS = {
        "迷迭香": "manufacture",   # 感知信息(dorm_headcount) → 思维链环
        "黑键": "trading",         # 感知信息(dorm_headcount) → 无声共鸣
        "车尔尼": "dormitory",     # 小节 → 感知信息
        "爱丽丝": "dormitory",     # 梦境 → 感知信息
        "塑心": "dormitory",       # 无声共鸣(dorm_room_members)
        "至简": "manufacture",     # 工程机器人(facility_levels)
        "截云": "manufacture",     # 巫术结晶(人间烟火/5)
        "令": "control",           # 人间烟火 + 感知信息 (mood≤12)
        "夕": "control",           # 人间烟火 + 感知信息 (mood>12)
        "森西": "dormitory",       # 魔物料理(dorm_level)
        "桑葚": "hire",            # 人间烟火(recruit_slots)
        "乌有": "trading",         # 人间烟火(dorm_headcount)
        "絮雨": "hire",            # 记忆碎片 → 感知信息
        "深律": "hire",            # 无声共鸣(recruit_slots)
    }

    def _synergy_seeds(self) -> list[dict[str, str]]:
        """Generate synergy group seeds from resource pool chains."""
        available = {nm for nm in self._POOL_PRODUCERS if nm in self.prof}
        seeds: list[dict[str, str]] = []

        if "令" in available or "夕" in available:
            seed: dict[str, str] = {}
            for nm in ("令", "夕"):
                if nm in available:
                    seed[nm] = "control"
            if "迷迭香" in available:
                seed["迷迭香"] = "manufacture"
            if "至简" in available:
                seed["至简"] = "manufacture"
            for nm in ("车尔尼", "爱丽丝", "塑心"):
                if nm in available:
                    seed[nm] = "dormitory"
            if "黑键" in available:
                seed["黑键"] = "trading"
            seeds.append(seed)

        if "至简" in available:
            seeds.append({"至简": "manufacture"})

        for ctrl_nm in available:
            if self._POOL_PRODUCERS[ctrl_nm] != "control":
                continue
            st = self.prof[ctrl_nm].stat("control")
            if st and (max(st.prod_global.values(), default=0) > 0 or st.trade_eff_global > 0):
                seeds.append({ctrl_nm: "control"})

        return seeds

    def _assign_seeded(self, m: int, t: int, p: int, seed: dict[str, str]) -> Assignment:
        """Like _assign but pre-places seed operators before greedy fill.

        Uses native seed integration in _assign: seed operators are placed
        first in their target facilities, then remaining slots are filled
        by the greedy algorithm. No displacement needed — seed ops are added
        to 'used' during pre-placement, preventing greedy from touching them.
        """
        valid_seed = {nm: room for nm, room in seed.items() if nm in self.prof}
        return self._assign(m, t, p, seed=valid_seed)

    def _remove_from_asg(self, asg: Assignment, nm: str):
        """Remove an operator from whatever facility they're in."""
        if nm in asg.control:
            asg.control.remove(nm)
        for _line, ops in asg.manufacture:
            if nm in ops:
                ops.remove(nm)
        for ops in asg.trading:
            if nm in ops:
                ops.remove(nm)
        for ops in asg.power:
            if nm in ops:
                ops.remove(nm)
        if nm in asg.meeting:
            asg.meeting.remove(nm)
        if nm in asg.hire:
            asg.hire.remove(nm)
        for ops in asg.dormitory:
            if nm in ops:
                ops.remove(nm)

    def _place_into(self, asg: Assignment, nm: str, target: str,
                    locked: set[str] | None = None) -> str | None:
        """Place operator into a facility. Returns displaced operator or None."""
        locked = locked or set()

        def _insert_or_replace(lst, max_slots):
            if len(lst) < max_slots:
                lst.append(nm)
                return None
            for i in range(len(lst) - 1, -1, -1):
                if lst[i] not in locked:
                    old = lst[i]
                    lst[i] = nm
                    return old
            lst.append(nm)
            return None

        def _insert_room(rooms_list, max_s):
            best_room = min(range(len(rooms_list)),
                            key=lambda i: len(rooms_list[i]))
            ops = rooms_list[best_room]
            if len(ops) < max_s:
                ops.append(nm)
                return None
            for i in range(len(ops) - 1, -1, -1):
                if ops[i] not in locked:
                    old = ops[i]
                    ops[i] = nm
                    return old
            ops.append(nm)
            return None

        if target == "control":
            return _insert_or_replace(asg.control, self._fixed_facility_slots("control"))
        elif target == "manufacture":
            if asg.manufacture:
                rooms = [ops for _l, ops in asg.manufacture]
                return _insert_room(rooms, self._facility_slots("manufacture"))
        elif target == "trading":
            if asg.trading:
                return _insert_room(asg.trading, self._facility_slots("trading"))
        elif target == "dormitory":
            if asg.dormitory:
                slots = self.cfg["dormitory"]["slots"]
                best_room = min(range(len(asg.dormitory)),
                                key=lambda i: len(asg.dormitory[i]))
                ops = asg.dormitory[best_room]
                if len(ops) < slots:
                    ops.append(nm)
                    return None
                for i in range(len(ops) - 1, -1, -1):
                    if ops[i] not in locked:
                        old = ops[i]
                        ops[i] = nm
                        return old
                ops.append(nm)
                return None
        elif target == "meeting":
            return _insert_or_replace(asg.meeting, self._fixed_facility_slots("meeting"))
        elif target == "hire":
            _insert_or_replace(asg.hire, self._fixed_facility_slots("hire"))

    def _optimize_lines(self, asg: Assignment) -> None:
        man = asg.manufacture
        for _ in range(2):
            changed = False
            for i in range(len(man)):
                best_line, best_ap = man[i][0], -1.0
                for line in self.lines:
                    man[i] = (line, man[i][1])
                    ap = self.eng.evaluate(asg).ap_per_day
                    if ap > best_ap:
                        best_ap, best_line = ap, line
                if man[i][0] != best_line:
                    changed = True
                man[i] = (best_line, man[i][1])
            if not changed:
                break

    def _fill_dorms(self, asg: Assignment, used: set[str],
                    seed: dict[str, str] | None = None) -> None:
        dc = self.cfg["dormitory"]
        seed_dorm = [nm for nm, target in (seed or {}).items()
                     if target == "dormitory" and nm in self.prof and nm not in used]
        for nm in seed_dorm:
            used.add(nm)
        recov_cands = [nm for nm in self._candidates("dormitory", lambda st: st.dorm_recover)
                       if nm not in used]
        idx = 0
        seed_idx = 0
        for _ in range(self._dorm_room_limit()):
            room: list[str] = []
            while seed_idx < len(seed_dorm) and len(room) < dc["slots"]:
                room.append(seed_dorm[seed_idx]); seed_idx += 1
            while len(room) < dc["slots"] and idx < len(recov_cands):
                room.append(recov_cands[idx]); used.add(recov_cands[idx]); idx += 1
            asg.dormitory.append(room)

    def _local_search(self, asg: Assignment, iters: int = 60,
                      exclude: set[str] | None = None,
                      locked: set[str] | None = None) -> Assignment:
        """尝试用未用候选替换制造/贸易位以提升日均理智。locked operators cannot be swapped out."""
        used = self._used_set(asg) | (exclude or set())
        locked = locked or set()
        cur = self.eng.evaluate(asg).ap_per_day
        # 生产位可替换的候选池
        man_pool = [nm for nm in self._candidates("manufacture", self._best_prod) if nm not in used]
        trade_pool = [
            nm for nm in self.prof
            if nm not in used
            and ((self.prof[nm].stat("trading") and self.prof[nm].stat("trading").trade_eff > 0) or self._quality_trade_eff(nm) > 0)
        ]
        trade_pool.sort(
            key=lambda nm: (
                (self.prof[nm].stat("trading").trade_eff if self.prof[nm].stat("trading") else 0.0)
                + self._quality_trade_eff(nm)
            ),
            reverse=True,
        )
        # 特殊干员(但书/龙舌兰)静态 trade_eff≈0, 不会进 _candidates, 显式优先纳入
        for nm in SPECIAL_TRADE_EFF:
            if nm in self.prof and nm not in used and nm not in trade_pool:
                trade_pool.insert(0, nm)
        improved = True
        rounds = 0
        while improved and rounds < iters:
            improved = False
            rounds += 1
            for ri, (line, ops) in enumerate(asg.manufacture):
                for si in range(len(ops)):
                    if ops[si] in locked:
                        continue
                    for cnm in man_pool[:8]:
                        if cnm in self._used_set(asg):
                            continue
                        old = ops[si]
                        ops[si] = cnm
                        ap = self.eng.evaluate(asg).ap_per_day
                        if ap > cur + 1e-6:
                            cur = ap; improved = True
                        else:
                            ops[si] = old
            for ri, ops in enumerate(asg.trading):
                for si in range(len(ops)):
                    if ops[si] in locked:
                        continue
                    for cnm in trade_pool[:8]:
                        if cnm in self._used_set(asg):
                            continue
                        old = ops[si]
                        ops[si] = cnm
                        ap = self.eng.evaluate(asg).ap_per_day
                        if ap > cur + 1e-6:
                            cur = ap; improved = True
                        else:
                            ops[si] = old
        return asg

    def _partners(self) -> dict[str, set[str]]:
        """从技能描述解析"当与X"配对伙伴, 用于团队协同的同室安置。"""
        if getattr(self, "_partner_cache", None) is not None:
            return self._partner_cache
        import re
        out: dict[str, set[str]] = {}
        for nm, prof in self.prof.items():
            for _room, buffs in prof.room_buffs.items():
                for b in buffs:
                    for mt in re.finditer(r"当与([^\s，。]{1,8}?)(?:在同一个|一起进驻)", b.desc):
                        other = mt.group(1).strip("”“\"")
                        if other in self.prof:
                            out.setdefault(nm, set()).add(other)
        # 特殊贸易组合(巫恋低语解放席位)硬编码协同
        for nm, mates in SPECIAL_TRADE_PARTNERS.items():
            if nm in self.prof:
                out.setdefault(nm, set()).update(m for m in mates if m in self.prof)
        self._partner_cache = out
        return out

    def _synergy_pass(self, asg: Assignment, unavailable: set[str] | None = None,
                      locked: set[str] | None = None) -> Assignment:
        """配对协同安置: 已排干员若有可用伙伴, 尝试把伙伴塞进同一房间(替换最弱位)以激活联动。"""
        partners = self._partners()
        unavailable = unavailable or set()
        locked = locked or set()
        cur = self.eng.evaluate(asg).ap_per_day
        rooms = [("manufacture", i, ops) for i, (_l, ops) in enumerate(asg.manufacture)]
        rooms += [("trading", i, ops) for i, ops in enumerate(asg.trading)]
        rooms.append(("control", 0, asg.control))
        for _rt, _idx, ops in rooms:
            for placed in list(ops):
                for mate in partners.get(placed, ()):
                    if mate in unavailable or mate in self._used_set(asg):
                        continue
                    best_gain, best_si = 0.0, -1
                    for si in range(len(ops)):
                        if ops[si] == placed or ops[si] in locked:
                            continue
                        old = ops[si]
                        ops[si] = mate
                        ap = self.eng.evaluate(asg).ap_per_day
                        if ap - cur > best_gain + 1e-6:
                            best_gain, best_si = ap - cur, si
                        ops[si] = old
                    if best_si >= 0:
                        ops[best_si] = mate
                        cur += best_gain
        return asg

    @staticmethod
    def _used_set(asg: Assignment) -> set[str]:
        s = set(asg.control) | set(asg.meeting) | set(asg.hire) | set(asg.workshop) | set(asg.training)
        for _l, ops in asg.manufacture:
            s |= set(ops)
        for ops in asg.trading + asg.power + asg.dormitory:
            s |= set(ops)
        return s

    @staticmethod
    def _all_positions(asg: Assignment) -> list[tuple[str, int, int]]:
        """All occupied positions: (room_type, room_index, slot_index)."""
        pos = []
        for si in range(len(asg.control)):
            pos.append(("control", 0, si))
        for ri, (_l, ops) in enumerate(asg.manufacture):
            for si in range(len(ops)):
                pos.append(("manufacture", ri, si))
        for ri, ops in enumerate(asg.trading):
            for si in range(len(ops)):
                pos.append(("trading", ri, si))
        for ri, ops in enumerate(asg.power):
            for si in range(len(ops)):
                pos.append(("power", ri, si))
        for si in range(len(asg.meeting)):
            pos.append(("meeting", 0, si))
        for si in range(len(asg.hire)):
            pos.append(("hire", 0, si))
        for ri, ops in enumerate(asg.dormitory):
            for si in range(len(ops)):
                pos.append(("dormitory", ri, si))
        return pos

    @staticmethod
    def _get_op(asg: Assignment, rt: str, ri: int, si: int) -> str:
        if rt == "control":
            return asg.control[si]
        if rt == "manufacture":
            return asg.manufacture[ri][1][si]
        if rt == "trading":
            return asg.trading[ri][si]
        if rt == "power":
            return asg.power[ri][si]
        if rt == "meeting":
            return asg.meeting[si]
        if rt == "hire":
            return asg.hire[si]
        if rt == "dormitory":
            return asg.dormitory[ri][si]
        return ""

    @staticmethod
    def _set_op(asg: Assignment, rt: str, ri: int, si: int, nm: str):
        if rt == "control":
            asg.control[si] = nm
        elif rt == "manufacture":
            asg.manufacture[ri][1][si] = nm
        elif rt == "trading":
            asg.trading[ri][si] = nm
        elif rt == "power":
            asg.power[ri][si] = nm
        elif rt == "meeting":
            asg.meeting[si] = nm
        elif rt == "hire":
            asg.hire[si] = nm
        elif rt == "dormitory":
            asg.dormitory[ri][si] = nm

    def _cross_facility_search(self, asg: Assignment, iters: int = 10,
                               locked: set[str] | None = None) -> Assignment:
        """Targeted cross-facility moves that don't disrupt dorm recovery.

        Three move types (runs until no improvement or max iters):
        1. Manufacture ↔ Trading swaps (cross-production)
        2. Control re-seating from unused pool
        3. Unused → Dorm (resource pool producers only for speed)
        """
        locked = locked or set()
        cur = self.eng.evaluate(asg).ap_per_day
        for _ in range(iters):
            prev = cur

            # 1. Manufacture ↔ Trading cross-swaps
            for ri, (_line, m_ops) in enumerate(asg.manufacture):
                for msi in range(len(m_ops)):
                    if m_ops[msi] in locked:
                        continue
                    for ti, t_ops in enumerate(asg.trading):
                        for tsi in range(len(t_ops)):
                            if t_ops[tsi] in locked:
                                continue
                            m_old, t_old = m_ops[msi], t_ops[tsi]
                            m_ops[msi], t_ops[tsi] = t_old, m_old
                            ap = self.eng.evaluate(asg).ap_per_day
                            if ap > cur + 1e-6:
                                cur = ap
                            else:
                                m_ops[msi], t_ops[tsi] = m_old, t_old

            # 2. Control re-seating: try each unused control-capable op in each slot
            ctrl_candidates = [
                nm for nm in self.prof
                if nm not in self._used_set(asg) and self.prof[nm].stat("control")
                and (max(self.prof[nm].stat("control").prod_global.values(), default=0) > 0
                     or self.prof[nm].stat("control").trade_eff_global > 0
                     or self.prof[nm].stat("control").global_recover > 0)
            ]
            for cnm in ctrl_candidates[:8]:
                for si in range(len(asg.control)):
                    if asg.control[si] in locked:
                        continue
                    old = asg.control[si]
                    asg.control[si] = cnm
                    ap = self.eng.evaluate(asg).ap_per_day
                    if ap > cur + 1e-6:
                        cur = ap
                        break
                    asg.control[si] = old

            # 3. Unused → Dorm: only resource pool producers (small candidate set)
            used = self._used_set(asg)
            pool_cands = [nm for nm in self._POOL_PRODUCERS
                          if nm in self.prof and nm not in used
                          and self._POOL_PRODUCERS[nm] == "dormitory"]
            for unm in pool_cands:
                best_gain, best_pos = 0.0, None
                for ri, d_ops in enumerate(asg.dormitory):
                    for si in range(len(d_ops)):
                        if d_ops[si] in locked:
                            continue
                        old = d_ops[si]
                        d_ops[si] = unm
                        ap = self.eng.evaluate(asg).ap_per_day
                        if ap - cur > best_gain + 1e-6:
                            best_gain, best_pos = ap - cur, (ri, si)
                        d_ops[si] = old
                if best_pos:
                    ri, si = best_pos
                    asg.dormitory[ri][si] = unm
                    cur += best_gain

            if cur - prev < 1e-6:
                break
        return asg

    # ---- 单站优化 ----

    def optimize_station(self, room_type: str, index: int,
                         locked: Assignment, pool: list[str] | None = None) -> list[str]:
        """Find the best operators for one station, given a partially-locked assignment.

        Args:
            room_type: "manufacture", "trading", "control", "meeting", "hire", "power", "dormitory"
            index: room index (0 for control/meeting/hire, room index for manufacture/trading/power/dormitory)
            locked: the current assignment (operators in other stations are fixed)
            pool: candidate operators to try; defaults to all unused operators with relevant skills

        Returns:
            Best operator list for the specified station.
        """
        import copy, itertools
        used = self._used_set(locked)

        # Get current occupants of target station
        current_ops = []
        if room_type == "control":
            current_ops = list(locked.control)
        elif room_type == "manufacture" and index < len(locked.manufacture):
            current_ops = list(locked.manufacture[index][1])
        elif room_type == "trading" and index < len(locked.trading):
            current_ops = list(locked.trading[index])
        elif room_type == "power" and index < len(locked.power):
            current_ops = list(locked.power[index])
        elif room_type == "meeting":
            current_ops = list(locked.meeting)
        elif room_type == "hire":
            current_ops = list(locked.hire)
        elif room_type == "dormitory" and index < len(locked.dormitory):
            current_ops = list(locked.dormitory[index])

        # Remove current occupants from "used" — they're available for reassignment
        used -= set(current_ops)
        n_slots = len(current_ops) or self._facility_slots(room_type)

        if pool is None:
            pool = [nm for nm in self.prof if nm not in used]

        # Try all combinations (or top-K if pool is large)
        candidates = pool[:20]
        best_ops, best_ap = current_ops, self.eng.evaluate(locked).ap_per_day

        for combo in itertools.combinations(candidates, min(n_slots, len(candidates))):
            test = copy.deepcopy(locked)
            combo_list = list(combo)
            if room_type == "control":
                test.control = combo_list
            elif room_type == "manufacture":
                test.manufacture[index] = (test.manufacture[index][0], combo_list)
            elif room_type == "trading":
                test.trading[index] = combo_list
            elif room_type == "power":
                test.power[index] = combo_list
            elif room_type == "meeting":
                test.meeting = combo_list
            elif room_type == "hire":
                test.hire = combo_list
            elif room_type == "dormitory":
                test.dormitory[index] = combo_list

            ap = self.eng.evaluate(test).ap_per_day
            if ap > best_ap + 1e-6:
                best_ap, best_ops = ap, combo_list

        return best_ops

    # ---- 轮休排班辅助 ----

    def _max_work_gaps(self) -> int:
        """宿舍恢复率决定每位干员每天最多可工作的 gap 数。"""
        n_dorms = self._dorm_room_limit()
        if n_dorms > 0:
            recovery = sum(dorm_base_recover_for_room(self.cfg, i)
                           for i in range(n_dorms)) / n_dorms
        else:
            recovery = float(self.cfg["dormitory"].get("base_recover_per_hour", 2.0))
        drain = self.cfg["mood"]["base_drain_per_hour"]
        n_gaps = len(self.sch.gaps)
        avg_gap = 24.0 / n_gaps
        if drain + recovery <= 0:
            return n_gaps
        max_h = 24.0 * recovery / (drain + recovery)
        return min(n_gaps, max(1, int(max_h / avg_gap)))

    def _working_positions(self, asg: Assignment) -> list[tuple[str, str, int, int]]:
        """所有工作位的 (干员名, 设施类型, 房间号, 槽位号)。"""
        pos: list[tuple[str, str, int, int]] = []
        for si, nm in enumerate(asg.control):
            pos.append((nm, "control", 0, si))
        for ri, (_l, ops) in enumerate(asg.manufacture):
            for si, nm in enumerate(ops):
                pos.append((nm, "manufacture", ri, si))
        for ri, ops in enumerate(asg.trading):
            for si, nm in enumerate(ops):
                pos.append((nm, "trading", ri, si))
        for ri, ops in enumerate(asg.power):
            for si, nm in enumerate(ops):
                pos.append((nm, "power", ri, si))
        for si, nm in enumerate(asg.meeting):
            pos.append((nm, "meeting", 0, si))
        for si, nm in enumerate(asg.hire):
            pos.append((nm, "hire", 0, si))
        return pos

    def _position_value(self, nm: str, room: str, m: int, t: int, p: int) -> float:
        """干员在指定设施的启发式价值(用于休息分组排序)。"""
        prof = self.prof.get(nm)
        if not prof:
            return 0.0
        if room == "manufacture":
            return self._effective_prod(nm, m, t, p)
        if room == "trading":
            ts = prof.stat("trading")
            return (ts.trade_eff if ts else 0.0) + SPECIAL_TRADE_EFF.get(nm, 0.0) + self._quality_trade_eff(nm)
        if room == "control":
            st = prof.stat("control")
            if not st:
                return 0.0
            return (max(st.prod_global.values(), default=0.0) * 3
                    + st.trade_eff_global + st.global_recover * 5 + st.global_recover_elite * 3)
        if room == "power":
            st = prof.stat("power")
            return st.power if st else 0.0
        if room == "meeting":
            st = prof.stat("meeting")
            return st.clue if st else 0.0
        if room == "hire":
            st = prof.stat("hire")
            return st.contact if st else 0.0
        return 0.0

    def _identify_007(self, template: Assignment, m: int, t: int, p: int,
                      max_perpetual: int = 3) -> set[str]:
        """Identify operators with unique non-substitutable mechanics for 007.

        Prioritizes operators in SPECIAL_TRADE_EFF/PARTNERS (违约单, 低語, 投資 etc.)
        that are already placed in the template.
        """
        used = self._used_set(template)
        special = set(SPECIAL_TRADE_EFF) | set(SPECIAL_TRADE_PARTNERS)
        # Only operators with unique mechanics that are currently placed in production
        prod_ops = set()
        for _l, ops in template.manufacture:
            prod_ops |= set(ops)
        for ops in template.trading:
            prod_ops |= set(ops)

        perpetual = {nm for nm in special if nm in prod_ops}
        return perpetual

    def _make_rest_groups(self, positions: list[tuple[str, str, int, int]], n_gaps: int,
                          max_work: int, m: int, t: int, p: int,
                          perpetual: set[str] | None = None) -> list[set[str]]:
        """将工作干员按价值均衡分配到 n_gaps 个休息组。

        perpetual operators are excluded (they work all gaps).
        每位干员需休息 (n_gaps - max_work) 个 gap; 高价值干员优先分散到负载最低的组,
        使各 gap 产出损失尽量均匀。
        """
        perpetual = perpetual or set()
        rest_per = n_gaps - max_work
        if rest_per <= 0:
            return [set() for _ in range(n_gaps)]
        seen: dict[str, float] = {}
        for nm, room, _ri, _si in positions:
            if nm not in seen and nm not in perpetual:
                seen[nm] = self._position_value(nm, room, m, t, p)
        sorted_ops = sorted(seen, key=seen.get, reverse=True)
        groups: list[list[str]] = [[] for _ in range(n_gaps)]
        load = [0.0] * n_gaps
        for nm in sorted_ops:
            indices = sorted(range(n_gaps), key=lambda i: load[i])
            for i in indices[:rest_per]:
                groups[i].append(nm)
                load[i] += seen[nm]
        return [set(g) for g in groups]

    def _control_themes(self) -> list[list[str]]:
        """Generate diverse control lineups for per-shift rotation.

        Each theme uses a different core set of control operators so that
        different shifts get different global bonuses, naturally driving
        diverse production lineups.
        """
        ctrl_slots = self._fixed_facility_slots("control")
        scored = []
        for nm in self.prof:
            st = self.prof[nm].stat("control")
            if not st:
                continue
            v = (max(st.prod_global.values(), default=0.0) * 3
                 + st.trade_eff_global + st.global_recover * 5
                 + st.global_recover_elite * 3)
            if v > 0:
                scored.append((v, nm))
        scored.sort(reverse=True)
        all_ctrl = [nm for _, nm in scored]

        themes = []
        used_across: set[str] = set()
        max_themes = len(self.sch.gaps) + 2
        for _ in range(max_themes):
            fresh = [nm for nm in all_ctrl if nm not in used_across]
            reuse = [nm for nm in all_ctrl if nm in used_across]
            pool = fresh + reuse
            theme = pool[:ctrl_slots]
            if not theme or any(set(theme) == set(t) for t in themes):
                break
            themes.append(theme)
            used_across.update(theme[:max(1, ctrl_slots // 2)])

        if "令" in self.prof and "夕" in self.prof:
            if not any("令" in t and "夕" in t for t in themes):
                base = ["令", "夕"]
                fill = [nm for nm in all_ctrl if nm not in base][:ctrl_slots - 2]
                themes.append(base + fill)

        return themes if themes else [all_ctrl[:ctrl_slots]]

    def _perpetual_007_ops(self, template: Assignment) -> set[str]:
        """Operators that should work all shifts (007 perpetual).

        SPECIAL_TRADE_EFF operators (违约单, 投資) in production positions
        and 菲亚梅塔 available for mood swap support.
        """
        if "菲亚梅塔" not in self.prof:
            return set()
        prod_ops: set[str] = set()
        for _l, ops in template.manufacture:
            prod_ops |= set(ops)
        for ops in template.trading:
            prod_ops |= set(ops)
        return {nm for nm in SPECIAL_TRADE_EFF if nm in prod_ops}

    def _apply_control_theme(self, asg: Assignment, theme: list[str],
                             m: int, t: int, p: int,
                             locked: set[str] | None = None):
        """Replace control operators with a themed lineup."""
        new_ctrl = [nm for nm in theme
                    if nm in self.prof][:self._fixed_facility_slots("control")]
        old_ctrl = list(asg.control)
        if set(new_ctrl) == set(old_ctrl):
            return
        for nm in new_ctrl:
            if nm not in old_ctrl:
                self._remove_from_asg(asg, nm)
        displaced = [nm for nm in old_ctrl if nm not in new_ctrl]
        asg.control = list(new_ctrl)
        for nm in displaced:
            if nm in self._used_set(asg):
                continue
            prof = self.prof.get(nm)
            if not prof:
                continue
            ts = prof.stat("trading")
            eff_trade = ((ts.trade_eff if ts else 0.0)
                         + SPECIAL_TRADE_EFF.get(nm, 0.0)
                         + self._quality_trade_eff(nm))
            eff_mfg = self._effective_prod(nm, m, t, p)
            if eff_mfg > eff_trade and eff_mfg > 0:
                self._place_into(asg, nm, "manufacture", locked=locked)
            elif eff_trade > 0:
                self._place_into(asg, nm, "trading", locked=locked)
            elif prof.stat("dormitory"):
                self._place_into(asg, nm, "dormitory")

    def _setup_fiammetta(self, asg: Assignment, perpetual: set[str]):
        """Place Fiammetta in dormitory for 007 mood swap support."""
        if "菲亚梅塔" not in self.prof or not perpetual:
            return
        for rt, ri, si in self._all_positions(asg):
            if self._get_op(asg, rt, ri, si) == "菲亚梅塔":
                if rt == "dormitory":
                    return
                break
        else:
            return
        self._remove_from_asg(asg, "菲亚梅塔")
        if asg.dormitory:
            dorm_slots = self.cfg["dormitory"]["slots"]
            for d_ops in asg.dormitory:
                if len(d_ops) < dorm_slots:
                    d_ops.append("菲亚梅塔")
                    return

    def _joint_shifts(self, m: int, t: int, p: int, n_gaps: int,
                      max_work: int, local_search: bool,
                      user_seed: dict[str, str] | None = None) -> list[Assignment]:
        """Build shifts with independent lineups, then resolve overwork.

        1. Build each shift independently (best possible lineup per gap)
        2. Find operators working > max_work gaps
        3. Remove them from their lowest-contribution gap, fill with substitute
        """
        import sys, copy

        # Step 1: build each shift from a different seed (forces diverse lineups)
        seeds = [user_seed or {}] + self._synergy_seeds()
        gap_order = sorted(range(n_gaps), key=lambda i: self.sch.gaps[i], reverse=True)
        raw_shifts = [None] * n_gaps
        for priority, g_idx in enumerate(gap_order):
            seed = seeds[priority % len(seeds)] if not user_seed else user_seed
            if seed:
                asg = self._assign_seeded(m, t, p, seed)
            else:
                asg = self._assign(m, t, p)
            self._optimize_lines(asg)
            seed_locked = set(seed) if seed else None
            asg = self._optimize_assignment(asg, local_search, locked=seed_locked,
                                            label=f"joint-g{g_idx}")
            self._optimize_lines(asg)
            raw_shifts[g_idx] = asg

        # Step 2: count each operator's gap usage (exclude dorm — they're passive)
        def working_ops(asg):
            s = set(asg.control) | set(asg.meeting) | set(asg.hire)
            for _l, ops in asg.manufacture:
                s |= set(ops)
            for ops in asg.trading + asg.power:
                s |= set(ops)
            return s

        gap_count = {}
        for g_idx in range(n_gaps):
            for nm in working_ops(raw_shifts[g_idx]):
                gap_count.setdefault(nm, []).append(g_idx)

        # Step 3: for overworked operators, drop their lowest-contribution gap
        overworked = {nm: gaps for nm, gaps in gap_count.items() if len(gaps) > max_work}
        for nm, gaps in overworked.items():
            # Find gap where this operator contributes least
            marginals = []
            for g_idx in gaps:
                asg = raw_shifts[g_idx]
                base_ap = self.eng.evaluate(asg).ap_per_day
                test = copy.deepcopy(asg)
                self._remove_from_asg(test, nm)
                test_ap = self.eng.evaluate(test).ap_per_day
                marginals.append((base_ap - test_ap, g_idx))
            marginals.sort()
            # Drop from lowest-contribution gaps until ≤ max_work
            for _, drop_idx in marginals[:len(gaps) - max_work]:
                self._remove_from_asg(raw_shifts[drop_idx], nm)

        # Step 4: fill gaps left by removed operators
        for g_idx in range(n_gaps):
            asg = raw_shifts[g_idx]
            self._optimize_lines(asg)
            if local_search:
                asg = self._local_search(asg, iters=10)
                asg = self._synergy_pass(asg)
            raw_shifts[g_idx] = asg

        print(f"\n  joint: {len(overworked)} overworked resolved", file=sys.stderr)
        return raw_shifts

    def _apply_rotation(self, template: Assignment, resting_set: set[str],
                        m: int, t: int, p: int) -> Assignment:
        """复制模板排班, 将休息干员替换为最优替补。"""
        import copy
        asg = copy.deepcopy(template)
        if not resting_set:
            return asg
        resting = set(resting_set)
        used = self._used_set(asg)

        def pick(room: str, key_fn, *, category: str | None = None) -> str | None:
            best_nm, best_val = None, -1.0
            for cnm, cprof in self.prof.items():
                if cnm in used:
                    continue
                st = cprof.stat(room)
                if not st:
                    continue
                if category:
                    cat = self._prod_category(cnm)
                    if cat != category and cat != "all":
                        continue
                val = key_fn(cnm, st)
                if val > best_val:
                    best_val, best_nm = val, cnm
            return best_nm

        def fill(slot_list, room, key_fn, **kw):
            for i in range(len(slot_list)):
                if slot_list[i] in resting:
                    s = pick(room, key_fn, **kw)
                    if s:
                        slot_list[i] = s
                        used.add(s)

        # 先填候选池小的设施, 防止被大池(制造/贸易)消耗殆尽
        for _ri, ops in enumerate(asg.power):
            fill(ops, "power", lambda n, st: st.power)
        fill(asg.meeting, "meeting", lambda n, st: st.clue)
        fill(asg.hire, "hire", lambda n, st: st.contact)
        fill(asg.control, "control", lambda n, st: (
            max(st.prod_global.values(), default=0.0) * 3
            + st.trade_eff_global + st.global_recover * 5 + st.global_recover_elite * 3))

        for _ri, ops in enumerate(asg.trading):
            for si in range(len(ops)):
                if ops[si] in resting:
                    s = pick("trading", lambda n, st: (
                        st.trade_eff + SPECIAL_TRADE_EFF.get(n, 0.0) + self._quality_trade_eff(n)))
                    if s:
                        ops[si] = s
                        used.add(s)

        for _ri, (_line, ops) in enumerate(asg.manufacture):
            for si in range(len(ops)):
                if ops[si] in resting:
                    cat = self._prod_category(ops[si])
                    s = pick("manufacture",
                             lambda n, st: self._effective_prod(n, m, t, p), category=cat)
                    if not s:
                        s = pick("manufacture",
                                 lambda n, st: self._effective_prod(n, m, t, p))
                    if s:
                        ops[si] = s
                        used.add(s)

        # 未能替补的休息干员从排班中移除(simulate 会视其为 off-shift 自动休息)
        asg.control = [nm for nm in asg.control if nm not in resting]
        asg.manufacture = [(_l, [nm for nm in ops if nm not in resting])
                           for _l, ops in asg.manufacture]
        asg.trading = [[nm for nm in ops if nm not in resting] for ops in asg.trading]
        asg.power = [[nm for nm in ops if nm not in resting] for ops in asg.power]
        asg.meeting = [nm for nm in asg.meeting if nm not in resting]
        asg.hire = [nm for nm in asg.hire if nm not in resting]
        return asg

    def _optimize_assignment(self, asg: Assignment, local_search: bool,
                             locked: set[str] | None = None, label: str = "") -> Assignment:
        """Full optimization pipeline on a single assignment."""
        import sys, time
        if local_search:
            t0 = time.time()
            asg = self._local_search(asg, locked=locked)
            asg = self._synergy_pass(asg, locked=locked)
            asg = self._local_search(asg, locked=locked)
            t1 = time.time()
            asg = self._cross_facility_search(asg, locked=locked)
            t2 = time.time()
            asg = self._local_search(asg, locked=locked)
            t3 = time.time()
            print(f"\r    {label} local={t1-t0:.1f}s cross={t2-t1:.1f}s final={t3-t2:.1f}s  "
                  f"ap={self.eng.evaluate(asg).ap_per_day:.0f}    ", end="", file=sys.stderr)
        return asg

    def optimize(self, local_search: bool = True, progress=None,
                 layout: tuple[int, int, int] | None = None,
                 user_seed: dict[str, str] | None = None) -> tuple[Assignment, Result, tuple]:
        best = None
        auto_seeds = self._synergy_seeds()
        if user_seed:
            seeds = [user_seed]
        else:
            seeds = [{}] + auto_seeds
        layouts = [layout] if layout else self._enumerate_layouts()
        total = len(layouts) * len(seeds)
        step = 0
        for m, t, p in layouts:
            for seed in seeds:
                asg = self._assign_seeded(m, t, p, seed) if seed else self._assign(m, t, p)
                self._optimize_lines(asg)
                seed_locked = set(seed) if seed else None
                asg = self._optimize_assignment(asg, local_search, locked=seed_locked)
                res = self.eng.evaluate(asg)
                step += 1
                if progress:
                    progress(step, total, (m, t, p), res.ap_per_day)
                if best is None or res.ap_per_day > best[1].ap_per_day:
                    best = (asg, res, (m, t, p))
        return best

    def optimize_rotation(self, n_shifts: int, local_search: bool = True,
                          progress=None,
                          layout: tuple[int, int, int] | None = None,
                          user_seed: dict[str, str] | None = None) -> tuple[list[Assignment], list[Result], tuple]:
        """生成按上线间隔轮休的排班(每 gap 一组, 不同休息轮换)。

        工休比由宿舍恢复率自动决定 (243@Lv5 → 3:1, 252@Lv1 → 1:1)。
        算法:
          1. 建最优满员模板(local_search + synergy)
          2. 识别 007 永驻干员(排除出休息组)
          3. 生成多组控制中枢主题(不同全局加成)
          4. 按价值均衡分休息组(007 干员除外)
          5. 逐 gap 用替补填入休息空位, 并切换控制中枢主题
          6. 各 gap 独立局部搜索 + 生产线优化
        返回 n_gaps 个 Assignment (每个上线间隔一组)。
        """
        if n_shifts <= 1:
            asg, res, layout = self.optimize(local_search=local_search, progress=progress,
                                             layout=layout, user_seed=user_seed)
            return [asg], [res], layout

        import sys
        n_gaps = len(self.sch.gaps)
        max_work = self._max_work_gaps()
        layouts = [layout] if layout else self._enumerate_layouts()
        best_overall = None
        total = len(layouts)
        step = 0

        for m, t, p in layouts:
            themes = self._control_themes()
            gap_order = sorted(range(n_gaps),
                               key=lambda i: self.sch.gaps[i], reverse=True)

            synergy_seeds = self._synergy_seeds()

            # Build order: longest first, then shortest, then middle.
            # Operators from the longest shift get paired with the shortest
            # (16h work + 8h rest is sustainable; 21h work + 3h rest is not).
            build_order = [gap_order[0]]
            if len(gap_order) > 2:
                build_order.append(gap_order[-1])
                build_order.extend(gap_order[1:-1])
            else:
                build_order.extend(gap_order[1:])

            usage: dict[str, int] = {}
            shifts_built = [None] * n_gaps
            for priority, g_idx in enumerate(build_order):
                maxed = {nm for nm, cnt in usage.items() if cnt >= max_work}
                seed = synergy_seeds[priority] if priority < len(synergy_seeds) else {}
                if user_seed:
                    seed = dict(user_seed)
                seed = {nm: r for nm, r in seed.items() if nm not in maxed}

                if seed:
                    gap_asg = self._assign_seeded(m, t, p, seed)
                else:
                    gap_asg = self._assign(m, t, p, exclude=maxed)
                self._optimize_lines(gap_asg)
                seed_locked = set(seed) if seed else None
                gap_asg = self._optimize_assignment(
                    gap_asg, local_search, locked=seed_locked,
                    label=f"shift-g{g_idx}")
                self._optimize_lines(gap_asg)

                shifts_built[g_idx] = gap_asg
                working = set(gap_asg.control) | set(gap_asg.meeting) | set(gap_asg.hire)
                for _l, ops in gap_asg.manufacture:
                    working |= set(ops)
                for ops in gap_asg.trading + gap_asg.power:
                    working |= set(ops)
                for nm in working:
                    usage[nm] = usage.get(nm, 0) + 1

            shifts = shifts_built

            results = [self.eng.evaluate(a) for a in shifts]
            weighted = sum(r.ap_per_day * self.sch.gaps[i] / 24.0
                           for i, r in enumerate(results))

            step += 1
            if progress:
                progress(step, total, (m, t, p), weighted)
            if best_overall is None or weighted > best_overall[0]:
                best_overall = (weighted, shifts, results, (m, t, p))

        _, shifts, results, layout = best_overall
        return shifts, results, layout
