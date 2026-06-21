"""数据完整性 + 配置基线快照测试。"""
import json

from conftest import (
    ROOT,
    CONFIG,
    CONFIG_252,
    _DB,
    _prof,
)
from arknights_base_simulation.effects import parse_buff
from arknights_base_simulation.skills import Buff
from arknights_base_simulation.synergy import (
    FACTION_ALIASES,
    FACTION_EXTRA,
    FACTIONS,
    RESOURCE_NOTES,
    RES_NAMES,
    factions_of,
)

def test_data_loads():
    trade = _DB.buffs_for("能天使", 2, 90)["trading"]
    assert len(trade) == 1 and trade[0].value == 35.0
    assert len(_DB.buffs_for("缇缇", 2, 90)["workshop"]) == 2


def test_nonzero_oa_values_without_static_parser_are_intentional_special_cases():
    """OA数值非0但静态解析为空的技能必须是已审查的特殊机制。"""
    skills = json.loads((ROOT / "data" / "skills.json").read_text(encoding="utf-8"))
    values = json.loads((ROOT / "data" / "values.json").read_text(encoding="utf-8"))
    allowed = {
        ("撷英调香师", "dormitory", "净化呼吸"),
        ("响石", "meeting", "特殊渠道顾问"),
        ("斩业星熊", "control", "下城人脉"),
        ("烛煌", "control", "断金之交"),
        ("魔王", "control", "“未完的故事”"),
        ("魔王", "control", "魔王传承"),
        ("阿斯卡纶", "control", "S.W.E.E.P.主管"),
        ("跃跃", "meeting", "无辜笑脸"),
        ("摩根", "dormitory", "头号陪练"),
        ("正义骑士号", "power", "“滴滴，启动！”"),
        ("琴柳", "control", "感染力"),
    }
    missing = set()
    for rec in skills:
        key = f"{rec['name']}|{rec['roomType']}|{rec['buffName']}|{rec['phase']}|{rec['level']}"
        if abs(float(values.get(key, 0.0))) < 1e-9:
            continue
        buff = Buff(
            rec["name"],
            rec["roomType"],
            rec["buffName"],
            rec["phase"],
            rec["level"],
            float(values.get(key, 0.0)),
            rec.get("desc", ""),
        )
        if not parse_buff(buff):
            missing.add((rec["name"], rec["roomType"], rec["buffName"]))

    assert missing == allowed


def test_skill_value_tables_have_only_reviewed_key_gaps():
    """skills/values 键不应静默漂移; 缺 OA 值的技能必须靠文本规则覆盖。"""
    skills = json.loads((ROOT / "data" / "skills.json").read_text(encoding="utf-8"))
    values = json.loads((ROOT / "data" / "values.json").read_text(encoding="utf-8"))
    skill_keys = {
        f"{rec['name']}|{rec['roomType']}|{rec['buffName']}|{rec['phase']}|{rec['level']}"
        for rec in skills
    }
    value_keys = set(values)

    reviewed_missing_values = {
        "焰狐龙梓兰|control|办公室年度人物|0|1",
        "焰狐龙梓兰|trading|队长的自觉|2|1",
        "罗德岛隐秘队|dormitory|狩猎好帮手|0|1",
        "罗德岛隐秘队|meeting|隐秘专家|0|30",
        "雷狼龙S空爆|trading|气氛组|2|1",
        "雷狼龙S空爆|training|兴之所至·α|0|1",
        "雷狼龙S空爆|training|兴之所至·β|2|1",
    }

    assert value_keys - skill_keys == set()
    assert skill_keys - value_keys == reviewed_missing_values


def test_skill_value_numeric_surface_snapshot():
    """OA数值表的非零/零值分布应随 PRTS 技能快照一起受审查。"""
    skills = json.loads((ROOT / "data" / "skills.json").read_text(encoding="utf-8"))
    values = json.loads((ROOT / "data" / "values.json").read_text(encoding="utf-8"))
    nonzero_by_room = {}
    zero_by_room = {}
    nonzero_values = set()
    for rec in skills:
        key = f"{rec['name']}|{rec['roomType']}|{rec['buffName']}|{rec['phase']}|{rec['level']}"
        if key not in values:
            continue
        room = rec["roomType"]
        value = float(values[key])
        if abs(value) > 1e-9:
            nonzero_by_room[room] = nonzero_by_room.get(room, 0) + 1
            nonzero_values.add(value)
        else:
            zero_by_room[room] = zero_by_room.get(room, 0) + 1

    assert len(values) == 892
    assert len(nonzero_values) == 32
    assert nonzero_by_room == {
        "control": 77,
        "dormitory": 81,
        "hire": 43,
        "manufacture": 117,
        "meeting": 69,
        "power": 45,
        "trading": 86,
        "training": 122,
        "workshop": 87,
    }
    assert zero_by_room == {
        "control": 18,
        "dormitory": 22,
        "hire": 3,
        "manufacture": 26,
        "meeting": 18,
        "power": 3,
        "trading": 31,
        "training": 9,
        "workshop": 35,
    }


