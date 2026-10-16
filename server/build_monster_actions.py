# -*- coding: utf-8 -*-
"""从客户端 `MonsterActionMap` 生成服务端要用的「怪物动作 → 技能 id」表。

## 为什么需要这张表

服务端权威怪物 AI（方案 A）在判定帧要下发 `2411 cmdPlayerUseSkill`，其中的
`sk` 字段是**出手方用的技能 id**。前端 `BeHitManager.onServerCheckHit` →
`parseHPInfo` → `beHitRecover(attacker, defender, sk)` 会用这个 sk 去
`SkillConfig.getSkillInfoByID(sk)` 查技能，再用技能的 `hitEffect`（受击类型、
硬直时间、击退距离）来播玩家的受击表现；查不到会 **直接 return**（`BeHitManager.as:239-243`）。

怪物的技能 id 不是随便给的数字，而是 `MonsterActionMap` 里
`(monsterId, actionKey) → value` 的 value。依据：

```as3
// CMAIAttackState.doAttackAction
_loc1_ = MonsterActionMap.getMonsterActionInfo(_ai.host.mInfo.monsterId, _stateParam.skillInfo.actionKey);
_loc2_ = SkillConfig.instance.getSkillInfoByID(_loc1_.skillId);
CMActionController.instance.doMonsterSkill(_ai.host, _loc2_);
SkillHitClient.instance.monsterSkillHit(_loc2_.skillId, _ai.host.cId, _ai.host.cId, 4);
```

`MonsterActionMap.init()` 读的 XML 形如：

```xml
<monster id="1001,13001,..." typeID="1000">
  <action key="attack1" value="346"/>
</monster>
```

即 `monsterInfoData[id][key] = MonsterActionInfo(key, value, effects, frames)`
⇒ **value 就是 skillId**。

## 输入 / 输出

输入（按优先级）：
1. `04-data-json/game-data/01-skills/MonsterActionMap_dataClass.xml`  ← 原始 XML，首选
2. `04-data-json/raw-json/07_monster_action_map.json`                ← 已解析的兜底

输出：`server/data/monster_actions.json`（入库，运行时由 `monster_action.py` 读）

```json
{
  "_source": "...",
  "monsterCount": 3000,
  "monsters": {
    "3001": {"stand": 451, "walk": 452, "run": 453, "attack1": 455, ...},
    ...
  }
}
```

用法：
    python -m server.build_monster_actions      # 在工程根目录执行
"""
from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

#: 原始 XML（客户端 skillconfig SWF 内嵌的 monster-action.xml）
XML_CANDIDATES: List[str] = [
    os.path.join(ROOT_DIR, "04-data-json", "game-data", "01-skills",
                 "MonsterActionMap_dataClass.xml"),
]

#: 已解析成 JSON 的兜底来源（结构：{"entities": {"<id列表>": {"actions": {...}}}}）
JSON_CANDIDATES: List[str] = [
    os.path.join(ROOT_DIR, "04-data-json", "raw-json", "07_monster_action_map.json"),
]

OUT_PATH = os.path.join(os.path.dirname(__file__), "data", "monster_actions.json")


def parse_xml(path: str) -> Dict[int, Dict[str, int]]:
    """解析 `MonsterActionMap_dataClass.xml` → {mId: {actionKey: skillId}}。"""
    tree = ET.parse(path)
    monsters: Dict[int, Dict[str, int]] = {}
    for node in tree.getroot().findall("monster"):
        id_attr = node.get("id") or ""
        actions: Dict[str, int] = {}
        for act in node.findall("action"):
            key = act.get("key")
            value = act.get("value")
            if not key or value is None:
                continue
            try:
                actions[key] = int(value)
            except ValueError:
                continue
        if not actions:
            continue
        for raw_id in id_attr.split(","):
            raw_id = raw_id.strip()
            if not raw_id:
                continue
            try:
                monsters[int(raw_id)] = dict(actions)
            except ValueError:
                continue
    return monsters


def parse_json(path: str) -> Dict[int, Dict[str, int]]:
    """解析 `07_monster_action_map.json`（已解析版）→ 同样的结构。"""
    with open(path, "r", encoding="utf-8") as fp:
        doc = json.load(fp)
    monsters: Dict[int, Dict[str, int]] = {}
    for group_key, group in (doc.get("entities") or {}).items():
        actions_raw = (group or {}).get("actions") or {}
        actions: Dict[str, int] = {}
        for key, value in actions_raw.items():
            try:
                actions[str(key)] = int(value)
            except (TypeError, ValueError):
                continue
        if not actions:
            continue
        for raw_id in str(group_key).split(","):
            raw_id = raw_id.strip()
            if not raw_id:
                continue
            try:
                monsters[int(raw_id)] = dict(actions)
            except ValueError:
                continue
    return monsters


def build() -> int:
    monsters: Dict[int, Dict[str, int]] = {}
    source = ""
    for path in XML_CANDIDATES:
        if os.path.isfile(path):
            monsters = parse_xml(path)
            source = os.path.relpath(path, ROOT_DIR).replace("\\", "/")
            break
    if not monsters:
        for path in JSON_CANDIDATES:
            if os.path.isfile(path):
                monsters = parse_json(path)
                source = os.path.relpath(path, ROOT_DIR).replace("\\", "/")
                break

    if not monsters:
        print("[X] 找不到 MonsterActionMap 数据源，检查以下路径：")
        for path in XML_CANDIDATES + JSON_CANDIDATES:
            print("    -", path)
        return 1

    attack_total = sum(1 for a in monsters.values() if any(k.startswith("attack") for k in a))
    payload: Dict[str, Any] = {
        "_source": source,
        "monsterCount": len(monsters),
        "attackCapableCount": attack_total,
        "monsters": {str(mid): acts for mid, acts in sorted(monsters.items())},
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, separators=(",", ":"))
    print("[OK] %d 只怪物动作表 → %s（其中 %d 只有 attack* 技能）"
          % (len(monsters), os.path.relpath(OUT_PATH, ROOT_DIR), attack_total))
    print("     来源: %s" % source)

    # 抽查几只，便于人工核对
    for mid in (3001, 37401, 36101, 36202):
        if mid in monsters:
            acts = monsters[mid]
            print("     抽查 mId=%d: %s" % (mid, {k: v for k, v in acts.items()
                                                 if k.startswith(("attack", "stand", "run"))}))
    return 0


if __name__ == "__main__":
    sys.exit(build())
