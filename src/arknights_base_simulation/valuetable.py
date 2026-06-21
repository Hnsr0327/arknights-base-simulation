"""从一图流『物品价值表.xlsx』载入素材的等效理智, 映射到本模拟用到的产物键。

表头: 物品id, 物品名称, 等效理智, 物品稀有度。按 *物品id* 取值(id 比名称稳定)。
其中『经验』由中级作战记录(id 2003, 1000经验/件)折算: 经验/点 = 等效理智 / 1000。
"""
from __future__ import annotations

from pathlib import Path

# 本模拟产物键 -> (物品id, 每件包含的产物单位数)
# 注: 无人机不在此映射 —— 它是"加速生产"而非可售素材, 价值已并入制造/贸易线(见 engine 无人机加速),
# config.material_values_ap.无人机 仅作历史保留, 不参与估值。
ID_MAP = {
    "龙门币": ("4001", 1),
    "赤金": ("3003", 1),
    "源石碎片": ("30061", 1),     # 源石碎片 (Originium Shard); 注意 30011 是固源岩, 勿混
    "招募许可": ("7001", 1),       # 表中名为『招聘许可』
    "经验": ("2003", 1000),        # 中级作战记录 = 1000 经验
    "合成玉": ("4003", 1),
    "技巧概要": ("3303", 1),       # 技巧概要·卷3 (若存在)
}


def load_value_table(xlsx_path: str | Path) -> dict[str, float]:
    """返回 {产物键: 理智/单位}, 只包含表中实际查到的项。"""
    import openpyxl

    wb = openpyxl.load_workbook(str(xlsx_path), read_only=True, data_only=True)
    ws = wb.active
    by_id: dict[str, float] = {}
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        if i == 0 or not row or row[0] is None:
            continue
        try:
            by_id[str(row[0])] = float(row[2])
        except (TypeError, ValueError):
            continue
    wb.close()

    out: dict[str, float] = {}
    for key, (item_id, per_item) in ID_MAP.items():
        if item_id in by_id:
            out[key] = by_id[item_id] / per_item
    return out


if __name__ == "__main__":  # pragma: no cover
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parent.parent.parent / "data" / "物品价值表.xlsx")
    for k, v in load_value_table(path).items():
        print(f"{k:10} {v}")