def test_skill_group_table_references_existing_multi_member_groups():
    """groups 互斥升级链必须引用真实技能, 且不应出现无意义的单成员组。"""
    skills = json.loads((ROOT / "data" / "skills.json").read_text(encoding="utf-8"))
    groups = json.loads((ROOT / "data" / "groups.json").read_text(encoding="utf-8"))
    by_skill_key = {
        f"{rec['charId']}|{rec['roomType']}|{rec['buffName']}|{rec['phase']}|{rec['level']}": rec
        for rec in skills
    }
    members_by_group = {}
    for key, group in groups.items():
        members_by_group.setdefault(group, []).append(key)

    assert len(groups) == 329
    assert len(members_by_group) == 164
    assert set(groups) - set(by_skill_key) == set()
    assert {group for group, members in members_by_group.items() if len(members) == 1} == set()
    for members in members_by_group.values():
        records = [by_skill_key[key] for key in members]
        assert len({rec["charId"] for rec in records}) == 1
        assert len({rec["roomType"] for rec in records}) == 1
        assert len({(rec["phase"], rec["level"]) for rec in records}) == len(records)


def test_manual_faction_patch_references_known_ops_and_is_loaded():
    """手动补充派系成员必须是已审查名单, 且能被 factions_of 实际读到。"""
    skills = json.loads((ROOT / "data" / "skills.json").read_text(encoding="utf-8"))
    skill_names = {rec["name"] for rec in skills}
    expected_by_faction = {
        "怪物猎人小队": {"火龙S黑角", "麒麟R夜刀", "泰拉大陆调查团"},
        "泡影国狩猎小队": {"焰狐龙梓兰", "雷狼龙S空爆", "罗德岛隐秘队"},
        "杜林族": {"至简", "桃金娘", "褐果", "杜林", "特克诺"},
        "作业平台": {"Lancet-2", "Castle-3", "THRM-EX", "正义骑士号", "Friston-3", "PhonoR-0", "CONFESS-47", "GALLUS²"},
        "行医": {"蜜莓", "桑葚", "褐果", "哈洛德", "纯烬艾雅法拉"},
        "莱欧斯小队": {"玛露西尔", "莱欧斯", "齐尔查克", "森西"},
        "骑士": {"耀骑士临光", "临光", "瑕光", "鞭刃", "焰尾", "远牙", "灰毫", "野鬃", "正义骑士号", "砾", "薇薇安娜"},
        "进攻方": {"灰烬", "闪击", "双月", "导火索"},
        "防守方": {"战车", "霜华", "艾拉", "医生"},
        "异格干员": {
            "炎狱炎熔", "寒芒克洛丝", "濯尘芙蓉", "假日威龙陈", "耀骑士临光", "归溟幽灵鲨",
            "百炼嘉维尔", "缄默德克萨斯", "纯烬艾雅法拉", "琳琅诗怀雅", "淬羽赫默", "圣约送葬人",
            "涤火杰西卡", "承曦格雷伊", "历阵锐枪芬", "新约能天使", "荒芜拉普兰德", "赤刃明霄陈",
            "怒潮凛冬", "凛御银灰", "圣聆初雪", "撷英调香师", "溯光星源", "凯尔希·思衡托",
            "雷狼龙S空爆", "火龙S黑角", "麒麟R夜刀",
        },
    }
    actual_by_faction = {}
    for name, factions in FACTION_EXTRA.items():
        for faction in factions:
            actual_by_faction.setdefault(faction, set()).add(name)

    assert actual_by_faction == expected_by_faction
    for faction, names in expected_by_faction.items():
        assert names <= skill_names
        for name in names:
            assert name in FACTIONS
            assert faction in factions_of(name)


