"""基建技能库 (数据来自一图流 buildingSkillFilter / logistics 的 oa 值表)。

skills.json: 899 条记录 {charId,name,buffName,roomType,phase,level,desc}
values.json: 892 条 "name|roomType|buffName|phase|level" -> 数值 (oa 表)

roomType 与设施对应:
    manufacture 制造站 / trading 贸易站 / power 发电站 / control 控制中枢
    dormitory 宿舍 / meeting 会客室 / hire 办公室 / training 训练室 / workshop 加工站

phase = 解锁所需精英化等级 (0/1/2); level = 解锁所需干员等级 (多为 1, 部分机器人为 30)。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"

ROOM_CN = {
    "manufacture": "制造站",
    "trading": "贸易站",
    "power": "发电站",
    "control": "控制中枢",
    "dormitory": "宿舍",
    "meeting": "会客室",
    "hire": "办公室",
    "training": "训练室",
    "workshop": "加工站",
}

@dataclass(frozen=True)
class Buff:
    """一个已解锁的基建技能效果。"""

    name: str          # 干员名
    room: str          # roomType
    buff_name: str     # 技能名
    phase: int         # 解锁精英化
    level: int         # 解锁等级
    value: float       # oa 表数值 (制造站=生产力%, 贸易站=订单效率%, 宿舍=心情恢复×0.01/h, ...)
    desc: str = ""


class SkillDB:
    """基建技能库, 按干员练度解析其在各设施可提供的技能。"""

    def __init__(
        self,
        skills_path: Path | None = None,
        values_path: Path | None = None,
        groups_path: Path | None = None,
    ):
        skills_path = skills_path or DATA_DIR / "skills.json"
        values_path = values_path or DATA_DIR / "values.json"
        groups_path = groups_path or DATA_DIR / "groups.json"
        records = json.loads(Path(skills_path).read_text(encoding="utf-8"))
        self._values: dict[str, float] = json.loads(Path(values_path).read_text(encoding="utf-8"))
        # gr 表: "charId|roomType|buffName|phase|level" -> "charId|N" 互斥替代组。
        # 同组的技能是同一干员同一槽位的升级链(取已解锁最高阶); 不同组/无组 -> 共存。
        self._groups: dict[str, str] = json.loads(Path(groups_path).read_text(encoding="utf-8"))

        self._by_name: dict[str, list[dict]] = {}
        for r in records:
            self._by_name.setdefault(r["name"], []).append(r)

    @staticmethod
    def _full_key(rec: dict) -> str:
        return f"{rec['charId']}|{rec['roomType']}|{rec['buffName']}|{rec['phase']}|{rec['level']}"

    @staticmethod
    def _fallback_group_id(rec: dict) -> str:
        """兼容缺失 gr 记录的标准 α/β/γ 升级链。"""
        base = re.sub(r"[·・][αβγ]$", "", rec["buffName"])
        if base == rec["buffName"]:
            return SkillDB._full_key(rec)
        return f"{rec['charId']}|{rec['roomType']}|{base}"

    def _value_of(self, rec: dict) -> float:
        key = f"{rec['name']}|{rec['roomType']}|{rec['buffName']}|{rec['phase']}|{rec['level']}"
        return float(self._values.get(key, 0.0))

    @lru_cache(maxsize=None)
    def has_operator(self, name: str) -> bool:
        return name in self._by_name

    def buffs_for(self, name: str, elite: int, level: int) -> dict[str, list[Buff]]:
        """返回 {roomType: [Buff, ...]} —— 该干员在各设施实际生效的技能。

        解锁判定: phase <= elite 且 level <= 干员等级。
        同一设施内: 同一 gr 互斥组 (data/groups.json) 只保留已解锁的最高阶版本 (L=phase*1000+level);
        不同组/无组的技能视为可同时生效 (部分干员一个设施有两个独立基建技能)。
        注: 升级链(如 ·α/·β)的归并依据是 gr 互斥组, 不是技能名后缀。
        """
        recs = self._by_name.get(name, [])
        unlocked = [r for r in recs if r["phase"] <= elite and r["level"] <= level]

        # 互斥组内取已解锁的最高阶 (L = phase*1000 + level), 不同组共存。
        # 无 gr 条目的记录各自成组 (用 full_key 作组键)。
        best: dict[str, dict] = {}  # group_id -> {"rec":..., "L":...}
        for r in unlocked:
            fk = self._full_key(r)
            group_id = self._groups.get(fk, self._fallback_group_id(r))
            rank = r["phase"] * 1000 + r["level"]
            cur = best.get(group_id)
            if cur is None or rank > cur["L"]:
                best[group_id] = {"rec": r, "L": rank}

        out: dict[str, list[Buff]] = {}
        for v in best.values():
            r = v["rec"]
            buff = Buff(
                name=r["name"], room=r["roomType"], buff_name=r["buffName"],
                phase=r["phase"], level=r["level"],
                value=self._value_of(r), desc=r.get("desc", ""),
            )
            out.setdefault(buff.room, []).append(buff)
        return out

    def rooms_available(self, name: str, elite: int, level: int) -> set[str]:
        return set(self.buffs_for(name, elite, level).keys())


if __name__ == "__main__":  # pragma: no cover
    db = SkillDB()
    for nm, e, lv in [("能天使", 2, 90), ("缇缇", 2, 90), ("丰川祥子", 2, 90), ("GALLUS²", 0, 30)]:
        print(f"\n## {nm} E{e} Lv{lv}")
        for room, buffs in db.buffs_for(nm, e, lv).items():
            for b in buffs:
                print(f"  [{ROOM_CN[room]}] {b.buff_name} = {b.value}  | {b.desc[:40]}")
