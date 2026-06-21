"""把一条基建技能 (Buff) 解析成结构化效果。

oa 数值表只给『数值』, 语义在 description 文本里 (一图流的 logistics.js 不做语义解析)。
本模块用关键词/正则从描述里判断效果类型与作用对象, 数值优先取 oa(buff.value),
描述里另有明确数字的(如 订单上限+N、心情消耗+x)则从文本解析。

只对『经济上重要』的效果做精确建模: 制造站生产力、贸易站订单效率/上限、发电充能、
控制中枢全局加成、心情消耗/恢复、宿舍恢复、会客室线索、办公室联络。其余按近似处理。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .skills import Buff

# ---- 文本数字提取 ----
_NUM = r"([0-9]+(?:\.[0-9]+)?)"


def _find(pat: str, text: str) -> float | None:
    m = re.search(pat, text)
    return float(m.group(1)) if m else None


@dataclass
class Effect:
    kind: str          # 见下方说明
    amount: float
    target: str = ""   # 生产线类别等
    mood_cost: float | None = None

    # kind 取值:
    #   prod            制造站生产力% (target: record/gold/shard/all)
    #   prod_global     控制中枢→所有制造站生产力% (target: record/gold/shard/all)
    #   capacity        制造站仓库容量上限 (+/-)
    #   trade_eff       贸易站订单获取效率%
    #   trade_eff_global 控制中枢→所有贸易站订单效率%
    #   clue_global      控制中枢→会客室线索搜集速度%
    #   contact_global   控制中枢→办公室联络速度%
    #   order_limit     贸易站订单上限 (+/-)
    #   power           发电站无人机充能%
    #   byproduct       加工站副产品概率提升%
    #   byproduct_mood_cost 加工站原始心情消耗为N的副产品概率提升%
    #   room_drain      所在工作房间全体每小时心情消耗减免
    #   room_drain_delta 所在工作房间全体每小时心情消耗增加
    #   room_drain_delta_other 所在工作房间除自身外每小时心情消耗增加
    #   mood_drain      工作时每小时心情净消耗增量 (可为负, 表示降低消耗/自恢复)
    #   dorm_recover    宿舍每小时心情恢复 (该干员入驻宿舍提供)
    #   control_recover 控制中枢→控制中枢内干员每小时心情恢复
    #   global_recover  控制中枢→宿舍内干员每小时心情恢复
    #   global_recover_elite 控制中枢→宿舍内罗德岛精英干员每小时心情恢复
    #   other_recover   控制中枢→其他工作设施内干员每小时心情恢复
    #   clue            会客室线索搜集速度%
    #   contact         办公室联络速度%
    #   train_speed     训练室专精训练速度%
    #   train_initial_progress 训练开始时立即完成的进度%


def _line_target(desc: str) -> str:
    """判断制造站/全局生产力作用的生产线类别。"""
    if "贵金属" in desc or "赤金" in desc:
        return "gold"
    if "作战记录" in desc:
        return "record"
    if "源石" in desc:
        return "shard"
    return "all"


def _workshop_target(desc: str) -> str:
    """判断加工站副产品技能适用的加工类别。"""
    if "任意类材料" in desc:
        return "any"
    if "精英材料" in desc:
        return "elite"
    if "技巧概要" in desc:
        return "skill"
    if "芯片" in desc:
        return "chip"
    if "基建材料" in desc:
        return "building"
    material_map = {
        "炽合金": "alloy",
        "聚酸酯": "polyester",
        "异铁": "oriron",
        "源岩": "rock",
        "晶体": "crystal",
        "酮凝集": "ketone",
        "装置": "device",
    }
    for kw, target in material_map.items():
        if kw in desc:
            return target
    return "any"


def parse_buff(buff: Buff) -> list[Effect]:
    """解析单条 Buff 为效果列表。"""
    d = buff.desc
    v = buff.value
    room = buff.room
    eff: list[Effect] = []

    # 通用: 心情每小时消耗 +/- (任何设施工作时都可能带)
    inc = _find(rf"(?:心情每小时消耗|每小时心情消耗)\+{_NUM}", d)
    if inc:
        if "控制中枢内除自身以外" in d:
            eff.append(Effect("room_drain_delta_other", inc))
        elif any(k in d for k in ("全体干员", "所有干员", "全体", "贸易站内全体", "当前贸易站内干员",
                                "当前制造站内所有干员", "控制中枢内所有干员")):
            eff.append(Effect("room_drain_delta", inc))
        else:
            eff.append(Effect("mood_drain", inc))
    dec = _find(rf"(?:心情每小时消耗|每小时心情消耗)-{_NUM}", d)
    if dec:
        if any(k in d for k in ("全体干员", "所有干员", "贸易站内全体", "贸易站内干员",
                                "当前贸易站内干员", "当前制造站内所有干员")):
            eff.append(Effect("room_drain", dec))
        else:
            eff.append(Effect("mood_drain", -dec))

    if room == "manufacture":
        if v:
            eff.append(Effect("prod", v, _line_target(d)))
        prod_d = _find(rf"生产力-{_NUM}%", d)
        if prod_d:
            eff.append(Effect("prod", -prod_d, _line_target(d)))
        cap = _find(rf"仓库容量上限\+{_NUM}", d)
        if cap:
            eff.append(Effect("capacity", cap))
        cap_d = _find(rf"仓库容量上限-{_NUM}", d)
        if cap_d:
            eff.append(Effect("capacity", -cap_d))

    elif room == "trading":
        if v:
            eff.append(Effect("trade_eff", v))
        else:
            trade_eff = _find(rf"订单(?:获取)?效率\+{_NUM}%", d)
            if trade_eff is not None and not re.search(r"每(?:有|个|名|1)", d):
                eff.append(Effect("trade_eff", trade_eff))
        ol = _find(rf"订单上限\+{_NUM}", d)
        if ol:
            eff.append(Effect("order_limit", ol))
        ol_d = _find(rf"订单上限-{_NUM}", d)
        if ol_d:
            eff.append(Effect("order_limit", -ol_d))

    elif room == "power":
        if v and ("无人机" in d or "充能速度" in d):
            eff.append(Effect("power", v))

    elif room == "control":
        # 全局加成: 区分制造站生产力 / 贸易站订单效率 / 心情恢复作用域
        if "制造站" in d and ("生产力" in d):
            eff.append(Effect("prod_global", v, _line_target(d)))
        if "贸易站" in d and ("订单" in d and "效率" in d):
            eff.append(Effect("trade_eff_global", v))
        if "会客室" in d and "线索" in d:
            eff.append(Effect("clue_global", v))
        if ("人力办公室" in d or "人脉资源" in d) and "联络速度" in d and "小于30%" not in d:
            contact = v or (_find(rf"联络速度\+{_NUM}%", d) or 0.0)
            eff.append(Effect("contact_global", contact))
        other_rec = _find(rf"(?:其他设施|部分设施)内处于工作状态的干员心情每小时恢复\+{_NUM}", d)
        if other_rec:
            eff.append(Effect("other_recover", other_rec))
        rec = _find(rf"心情每小时恢复\+{_NUM}", d)
        if rec:
            if re.search(r"控制中枢内(?:所有干员|每[个名].*?干员).*?心情每小时恢复", d):
                eff.append(Effect("control_recover", rec))
            elif re.search(r"宿舍内.*?精英干员.*?心情每小时恢复", d):
                eff.append(Effect("global_recover_elite", rec))
            elif re.search(r"宿舍内.*?干员.*?心情每小时恢复", d):
                eff.append(Effect("global_recover", rec))

    elif room == "dormitory":
        # 宿舍恢复按作用对象分三类(决定能否加速"来休息的疲劳干员"):
        #   自身          -> dorm_recover_self  (只恢复恢复位自己, 不帮别人)
        #   所有干员/全体 -> dorm_recover_all   (全员, 同种取最高)
        #   某个/一名干员 -> dorm_recover_other (单个他人, 同种取最高)
        # 复合技能(如"自身+X, 同时所有干员+Y")按子句逐条拆分; 注意部分描述写"...某个干员每小时
        # 恢复+X"(无"心情"二字), 故正则不强求"心情"前缀。
        def _dorm_kind(clause: str) -> str:
            if ("所有干员" in clause) or ("全体" in clause):
                return "dorm_recover_all"
            if any(k in clause for k in ("某个", "一名", "除自身以外", "前一位", "其他干员")):
                return "dorm_recover_other"
            if "自身" in clause:
                return "dorm_recover_self"
            return "dorm_recover_other"   # 无明确主语默认按给他人

        ms = list(re.finditer(rf"恢复([+-]){_NUM}", d))
        if ms:
            # 描述里逐条恢复子句各自分类(子句 = 上一个标点到该数字)
            for m in ms:
                seg_start = max((d.rfind(p, 0, m.start()) for p in "，。；、"), default=-1) + 1
                clause = d[seg_start:m.end()]
                amount = float(m.group(2))
                if m.group(1) == "-":
                    amount = -amount
                eff.append(Effect(_dorm_kind(clause), amount))
        elif v and "恢复效果额外" not in d:
            # 描述无显式数字, 用 oa 值(×0.01), 按整条描述分类
            eff.append(Effect(_dorm_kind(d), v / 100.0))

    elif room == "meeting":
        # 线索交流期间的技能不提高日常线索搜集速度；当前模型只给线索搜集估值。
        if v and "处于线索交流时" not in d:
            eff.append(Effect("clue", v))

    elif room == "hire":
        contact = _find(rf"人脉资源的联络速度\+{_NUM}%", d)
        if contact is not None:
            eff.append(Effect("contact", contact))
        elif v:
            eff.append(Effect("contact", v))

    elif room == "training":
        if v:
            eff.append(Effect("train_speed", v))
        else:
            speed = _find(rf"专精技能训练速度\+{_NUM}%", d)
            if speed is not None:
                eff.append(Effect("train_speed", speed))
        if "下次训练所需时间-50%" in d:
            eff.append(Effect("train_initial_progress", 50.0))

    elif room == "workshop":
        if "副产品" in d:
            pct = _find(rf"副产品.*?(?:提升|提高)\s*{_NUM}%", d)
            if pct:
                original_cost = _find(rf"原始心情消耗为{_NUM}", d)
                if original_cost is not None:
                    eff.append(Effect("byproduct_mood_cost", pct, _workshop_target(d), original_cost))
                else:
                    eff.append(Effect("byproduct", pct, _workshop_target(d)))

    return eff


def parse_buffs(buffs: list[Buff]) -> list[Effect]:
    out: list[Effect] = []
    for b in buffs:
        out.extend(parse_buff(b))
    return out