def test_faction_aliases_reference_existing_loaded_factions():
    """派系关键词解析表必须映射到已加载且有成员的标准派系名。"""
    expected = {
        "岁": "炎-岁",
        "深海猎人": "深海猎人",
        "莱茵生命": "莱茵生命",
        "红松骑士团": "红松骑士团",
        "格拉斯哥帮": "格拉斯哥帮",
        "乌萨斯学生自治团": "乌萨斯学生自治团",
        "黑钢国际": "黑钢国际",
        "谢拉格": "谢拉格",
        "萨米": "萨米",
        "叙拉古": "叙拉古",
        "拉特兰": "拉特兰",
        "米诺斯": "米诺斯",
        "莱茵": "莱茵生命",
        "骑士": "骑士",
        "怪物猎人小队": "怪物猎人小队",
        "彩虹小队": "彩虹小队",
        "泡影国狩猎小队": "泡影国狩猎小队",
        "深海猎人干员": "深海猎人",
        "龙门近卫局": "龙门近卫局",
        "萨尔贡": "萨尔贡",
        "杜林族": "杜林族",
        "鲤氏侦探事务所": "鲤氏侦探事务所",
        "作业平台": "作业平台",
        "行医": "行医",
        "莱欧斯小队": "莱欧斯小队",
        "异格干员": "异格干员",
        "异格": "异格干员",
        "进攻方": "进攻方",
        "防守方": "防守方",
        "精英干员": "罗德岛-精英干员",
        "精英": "罗德岛-精英干员",
        "A1小队": "行动预备组A1",
    }
    loaded_factions = {faction for factions in FACTIONS.values() for faction in factions}

    assert FACTION_ALIASES == expected
    assert set(FACTION_ALIASES.values()) <= loaded_factions
    for faction in set(FACTION_ALIASES.values()):
        assert any(faction in factions for factions in FACTIONS.values())


def test_resource_pool_terms_in_skill_data_are_reviewed():
    """中间产物/资源池词表新增时必须同步更新动态解析和测试。"""
    skills = json.loads((ROOT / "data" / "skills.json").read_text(encoding="utf-8"))
    expected_counts = {
        "感知信息": 7,
        "思维链环": 3,
        "记忆碎片": 2,
        "梦境": 2,
        "人间烟火": 12,
        "情报储备": 4,
        "乌萨斯特饮": 4,
        "热情值": 7,
        "工程机器人": 3,
        "木天蓼": 4,
        "魔物料理": 4,
        "无声共鸣": 6,
        "巫术结晶": 3,
        "小节": 2,
    }
    observed = {
        term: sum(1 for rec in skills if term in rec.get("desc", ""))
        for term in expected_counts
    }
    producer_only_terms = {"梦境", "记忆碎片", "小节"}

    assert observed == expected_counts
    assert set(RES_NAMES) == set(expected_counts) - producer_only_terms
    assert all(term in RESOURCE_NOTES for term in expected_counts)


def test_skill_data_matches_prts_skill_table_surface_snapshot():
    """PRTS 后勤技能一览当前设施/技能名覆盖快照, 防止数据源缺项或重复失真。"""
    skills = json.loads((ROOT / "data" / "skills.json").read_text(encoding="utf-8"))
    unique_by_room = {}
    records_by_room = {}
    for rec in skills:
        room = rec["roomType"]
        records_by_room[room] = records_by_room.get(room, 0) + 1
        unique_by_room.setdefault(room, set()).add(rec["buffName"])
    duplicate_pair_counts = {}
    duplicate_record_surplus = {}
    for room, names in unique_by_room.items():
        for name in names:
            count = sum(1 for rec in skills if rec["roomType"] == room and rec["buffName"] == name)
            if count > 1:
                duplicate_pair_counts[room] = duplicate_pair_counts.get(room, 0) + 1
                duplicate_record_surplus[room] = duplicate_record_surplus.get(room, 0) + count - 1

    assert len(skills) == 899
    assert {room: len(names) for room, names in sorted(unique_by_room.items())} == {
        "control": 87,
        "dormitory": 83,
        "hire": 41,
        "manufacture": 105,
        "meeting": 62,
        "power": 37,
        "trading": 88,
        "training": 82,
        "workshop": 104,
    }
    assert records_by_room == {
        "control": 96,
        "dormitory": 104,
        "hire": 46,
        "manufacture": 143,
        "meeting": 88,
        "power": 48,
        "trading": 119,
        "training": 133,
        "workshop": 122,
    }
    assert duplicate_pair_counts == {
        "control": 6,
        "dormitory": 7,
        "hire": 3,
        "manufacture": 15,
        "meeting": 4,
        "power": 6,
        "trading": 11,
        "training": 17,
        "workshop": 11,
    }
    assert duplicate_record_surplus == {
        "control": 9,
        "dormitory": 21,
        "hire": 5,
        "manufacture": 38,
        "meeting": 26,
        "power": 11,
        "trading": 31,
        "training": 51,
        "workshop": 18,
    }


