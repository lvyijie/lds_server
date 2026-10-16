# -*- coding: utf-8 -*-
"""场景可行走掩码的运行时查表 + 坐标吸附（副本刷怪用）。

掩码由 `server/build_scene_walkable.py` 从客户端场景 SWF 的 `obstruction` 层生成，
规则与客户端 `AbstractScene.setMapArray()` **逐字一致**（R>127 且不透明 = 可走）。

## 为什么刷怪要吸附

副本怪坐标来自官方抓包，但**官方是服务端驱动怪物的**（2451 直接让怪走），
无视可行走网格。本工程改成了客户端自管怪（`CMAIChaseState.chasingByAStar` →
`PathFinder`），**起点落在不可走格就寻不出路径 ⇒ 怪永远卡住**：玩家走不到、
宠物够不到、本幕永远清不空 ⇒ 传送门不开。

实测 `fubennbuolanone` 的 `(49,31)` 落在障碍区（周围 7×7 全阻），4 个难度都用了它。

## 缺表时的行为

找不到该场景的掩码时**不吸附、原样返回**（并只告警一次）——
不能让"没构建掩码"把服务端搞崩，只是退化成旧行为。
"""
from __future__ import annotations

import base64
import json
import logging
import os
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "scene_walkable.json")

#: 掩码的网格边长（客户端 `AbstractSceneHelper.getGridSize()` = 8）
GRID_SIZE = 8

#: {场景名: (cols, rows, bytes)}，惰性载入一次
_TABLE: Optional[Dict[str, Tuple[int, int, bytes]]] = None
_WARNED: set = set()


def _load() -> Dict[str, Tuple[int, int, bytes]]:
    global _TABLE
    if _TABLE is not None:
        return _TABLE
    _TABLE = {}
    try:
        with open(_DATA_PATH, "r", encoding="utf-8") as fp:
            doc = json.load(fp)
    except (OSError, ValueError) as exc:
        logger.warning("载入场景可行走掩码失败(%s): %s；刷怪坐标将不做吸附",
                       _DATA_PATH, exc)
        return _TABLE
    for sn, rec in ((doc or {}).get("scenes") or {}).items():
        try:
            cols = int(rec["w"])
            rows = int(rec["h"])
            bits = base64.b64decode(rec["mask"])
        except (KeyError, TypeError, ValueError):
            continue
        _TABLE[str(sn)] = (cols, rows, bits)
    logger.info("载入场景可行走掩码: %d 个场景", len(_TABLE))
    return _TABLE


def has_mask(scene_name: str) -> bool:
    return str(scene_name) in _load()


def is_walkable(scene_name: str, x: int, y: int) -> Optional[bool]:
    """返回 True/False；该场景没有掩码时返回 **None**（未知，不判）。"""
    rec = _load().get(str(scene_name))
    if rec is None:
        sn = str(scene_name)
        if sn not in _WARNED:
            _WARNED.add(sn)
            logger.warning("场景 %s 没有可行走掩码（未构建？），刷怪坐标不做吸附", sn)
        return None
    cols, rows, bits = rec
    c, r = int(x), int(y)
    if c < 0 or r < 0 or c >= cols or r >= rows:
        return False
    i = c * rows + r
    return bool((bits[i >> 3] >> (i & 7)) & 1)


def snap(scene_name: str, x: int, y: int, max_radius: int = 24
         ) -> Tuple[int, int, Optional[float]]:
    """把坐标吸附到最近的可走格。

    返回 `(new_x, new_y, distance)`；坐标已可走时 distance=0。
    该场景没有掩码、或 `max_radius` 内找不到可走格时，原样返回、distance=None。

    搜索方式是**按半径环形扩张**（先近后远），所以结果就是最近的可走格
    （欧氏距离意义下近似；同环内按「先列后行」取第一个）。
    """
    ok = is_walkable(scene_name, x, y)
    if ok is None:
        return int(x), int(y), None
    if ok:
        return int(x), int(y), 0.0

    rec = _load().get(str(scene_name))
    if rec is None:
        return int(x), int(y), None
    cols, rows, bits = rec

    def walkable(c: int, r: int) -> bool:
        if c < 0 or r < 0 or c >= cols or r >= rows:
            return False
        i = c * rows + r
        return bool((bits[i >> 3] >> (i & 7)) & 1)

    # 环形扩张：保证按距离从近到远
    for radius in range(1, max_radius + 1):
        best = None
        best_d2 = None
        for dc in range(-radius, radius + 1):
            for dr in range(-radius, radius + 1):
                # 只取这一圈的边界，避免重复检查内侧
                if max(abs(dc), abs(dr)) != radius:
                    continue
                c, r = int(x) + dc, int(y) + dr
                if not walkable(c, r):
                    continue
                d2 = dc * dc + dr * dr
                if best_d2 is None or d2 < best_d2:
                    best_d2, best = d2, (c, r)
        if best is not None:
            return best[0], best[1], float(best_d2) ** 0.5
    return int(x), int(y), None
