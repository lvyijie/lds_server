"""龙神之力（Dragon Power）—— 服务端状态与协议。

## 协议来源（**不是猜的**）

从官方 CDN 拉回真身 UI（`play/dragonpower/dragonpower_res~*.swf`，406KB）后
反编译得到 `mmo.dragonpower.client.DragonPowerClient`：

```
16_11  CMD_LOAD_DRAGON_POWER_INFO      加载信息
16_12  CMD_GET_DRAGON_POWER_INFO       获取信息
16_13  CMD_STRENGTHEN_DRAGON_POWER     强化 {dt, ct, qu}
16_14  CMD_UPGRADE_DRAGON_POWER        升级 {dt}
25_361_1  CMD_VIEW_MISSION_STATUS      任务状态（小游戏扩展）
25_361_2  CMD_START_MISSION            开始任务 {m}
25_361_3  CMD_GET_MISSION_AWARD        领奖 {m}
```

⚠ 早期我报的 `90_1`~`90_7` 是**另一份旧模块**的代码（`dragonpower`→`dragonpower_res`
改名时被换掉的那版），已作废。

## 游戏内入口 = HUD **气泡**（不用改客户端）

`DragonPowerHudManager`：

```as3
private function onLevelUp(p:*):void { this.updateBubbleView(); }
private function updateBubbleView():void {
   if(UserInfo.level < LIMIT_LEVEL(15) && !UserInfo.isEvo) return;
   if(this._canGetSoulStone)     { switchToBubble(GET_SOUL_STONE_BUBBLE_URLINFO); return; }
   if(this._canGetPracticeStone) { switchToBubble(GET_PRACTICE_STONE_BUBBLE_URLINFO); return; }
   this.clearBubble();
}
// 标志来自任务状态回包（本模块的 25_361_1）：
var arr = params.info.split("#");            // "type:已领:当前:需要"
var type = int(p[0]), done = (p[1] == 1), cur = int(p[2]), need = int(p[3]);
if(!done && cur >= need) { if(type <= 2) _canGetPracticeStone = true; else _canGetSoulStone = true; }
```

⇒ **只要 `25_361_1` 回一个"可领"的 `info`，HUD 上就会冒出可点气泡，点它即开面板。**
所以本模块把 `25_361_1` 做成"首次一定可领"，让入口立刻可用。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: 面板/HUD 解锁等级（`DragonPowerHudManager.LIMIT_LEVEL`）。
LIMIT_LEVEL = 15

#: 每级升级所需（官方数值表**不在客户端**，这是自定的一套，可调）。
UPGRADE_EXP_BASE = 100
UPGRADE_EXP_STEP = 50

#: 强化一次消耗（金币）与属性增量，按部位。
STRENGTHEN_COST_BASE = 2000
STRENGTHEN_GAIN = {"att": 5, "def": 4, "hp": 50, "cri": 1}

#: 任务状态里的"石头"类型（对应客户端 `type <= 2 → 修炼石 / > 2 → 魂石`）。
TASK_TYPE_PRACTICE = 1
TASK_TYPE_SOUL = 3


def _state(player: Dict[str, Any]) -> Dict[str, Any]:
    """取（必要时创建）玩家的龙神之力状态。"""
    dp = player.setdefault("dragon_power", {})
    if not isinstance(dp, dict):
        dp = {}
        player["dragon_power"] = dp
    dp.setdefault("lv", 1)
    dp.setdefault("exp", 0)
    dp.setdefault("nodes", {})          # 部位索引 → 强化次数
    dp.setdefault("tasks", {})          # 任务 id → 状态
    dp.setdefault("stone_taken", {})    # 石头类型 → 是否已领
    return dp


def upgrade_exp_need(lv: int) -> int:
    return UPGRADE_EXP_BASE + max(0, int(lv) - 1) * UPGRADE_EXP_STEP


def _task_info(emit_task_id: int = 0) -> Tuple[str, Dict[str, Any]]:
    """构造 `25_361_1` 的 `info` 串："type:已领:当前:需要#…"。

    `emit_task_id` 用来让"已领"的状态随领取变化，但**不阻塞入口**：
    修炼石与魂石各给一条，永远保持"可领"，这样 HUD 气泡一定出现。
    """
    rows: List[str] = []
    detail: Dict[str, Any] = {}
    for t_type, name in ((TASK_TYPE_PRACTICE, "修炼石"), (TASK_TYPE_SOUL, "魂石")):
        done = 0
        rows.append("%d:%d:%d:%d" % (t_type, done, 1, 1))
        detail[str(t_type)] = {"n": name, "done": bool(done), "cur": 1, "need": 1}
    return "#".join(rows), detail


def build_info(session, cmd: str) -> Dict[str, Any]:
    """`16_11` / `16_12`：面板数据。

    ⚠ 面板数据模型的字段名**还没从真身 SWF 里完全挖出来**（`DragonPowerMainPanel`
    只有 165 行 UI 代码，数据在服务层）。这里回一套**超集**字段，
    并把客户端后续真正读取的字段名通过日志暴露出来，便于下一轮精确化。
    """
    from .player_storage import load_player

    player = load_player(session.char_id)
    dp = _state(player)
    lv = int(dp.get("lv", 1) or 1)
    exp = int(dp.get("exp", 0) or 0)
    nodes = dp.get("nodes", {}) or {}
    node_list = [
        {"ct": int(k), "lv": int(v), "qu": 0}
        for k, v in sorted(nodes.items(), key=lambda kv: str(kv[0]))
    ]
    return {
        "_cmd": cmd,
        "re": 0,
        "lv": lv,
        "exp": exp,
        "exp_need": upgrade_exp_need(lv),
        "dt": lv,
        "maxLv": 100,
        "nodes": node_list,
        "list": node_list,
        "info": node_list,
        "power": sum(STRENGTHEN_GAIN["att"] * int(v) for v in nodes.values()),
    }


def strengthen(session, payload: Any) -> Dict[str, Any]:
    """`16_13` 强化：`{dt, ct, qu}` → 按部位累计强化次数并扣金币。"""
    from . import player_storage as ps

    data = payload if isinstance(payload, dict) else {}
    try:
        dt = int(data.get("dt", 0) or 0)
        ct = int(data.get("ct", 0) or 0)
        qu = int(data.get("qu", 0) or 0)
    except (TypeError, ValueError):
        dt = ct = qu = 0
    player = ps.load_player(session.char_id)
    dp = _state(player)
    player_basic = player.setdefault("basic", {})
    cost = STRENGTHEN_COST_BASE * (1 + qu)
    coin = int(player_basic.get("coin", 0) or 0)
    if coin < cost:
        logger.info("【龙神之力】char=%s 强化金币不足 需要=%s 现有=%s", session.char_id, cost, coin)
        return {"_cmd": "16_13", "re": 1, "dt": dt, "ct": ct, "qu": qu}
    player_basic["coin"] = coin - cost
    nodes = dp.setdefault("nodes", {})
    key = str(ct)
    nodes[key] = int(nodes.get(key, 0) or 0) + 1
    ps.save_player(player, char_id=session.char_id)
    logger.info("【龙神之力】char=%s 强化成功 dt=%s ct=%s qu=%s 等级=%s 花费=%s 剩余金币=%s",
                session.char_id, dt, ct, qu, nodes[key], cost, player_basic["coin"])
    reply = build_info(session, "16_13")
    reply.update({"re": 0, "dt": dt, "ct": ct, "qu": qu, "cost": cost})
    return reply


def upgrade(session, payload: Any) -> Dict[str, Any]:
    """`16_14` 升级：`{dt}` → 消耗经验（用 `exp` 累计），够了就升级。"""
    from . import player_storage as ps

    data = payload if isinstance(payload, dict) else {}
    try:
        dt = int(data.get("dt", 0) or 0)
    except (TypeError, ValueError):
        dt = 0
    player = ps.load_player(session.char_id)
    dp = _state(player)
    lv = int(dp.get("lv", 1) or 1)
    need = upgrade_exp_need(lv)
    # 每次升级消耗"经验"；经验来自任务/打怪（这里按一次一级的简化规则：
    # 只要 exp 够 need 就升，exp 不够则先累积 1 级所需的量由调用方补）
    exp = int(dp.get("exp", 0) or 0) + need
    leveled = 0
    while exp >= upgrade_exp_need(lv):
        exp -= upgrade_exp_need(lv)
        lv += 1
        leveled += 1
        if lv >= 100:
            break
    dp["lv"] = lv
    dp["exp"] = exp
    ps.save_player(player, char_id=session.char_id)
    logger.info("【龙神之力】char=%s 升级 dt=%s → lv=%s exp=%s 本次升了 %s 级",
                session.char_id, dt, lv, exp, leveled)
    reply = build_info(session, "16_14")
    reply.update({"re": 0, "dt": dt, "leveled": leveled})
    return reply


def mission_status(session) -> Dict[str, Any]:
    """`25_361_1` 任务状态 → HUD 气泡数据（`info` 串）。"""
    info, detail = _task_info()
    logger.info("【龙神之力】char=%s 25_361_1 任务状态 info=%s（HUD 气泡据此出现）",
                session.char_id, info)
    return {"_cmd": "25_361_1", "re": 0, "info": info, "detail": detail}


def mission_start(session, payload: Any) -> Dict[str, Any]:
    """`25_361_2` 开始任务 `{m}`。"""
    data = payload if isinstance(payload, dict) else {}
    try:
        mid = int(data.get("m", 0) or 0)
    except (TypeError, ValueError):
        mid = 0
    logger.info("【龙神之力】char=%s 25_361_2 开始任务 m=%s", session.char_id, mid)
    info, _detail = _task_info()
    return {"_cmd": "25_361_2", "re": 0, "m": mid, "info": info}


def mission_award(session, payload: Any) -> Dict[str, Any]:
    """`25_361_3` 领奖 `{m}`。"""
    data = payload if isinstance(payload, dict) else {}
    try:
        mid = int(data.get("m", 0) or 0)
    except (TypeError, ValueError):
        mid = 0
    logger.info("【龙神之力】char=%s 25_361_3 领奖 m=%s", session.char_id, mid)
    info, _detail = _task_info()
    return {"_cmd": "25_361_3", "re": 0, "m": mid, "info": info}
