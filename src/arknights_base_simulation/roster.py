"""干员练度表解析。

读取形如 干员练度表.xlsx 的表格, 产出每个 *已招募* 干员的练度信息
(精英化等级 / 等级 / 潜能 / 各技能专精), 供技能解锁判定使用。

表头 (列顺序固定):
    干员名称, 是否已招募, 星级, 等级, 精英化等级, 潜能等级,
    通用技能等级, 1技能专精等级, 2技能专精等级, 3技能专精等级,
    χ分支模组, γ分支模组, Δ分支模组, α分支模组
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# 阿米娅的职业分支在练度表里写成 "阿米娅（医疗）" / "阿米娅（近卫）",
# 基建技能数据里只有 "阿米娅"。归一化到基建技能用的名字。
NAME_ALIASES = {
    "阿米娅（医疗）": "阿米娅",
    "阿米娅（近卫）": "阿米娅",
    "阿米娅(医疗)": "阿米娅",
    "阿米娅(近卫)": "阿米娅",
}


@dataclass
class Operator:
    """一个已招募干员的练度。"""

    name: str            # 归一化后的干员名 (与基建技能库一致)
    raw_name: str        # 练度表里的原始名
    rarity: int          # 星级
    level: int           # 等级
    elite: int           # 精英化等级 0/1/2
    potential: int = 1   # 潜能等级
    skill_levels: tuple[int, int, int] = (0, 0, 0)  # 三个技能专精等级
    modules: dict[str, int] = field(default_factory=dict)

    def __repr__(self) -> str:  # pragma: no cover - 仅调试
        return f"Operator({self.name}, E{self.elite}, Lv{self.level})"


def _as_int(value, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def load_roster(xlsx_path: str | Path) -> list[Operator]:
    """解析练度表, 返回已招募干员列表。

    需要 openpyxl。只保留 ``是否已招募`` 为真的行。
    """
    import openpyxl

    wb = openpyxl.load_workbook(str(xlsx_path), read_only=True, data_only=True)
    ws = wb.active

    operators: list[Operator] = []
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:  # 表头
            continue
        if not row or not row[0]:
            continue
        raw_name = str(row[0]).strip()
        recruited = bool(row[1]) if len(row) > 1 else True
        if not recruited:
            continue

        name = NAME_ALIASES.get(raw_name, raw_name)
        modules = {}
        if len(row) >= 14:
            for key, idx in (("χ", 10), ("γ", 11), ("Δ", 12), ("α", 13)):
                lvl = _as_int(row[idx])
                if lvl:
                    modules[key] = lvl

        operators.append(
            Operator(
                name=name,
                raw_name=raw_name,
                rarity=_as_int(row[2], 1),
                level=_as_int(row[3], 1),
                elite=_as_int(row[4], 0),
                potential=_as_int(row[5], 1) or 1,
                skill_levels=(
                    _as_int(row[7]),
                    _as_int(row[8]),
                    _as_int(row[9]),
                ),
                modules=modules,
            )
        )

    wb.close()
    return operators


if __name__ == "__main__":  # pragma: no cover
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent.parent / "data" / "干员练度表.xlsx")
    ops = load_roster(path)
    print(f"已招募 {len(ops)} 名干员")
    for op in ops[:15]:
        print(" ", op)