def test_unlock_by_elite():
    assert _DB.buffs_for("能天使", 0, 90)["trading"][0].value == 20.0


def test_skilldb_keeps_highest_unlocked_group_member_and_preserves_distinct_groups():
    """同一互斥组只取最高已解锁技能; 不同组的同设施技能必须共存。"""
    assert [b.buff_name for b in _DB.buffs_for("能天使", 2, 90)["trading"]] == ["物流专家"]
    assert [b.buff_name for b in _DB.buffs_for("能天使", 0, 90)["trading"]] == ["企鹅物流·α"]

    titti_workshop = {b.buff_name for b in _DB.buffs_for("缇缇", 2, 90)["workshop"]}
    assert titti_workshop == {"与历史对话", "修旧如旧·β"}

    durin_lv1 = {b.buff_name for b in _DB.buffs_for("杜林", 0, 1)["dormitory"]}
    durin_lv30 = {b.buff_name for b in _DB.buffs_for("杜林", 0, 30)["dormitory"]}
    assert durin_lv1 == {"慵懒"}
    assert durin_lv30 == {"嗜睡"}


def test_prts_baseline_config_tables_are_reviewed():
    """PRTS 基建基础常量应集中锁定, 避免 config 漂移只被行为测试间接发现。"""
    assert CONFIG["layout"] == {
        "production_slots": 9,
        "min_power": 1,
        "max_manufacture": 5,
        "max_trading": 5,
        "max_power": 3,
        "fixed_rooms": {"control": 1, "meeting": 1, "hire": 1, "workshop": 1, "training": 1, "dormitory": 4},
        "_comment": CONFIG["layout"]["_comment"],
    }
    assert CONFIG["control"]["slots_by_level"] == {"1": 1, "2": 2, "3": 3, "4": 4, "5": 5}
    assert CONFIG["control"]["drone_assist_min_level"] == 3

    assert CONFIG["manufacture"]["slots_by_level"] == {"1": 1, "2": 2, "3": 3}
    assert CONFIG["manufacture"]["capacity_volume_by_level"] == {"1": 24, "2": 36, "3": 54}
    assert CONFIG["manufacture"]["lines"]["作战记录"]["min_level"] == 3
    assert CONFIG["manufacture"]["lines"]["作战记录"]["base_minutes_per_item"] == 180
    assert CONFIG["manufacture"]["lines"]["作战记录"]["volume_per_item"] == 5
    assert CONFIG["manufacture"]["lines"]["赤金"]["min_level"] == 1
    assert CONFIG["manufacture"]["lines"]["赤金"]["base_minutes_per_item"] == 72
    assert CONFIG["manufacture"]["lines"]["赤金"]["volume_per_item"] == 2

    assert CONFIG["trading"]["slots_by_level"] == {"1": 1, "2": 2, "3": 3}
    assert CONFIG["trading"]["strategy"] == "gold"
    assert CONFIG["trading"]["order_limit_by_level"] == {"1": 6, "2": 8, "3": 10}
    assert CONFIG["trading"]["gold_order_by_level"] == {
        "1": {"base_minutes_per_order": 144.0, "gold_per_order": 2.0, "lmd_per_order": 1000,
              "prob_2_gold": 1.0, "prob_3_gold": 0.0, "native_4_gold_probability": 0.0},
        "2": {"base_minutes_per_order": 170.4, "gold_per_order": 2.4, "lmd_per_order": 1200,
              "prob_2_gold": 0.6, "prob_3_gold": 0.4, "native_4_gold_probability": 0.0},
        "3": {"base_minutes_per_order": 203.4, "gold_per_order": 2.9, "lmd_per_order": 1450,
              "prob_2_gold": 0.3, "prob_3_gold": 0.5, "native_4_gold_probability": 0.2},
    }
    assert CONFIG["trading"]["orundum_order"] == {
        "min_level": 3,
        "base_minutes_per_order": 120.0,
        "source_shard_per_order": 2.0,
        "orundum_per_order": 20.0,
        "_comment": CONFIG["trading"]["orundum_order"]["_comment"],
    }

    assert CONFIG["power"]["supply_per_station_by_level"] == {"1": 60, "2": 130, "3": 270}
    assert CONFIG["power"]["drone_per_hour_base"] == 10.0
    assert CONFIG["power"]["drone_cap"] == 235
    assert CONFIG["power"]["base_charge_bonus_per_operator"] == 5.0
    assert CONFIG["power"]["drone_minutes_per_drone"] == 3.0

    assert CONFIG["electricity"]["consumption_by_level"] == {
        "manufacture": {"1": 10, "2": 30, "3": 60},
        "trading": {"1": 10, "2": 30, "3": 60},
        "meeting": {"1": 10, "2": 30, "3": 60},
        "hire": {"1": 10, "2": 30, "3": 60},
        "training": {"1": 10, "2": 30, "3": 60},
        "workshop": {"1": 10, "2": 10, "3": 10},
        "dormitory": {"1": 10, "2": 20, "3": 30, "4": 45, "5": 65},
    }
    assert CONFIG["dormitory"]["max_rooms"] == 4
    assert CONFIG["dormitory"]["slots"] == 5
    assert CONFIG["meeting"]["slots_by_level"] == {"1": 2, "2": 2, "3": 2}
    assert CONFIG["meeting"]["clue_per_hour_base"] == 0.05
    assert CONFIG["meeting"]["clue_limit"] == 10
    assert CONFIG["meeting"]["daily_clue_if_staffed"] == 1.0
    assert CONFIG["meeting"]["level_bonus"] == {"1": 7.0, "2": 9.0, "3": 11.0}
    assert CONFIG["hire"]["slots_by_level"] == {"1": 1, "2": 1, "3": 1}
    assert CONFIG["hire"]["recruit_slots_by_level"] == {"1": 2, "2": 3, "3": 4}
    assert CONFIG["hire"]["contact_per_hour_base"] == 1 / 12
    assert CONFIG["hire"]["contact_limit"] == 3
    assert CONFIG["workshop"]["slots_by_level"] == {"1": 1, "2": 1, "3": 1}
    assert CONFIG["training"]["max_mastery_by_level"] == {"1": 1, "2": 2, "3": 3}
    assert CONFIG["mood"]["cap"] == 24.0
    assert CONFIG["mood"]["base_drain_per_hour"] == 1.0
    assert CONFIG["mood"]["control_drain_reduction"] == 0.05
    assert CONFIG["mood"]["occupancy_reduction"] == [0.0, 0.0, 0.05, 0.1]


