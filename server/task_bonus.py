"""任务奖励发放 —— 用**官方 `TaskBonus` 表**（`server/data/task_bonus.json`）。

## 为什么要有它

任务定义（`TaskConfig.as` / `task_catalog.json`）里**没有奖励字段**；
官方把奖励放在另一张表里，客户端 `TaskBonusReader` 读的就是它。
我们原来完成任务只改 `tasks[...]` 状态、**什么都不发** ⇒
用户实测：「无法获得金币 貌似我的金币总是0」✓ 根因就在这。

## 词义（`TaskBonus.parseData`，官方实现）

* `100:sub:value` → 金币
* `102:10:value` → 宠物经验；其他 `102:sub:value` → **人物经验**
* 其余 → 额外奖励 `type:sub:value`，本模块按下面的规则入库：

| type | 含义 | 处理 |
|---|---|---|
| `105` | 技能点 | `skills.sp += value`，下发 `2504` |
| `13` | **服饰**（转职套装，如 106 的 100281-100285、227 的 100731-100735） | `add_cloth` + `12_3_1` 刷新衣柜 |
| 其他（`2` / `50` / `109` / `110` …） | 未完全确定的道具类 | 尽力 `add_bag_item` 进背包，失败就**只记日志不报错** |

⚠ 最后一类**没有官方字段说明**（客户端只把它们当"额外奖励"图标显示），
所以按"进背包"处理并在日志里写明，便于以后按实测修正。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BONUS_PATH = Path(__file__).resolve().parent / "data" / "task_bonus.json"

try:
    TASK_BONUS: Dict[str, Dict[str, Any]] = json.loads(BONUS_PATH.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:  # pragma: no cover - 数据缺失时降级
    logger.exception("无法加载官方任务奖励表 %s", BONUS_PATH)
    TASK_BONUS = {}

#: 额外奖励类型码。
BONUS_TYPE_SKILL_POINT = 105      # 技能点
BONUS_TYPE_CLOTH = 13             # 服饰（转职套装）

#: 已知的"额外奖励"类型码 → 便于日志说明（未列出的按背包道具处理）。
_BONUS_TYPE_HINT = {
    2: "道具",
    50: "道具/装备",
    105: "技能点",
    109: "道具",
    110: "道具/技能相关",
    13: "服饰",
}


def get_task_bonus(task_id: Any) -> Optional[Dict[str, Any]]:
    return TASK_BONUS.get(str(task_id))


def bonus_summary(task_id: Any) -> str:
    """给日志用的一行摘要。"""
    bonus = get_task_bonus(task_id)
    if not bonus:
        return "无官方奖励记录"
    parts = []
    if bonus.get("money"):
        parts.append("金币 %d" % bonus["money"])
    if bonus.get("human_exp"):
        parts.append("经验 %d" % bonus["human_exp"])
    if bonus.get("bottle_exp"):
        parts.append("宠物经验 %d" % bonus["bottle_exp"])
    for kind, sub, value in bonus.get("ext") or []:
        parts.append("%s:%s:%s(%s)" % (kind, sub, value, _BONUS_TYPE_HINT.get(int(kind), "道具")))
    return " + ".join(parts) if parts else "无官方奖励记录"


def grant_task_bonus(session, task_id: Any) -> Optional[Dict[str, Any]]:
    """按官方表给玩家发任务奖励；没有该任务的记录时返回 None。

    `session` 需要提供 `char_id` / `send_ext` / `grant_dungeon_reward`。
    """
    bonus = get_task_bonus(task_id)
    if not bonus:
        return None

    from . import player_storage as ps

    money = int(bonus.get("money") or 0)
    human_exp = int(bonus.get("human_exp") or 0)
    bottle_exp = int(bonus.get("bottle_exp") or 0)
    granted: Dict[str, Any] = {
        "money": money, "human_exp": human_exp, "bottle_exp": bottle_exp,
        "sp": 0, "clothes": [], "items": [], "bottle_exp_note": None,
    }

    # ── 金币 + 人物经验：复用副本奖励那套下发（2141 / 8_1 / 属性同步 / 升级事件）──
    if money or human_exp:
        try:
            session.grant_dungeon_reward(human_exp, money)
        except Exception:  # pragma: no cover - 不让发奖失败打断任务流程
            logger.exception("任务奖励：经验/金币发放失败 task=%s", task_id)

    # ── 额外奖励 ──────────────────────────────────────────────────────────
    bag_updates: List[Dict[str, Any]] = []
    sp_gain = 0
    for entry in bonus.get("ext") or []:
        try:
            kind, _sub, value = int(entry[0]), int(entry[1]), int(entry[2])
        except (TypeError, ValueError, IndexError):
            continue
        item_id = int(_sub)
        count = max(1, int(value))
        if kind == BONUS_TYPE_SKILL_POINT:
            sp_gain += count
            continue
        # ★ `kind == 1`（觉醒任务 509 用的类型码）发的就是转职圣衣：
        #   `800020`–`800025` 在主服装表里是 `type=101` 套装圣衣，
        #   `job` 形如 `[基础职业, 觉醒职业]`。
        #   旧逻辑只认 `kind==13` 是服饰，`kind==1` 全走背包分支，
        #   于是圣衣被当成**装备**发出去；装备表里同 id 是别的职业的装备
        #   （`800021` = 陨石魔衣 `job=6,56`）⇒ 弓箭手穿不上（`res=14`）。
        #   现在按「服装表里是不是圣衣」判定，与任务表里的 kind 解耦。
        cloth_meta = ps.get_cloth_meta(item_id)
        is_cloth = (
            kind == BONUS_TYPE_CLOTH
            or (cloth_meta is not None and int(cloth_meta.get("type") or 0) == 101)
        )
        if is_cloth:
            try:
                ps.add_cloth(session.char_id, item_id)
                granted["clothes"].append(item_id)
            except Exception:
                logger.exception("任务奖励：服饰入库失败 task=%s cloth=%s", task_id, item_id)
            continue
        # 其余类型：按背包道具尽力入库
        try:
            entry_obj = ps.add_bag_item(session.char_id, item_id, count)
            if isinstance(entry_obj, dict):
                bag_updates.append(entry_obj)
            granted["items"].append((kind, item_id, count))
        except Exception as exc:
            logger.warning("任务奖励：道具入库失败（类型码未确定）task=%s %s:%s:%s -> %s",
                           task_id, kind, item_id, count, exc)

    if sp_gain:
        try:
            player = ps.load_player(session.char_id)
            skills = player.setdefault("skills", {"sp": 100, "list": []})
            skills["sp"] = max(0, int(skills.get("sp", 100) or 0)) + sp_gain
            ps.save_player(player, char_id=session.char_id)
            granted["sp"] = sp_gain
            session.send_ext({"_cmd": "2504", "sp": int(skills["sp"])})
        except Exception:
            logger.exception("任务奖励：技能点发放失败 task=%s", task_id)

    if bag_updates:
        try:
            session.send_ext({"_cmd": "8_0", "up": bag_updates})
        except Exception:
            logger.exception("任务奖励：背包刷新下发失败 task=%s", task_id)

    if granted["clothes"]:
        try:
            _send_clothes_refresh(session)
        except Exception:
            logger.exception("任务奖励：衣柜刷新下发失败 task=%s", task_id)

    if bottle_exp:
        # ⚠ 宠物经验(`102:10`)需要"给哪只宠物"的上下文，任务奖励里没有 ⇒ 只记日志。
        granted["bottle_exp_note"] = "宠物经验 %d 未发放（缺少目标宠物）" % bottle_exp
        logger.info("任务奖励：宠物经验 %d 未发放（表里没有指定宠物）task=%s", bottle_exp, task_id)

    logger.info("【任务奖励】char=%s task=%s 发放：%s（服饰 %s / 道具 %s / 技能点 %s）",
                getattr(session, "char_id", "?"), task_id, bonus_summary(task_id),
                granted["clothes"], granted["items"], granted["sp"])
    return granted


def _send_clothes_refresh(session) -> None:
    """下发 `12_3_1`（已拥有服饰列表），与 `12_3_1` 处理器同一套字段。"""
    from .player_storage import get_cloth_expiry, get_owned_clothes, get_wearing_clothes

    owned = get_owned_clothes(session.char_id)
    wearing = set(get_wearing_clothes(session.char_id))
    res_parts = [
        "%s_%d_0__1791561600000_%s__" % (
            cloth_id, 1 if cloth_id in wearing else 0,
            get_cloth_expiry(session.char_id, cloth_id))
        for cloth_id in owned
    ]
    from .player_storage import load_player

    session.send_ext({
        "_cmd": "12_3_1",
        "cid": float(session.char_id),
        "res": ",".join(res_parts),
        "cv": int(load_player(session.char_id).get("bag", {}).get("cv", 0)),
    })
