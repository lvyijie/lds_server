# -*- coding: utf-8 -*-
"""怪物动作表运行时加载器（服务端权威怪物 AI 用）。

只做两件事：
1. 读 `server/data/monster_actions.json`（由 `server/build_monster_actions.py` 生成）
2. 提供 `get_attack_skills(monster_id)` → `{"attack1": 455, "attack2": ...}`

背景见 `build_monster_actions.py` 的模块注释：怪物的 `attack*` 动作 value
**就是 skillId**，2411 的 `sk` 字段必须填它，否则前端
`BeHitManager.beHitRecover` 在 `SkillConfig.getSkillInfoByID(sk)` 查不到会直接
return ⇒ 玩家没有受击表现。

加载失败时**不抛异常**：返回空表并告警。缺表只会让 2411 的 `sk` 退化，
不至于把整个网关拖崩。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "monster_actions.json")

#: {mId: {actionKey: skillId}}
_ACTIONS: Optional[Dict[int, Dict[str, int]]] = None


def _load() -> Dict[int, Dict[str, int]]:
    """惰性载入一次；进程内缓存。"""
    global _ACTIONS
    if _ACTIONS is not None:
        return _ACTIONS
    _ACTIONS = {}
    try:
        with open(_DATA_PATH, "r", encoding="utf-8") as fp:
            doc = json.load(fp)
    except (OSError, ValueError) as exc:
        logger.warning("载入怪物动作表失败(%s): %s；2411 的 sk 将退化为 0",
                       _DATA_PATH, exc)
        return _ACTIONS

    monsters = (doc or {}).get("monsters") or {}
    for raw_mid, acts in monsters.items():
        try:
            mid = int(raw_mid)
        except (TypeError, ValueError):
            continue
        clean: Dict[str, int] = {}
        for key, value in (acts or {}).items():
            try:
                clean[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        if clean:
            _ACTIONS[mid] = clean
    logger.info("载入怪物动作表: %d 只怪物（来源 %s）",
                len(_ACTIONS), (doc or {}).get("_source") or _DATA_PATH)
    return _ACTIONS


def get_actions(monster_id: Any) -> Dict[str, int]:
    """取某只怪物的全部动作（`{actionKey: 技能id}`）；取不到返回空字典。"""
    try:
        mid = int(monster_id)
    except (TypeError, ValueError):
        return {}
    return _load().get(mid) or {}


def get_attack_skills(monster_id: Any) -> Dict[str, int]:
    """只取 `attack*` 动作，按 key 排序（attack1 < attack2 ...）⇒ 出手顺序稳定。"""
    actions = get_actions(monster_id)
    return {k: actions[k] for k in sorted(actions) if k.startswith("attack")}


def get_skill_id(monster_id: Any, action_key: str) -> Optional[int]:
    """取某个动作对应的技能 id；取不到返回 None。"""
    return get_actions(monster_id).get(action_key)