def test_252_config_keeps_shared_prts_baseline_tables_in_sync():
    """252配置只应覆盖布局/等级; PRTS基础表不应与主配置漂移。"""

    def without_private_comments(value):
        if isinstance(value, dict):
            return {
                k: without_private_comments(v)
                for k, v in value.items()
                if not str(k).startswith("_comment")
            }
        if isinstance(value, list):
            return [without_private_comments(v) for v in value]
        return value

    shared_paths = (
        ("control", "slots_by_level"),
        ("control", "drone_assist_min_level"),
        ("manufacture", "slots_by_level"),
        ("manufacture", "capacity_volume_by_level"),
        ("manufacture", "lines"),
        ("trading", "slots_by_level"),
        ("trading", "order_limit_by_level"),
        ("trading", "gold_order_by_level"),
        ("trading", "orundum_order"),
        ("power", "supply_per_station_by_level"),
        ("power", "drone_per_hour_base"),
        ("power", "drone_cap"),
        ("power", "base_charge_bonus_per_operator"),
        ("power", "drone_minutes_per_drone"),
        ("electricity", "consumption_by_level"),
        ("meeting", "clue_per_hour_base"),
        ("meeting", "clue_limit"),
        ("meeting", "daily_clue_if_staffed"),
        ("meeting", "level_bonus"),
        ("hire", "contact_per_hour_base"),
        ("hire", "contact_limit"),
        ("training", "max_mastery_by_level"),
        ("mood", "cap"),
        ("mood", "base_drain_per_hour"),
        ("mood", "control_drain_reduction"),
        ("mood", "occupancy_reduction"),
    )
    for section, key in shared_paths:
        assert without_private_comments(CONFIG_252[section][key]) == without_private_comments(CONFIG[section][key])

