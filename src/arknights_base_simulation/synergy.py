"""动态联动引擎: 上下文(布局/派系计数/抱团资源池) + 条件/派系/资源效果解析。

两遍计算:
  1) 由 Assignment 建 BaseContext —— 干员位置、各派系基建/房间人数、各抱团资源池存量。
  2) 解析每个干员技能时, 用上下文求条件加成的真实值:
       - 配对/在场条件: "当与X在同一个设施"/"X在基建内" -> 满足才计(否则门控为0, 避免高估)
       - 派系计数: "每名/每有1名 X派系 干员 ... +y%" -> y% × 实际派系人数(受上限)
       - 资源消耗: "每N点 资源 +M% ..." -> (资源量/N)×M%

资源池公式逐条对照 PRTS/游戏内描述硬编码(见 RESOURCE_NOTES)。涉及干员约百名, 其余走静态值。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

# 怪物猎人小队/泡影国狩猎小队/杜林族/作业平台/职业分支/异格干员不在官方 master 镜像里,
# 手动补充 PRTS 后勤条目列出的成员。
MONSTER_HUNTER = {"火龙S黑角", "麒麟R夜刀", "泰拉大陆调查团"}
BUBBLE_HUNTERS = {"焰狐龙梓兰", "雷狼龙S空爆", "罗德岛隐秘队"}
DURIN = {"至简", "桃金娘", "褐果", "杜林", "特克诺"}
WORK_PLATFORMS = {"Lancet-2", "Castle-3", "THRM-EX", "正义骑士号", "Friston-3", "PhonoR-0", "CONFESS-47", "GALLUS²"}
WANDERING_MEDICS = {"蜜莓", "桑葚", "褐果", "哈洛德", "纯烬艾雅法拉"}
LEIOS_TEAM = {"玛露西尔", "莱欧斯", "齐尔查克", "森西"}
KAZIMIERZ_KNIGHTS = {"耀骑士临光", "临光", "瑕光", "鞭刃", "焰尾", "远牙", "灰毫", "野鬃", "正义骑士号", "砾", "薇薇安娜"}
RAINBOW_ATTACKERS = {"灰烬", "闪击", "双月", "导火索"}
RAINBOW_DEFENDERS = {"战车", "霜华", "艾拉", "医生"}
ALTER_OPERATORS = {
    "炎狱炎熔", "寒芒克洛丝", "濯尘芙蓉", "假日威龙陈", "耀骑士临光", "归溟幽灵鲨",
    "百炼嘉维尔", "缄默德克萨斯", "纯烬艾雅法拉", "琳琅诗怀雅", "淬羽赫默", "圣约送葬人",
    "涤火杰西卡", "承曦格雷伊", "历阵锐枪芬", "新约能天使", "荒芜拉普兰德", "赤刃明霄陈",
    "怒潮凛冬", "凛御银灰", "圣聆初雪", "撷英调香师", "溯光星源", "凯尔希·思衡托",
    "雷狼龙S空爆", "火龙S黑角", "麒麟R夜刀",
}
FACTION_EXTRA = {nm: ["怪物猎人小队"] for nm in MONSTER_HUNTER}
for _nm in BUBBLE_HUNTERS:
    FACTION_EXTRA.setdefault(_nm, []).append("泡影国狩猎小队")
for _nm in DURIN:
    FACTION_EXTRA.setdefault(_nm, []).append("杜林族")
for _nm in WORK_PLATFORMS:
    FACTION_EXTRA.setdefault(_nm, []).append("作业平台")
for _nm in WANDERING_MEDICS:
    FACTION_EXTRA.setdefault(_nm, []).append("行医")
for _nm in LEIOS_TEAM:
    FACTION_EXTRA.setdefault(_nm, []).append("莱欧斯小队")
for _nm in KAZIMIERZ_KNIGHTS:
    FACTION_EXTRA.setdefault(_nm, []).append("骑士")
for _nm in RAINBOW_ATTACKERS:
    FACTION_EXTRA.setdefault(_nm, []).append("进攻方")
for _nm in RAINBOW_DEFENDERS:
    FACTION_EXTRA.setdefault(_nm, []).append("防守方")
for _nm in ALTER_OPERATORS:
    FACTION_EXTRA.setdefault(_nm, []).append("异格干员")

# 资源池里"每级宿舍"未显式配置时默认取满级 5; "招募位(非初始)"默认办公室满级 4 位 -> 3 个非初始
DORM_LEVEL = 5
OFFICE_RECRUIT_SLOTS_NONINIT = 3
# PRTS 罗德岛基建: 全部清理后无人机持有上限为 235 架; 配置可覆盖用于非满清理/测试。
DRONE_CAP = 235


def _load_factions() -> dict[str, list[str]]:
    p = DATA_DIR / "factions.json"
    out: dict[str, list[str]] = {}
    if p.exists():
        raw = json.loads(p.read_text(encoding="utf-8"))
        for name, info in raw.items():
            out[name] = list(info.get("factions", []))
    for name, fs in FACTION_EXTRA.items():
        out.setdefault(name, [])
        for f in fs:
            if f not in out[name]:
                out[name].append(f)
    return out


FACTIONS = _load_factions()


def factions_of(name: str) -> list[str]:
    return FACTIONS.get(name, [])


# ----------------------------------------------------------------- 上下文
@dataclass
class BaseContext:
    """一次排班的全局上下文。"""
    # operator -> (room_type, room_index)
    placement: dict[str, tuple[str, int]] = field(default_factory=dict)
    present: set[str] = field(default_factory=set)
    # room_type -> {room_index -> [operator,...]}
    rooms: dict[str, dict[int, list[str]]] = field(default_factory=dict)
    manufacture_lines: dict[int, str] = field(default_factory=dict)
    dorm_headcount: int = 0          # 宿舍总人数(部分资源按宿舍人数)
    mood: dict[str, float] | None = None
    active_frac: dict[str, float] = field(default_factory=dict)
    active_frac_default: float = 1.0
    fatigued: set[str] = field(default_factory=set)
    active_buffs: dict = field(default_factory=dict)
    recruit_slots_noninitial: int = OFFICE_RECRUIT_SLOTS_NONINIT
    dorm_levels: dict[int, int] = field(default_factory=dict)
    facility_levels: dict[str, int] = field(default_factory=dict)
    drone_cap: float = DRONE_CAP
    pools: dict[str, float] = field(default_factory=dict)

    def room_members(self, room: str, idx: int) -> list[str]:
        return self.rooms.get(room, {}).get(idx, [])

    def faction_count_base(self, faction: str, exclude: str | None = None) -> int:
        return sum(1 for nm in self.present
                   if nm != exclude and faction in factions_of(nm))

    def faction_count_room(self, faction: str, room: str, idx: int, exclude: str | None = None) -> int:
        return sum(1 for nm in self.room_members(room, idx)
                   if nm != exclude and faction in factions_of(nm))

    def faction_count_in_rooms(self, faction: str, room: str) -> int:
        """某派系在某*类*设施(所有该类房间)里的人数。"""
        n = 0
        for ops in self.rooms.get(room, {}).values():
            n += sum(1 for nm in ops if faction in factions_of(nm))
        return n

    def active_faction_count_base(self, faction: str, exclude: str | None = None) -> int:
        return sum(1 for nm in self.present
                   if nm != exclude and self.active(nm) and faction in factions_of(nm))

    def active_faction_count_room(self, faction: str, room: str, idx: int, exclude: str | None = None) -> int:
        return sum(1 for nm in self.room_members(room, idx)
                   if nm != exclude and self.active(nm) and faction in factions_of(nm))

    def active_faction_count_in_rooms(self, faction: str, room: str) -> int:
        """某派系在某类设施里仍处于工作状态的人数。"""
        n = 0
        for ops in self.rooms.get(room, {}).values():
            n += sum(1 for nm in ops if self.active(nm) and faction in factions_of(nm))
        return n

    def frac(self, name: str) -> float:
        return self.active_frac.get(name, self.active_frac_default)

    def active(self, name: str) -> bool:
        if self.mood is not None and self.mood.get(name, 24.0) <= 1e-9:
            return False
        return self.frac(name) > 1e-9

    def dorm_level(self, idx: int) -> int:
        return int(self.dorm_levels.get(idx, DORM_LEVEL))

    def total_dorm_levels(self) -> int:
        return sum(self.dorm_level(idx) for idx in self.rooms.get("dormitory", {}))

    def facility_level(self, room: str) -> int:
        if room == "dormitory":
            return DORM_LEVEL
        return int(self.facility_levels.get(room, 3))

    def faction_weight_room(self, faction: str, room: str, idx: int, exclude: str | None = None) -> float:
        return sum(1 for nm in self.room_members(room, idx)
                   if nm != exclude and faction in factions_of(nm))

    def active_faction_weight_room(self, faction: str, room: str, idx: int, exclude: str | None = None) -> float:
        return sum(self.frac(nm) for nm in self.room_members(room, idx)
                   if nm != exclude and faction in factions_of(nm))


def build_context(asg, pool_fn=None, mood: dict[str, float] | None = None,
                  active_frac: dict[str, float] | None = None,
                  active_frac_default: float = 1.0,
                  fatigued: set[str] | None = None, active_buffs: dict | None = None,
                  recruit_slots_noninitial: int = OFFICE_RECRUIT_SLOTS_NONINIT,
                  dorm_levels: dict[int, int] | None = None,
                  facility_levels: dict[str, int] | None = None,
                  drone_cap: float = DRONE_CAP) -> BaseContext:
    """由 Assignment 建上下文。asg 见 engine.Assignment。"""
    ctx = BaseContext()
    ctx.mood = mood
    ctx.active_frac = active_frac or {}
    ctx.active_frac_default = float(active_frac_default)
    ctx.fatigued = fatigued or set()
    ctx.active_buffs = active_buffs or {}
    ctx.recruit_slots_noninitial = max(0, int(recruit_slots_noninitial))
    ctx.dorm_levels = {int(k): int(v) for k, v in (dorm_levels or {}).items()}
    ctx.facility_levels = {str(k): int(v) for k, v in (facility_levels or {}).items()}
    ctx.drone_cap = float(drone_cap)

    def place(room, idx, ops):
        ctx.rooms.setdefault(room, {})[idx] = list(ops)
        for nm in ops:
            ctx.placement[nm] = (room, idx)
            ctx.present.add(nm)

    for i, (line, ops) in enumerate(asg.manufacture):
        ctx.manufacture_lines[i] = line
        place("manufacture", i, ops)
    for i, ops in enumerate(asg.trading):
        place("trading", i, ops)
    for i, ops in enumerate(asg.power):
        place("power", i, ops)
    place("control", 0, asg.control)
    place("meeting", 0, asg.meeting)
    place("hire", 0, asg.hire)
    place("workshop", 0, asg.workshop)
    place("training", 0, asg.training)
    for i, ops in enumerate(asg.dormitory):
        place("dormitory", i, ops)
    ctx.dorm_headcount = sum(len(r) for r in asg.dormitory)

    ctx.pools = (pool_fn or compute_pools)(ctx)
    return ctx


# ----------------------------------------------------------------- 抱团资源池
# 各池由"产出干员"贡献; 部分池为转化链(感知信息 -> 思维链环/无声共鸣; 人间烟火 -> 巫术结晶)。
# 公式硬编码自描述; 心情阈值类(夕/令)由 ctx.mood 选择分支, 无心情态时默认高心情分支。
RESOURCE_NOTES = (
    "梦境/记忆碎片/小节/人间烟火/感知信息/无声共鸣/思维链环/工程机器人/"
    "热情值/魔物料理/木天蓼/乌萨斯特饮/情报储备/巫术结晶"
)


def _in(ctx, name, room=None):
    if name not in ctx.present:
        return False
    if room and ctx.placement.get(name, ("",))[0] != room:
        return False
    return True


def _mood_gt(ctx: BaseContext, name: str, threshold: float) -> bool:
    """心情阈值技能: 无时间轴心情时按高心情稳态分支处理。"""
    if ctx.mood is None or name not in ctx.mood:
        return True
    return ctx.mood[name] > threshold


def _completed_window(ctx: BaseContext, name: str) -> bool:
    """当前结算窗口结束时未触发涣散清空。

    瞬态模拟显式传入 fatigued; 对絮雨这类"心情耗尽时清空累积资源"的技能, 只有真正降到
    0心情的窗口才清空。稳态评估无 fatigued 时视为未清空。
    """
    return name not in ctx.fatigued


def compute_pools(ctx: BaseContext) -> dict[str, float]:
    p: dict[str, float] = {}

    def add(pool, amt):
        p[pool] = p.get(pool, 0.0) + amt

    def station_count(room: str) -> int:
        return len(ctx.rooms.get(room, {}))

    # ---- 热情值 (MyGO/邦多利, 控制中枢) ----
    if _in(ctx, "八幡海铃", "control") and ctx.active("八幡海铃"): add("热情值", 10)
    if _in(ctx, "祐天寺若麦", "control") and ctx.active("祐天寺若麦"): add("热情值", 10)
    if _in(ctx, "若叶睦", "control") and ctx.active("若叶睦"): add("热情值", 20)
    if _in(ctx, "三角初华", "control") and ctx.active("三角初华"): add("热情值", ctx.dorm_headcount)  # 宿舍每1人+1

    # ---- 木天蓼 (怪物猎人, 控制中枢) ----
    if _in(ctx, "麒麟R夜刀", "control") and ctx.active("麒麟R夜刀"): add("木天蓼", 8)
    if _in(ctx, "火龙S黑角", "control") and ctx.active("火龙S黑角"):
        add("木天蓼", 2 * ctx.active_faction_weight_room("怪物猎人小队", "control", 0))

    # ---- 乌萨斯特饮 / 情报储备 (彩虹小队, 控制中枢) ----
    if _in(ctx, "战车", "control") and ctx.active("战车"):
        add("乌萨斯特饮", ctx.active_faction_weight_room("乌萨斯学生自治团", "control", 0))
    if _in(ctx, "灰烬", "control") and ctx.active("灰烬"):
        add("情报储备", ctx.active_faction_weight_room("彩虹小队", "control", 0))

    # ---- 人间烟火 (岁) ----
    if _in(ctx, "重岳", "control"):
        sui_nondorm = sum(
            ctx.frac(nm)
            for nm in ctx.present
            if "炎-岁" in factions_of(nm) and ctx.placement[nm][0] != "dormitory"
        )
        if ctx.active("重岳"):
            add("人间烟火", 5 * min(5, sui_nondorm))
    if _in(ctx, "夕", "control") and ctx.active("夕") and not _mood_gt(ctx, "夕", 12): add("人间烟火", 15)
    if _in(ctx, "令", "control") and ctx.active("令") and _mood_gt(ctx, "令", 12): add("人间烟火", 15)
    if _in(ctx, "桑葚", "hire") and ctx.active("桑葚"): add("人间烟火", 10 * ctx.recruit_slots_noninitial)
    if _in(ctx, "乌有", "trading") and ctx.active("乌有"): add("人间烟火", ctx.dorm_headcount)

    # ---- 魔物料理 (莱欧斯小队) ----
    if _in(ctx, "森西", "dormitory") and ctx.active("森西"):
        add("魔物料理", ctx.dorm_level(ctx.placement["森西"][1]))  # 宿舍每级+1层

    # ---- 感知信息 (转化链上游) ----
    if _in(ctx, "爱丽丝", "dormitory") and ctx.active("爱丽丝"):
        add("梦境", ctx.dorm_level(ctx.placement["爱丽丝"][1]))        # 当前宿舍每级+1层
    if _in(ctx, "车尔尼", "dormitory") and ctx.active("车尔尼"):
        add("小节", ctx.dorm_level(ctx.placement["车尔尼"][1]))
    if _in(ctx, "絮雨", "hire") and ctx.active("絮雨") and _completed_window(ctx, "絮雨"):
        add("记忆碎片", 10 * ctx.recruit_slots_noninitial)
    if _in(ctx, "迷迭香", "manufacture") and ctx.active("迷迭香"): add("感知信息", ctx.dorm_headcount)
    if _in(ctx, "黑键", "trading") and ctx.active("黑键"): add("感知信息", ctx.dorm_headcount)
    if _in(ctx, "爱丽丝", "dormitory") and ctx.active("爱丽丝"): add("感知信息", p.get("梦境", 0.0))
    if _in(ctx, "车尔尼", "dormitory") and ctx.active("车尔尼"): add("感知信息", p.get("小节", 0.0))
    if _in(ctx, "絮雨", "hire") and ctx.active("絮雨") and _completed_window(ctx, "絮雨"):
        add("感知信息", p.get("记忆碎片", 0.0))
    if _in(ctx, "夕", "control") and ctx.active("夕") and _mood_gt(ctx, "夕", 12): add("感知信息", 10)
    if _in(ctx, "令", "control") and ctx.active("令") and not _mood_gt(ctx, "令", 12): add("感知信息", 10)

    # ---- 无声共鸣 ----
    if _in(ctx, "塑心", "dormitory"):
        _idx = ctx.placement["塑心"][1]
        if ctx.active("塑心"):
            add("无声共鸣", len(ctx.room_members("dormitory", _idx)))  # 仅塑心所在那一间宿舍的人数
    if _in(ctx, "深律", "hire") and ctx.active("深律"): add("无声共鸣", 15 * ctx.recruit_slots_noninitial)
    if _in(ctx, "黑键", "trading") and ctx.active("黑键"): add("无声共鸣", p.get("感知信息", 0.0))

    # ---- 思维链环 (迷迭香: 感知信息 -> 思维链环, + 宿舍人数) ----
    if _in(ctx, "迷迭香", "manufacture") and ctx.active("迷迭香"):
        add("思维链环", p.get("感知信息", 0.0))

    # ---- 巫术结晶 (截云: 人间烟火/5) ----
    if _in(ctx, "截云", "manufacture") and ctx.active("截云"):
        add("巫术结晶", p.get("人间烟火", 0.0) / 5.0)

    # ---- 工程机器人 (至简) ----
    if _in(ctx, "至简", "manufacture"):
        robots = ctx.total_dorm_levels()
        for room in ("manufacture", "trading", "power", "control", "meeting", "hire", "workshop", "training"):
            robots += station_count(room) * ctx.facility_level(room)
        if ctx.active("至简"):
            add("工程机器人", min(64, robots))

    return p


# ----------------------------------------------------------------- 条件/计数/资源 解析
# 派系关键词 -> factions.json 里的标准名
FACTION_ALIASES = {
    "岁": "炎-岁", "深海猎人": "深海猎人", "莱茵生命": "莱茵生命", "红松骑士团": "红松骑士团",
    "格拉斯哥帮": "格拉斯哥帮", "乌萨斯学生自治团": "乌萨斯学生自治团", "黑钢国际": "黑钢国际",
    "谢拉格": "谢拉格", "萨米": "萨米", "叙拉古": "叙拉古", "拉特兰": "拉特兰", "米诺斯": "米诺斯",
    "莱茵": "莱茵生命", "骑士": "骑士", "怪物猎人小队": "怪物猎人小队", "彩虹小队": "彩虹小队",
    "泡影国狩猎小队": "泡影国狩猎小队",
    "深海猎人干员": "深海猎人",
    "龙门近卫局": "龙门近卫局", "萨尔贡": "萨尔贡", "杜林族": "杜林族",
    "鲤氏侦探事务所": "鲤氏侦探事务所",
    "作业平台": "作业平台",
    "行医": "行医",
    "莱欧斯小队": "莱欧斯小队",
    "异格干员": "异格干员", "异格": "异格干员",
    "进攻方": "进攻方", "防守方": "防守方",
    "精英干员": "罗德岛-精英干员", "精英": "罗德岛-精英干员",
    "A1小队": "行动预备组A1",
}

_NUM = r"([0-9]+(?:\.[0-9]+)?)"
RES_NAMES = ["人间烟火", "思维链环", "感知信息", "无声共鸣", "热情值", "魔物料理",
             "木天蓼", "乌萨斯特饮", "情报储备", "巫术结晶", "工程机器人"]

MLYNAR_OTHER_RECOVER_BUFFS = {
    "左膀右臂", "S.W.E.E.P.", "零食网络", "清理协议", "替身", "必要责任", "护卫",
    "小小的领袖", "独善其身", "笑靥如春", "金盏花诗会", "捍卫之道", "博识生手",
    "点滴关照", "总工程师",
}


def dorm_target_extra_recover_for(buff, target: str, room_ops: list[str]) -> float:
    """解析宿舍单体恢复对指定目标的条件额外值。"""
    d = buff.desc
    if (
        buff.room == "dormitory"
        and "推进之王对该宿舍中格拉斯哥帮干员恢复效果额外" in d
        and "推进之王" in room_ops
        and target != buff.name
        and "格拉斯哥帮" in factions_of(target)
    ):
        m = re.search(rf"恢复效果额外\+{_NUM}", d)
        return float(m.group(1)) if m else 0.0
    if buff.room != "dormitory" or "如果目标是" not in d or "恢复效果额外" not in d:
        return 0.0
    m = re.search(rf"如果目标是(.+?)，则恢复效果额外\+{_NUM}", d)
    if not m:
        return 0.0
    target_desc = m.group(1)
    extra = float(m.group(2))

    def target_ok(nm: str) -> bool:
        if nm == buff.name:
            return False
        if "怪物猎人小队" in target_desc and "怪物猎人小队" in factions_of(nm):
            return True
        if "泡影国狩猎小队" in target_desc and "泡影国狩猎小队" in factions_of(nm):
            return True
        for kw, faction in FACTION_ALIASES.items():
            if kw in target_desc and faction in factions_of(nm):
                return True
        for known_name in FACTIONS:
            if known_name in target_desc and nm == known_name:
                return True
        return False

    return extra if target_ok(target) else 0.0


def dorm_target_extra_recover(buff, room_ops: list[str]) -> float:
    """解析宿舍单体恢复的目标条件额外值。同宿舍存在合规目标时才计入。"""
    return max((dorm_target_extra_recover_for(buff, nm, room_ops) for nm in room_ops), default=0.0)


def dorm_low_mood_extra_recover_for(buff, target: str, mood: dict[str, float]) -> float:
    """解析“该宿舍内心情N以下的干员恢复效果额外+X”的目标额外值。"""
    if buff.room != "dormitory" or "心情" not in buff.desc or "恢复效果额外" not in buff.desc:
        return 0.0
    m = re.search(rf"该宿舍内心情([0-9]+(?:\.[0-9]+)?)以下的干员.*?恢复效果额外\+{_NUM}", buff.desc)
    if not m:
        return 0.0
    return float(m.group(2)) if mood.get(target, 24.0) <= float(m.group(1)) else 0.0


def dorm_low_mood_extra_recover_triggered(buff, room_ops: list[str], mood: dict[str, float]) -> float:
    """返回本房间已被低心情目标触发的阈值额外恢复, 用于从全员基础值中拆出目标项。"""
    return max((dorm_low_mood_extra_recover_for(buff, nm, mood) for nm in room_ops), default=0.0)


def dorm_all_recover_for(buff, target: str) -> float:
    """解析宿舍全员恢复对指定目标是否生效。"""
    if buff.room != "dormitory":
        return 0.0
    best = 0.0
    for m in re.finditer(rf"恢复\+{_NUM}", buff.desc):
        seg_start = max((buff.desc.rfind(p, 0, m.start()) for p in "，。；、"), default=-1) + 1
        clause = buff.desc[seg_start:m.end()]
        if ("所有干员" not in clause) and ("全体" not in clause):
            continue
        if "除自身以外" in clause and target == buff.name:
            continue
        best = max(best, float(m.group(1)))
    return best


def dorm_single_recover_for(buff, target: str) -> float:
    """解析宿舍单体恢复基础值对指定目标是否生效。"""
    if buff.room != "dormitory":
        return 0.0
    best = 0.0
    for m in re.finditer(rf"恢复\+{_NUM}", buff.desc):
        seg_start = max((buff.desc.rfind(p, 0, m.start()) for p in "，。；、"), default=-1) + 1
        clause = buff.desc[seg_start:m.end()]
        if ("所有干员" in clause) or ("全体" in clause):
            continue
        if not any(k in clause for k in ("某个", "一名", "除自身以外", "前一位", "其他干员")):
            continue
        excludes_self = any(k in clause for k in ("除自身以外", "前一位", "其他干员"))
        if excludes_self and target == buff.name:
            continue
        best = max(best, float(m.group(1)))
    return best


def dorm_average_recover_pool(buff) -> float:
    """解析平均分配给心情未满宿舍成员的总恢复池, 如 冰酿·小酌怡情。"""
    if buff.room != "dormitory" or "平均分配" not in buff.desc or "总计每小时心情恢复" not in buff.desc:
        return 0.0
    m = re.search(rf"总计每小时心情恢复\+{_NUM}", buff.desc)
    return float(m.group(1)) if m else 0.0


def _resource_units(ctx: BaseContext, res: str, n: float) -> int:
    """PRTS "每N点/瓶/个"按完整份数触发。"""
    return int(ctx.pools.get(res, 0.0) // max(1.0, n))


def has_unconditional_effect(desc: str) -> bool:
    """条件词(当/如果/若)之前是否已有无条件数值效果(+x% 或 +x)。
    有 -> 该技能含无条件基底, 条件不满足时基底仍生效, 不应整条门控。"""
    m = re.search(r"(当(?!前)|如果|若)", desc)  # "当前"不是条件词
    head = desc[: m.start()] if m else desc
    return bool(re.search(r"\+[0-9]", head))


def pair_condition_met(buff, ctx: BaseContext, self_name: str) -> bool | None:
    """配对/在场条件判定。返回 None 表示该 buff 无此类条件(正常计)。"""
    d = buff.desc
    room_map = {"控制中枢": "control", "会客室": "meeting", "贸易站": "trading",
                "制造站": "manufacture", "办公室": "hire", "人力办公室": "hire",
                "宿舍": "dormitory", "发电站": "power", "训练室": "training",
                "加工站": "workshop"}
    # "如果会客室内只有自身处于工作状态时": 同一具体房间内除自身外没有有效工作的干员。
    if "只有自身处于工作状态" in d:
        room, idx = ctx.placement.get(self_name, (buff.room, 0))
        return all(nm == self_name or not ctx.active(nm) for nm in ctx.room_members(room, idx))
    # "当与X派系干员进驻控制中枢一起工作时"。X 是派系名, 需同一具体房间有除自身外的该派系干员。
    m = re.search(r"当与([^\s，。]{1,12}?)(?:干员)?进驻[^\s，。]*一起工作", d)
    if m:
        fac_kw = m.group(1)
        other = fac_kw.strip("”“\"")
        if other in FACTIONS or other in ctx.present:
            return (
                other in ctx.present
                and ctx.active(other)
                and ctx.placement.get(other) == ctx.placement.get(self_name)
            )
        faction = None
        for kw, std in FACTION_ALIASES.items():
            if kw in fac_kw:
                faction = std
                break
        if faction:
            room, idx = ctx.placement.get(self_name, (buff.room, 0))
            return any(
                nm != self_name and ctx.active(nm) and faction in factions_of(nm)
                for nm in ctx.room_members(room, idx)
            )
    # "当与X在同一个{设施}时" / "当与X一起进驻控制中枢"
    m = re.search(r"当与([^\s，。]{1,8}?)(?:在同一个|一起进驻)", d)
    if m:
        other = m.group(1).strip("”“\"")
        if other in ctx.present:
            r_self = ctx.placement.get(self_name)
            r_other = ctx.placement.get(other)
            if r_self and r_other:
                return ctx.active(other) and r_self == r_other  # 同一具体房间
        return False
    # "当{X}在基建内时" / "如果{X}进驻在{设施}" / "若{X}在{设施}"
    m = re.search(r"(?:当|如果|若)([^\s，。]{1,8}?)(?:在基建内|进驻在?([^\s，。；]*?)|在([^\s，。；]*?))(?:时|，|,|则|$)", d)
    if m:
        other = m.group(1).strip("”“\"")
        room_hint = (m.group(2) or m.group(3) or "")
        hinted_room = None
        for cn, rn in room_map.items():
            if cn in room_hint:
                hinted_room = rn
                break
        # 仅当 other 是已知干员名才作为条件(否则可能是普通描述)
        if other in FACTIONS or other in ctx.present or hinted_room is not None:
            ok = other in ctx.present and ctx.active(other)
            if hinted_room is not None:
                ok = ok and ctx.placement.get(other, ("",))[0] == hinted_room
            return ok
    return None


def faction_count_scale(buff, ctx: BaseContext, self_name: str, room: str, idx: int):
    """解析"每(有1)名 X派系 干员 ... +M%"。返回 (amount, kind_override) 或 None。
    kind_override 非 None 时强制效果种类(如控制中枢"同一贸易站"=trade_eff_global)。"""
    d = buff.desc
    m = re.search(rf"每(?:有)?(?:1|一)?[名个]([^\s，。]{{2,10}}?)干员.*?\+{_NUM}%", d)
    if not m:
        return None
    fac_kw, per = m.group(1), float(m.group(2))
    faction = None
    for kw, std in FACTION_ALIASES.items():
        if kw in fac_kw:
            faction = std
            break
    if faction is None:
        return None
    cap_m = re.search(r"最多(?:生效)?([0-9]+)\s*[名个]", d)
    cap = int(cap_m.group(1)) if cap_m else 999

    # 跨房间 scope: 控制中枢驻员的技能按目标设施里的派系人数计。
    if "进驻在会客室" in d and room == "control":
        cnt = ctx.active_faction_count_in_rooms(faction, "meeting")
        return (per * min(cap, cnt), "clue_global")
    if "贸易站" in d and room == "control":
        cnt = ctx.active_faction_count_in_rooms(faction, "trading")
        return (per * min(cap, cnt), "trade_eff_global")
    if "制造站" in d and room == "control":
        cnt = ctx.active_faction_count_in_rooms(faction, "manufacture")
        return (per * min(cap, cnt), "prod_global")
    if faction == "泡影国狩猎小队" and room == "trading" and "进驻贸易站" in d:
        cnt = ctx.active_faction_count_room(faction, room, idx)
        return (per * min(cap, cnt), None)
    if "基建内" in d and "除自身以外" not in d:
        cnt = ctx.active_faction_count_base(faction)
    elif room == "training" and "基建内" in d:
        cnt = ctx.active_faction_count_base(faction, exclude=self_name)
    # 本房间 scope: 含"当前/同个/同一/本/该设施"
    elif any(k in d for k in ["当前", "同个", "同一", "本设施", "该设施"]):
        cnt = ctx.active_faction_count_room(faction, room, idx, exclude=self_name)
    else:
        cnt = ctx.active_faction_count_base(faction, exclude=self_name)
    return (per * min(cap, cnt), None)


def headcount_scale(buff, ctx: BaseContext, self_name: str, room: str, idx: int):
    """解析非派系的人数缩放:"(本设施内)除自身以外每名(处于工作状态的/在场)干员 +M%"。
    返回 amount(百分比) 或 None。仅当限定词不是派系时生效(派系交给 faction_count_scale)。"""
    d = buff.desc
    m = re.search(rf"每[名个]([^\s，。]{{0,12}}?)干员[^+]*?\+{_NUM}%", d)
    if not m:
        return None
    qual = m.group(1)
    if any(kw in qual for kw in FACTION_ALIASES):
        return None   # 派系限定 -> 由 faction_count_scale 处理
    per = float(m.group(2))
    require_active = "处于工作状态" in d
    if "基建内" in d:
        cnt = sum(
            1 for nm in ctx.present
            if nm != self_name and (not require_active or ctx.active(nm))
        )
    else:
        cnt = sum(
            1 for nm in ctx.room_members(room, idx)
            if nm != self_name and (not require_active or ctx.active(nm))
        )
    cap_m = re.search(r"最多(?:生效)?([0-9]+)\s*[名个]", d)
    if cap_m:
        cnt = min(cnt, int(cap_m.group(1)))
    return per * cnt


def manufacture_facility_count_scale(buff, ctx: BaseContext, room: str, idx: int):
    """解析制造站特殊数量缩放: 当前站人数/发电站/贸易站数量 -> 当前制造站生产力。"""
    d = buff.desc
    if buff.room != "manufacture" or "生产力" not in d:
        return None
    m = re.search(rf"每个当前制造站内干员为当前制造站\+{_NUM}%生产力", d)
    if m:
        return len(ctx.room_members(room, idx)) * float(m.group(1))
    m = re.search(rf"每个发电站为当前制造站\+{_NUM}%的?生产力", d)
    if m:
        return effective_power_station_count(ctx) * float(m.group(1))
    m = re.search(rf"每个贸易站为当前制造站.*?\+{_NUM}%", d)
    if m:
        return len(ctx.rooms.get("trading", {})) * float(m.group(1))
    return None


def power_platform_count_scale(buff, ctx: BaseContext):
    """解析发电站内作业平台数量驱动的制造站生产力。"""
    d = buff.desc
    if buff.room != "manufacture" or "作业平台进驻发电站" not in d or "生产力" not in d:
        return None
    m = re.search(rf"每有1台作业平台进驻发电站.*?\+{_NUM}%", d)
    if not m:
        return None
    count = sum(
        1
        for nm in ctx.present
        if ctx.active(nm)
        and "作业平台" in factions_of(nm)
        and ctx.placement.get(nm, ("",))[0] == "power"
    )
    return count * float(m.group(1))


def _manufacture_skill_class_count(ctx: BaseContext, room: str, idx: int, class_name: str) -> int:
    members = ctx.room_members(room, idx)
    standardize_extra = (
        class_name == "标准化"
        and any(
            nm == "海沫"
            and ctx.active(nm)
            and any(b.buff_name == "意识兼容" for b in ctx.active_buffs.get(nm, {}).get("manufacture", []))
            for nm in members
        )
    )
    count = 0
    for nm in members:
        if not ctx.active(nm):
            continue
        for b in ctx.active_buffs.get(nm, {}).get("manufacture", []):
            bn = b.buff_name
            if bn.startswith(f"{class_name}·"):
                count += 1
            elif standardize_extra and (bn.startswith("莱茵科技·") or bn.startswith("红松骑士团·")):
                count += 1
    return count


def manufacture_skill_class_effects(buff, ctx: BaseContext, self_name: str, room: str, idx: int):
    """解析当前制造站内某类技能数量 -> 自身生产力/仓库容量。"""
    if buff.room != "manufacture":
        return None
    d = buff.desc
    from .effects import Effect, parse_buff, _line_target

    m = re.search(rf"当前制造站内每个(莱茵科技|标准化|金属工艺)类技能为自身\+{_NUM}%的?生产力", d)
    if m:
        cnt = _manufacture_skill_class_count(ctx, room, idx, m.group(1))
        eff = [e for e in parse_buff(buff) if e.kind != "prod"]
        eff.append(Effect("prod", cnt * float(m.group(2)), _line_target(d)))
        return eff

    m = re.search(rf"当前制造站内每个(莱茵科技|标准化|金属工艺)类技能为自身\+{_NUM}的?仓库上限容量", d)
    if m:
        cnt = _manufacture_skill_class_count(ctx, room, idx, m.group(1))
        eff = [e for e in parse_buff(buff) if e.kind != "capacity"]
        eff.append(Effect("capacity", cnt * float(m.group(2))))
        return eff
    return None


def effective_power_station_count(ctx: BaseContext) -> int:
    """按只影响设施数量的后勤技能折算有效发电站数量。"""
    count = len(ctx.rooms.get("power", {}))
    if (_in(ctx, "森蚺", "control") and ctx.active("森蚺")
            and _in(ctx, "Lancet-2", "power") and ctx.active("Lancet-2")):
        count += 2
    for nm, (room, idx) in ctx.placement.items():
        if room != "power" or not ctx.active(nm):
            continue
        prof_buffs = ctx.active_buffs.get(nm, {})
        if not any("如果其他发电站内没有进驻作业平台" in b.desc and "发电站额外+1" in b.desc
                   for b in prof_buffs.get("power", [])):
            continue
        has_platform_elsewhere = any(
            other != nm
            and ctx.active(other)
            and "作业平台" in factions_of(other)
            and ctx.placement.get(other, ("", -1))[0] == "power"
            and ctx.placement.get(other, ("", -1))[1] != idx
            for other in ctx.present
        )
        if not has_platform_elsewhere:
            count += 1
    return count


def faction_facility_count_scale(buff, ctx: BaseContext):
    """解析基建内每有一间进驻某派系/类别干员的设施 +M%。"""
    d = buff.desc
    m = re.search(rf"每有一间进驻([^\s，。；]{{1,12}}?)(?:干员)?的设施.*?(?:额外)?\+{_NUM}%", d)
    if not m:
        return None
    fac_kw, per = m.group(1), float(m.group(2))
    faction = next((std for kw, std in FACTION_ALIASES.items() if kw in fac_kw), None)
    if faction is None:
        return None
    cnt = 0
    for rooms in ctx.rooms.values():
        for ops in rooms.values():
            if any(ctx.active(nm) and faction in factions_of(nm) for nm in ops):
                cnt += 1
    cap_m = re.search(r"最多([0-9]+)\s*间", d)
    if cap_m:
        cnt = min(cnt, int(cap_m.group(1)))
    return _base_pct(d) + cnt * per


def _base_pct(desc: str) -> float:
    """缩放子句("每名/每有/每N点...")之前的无条件 +M% 基底(用于派系/资源/人数缩放时保留基底)。"""
    cuts = [m.start() for m in re.finditer(r"每(?:[名个间级]|有|拥有|[0-9])", desc)]
    cuts += [m.start() for m in re.finditer(r"(?:会客室|训练室|制造站|发电站)每", desc)]
    cuts += [m.start() for m in re.finditer(r"(当(?!前)|如果|若|与其他|当前[^\s，。；]*内存在)", desc)]
    head = desc[: min(cuts)] if cuts else desc
    nums = re.findall(r"(?:\+|提升|提高)\s*([0-9]+(?:\.[0-9]+)?)%", head)
    return float(nums[-1]) if nums else 0.0


def resource_scale(buff, ctx: BaseContext):
    """解析"每[N][量词]资源 ... +M% ..."。量词含 点/个/瓶/层(木天蓼用"个", 乌萨斯特饮用"瓶")。
    同一技能可同时引用多个资源(如 情报储备 + 乌萨斯特饮), 按同一主效果累加。
    返回 amount(百分比) 或 None。"""
    d = buff.desc
    total = 0.0
    matched = False
    for res in RES_NAMES:
        if res not in d:
            continue
        # 资源名与百分比之间可能写作"+M%"或"(额外)提升M%"(无加号), 故兼容两种。
        m = re.search(rf"每(?:有|拥有)?([0-9]+)?[点个瓶层]*{res}.*?(?:\+|提升|增加)\s*{_NUM}%", d)
        if m:
            n = float(m.group(1)) if m.group(1) else 1.0
            per = float(m.group(2))
            total += _resource_units(ctx, res, n) * per
            matched = True
        th = re.search(rf"{res}达到\s*([0-9]+(?:\.[0-9]+)?)[点个瓶层]?.*?(?:额外)?(?:\+|提升|增加)\s*{_NUM}%", d)
        if th and ctx.pools.get(res, 0.0) >= float(th.group(1)):
            total += float(th.group(2))
            matched = True
    return total if matched else None


def resource_capacity_scale(buff, ctx: BaseContext):
    """解析资源驱动的仓库容量上限, 如 每1瓶乌萨斯特饮 仓库容量上限+2。"""
    d = buff.desc
    total = 0.0
    matched = False
    for res in RES_NAMES:
        if res not in d:
            continue
        m = re.search(rf"每(?:有|拥有)?([0-9]+)?[点个瓶层]*{res}.*?仓库容量上限\+{_NUM}", d)
        if m:
            n = float(m.group(1)) if m.group(1) else 1.0
            total += _resource_units(ctx, res, n) * float(m.group(2))
            matched = True
    return total if matched else None


def resource_dorm_recover_scale(buff, ctx: BaseContext):
    """解析宿舍资源驱动恢复额外项, 如 每5点无声共鸣 全员恢复额外+0.01。"""
    d = buff.desc
    if buff.room != "dormitory" or "恢复" not in d:
        return None
    total = 0.0
    matched = False
    for res in RES_NAMES:
        if res not in d:
            continue
        m = re.search(rf"每(?:有|拥有)?([0-9]+)?[点个瓶层]*{res}.*?恢复额外\+{_NUM}", d)
        if m:
            n = float(m.group(1)) if m.group(1) else 1.0
            total += _resource_units(ctx, res, n) * float(m.group(2))
            matched = True
    if not matched:
        return None
    m_base = re.search(rf"所有干员.*?恢复\+{_NUM}", d)
    base = float(m_base.group(1)) if m_base else 0.0
    return base + total


def _other_dorm_all_recover_base(buff, ctx: BaseContext) -> float:
    """同一干员其他宿舍技能提供的全员恢复基底, 用于“恢复效果额外”技能。"""
    from .effects import parse_buff

    base = 0.0
    for b in ctx.active_buffs.get(buff.name, {}).get("dormitory", []):
        if b is buff or "恢复效果额外" in b.desc:
            continue
        for e in parse_buff(b):
            if e.kind == "dorm_recover_all":
                base = max(base, e.amount)
    return base


def dorm_level_recover_scale(buff, ctx: BaseContext):
    """解析当前宿舍等级驱动恢复, 如 当前宿舍每级为恢复效果额外+0.02。"""
    d = buff.desc
    if buff.room != "dormitory" or "当前宿舍每级" not in d or "恢复效果额外" not in d:
        return None
    m_base = re.search(rf"所有干员.*?恢复\+{_NUM}", d)
    m_extra = re.search(rf"当前宿舍每级.*?恢复效果额外\+{_NUM}", d)
    if not m_extra:
        return None
    base = float(m_base.group(1)) if m_base else 0.0
    room, idx = ctx.placement.get(buff.name, ("", 0))
    level = ctx.dorm_level(idx) if room == "dormitory" else DORM_LEVEL
    return base + level * float(m_extra.group(1))


def power_count_dorm_recover_scale(buff, ctx: BaseContext):
    """解析发电站数量驱动宿舍恢复, 如 每有1间发电站恢复效果额外+0.05。"""
    d = buff.desc
    if buff.room != "dormitory" or "发电站" not in d or "恢复效果额外" not in d:
        return None
    m_base = re.search(rf"所有干员.*?恢复\+{_NUM}", d)
    m_extra = re.search(rf"每有?1?间发电站.*?恢复效果额外\+{_NUM}", d)
    if not m_extra:
        return None
    base = float(m_base.group(1)) if m_base else 0.0
    return base + effective_power_station_count(ctx) * float(m_extra.group(1))


def office_recruit_slot_dorm_recover_scale(buff, ctx: BaseContext):
    """解析非初始招募位驱动宿舍恢复, 如 每个招募位恢复效果额外+0.05。"""
    d = buff.desc
    if buff.room != "dormitory" or "招募位" not in d or "恢复效果" not in d:
        return None
    m_base = re.search(rf"所有干员.*?恢复\+{_NUM}", d)
    m_extra = re.search(rf"每个招募位.*?额外\+{_NUM}恢复效果", d)
    if not m_extra:
        return None
    base = float(m_base.group(1)) if m_base else 0.0
    return base + ctx.recruit_slots_noninitial * float(m_extra.group(1))


def faction_dorm_recover_scale(buff, ctx: BaseContext):
    """解析基建内派系/分支人数驱动宿舍恢复, 如 每名岁/行医干员恢复速度+0.06。"""
    d = buff.desc
    if buff.room != "dormitory" or "基建内" not in d or "恢复速度" not in d:
        return None
    m = re.search(rf"每名\s*([^\s，。]{{1,12}}?)\s*干员.*?恢复速度\+{_NUM}", d)
    if not m:
        return None
    fac_kw = m.group(1)
    faction = next((std for kw, std in FACTION_ALIASES.items() if kw in fac_kw), None)
    if not faction:
        return None
    cap_m = re.search(r"最多生效\s*([0-9]+)\s*名", d)
    cap = int(cap_m.group(1)) if cap_m else 999
    return min(cap, ctx.active_faction_count_base(faction)) * float(m.group(2))


def dorm_roommate_self_recover_scale(buff, ctx: BaseContext):
    """解析按同宿舍其他人数增加自身恢复, 如 每人额外为自身恢复+0.05。"""
    d = buff.desc
    if buff.room != "dormitory" or "每人额外为自身" not in d or "恢复" not in d:
        return None
    m_base = re.search(rf"自身心情每小时恢复\+{_NUM}", d)
    m_extra = re.search(rf"每人额外为自身心情每小时恢复\+{_NUM}", d)
    if not m_extra:
        return None
    room, idx = ctx.placement.get(buff.name, ("dormitory", 0))
    others = max(0, len(ctx.room_members(room, idx)) - 1)
    base = float(m_base.group(1)) if m_base else 0.0
    return base + others * float(m_extra.group(1))


def office_recruit_slot_clue_scale(buff, ctx: BaseContext):
    """解析办公室按非初始招募位提供的会客室线索速度加成。"""
    d = buff.desc
    if buff.room != "hire" or "每个招募位" not in d or "会客室线索搜集速度" not in d:
        return None
    m = re.search(rf"每个招募位.*?额外\+{_NUM}%会客室线索搜集速度", d)
    if not m:
        return None
    return ctx.recruit_slots_noninitial * float(m.group(1))


def office_recruit_slot_contact_scale(buff, ctx: BaseContext):
    """解析办公室按非初始招募位提升自身联络速度, 如 林·用人唯才。"""
    d = buff.desc
    if buff.room != "hire" or "每个招募位" not in d or "人脉资源的联络速度" not in d:
        return None
    m = re.search(rf"每个招募位.*?\+{_NUM}%人脉资源的联络速度", d)
    if not m:
        return None
    return ctx.recruit_slots_noninitial * float(m.group(1))


def meeting_recruit_slot_clue_scale(buff, ctx: BaseContext):
    """解析会客室按非初始招募位提升自身线索速度, 如 骋风·广交义友。"""
    d = buff.desc
    if buff.room != "meeting" or "每个招募位" not in d or "线索搜集速度" not in d:
        return None
    m = re.search(rf"每个招募位.*?提升\s*{_NUM}%线索搜集速度", d)
    if not m:
        return None
    return ctx.recruit_slots_noninitial * float(m.group(1))


def office_recruit_slot_mood_drain_scale(buff, ctx: BaseContext):
    """解析办公室按非初始招募位降低自身心情消耗, 如 每个招募位消耗-0.1。"""
    d = buff.desc
    if buff.room != "hire" or "每个招募位" not in d or "心情每小时消耗" not in d:
        return None
    m = re.search(rf"每个招募位.*?心情每小时消耗-{_NUM}", d)
    if not m:
        return None
    return -ctx.recruit_slots_noninitial * float(m.group(1))


def resource_mood_drain_scale(buff, ctx: BaseContext):
    """解析资源驱动的心情消耗增量, 如 每8点热情值 +0.01 或 热情值>=40时 +0.05。"""
    d = buff.desc
    total = 0.0
    matched = False
    for res in RES_NAMES:
        if res not in d:
            continue
        m = re.search(rf"每(?:有|拥有)?([0-9]+)?[点个瓶层]*{res}.*?心情每小时消耗\+{_NUM}", d)
        if m:
            n = float(m.group(1)) if m.group(1) else 1.0
            per = float(m.group(2))
            total += _resource_units(ctx, res, n) * per
            matched = True
        th = re.search(rf"{res}处于\s*([0-9]+(?:\.[0-9]+)?)[点个瓶层]?.*?及以上.*?心情每小时消耗\+{_NUM}", d)
        if th:
            if ctx.pools.get(res, 0.0) >= float(th.group(1)):
                total += float(th.group(2))
            matched = True
    return total if matched else None


def resource_room_drain_scale(buff, ctx: BaseContext):
    """解析房间全体心情消耗减免额外项, 如 每10点人间烟火 全体消耗额外-0.02。"""
    d = buff.desc
    if not any(k in d for k in ("全体干员", "所有干员", "贸易站内全体")):
        return None
    base = 0.0
    m_base = re.search(rf"(?:心情每小时消耗|每小时心情消耗)-{_NUM}", d)
    if m_base:
        base = float(m_base.group(1))
    extra = 0.0
    matched = False
    for res in RES_NAMES:
        if res not in d:
            continue
        m = re.search(rf"每(?:有|拥有)?([0-9]+)?[点个瓶层]*{res}.*?额外-{_NUM}", d)
        if m:
            n = float(m.group(1)) if m.group(1) else 1.0
            extra += _resource_units(ctx, res, n) * float(m.group(2))
            matched = True
    return base + extra if (m_base or matched) else None


def resource_other_recover_scale(buff, ctx: BaseContext):
    """解析控制中枢其他设施恢复的资源额外项, 如 每20点人间烟火 额外+0.05/h。"""
    d = buff.desc
    if buff.room != "control" or not any(k in d for k in ("其他设施", "部分设施")):
        return None
    base = 0.0
    m_base = re.search(rf"(?:其他设施|部分设施)内处于工作状态的干员心情每小时恢复\+{_NUM}", d)
    if m_base:
        base = float(m_base.group(1))
    extra = 0.0
    matched = False
    for res in RES_NAMES:
        if res not in d:
            continue
        m = re.search(rf"每(?:有|拥有)?([0-9]+)?[点个瓶层]*{res}.*?额外\+{_NUM}", d)
        if m:
            n = float(m.group(1)) if m.group(1) else 1.0
            per = float(m.group(2))
            extra += _resource_units(ctx, res, n) * per
            matched = True
    if "公事公办" in buff.buff_name:
        for nm in ctx.room_members("control", 0):
            if not ctx.active(nm):
                continue
            if any(b.buff_name in MLYNAR_OTHER_RECOVER_BUFFS for b in ctx.active_buffs.get(nm, {}).get("control", [])):
                extra = max(extra, 0.05)
                matched = True
                break
    return base + extra if (m_base or matched) else None


def control_room_recover_scale(buff, ctx: BaseContext):
    """解析控制中枢内派系人数驱动的中枢恢复, 如 每个谢拉格干员恢复+0.05。"""
    d = buff.desc
    if buff.room != "control" or "控制中枢内" not in d or "心情每小时恢复" not in d:
        return None
    m = re.search(rf"控制中枢内每(?:个|名)([^\s，。；]{{2,12}}?)干员.*?控制中枢内所有干员.*?恢复\+{_NUM}", d)
    if not m:
        return None
    fac_kw, per = m.group(1), float(m.group(2))
    faction = next((std for kw, std in FACTION_ALIASES.items() if kw in fac_kw), None)
    if not faction:
        return None
    if faction == "鲤氏侦探事务所" and "坚毅随和" in buff.buff_name:
        per = 0.05
    return per * ctx.active_faction_count_room(faction, "control", 0)


def conditional_extra_scale(buff, ctx: BaseContext, self_name: str, room: str, idx: int):
    """解析"基础主效果 + 条件额外+X%"。

    oa/values 表常给满条件总值, 但 PRTS 文本里的额外项必须按条件门控；未满足时只保留
    条件前的无条件基底。
    """
    d = buff.desc
    if "额外" not in d:
        return None
    base = _base_pct(d)
    extra = 0.0
    matched = False

    def pct_after(start: int) -> float | None:
        m = re.search(r"(?:额外(?:提升|提高)?|额外\+|额外提供)\s*\+?([0-9]+(?:\.[0-9]+)?)%", d[start:])
        if not m:
            m = re.search(r"额外(?:提升|提高)\s*([0-9]+(?:\.[0-9]+)?)%", d[start:])
        return float(m.group(1)) if m else None

    def add_if(ok: bool, start: int):
        nonlocal extra, matched
        pct = pct_after(start)
        if pct is None:
            return
        matched = True
        if ok:
            extra += pct

    # 当与X在同一个设施/同一个贸易站时...
    for m in re.finditer(r"当与([^\s，。；]{1,8}?)(?:在同一个|一起进驻)", d):
        other = m.group(1).strip("”“\"")
        ok = (
            other in ctx.present
            and ctx.active(other)
            and ctx.placement.get(other) == ctx.placement.get(self_name)
        )
        add_if(ok, m.end())

    # 当与X进驻某设施一起工作时...
    for m in re.finditer(r"当与([^\s，。；]{1,8}?)进驻[^\s，。；]*一起工作", d):
        other = m.group(1).strip("”“\"")
        ok = (
            other in ctx.present
            and ctx.active(other)
            and ctx.placement.get(other) == ctx.placement.get(self_name)
        )
        add_if(ok, m.end())

    # 与其他X干员进驻某设施一起工作时...
    for m in re.finditer(r"与其他([^\s，。；]{2,12}?)(?:干员)?进驻[^\s，。；]*一起工作", d):
        fac_kw = m.group(1)
        faction = next((std for kw, std in FACTION_ALIASES.items() if kw in fac_kw), None)
        if faction:
            ok = any(
                nm != self_name and ctx.active(nm) and faction in factions_of(nm)
                for nm in ctx.room_members(room, idx)
            )
            add_if(ok, m.end())

    # 当/如果X在基建内、进驻在某设施...
    for m in re.finditer(r"(?:当|如果)([^\s，。；]{1,8}?)(?:在基建内|进驻在?([^\s，。；]*))", d):
        other = m.group(1).strip("”“\"")
        room_hint = m.group(2) or ""
        hinted_room = None
        if room_hint:
            room_map = {"控制中枢": "control", "会客室": "meeting", "贸易站": "trading",
                        "制造站": "manufacture", "办公室": "hire", "宿舍": "dormitory",
                        "发电站": "power", "训练室": "training", "加工站": "workshop"}
            for cn, rn in room_map.items():
                if cn in room_hint:
                    hinted_room = rn
                    break
        if other in FACTIONS or other in ctx.present or hinted_room is not None:
            ok = other in ctx.present and ctx.active(other)
            if hinted_room is not None:
                ok = ok and ctx.placement.get(other, ("",))[0] == hinted_room
            add_if(ok, m.end())

    # 当前贸易站/当前房间内存在X派系干员时...
    for m in re.finditer(r"当前[^\s，。；]*内存在([^\s，。；]{2,12}?)(?:干员)?时", d):
        fac_kw = m.group(1)
        faction = next((std for kw, std in FACTION_ALIASES.items() if kw in fac_kw), None)
        if faction:
            ok = ctx.active_faction_count_room(faction, room, idx, exclude=self_name) > 0
            add_if(ok, m.end())

    # 当A、B入驻工作场所时...分别额外+X%
    m = re.search(r"当([^\s，。；]+?)入驻工作场所", d)
    if m:
        pct = pct_after(m.end())
        if pct is not None:
            matched = True
            names = [x.strip("”“\"") for x in re.split(r"[、,，]", m.group(1)) if x.strip()]
            extra += pct * sum(
                1
                for nm in names
                if nm in ctx.present and ctx.active(nm) and ctx.placement.get(nm, ("",))[0] != "dormitory"
            )

    return base + extra if matched else None


def conditional_mood_drain_applies(buff, ctx: BaseContext, self_name: str) -> bool:
    """附带心情消耗条件。False 时只过滤 mood_drain, 不影响同技能主效果。"""
    d = buff.desc
    if "当与萨尔贡干员进驻控制中枢一起工作时" in d and "自身心情每小时消耗" in d:
        return ctx.active_faction_count_room("萨尔贡", "control", 0, exclude=self_name) > 0
    return True


def facility_level_scale(buff, ctx: BaseContext):
    """解析设施等级驱动主效果。

    大模拟默认按满级基建估算: 宿舍L5、会客/训练L3。技能文本中的这类公式不能直接吃
    values 表的单位值或封顶值。
    """
    d = buff.desc
    base = _base_pct(d)
    total = 0.0
    matched = False

    m = re.search(rf"训练室每级.*?\+{_NUM}%.*?生产力", d)
    if m:
        total += ctx.facility_level("training") * float(m.group(1))
        matched = True

    m = re.search(rf"会客室每级额外提供{_NUM}%.*?效率", d)
    if m:
        total += ctx.facility_level("meeting") * float(m.group(1))
        matched = True

    m = re.search(rf"每间宿舍每级.*?(?:\+|提升|提高|额外\+)\s*{_NUM}%", d)
    if m:
        total += ctx.total_dorm_levels() * float(m.group(1))
        matched = True

    cap_m = re.search(r"最多(?:提供)?([0-9]+(?:\.[0-9]+)?)%?(?:效率|生产力|充能速度)?", d)
    if cap_m:
        total = min(total, float(cap_m.group(1)))

    return base + total if matched else None


def _gold_line_count(ctx: BaseContext, room: str, idx: int) -> int:
    """当前贸易站可见的赤金生产线数。

    PRTS: 每间制造站生产赤金 +1；鸿雪按基建内杜林族人数提供；绮良按已有生产线额外提供。
    绮良的额外线按进入技能前的生产线数结算，避免自我递归膨胀。
    """
    lines = sum(1 for line in ctx.manufacture_lines.values() if line == "赤金")
    members = ctx.room_members(room, idx)
    if "鸿雪" in members and ctx.active("鸿雪"):
        lines += min(4, ctx.active_faction_count_base("杜林族"))
    base_lines = lines
    if "绮良" in members and ctx.active("绮良"):
        kira_buffs = [b for b in ctx.active_buffs.get("绮良", {}).get("trading", [])
                      if "订单流可视化" in b.buff_name]
        step = 2 if any("每有2条赤金生产线" in b.desc for b in kira_buffs) else 4
        lines += (base_lines // step) * 2
    return int(lines)


def gold_line_scale(buff, ctx: BaseContext, self_name: str, room: str, idx: int):
    """解析赤金生产线驱动的贸易站效率。"""
    d = buff.desc
    if room != "trading" or "赤金生产线" not in d or "订单获取效率" not in d:
        return None
    base = _base_pct(d)
    lines = _gold_line_count(ctx, room, idx)
    if "每有1条赤金生产线" in d:
        return base + lines * 5.0
    m = re.search(rf"每有([0-9]+)条赤金生产线.*?额外(?:提升)?\+?{_NUM}%", d)
    if m:
        return base + (lines // int(m.group(1))) * float(m.group(2))
    return None


def facility_order_limit_scale(buff, ctx: BaseContext):
    """解析贸易站等级驱动的订单上限: 当前贸易站每级+N个订单上限。"""
    d = buff.desc
    if buff.room != "trading" or "当前贸易站每级" not in d or "订单上限" not in d:
        return None
    m = re.search(rf"当前贸易站每级\+{_NUM}个?订单上限", d)
    if not m:
        return None
    return ctx.facility_level("trading") * float(m.group(1))


def _order_limit_bonus_from_buff(buff, ctx: BaseContext, name: str) -> float:
    """当前贸易站内某技能提供的正向订单上限。"""
    frac = ctx.frac(name)
    if frac <= 0.0:
        return 0.0
    dyn = facility_order_limit_scale(buff, ctx)
    if dyn is not None:
        return max(0.0, dyn) * frac
    if pair_condition_met(buff, ctx, name) is False and not has_unconditional_effect(buff.desc):
        return 0.0
    m = re.search(rf"订单上限\+{_NUM}", buff.desc)
    return (float(m.group(1)) * frac) if m else 0.0


def room_order_limit_eff_scale(buff, ctx: BaseContext, self_name: str, room: str, idx: int):
    """解析按当前贸易站内订单上限提升量换算的订单效率。"""
    d = buff.desc
    if room != "trading" or "当前贸易站内干员提升的" not in d or "订单上限" not in d:
        return None
    m = re.search(rf"每([0-9]+(?:\.[0-9]+)?)?个订单上限.*?(?:提供|提升|增加){_NUM}%订单获取效率", d)
    if not m:
        return None
    step = float(m.group(1)) if m.group(1) else 1.0
    per = float(m.group(2))
    total_limit_bonus = 0.0
    for nm in ctx.room_members(room, idx):
        for b in ctx.active_buffs.get(nm, {}).get("trading", []):
            total_limit_bonus += _order_limit_bonus_from_buff(b, ctx, nm)
    eff = int(total_limit_bonus // max(1.0, step)) * per
    cap_m = re.search(r"最多提供([0-9]+(?:\.[0-9]+)?)%效率", d)
    if cap_m:
        eff = min(eff, float(cap_m.group(1)))
    return eff


def _trade_eff_bonus_from_buff(buff, ctx: BaseContext, name: str) -> float:
    """当前贸易站内某技能直接提供的订单效率, 用于二阶贸易技能估算。"""
    from .effects import parse_buff

    frac = ctx.frac(name)
    if frac <= 0.0:
        return 0.0
    if "当前贸易站内干员提供的每" in buff.desc:
        return 0.0
    if pair_condition_met(buff, ctx, name) is False and not has_unconditional_effect(buff.desc):
        return 0.0
    cond = conditional_extra_scale(buff, ctx, name, buff.room, ctx.placement.get(name, (buff.room, 0))[1])
    if cond is not None:
        return cond * frac
    res = resource_scale(buff, ctx)
    if res is not None:
        return res * frac
    gold_lines = gold_line_scale(buff, ctx, name, buff.room, ctx.placement.get(name, (buff.room, 0))[1])
    if gold_lines is not None:
        return gold_lines * frac
    recipe_eff = manufacture_recipe_type_scale(buff, ctx)
    if recipe_eff is not None:
        return recipe_eff * frac
    for e in parse_buff(buff):
        if e.kind == "trade_eff":
            return e.amount * frac
    return 0.0


def room_trade_eff_eff_scale(buff, ctx: BaseContext, self_name: str, room: str, idx: int):
    """解析按当前贸易站内干员提供的订单效率换算的订单效率。"""
    d = buff.desc
    if room != "trading" or "当前贸易站内干员提供的每" not in d or "订单获取效率" not in d:
        return None
    m = re.search(rf"每([0-9]+(?:\.[0-9]+)?)?%订单获取效率.*?额外提供{_NUM}%效率", d)
    if not m:
        return None
    step = float(m.group(1)) if m.group(1) else 1.0
    per = float(m.group(2))
    provided = 0.0
    for nm in ctx.room_members(room, idx):
        for b in ctx.active_buffs.get(nm, {}).get("trading", []):
            provided += _trade_eff_bonus_from_buff(b, ctx, nm)
    eff = int(provided // max(1.0, step)) * per
    cap_m = re.search(r"最多提供([0-9]+(?:\.[0-9]+)?)%效率", d)
    if cap_m:
        eff = min(eff, float(cap_m.group(1)))
    return eff


def other_trade_eff_order_limit_scale(buff, ctx: BaseContext, self_name: str, room: str, idx: int):
    """解析当前贸易站内其他干员提供的效率导致的订单上限变化。"""
    d = buff.desc
    if room != "trading" or "当前贸易站内其他干员提供的每" not in d or "订单上限-" not in d:
        return None
    m = re.search(rf"每([0-9]+(?:\.[0-9]+)?)?%订单获取效率使订单上限-{_NUM}", d)
    if not m:
        return None
    step = float(m.group(1)) if m.group(1) else 1.0
    per = float(m.group(2))
    provided = 0.0
    for nm in ctx.room_members(room, idx):
        if nm == self_name:
            continue
        for b in ctx.active_buffs.get(nm, {}).get("trading", []):
            provided += _trade_eff_bonus_from_buff(b, ctx, nm)
    return -int(provided // max(1.0, step)) * per


def manufacture_recipe_type_scale(buff, ctx: BaseContext):
    """解析按制造站当前加工配方类别数提供的贸易站效率。"""
    d = buff.desc
    if buff.room != "trading" or "制造站每有1类配方进行加工" not in d:
        return None
    m = re.search(rf"每有1类配方进行加工.*?额外\+{_NUM}%", d)
    if not m:
        return None
    types = {line for line in ctx.manufacture_lines.values() if line}
    return _base_pct(d) + len(types) * float(m.group(1))


def layout_branch_effects(buff, ctx: BaseContext):
    """解析布局分支技能, 如 望·权变 的外势/实地二选一。"""
    d = buff.desc
    if buff.room == "control" and "若外势大于等于实地" in d and "若实地大于外势" in d:
        from .effects import Effect

        external = len(ctx.rooms.get("trading", {})) + len(ctx.rooms.get("power", {}))
        field = len(ctx.rooms.get("manufacture", {}))
        if external >= field:
            return [Effect("trade_eff_global", 7.0)]
        return [Effect("prod_global", 2.0, "all")]
    return None


def conditional_power_effects(buff, ctx: BaseContext, self_name: str):
    """解析发电站条件充能技能, 避免把 +5% 条件项当成无条件充能。"""
    if buff.room != "power" or "无人机充能速度" not in buff.desc or "如果" not in buff.desc:
        return None

    from .effects import Effect

    d = buff.desc
    amount = buff.value
    if amount == 0.0:
        m = re.search(rf"无人机充能速度\+{_NUM}%", d)
        amount = float(m.group(1)) if m else 0.0
    ok = False
    if "其他作业平台进驻在发电站" in d:
        ok = any(
            nm != self_name
            and ctx.active(nm)
            and "作业平台" in factions_of(nm)
            and ctx.placement.get(nm, ("",))[0] == "power"
            for nm in ctx.present
        )
    elif "其他拉特兰干员进驻在发电站" in d:
        ok = any(
            nm != self_name
            and ctx.active(nm)
            and "拉特兰" in factions_of(nm)
            and ctx.placement.get(nm, ("",))[0] == "power"
            for nm in ctx.present
        )
    elif "逻各斯进驻在训练室协助位" in d:
        ok = ctx.active("逻各斯") and ctx.placement.get("逻各斯", ("",))[0] == "training"
    elif "凯尔希进驻在控制中枢" in d:
        ok = ctx.active("凯尔希") and ctx.placement.get("凯尔希", ("",))[0] == "control"
    else:
        return None
    return [Effect("power", amount)] if ok else []


def drone_cap_power_effects(buff, ctx: BaseContext):
    """解析按无人机持有上限换算的充能技能, 如 承曦格雷伊·巡线框架。"""
    if buff.room != "power" or "无人机上限" not in buff.desc or "无人机充能速度" not in buff.desc:
        return None

    from .effects import Effect

    d = buff.desc
    m = re.search(r"每\s*([0-9]+(?:\.[0-9]+)?)\s*架无人机上限\+([0-9]+(?:\.[0-9]+)?)%无人机充能速度", d)
    if not m:
        return None
    step = float(m.group(1))
    per = float(m.group(2))
    amount = int(ctx.drone_cap // step) * per
    cap_m = re.search(r"最多\+?([0-9]+(?:\.[0-9]+)?)%", d)
    if cap_m:
        amount = min(amount, float(cap_m.group(1)))
    return [Effect("power", amount)]


def conditional_control_effects(buff, ctx: BaseContext):
    """解析控制中枢纯条件全局技能, 避免把条件项当成无条件全局加成。"""
    d = buff.desc
    if buff.room != "control":
        return None

    from .effects import Effect

    if "如果有2台以上作业平台进驻在发电站" in d and "所有制造站生产力" in d:
        count = sum(
            1
            for nm in ctx.present
            if ctx.active(nm)
            and "作业平台" in factions_of(nm)
            and ctx.placement.get(nm, ("",))[0] == "power"
        )
        return [Effect("prod_global", 2.0, "all")] if count >= 2 else []

    return None


def gladiia_tidal_watch_effects(buff, ctx: BaseContext):
    """歌蕾蒂娅·潮汐守望: 深海猎人按宿舍/非宿舍位置调整自身心情消耗。"""
    if buff.room != "control" or buff.name != "歌蕾蒂娅" or buff.buff_name != "潮汐守望":
        return None

    from .effects import Effect

    delta = 0.0
    mood = ctx.mood or {}
    for nm in ctx.present:
        if "深海猎人" not in factions_of(nm) or not ctx.active(nm):
            continue
        room = ctx.placement.get(nm, ("", 0))[0]
        if room == "dormitory":
            delta -= 0.5
            if mood.get(nm, 24.0) >= 24.0 - 1e-6:
                delta -= 0.5
        else:
            delta += 0.5
    return [Effect("mood_drain", delta)] if abs(delta) > 1e-9 else []


def mood_gap_effects(buff, ctx: BaseContext):
    """解析按“自身心情落差”变化的技能。无心情态按满心情(落差0)处理。"""
    if buff.room != "manufacture" or buff.name != "铅踝":
        return None

    from .effects import Effect

    gap = max(0.0, 24.0 - (ctx.mood or {}).get(buff.name, 24.0))
    if buff.buff_name == "模糊视线":
        return [Effect("prod", 30.0 - int(gap // 4.0) * 5.0, "all")]
    if buff.buff_name == "窗外雪啸":
        if gap > 12.0:
            return [Effect("prod", 10.0, "all"), Effect("capacity", 6.0)]
        return []
    return None


def dorm_low_mood_effects(buff, ctx: BaseContext):
    """解析按宿舍内低心情人数变化的加工站技能。无心情态按无人低心情处理。"""
    if buff.room != "workshop" or "宿舍内每有" not in buff.desc or "副产品" not in buff.desc:
        return None

    from .effects import Effect, _workshop_target

    m = re.search(
        rf"宿舍内每有{_NUM}名心情{_NUM}以下.*?副产品.*?(?:提升|提高){_NUM}%",
        buff.desc,
    )
    if not m:
        return None

    mood = ctx.mood or {}
    step = float(m.group(1))
    threshold = float(m.group(2))
    amount = float(m.group(3))
    if step <= 0.0:
        return None
    low = 0
    for ops in ctx.rooms.get("dormitory", {}).values():
        low += sum(1 for nm in ops if mood.get(nm, 24.0) <= threshold)
    return [Effect("byproduct", int(low // step) * amount, _workshop_target(buff.desc))]


def dorm_low_mood_recover_effects(buff, ctx: BaseContext):
    """解析按当前宿舍低心情目标触发的宿舍恢复额外项。"""
    if buff.room != "dormitory":
        return None

    from .effects import Effect

    d = buff.desc
    room, idx = ctx.placement.get(buff.name, ("dormitory", 0))
    mood = ctx.mood or {}
    members = ctx.room_members(room, idx)

    if "该宿舍每有1名心情未满的干员" in d and "恢复效果额外" in d:
        base_m = re.search(rf"所有干员.*?恢复\+{_NUM}", d)
        extra_m = re.search(rf"每有1名心情未满的干员.*?恢复效果额外\+{_NUM}", d)
        if not extra_m:
            return None
        base = float(base_m.group(1)) if base_m else 0.0
        nonfull = sum(1 for nm in members if mood.get(nm, 24.0) < 24.0)
        return [Effect("dorm_recover_all", base + nonfull * float(extra_m.group(1)))]

    m = re.search(rf"该宿舍内心情([0-9]+(?:\.[0-9]+)?)以下的干员恢复效果额外\+{_NUM}", d)
    if m:
        base_m = re.search(rf"所有干员.*?恢复\+{_NUM}", d)
        base = float(base_m.group(1)) if base_m else _other_dorm_all_recover_base(buff, ctx)
        has_low = any(mood.get(nm, 24.0) <= float(m.group(1)) for nm in members)
        return [Effect("dorm_recover_all", base + (float(m.group(2)) if has_low else 0.0))]

    m = re.search(rf"该宿舍内心情([0-9]+(?:\.[0-9]+)?)以下的干员.*?恢复效果额外\+{_NUM}", d)
    if m:
        base_m = re.search(rf"所有干员.*?恢复\+{_NUM}", d)
        base = float(base_m.group(1)) if base_m else 0.0
        has_low = any(mood.get(nm, 24.0) <= float(m.group(1)) for nm in members)
        return [Effect("dorm_recover_all", base + (float(m.group(2)) if has_low else 0.0))]

    return None


def conditional_meeting_effects(buff, ctx: BaseContext):
    """解析会客室纯条件线索速度, 如 罗德岛隐秘队需焰狐龙梓兰在控制中枢。"""
    if buff.room != "meeting" or "线索搜集速度" not in buff.desc:
        return None
    d = buff.desc
    if "若焰狐龙梓兰进驻控制中枢" in d:
        from .effects import Effect

        if not ctx.active("焰狐龙梓兰") or ctx.placement.get("焰狐龙梓兰", ("",))[0] != "control":
            return []
        m = re.search(rf"线索搜集速度提升{_NUM}%", d)
        return [Effect("clue", float(m.group(1)) if m else 0.0)]
    return None


def room_scoped_control_effects(buff, ctx: BaseContext):
    """这些控制中枢技能作用到具体贸易/制造站, 不应折成全局加成。"""
    d = buff.desc
    if buff.room == "control" and any(k in d for k in (
        "每个存在3名谢拉格干员的贸易站",
        "同一贸易站中，每有1名格拉斯哥帮干员",
        "每个进驻在贸易站的叙拉古干员",
        "每个进驻在贸易站的谢拉格干员",
        "每个进驻在制造站的骑士干员",
        "每个进驻在制造站的黑钢国际干员",
        "每个进驻在制造站的红松骑士团干员",
        "当伊内丝入驻会客室时，会客室线索搜集速度",
        "当赫德雷入驻贸易站时，赫德雷所在贸易站订单上限",
    )):
        return []
    return None


# kind: 该设施"主效果"种类(被派系/资源缩放覆盖时用)
_ROOM_KIND = {
    "manufacture": "prod", "trading": "trade_eff", "meeting": "clue",
    "hire": "contact", "training": "train_speed", "power": "power", "workshop": "byproduct",
}


def resolve_buff_effects(buff, ctx: BaseContext, self_name: str):
    """上下文感知地把一条 Buff 解析为 Effect 列表。

    优先级: 配对/在场条件未满足 -> 门控为空; 否则若有派系计数/资源缩放 -> 用算出的真实值
    覆盖该设施主效果; 都没有 -> 回退静态 parse_buff。
    """
    from .effects import Effect, parse_buff, _line_target

    _MAIN_KINDS = ("prod", "trade_eff", "clue", "contact", "train_speed", "power", "byproduct",
                   "prod_global", "trade_eff_global", "clue_global", "contact_global")

    room = buff.room
    idx = ctx.placement.get(self_name, (room, 0))[1]

    layout_eff = layout_branch_effects(buff, ctx)
    if layout_eff is not None:
        return layout_eff
    power_eff = conditional_power_effects(buff, ctx, self_name)
    if power_eff is not None:
        return power_eff
    drone_cap_eff = drone_cap_power_effects(buff, ctx)
    if drone_cap_eff is not None:
        return drone_cap_eff
    control_eff = conditional_control_effects(buff, ctx)
    if control_eff is not None:
        return control_eff
    tidal_eff = gladiia_tidal_watch_effects(buff, ctx)
    if tidal_eff is not None:
        return tidal_eff
    gap_eff = mood_gap_effects(buff, ctx)
    if gap_eff is not None:
        return gap_eff
    low_mood_eff = dorm_low_mood_effects(buff, ctx)
    if low_mood_eff is not None:
        return low_mood_eff
    low_mood_rec_eff = dorm_low_mood_recover_effects(buff, ctx)
    if low_mood_rec_eff is not None:
        return low_mood_rec_eff
    meeting_eff = conditional_meeting_effects(buff, ctx)
    if meeting_eff is not None:
        return meeting_eff
    scoped_eff = room_scoped_control_effects(buff, ctx)
    if scoped_eff is not None:
        return scoped_eff
    if buff.room == "manufacture" and buff.buff_name == "配合意识":
        return []
    if buff.room == "manufacture" and buff.buff_name in ("回收利用", "大就是好！"):
        return [e for e in parse_buff(buff) if e.kind != "prod"]
    if buff.room == "control" and "丰川祥子心情每小时消耗" in buff.desc:
        if (
            "丰川祥子" not in ctx.present
            or ctx.placement.get("丰川祥子") != ctx.placement.get(self_name)
        ):
            return []
        return [e for e in parse_buff(buff) if e.kind != "mood_drain"]
    cancel_self_mood_drain = (
        (
            self_name == "若叶睦"
            and buff.room == "control"
            and "每有8点热情值，自身心情每小时消耗" in buff.desc
            and ctx.placement.get("丰川祥子") == ctx.placement.get(self_name)
        )
        or (
            buff.room == "manufacture"
            and "槐琥" in ctx.room_members(room, idx)
            and ctx.active("槐琥")
            and any(b.buff_name == "团队精神" for b in ctx.active_buffs.get("槐琥", {}).get("manufacture", []))
        )
        or (
            buff.room == "control"
            and "炎-岁" in factions_of(self_name)
            and "令" in ctx.room_members("control", 0)
            and ctx.active("令")
            and any(b.buff_name == "杯莫停" for b in ctx.active_buffs.get("令", {}).get("control", []))
        )
    )
    mood_drain_allowed = conditional_mood_drain_applies(buff, ctx, self_name)
    skill_class_eff = manufacture_skill_class_effects(buff, ctx, self_name, room, idx)
    if skill_class_eff is not None:
        return skill_class_eff

    # 缩放量与种类: 依次尝试 派系计数 -> 人数缩放 -> 资源缩放
    add_amt = None
    kind = None
    source = ""
    fac = faction_count_scale(buff, ctx, self_name, room, idx)
    if fac is not None:
        add_amt, kind_override = fac
        source = "faction_count"
        if kind_override:
            kind = kind_override
        elif room == "control":
            kind = ("prod_global" if ("制造站" in buff.desc and "生产力" in buff.desc) else
                    "trade_eff_global" if ("贸易站" in buff.desc and "订单" in buff.desc) else
                    "clue_global" if ("会客室" in buff.desc and "线索" in buff.desc) else
                    "contact_global" if ("人力办公室" in buff.desc or "人脉资源" in buff.desc) else None)
        else:
            kind = _ROOM_KIND.get(room)
    if add_amt is None:
        man_count = manufacture_facility_count_scale(buff, ctx, room, idx)
        if man_count is not None:
            add_amt, kind = man_count, "prod"
            source = "manufacture_facility_count"
    if add_amt is None:
        platform_count = power_platform_count_scale(buff, ctx)
        if platform_count is not None:
            add_amt, kind = platform_count, "prod"
            source = "power_platform_count"
    if add_amt is None:
        hc = headcount_scale(buff, ctx, self_name, room, idx)
        if hc is not None:
            add_amt, kind = hc, _ROOM_KIND.get(room)
            source = "headcount"
    if add_amt is None:
        fac_rooms = faction_facility_count_scale(buff, ctx)
        if fac_rooms is not None:
            add_amt = fac_rooms
            kind = _ROOM_KIND.get(room)
            source = "faction_facility_count"
    if add_amt is None:
        res = resource_scale(buff, ctx)
        if res is not None:
            add_amt = res
            kind = (("prod_global" if ("制造站" in buff.desc and "生产力" in buff.desc) else
                     "trade_eff_global" if ("贸易站" in buff.desc and "订单" in buff.desc) else
                     "clue_global" if ("会客室" in buff.desc and "线索" in buff.desc) else
                     "contact_global" if ("人力办公室" in buff.desc or "人脉资源" in buff.desc) else
                     None) if room == "control" else _ROOM_KIND.get(room, "prod"))
            source = "resource"
    if add_amt is None:
        cond_extra = conditional_extra_scale(buff, ctx, self_name, room, idx)
        if cond_extra is not None:
            add_amt = cond_extra
            kind = (("prod_global" if ("制造站" in buff.desc and "生产力" in buff.desc) else
                     "trade_eff_global" if ("贸易站" in buff.desc and "订单" in buff.desc) else
                     "clue_global" if ("会客室" in buff.desc and "线索" in buff.desc) else
                     "contact_global" if ("人力办公室" in buff.desc or "人脉资源" in buff.desc) else
                     None) if room == "control" else _ROOM_KIND.get(room, "prod"))
            source = "conditional_extra"
    if add_amt is None:
        facility = facility_level_scale(buff, ctx)
        if facility is not None:
            add_amt = facility
            kind = (("prod_global" if ("制造站" in buff.desc and "生产力" in buff.desc) else
                     "trade_eff_global" if ("贸易站" in buff.desc and "订单" in buff.desc) else
                     "clue_global" if ("会客室" in buff.desc and "线索" in buff.desc) else
                     "contact_global" if ("人力办公室" in buff.desc or "人脉资源" in buff.desc) else
                     None) if room == "control" else _ROOM_KIND.get(room, "prod"))
            source = "facility_level"
    if add_amt is None:
        gold_lines = gold_line_scale(buff, ctx, self_name, room, idx)
        if gold_lines is not None:
            add_amt = gold_lines
            kind = "trade_eff"
            source = "gold_line"
    if add_amt is None:
        limit_eff = room_order_limit_eff_scale(buff, ctx, self_name, room, idx)
        if limit_eff is not None:
            add_amt = limit_eff
            kind = "trade_eff"
            source = "room_order_limit_eff"
    if add_amt is None:
        eff_eff = room_trade_eff_eff_scale(buff, ctx, self_name, room, idx)
        if eff_eff is not None:
            add_amt = eff_eff
            kind = "trade_eff"
            source = "room_trade_eff_eff"
    if add_amt is None:
        recruit_contact = office_recruit_slot_contact_scale(buff, ctx)
        if recruit_contact is not None:
            add_amt = recruit_contact
            kind = "contact"
            source = "office_recruit_slot_contact"
    if add_amt is None:
        recipe_eff = manufacture_recipe_type_scale(buff, ctx)
        if recipe_eff is not None:
            add_amt = recipe_eff
            kind = "trade_eff"
            source = "manufacture_recipe_type"

    if add_amt is not None and kind:
        cond_extra_value = conditional_extra_scale(buff, ctx, self_name, room, idx)
        if source != "conditional_extra" and cond_extra_value is not None:
            if source in {"faction_facility_count", "facility_level", "gold_line", "manufacture_recipe_type"}:
                add_amt += max(0.0, cond_extra_value - _base_pct(buff.desc))
            else:
                add_amt += cond_extra_value
        # 保留同技能的无条件基底(base) + 心情消耗等附带项; 只替换主效果种类的数值。
        base = 0.0 if (cond_extra_value is not None
                       or facility_level_scale(buff, ctx) is not None
                       or gold_line_scale(buff, ctx, self_name, room, idx) is not None
                       or room_order_limit_eff_scale(buff, ctx, self_name, room, idx) is not None
                       or room_trade_eff_eff_scale(buff, ctx, self_name, room, idx) is not None
                       or manufacture_recipe_type_scale(buff, ctx) is not None
                       or manufacture_facility_count_scale(buff, ctx, room, idx) is not None
                       or power_platform_count_scale(buff, ctx) is not None
                       or faction_facility_count_scale(buff, ctx) is not None) else _base_pct(buff.desc)
        cap = resource_capacity_scale(buff, ctx)
        dyn_limit = facility_order_limit_scale(buff, ctx)
        dyn_eff_limit = other_trade_eff_order_limit_scale(buff, ctx, self_name, room, idx)
        eff = [e for e in parse_buff(buff)
               if e.kind not in _MAIN_KINDS
               and not (cap is not None and e.kind == "capacity")
               and not ((dyn_limit is not None or dyn_eff_limit is not None) and e.kind == "order_limit")
               and not (not mood_drain_allowed and e.kind == "mood_drain")]
        if kind in ("prod", "prod_global"):
            tgt = _line_target(buff.desc)
        elif kind == "byproduct":
            tgt = next((e.target for e in parse_buff(buff) if e.kind == "byproduct"), "any")
        else:
            tgt = ""
        eff.append(Effect(kind, base + add_amt, tgt))
        if cap is not None:
            eff.append(Effect("capacity", cap))
        if dyn_limit is not None:
            eff.append(Effect("order_limit", dyn_limit))
        if dyn_eff_limit is not None:
            eff.append(Effect("order_limit", dyn_eff_limit))
        dorm_rec = resource_dorm_recover_scale(buff, ctx)
        if dorm_rec is not None:
            eff.append(Effect("dorm_recover_all", dorm_rec))
        dorm_lvl_rec = dorm_level_recover_scale(buff, ctx)
        if dorm_lvl_rec is not None:
            eff = [e for e in eff if not e.kind.startswith("dorm_recover")]
            eff.append(Effect("dorm_recover_all", dorm_lvl_rec))
        power_dorm_rec = power_count_dorm_recover_scale(buff, ctx)
        if power_dorm_rec is not None:
            eff = [e for e in eff if not e.kind.startswith("dorm_recover")]
            eff.append(Effect("dorm_recover_all", power_dorm_rec))
        recruit_dorm_rec = office_recruit_slot_dorm_recover_scale(buff, ctx)
        if recruit_dorm_rec is not None:
            eff = [e for e in eff if not e.kind.startswith("dorm_recover")]
            eff.append(Effect("dorm_recover_all", recruit_dorm_rec))
        faction_dorm_rec = faction_dorm_recover_scale(buff, ctx)
        if faction_dorm_rec is not None:
            eff = [e for e in eff if not e.kind.startswith("dorm_recover")]
            eff.append(Effect("dorm_recover_all", faction_dorm_rec))
        roommate_self_rec = dorm_roommate_self_recover_scale(buff, ctx)
        if roommate_self_rec is not None:
            eff = [e for e in eff if e.kind != "dorm_recover_self"]
            eff.append(Effect("dorm_recover_self", roommate_self_rec))
        office_clue = office_recruit_slot_clue_scale(buff, ctx)
        if office_clue is not None:
            eff.append(Effect("clue_global", office_clue))
        recruit_contact = office_recruit_slot_contact_scale(buff, ctx)
        if recruit_contact is not None:
            eff = [e for e in eff if e.kind != "contact"]
            eff.append(Effect("contact", recruit_contact))
        meeting_recruit_clue = meeting_recruit_slot_clue_scale(buff, ctx)
        if meeting_recruit_clue is not None:
            eff = [e for e in eff if e.kind != "clue"]
            eff.append(Effect("clue", meeting_recruit_clue))
        recruit_mood = office_recruit_slot_mood_drain_scale(buff, ctx)
        if recruit_mood is not None:
            eff = [e for e in eff if e.kind != "mood_drain"]
            if not cancel_self_mood_drain:
                eff.append(Effect("mood_drain", recruit_mood))
        mood = resource_mood_drain_scale(buff, ctx)
        if mood is not None:
            eff = [e for e in eff if e.kind != "mood_drain"]
            if not cancel_self_mood_drain:
                eff.append(Effect("mood_drain", mood))
        room_drain = resource_room_drain_scale(buff, ctx)
        if room_drain is not None:
            eff = [e for e in eff if e.kind != "room_drain"]
            eff.append(Effect("room_drain", room_drain))
        other_rec = resource_other_recover_scale(buff, ctx)
        if other_rec is not None:
            eff = [e for e in eff if e.kind != "other_recover"]
            eff.append(Effect("other_recover", other_rec))
        control_rec = control_room_recover_scale(buff, ctx)
        if control_rec is not None:
            eff = [e for e in eff if e.kind != "control_recover"]
            eff.append(Effect("control_recover", control_rec))
        return eff

    # 配对/在场条件门控: 仅"完全条件型"(条件词前无无条件数值)且未满足时整条不计
    cond = pair_condition_met(buff, ctx, self_name)
    if cond is False and not has_unconditional_effect(buff.desc):
        return []

    # 普通(含已满足的配对条件、含无条件基底的部分条件型): 静态解析
    cap = resource_capacity_scale(buff, ctx)
    dyn_limit = facility_order_limit_scale(buff, ctx)
    dyn_eff_limit = other_trade_eff_order_limit_scale(buff, ctx, self_name, room, idx)
    eff = [e for e in parse_buff(buff)
           if not (cap is not None and e.kind == "capacity")
           and not ((dyn_limit is not None or dyn_eff_limit is not None) and e.kind == "order_limit")
           and not ((cancel_self_mood_drain or not mood_drain_allowed) and e.kind == "mood_drain")]
    if cap is not None:
        eff.append(Effect("capacity", cap))
    if dyn_limit is not None:
        eff.append(Effect("order_limit", dyn_limit))
    if dyn_eff_limit is not None:
        eff.append(Effect("order_limit", dyn_eff_limit))
    dorm_rec = resource_dorm_recover_scale(buff, ctx)
    if dorm_rec is not None:
        eff.append(Effect("dorm_recover_all", dorm_rec))
    dorm_lvl_rec = dorm_level_recover_scale(buff, ctx)
    if dorm_lvl_rec is not None:
        eff = [e for e in eff if not e.kind.startswith("dorm_recover")]
        eff.append(Effect("dorm_recover_all", dorm_lvl_rec))
    power_dorm_rec = power_count_dorm_recover_scale(buff, ctx)
    if power_dorm_rec is not None:
        eff = [e for e in eff if not e.kind.startswith("dorm_recover")]
        eff.append(Effect("dorm_recover_all", power_dorm_rec))
    recruit_dorm_rec = office_recruit_slot_dorm_recover_scale(buff, ctx)
    if recruit_dorm_rec is not None:
        eff = [e for e in eff if not e.kind.startswith("dorm_recover")]
        eff.append(Effect("dorm_recover_all", recruit_dorm_rec))
    faction_dorm_rec = faction_dorm_recover_scale(buff, ctx)
    if faction_dorm_rec is not None:
        eff = [e for e in eff if not e.kind.startswith("dorm_recover")]
        eff.append(Effect("dorm_recover_all", faction_dorm_rec))
    roommate_self_rec = dorm_roommate_self_recover_scale(buff, ctx)
    if roommate_self_rec is not None:
        eff = [e for e in eff if e.kind != "dorm_recover_self"]
        eff.append(Effect("dorm_recover_self", roommate_self_rec))
    office_clue = office_recruit_slot_clue_scale(buff, ctx)
    if office_clue is not None:
        eff.append(Effect("clue_global", office_clue))
    recruit_contact = office_recruit_slot_contact_scale(buff, ctx)
    if recruit_contact is not None:
        eff = [e for e in eff if e.kind != "contact"]
        eff.append(Effect("contact", recruit_contact))
    meeting_recruit_clue = meeting_recruit_slot_clue_scale(buff, ctx)
    if meeting_recruit_clue is not None:
        eff = [e for e in eff if e.kind != "clue"]
        eff.append(Effect("clue", meeting_recruit_clue))
    recruit_mood = office_recruit_slot_mood_drain_scale(buff, ctx)
    if recruit_mood is not None:
        eff = [e for e in eff if e.kind != "mood_drain"]
        if not cancel_self_mood_drain:
            eff.append(Effect("mood_drain", recruit_mood))
    mood = resource_mood_drain_scale(buff, ctx)
    if mood is not None:
        eff = [e for e in eff if e.kind != "mood_drain"]
        if not cancel_self_mood_drain:
            eff.append(Effect("mood_drain", mood))
    room_drain = resource_room_drain_scale(buff, ctx)
    if room_drain is not None:
        eff = [e for e in eff if e.kind != "room_drain"]
        eff.append(Effect("room_drain", room_drain))
    other_rec = resource_other_recover_scale(buff, ctx)
    if other_rec is not None:
        eff = [e for e in eff if e.kind != "other_recover"]
        eff.append(Effect("other_recover", other_rec))
    control_rec = control_room_recover_scale(buff, ctx)
    if control_rec is not None:
        eff = [e for e in eff if e.kind != "control_recover"]
        eff.append(Effect("control_recover", control_rec))
    return eff
