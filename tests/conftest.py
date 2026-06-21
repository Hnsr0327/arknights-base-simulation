"""Shared fixtures for arknights_base_simulation tests."""
import copy
import json
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from arknights_base_simulation.engine import (  # noqa: E402
    Assignment,
    Engine,
    OperatorProfile,
    Schedule,
    dorm_base_recover,
    dorm_base_recover_for_room,
)
from arknights_base_simulation.effects import parse_buff  # noqa: E402
from arknights_base_simulation.optimizer import Optimizer, build_profiles  # noqa: E402
from arknights_base_simulation.roster import Operator  # noqa: E402
from arknights_base_simulation.simulate import _dorm_recovery_bonus, _fiammetta_swap_pairs, _gap_assignment, simulate  # noqa: E402
from arknights_base_simulation.skills import Buff, SkillDB  # noqa: E402
from arknights_base_simulation.synergy import (  # noqa: E402
    FACTION_ALIASES,
    FACTION_EXTRA,
    FACTIONS,
    RESOURCE_NOTES,
    RES_NAMES,
    build_context,
    dorm_all_recover_for,
    dorm_average_recover_pool,
    dorm_single_recover_for,
    dorm_low_mood_extra_recover_for,
    dorm_target_extra_recover,
    dorm_target_extra_recover_for,
    factions_of,
)

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
CONFIG_252 = json.loads((ROOT / "data" / "non-standard-configs" / "config_252.json").read_text(encoding="utf-8"))
_DB = SkillDB()

def _prof(*names, elite=2, level=90, rarity=0):
    """Build {name: OperatorProfile(...)} dict."""
    if isinstance(rarity, dict):
        return {nm: OperatorProfile(nm, elite, level, _DB, rarity=rarity.get(nm, 0)) for nm in names}
    return {nm: OperatorProfile(nm, elite, level, _DB, rarity=rarity) for nm in names}


@contextmanager
def _override(eng, *, frac=None, mood=None, fatigued=None):
    """Temporarily set engine test overrides; restores to None on exit."""
    if frac is not None:
        eng._frac_override = frac
    if mood is not None:
        eng._mood_override = mood
    if fatigued is not None:
        eng._fatigued_override = fatigued
    try:
        yield
    finally:
        if frac is not None:
            eng._frac_override = None
        if mood is not None:
            eng._mood_override = None
        if fatigued is not None:
            eng._fatigued_override = None


def _toy_roster():
    return [
        Operator("能天使", "能天使", 6, 90, 2),
        Operator("德克萨斯", "德克萨斯", 5, 90, 2),
        Operator("缪尔赛思", "缪尔赛思", 6, 90, 2),
        Operator("食铁兽", "食铁兽", 5, 90, 2),
        Operator("夜莺", "夜莺", 6, 90, 2),
    ]

