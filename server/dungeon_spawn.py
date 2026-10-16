# -*- coding: utf-8 -*-
"""龙斗士 · 副本进本与刷怪配置模块 (dungeon_spawn.py)

## 定位

本模块是**配置读取层**：把 `server/data/dungeon_spawn.json`（由
`server/build_dungeon_spawn.py` 生成）读成结构化对象，并提供 2302 / 2431 组包能力。
运行时真正刷怪的是 `server/dungeon_engine.py`；这里保留一份独立、可单测的
配置视图与组包器，便于脚本、校验工具复用。

## 数据来源（全部为官方客户端资源 + 抓包实测，非推测）

    FbConfig.as                               副本表 / 场景表（类型、通关条件）
    aiscript/fb<fbId>~1000.swf                怪物数值（等级/血量/攻防/移速）
    official 抓包 2302 帧                     每幕真实刷怪构成与坐标

## 协议依据（官方抓包 `战士诺兰.pcapng` 实测）

    客户端上行                              服务端下行
    ─────────────────────────────────────────────────────────────────
    15_0 {fbId, fblv, sgc}          →   15_0  {res:1, fb:{fbId, lv, key, sgc, scenes[]}}
    Action=10012 (加入房间)          →   SYS 20019 (JOIN_OK)
                                        2303  {bi}            场景重置
                                        2311  {bi, il:[]}     地面掉落物
                                    →   2301  {bi, pk, cl[]}  ★ 战场初始化 + 角色本体
                                        2321  {bi, sl:[]}     机关/传送门
                                        2131                   角色实时数值
                                        2421  {bi, bv, cl}     血量同步
                                        2302  × N              ★ 每只怪独立一条
                                        2431  {bi, dl:[ci]}    ★ 怪物死亡注销
                                        2411 / 15_13           命中与伤害结算
                                        15_4                   通关结算

**官方协议里没有 `2300`。** 战场初始化就是 `2301`，刷怪就是逐只 `2302`。

## 2302 的真实字段（抓包原文）

    {_cmd:'2302', bi:187075, bot:2, bt:False, cau:False,
     ci:-4647998.0, cti:37401.0, n:'蓝蓝毛贼', lv:8,
     chp:952, mhp:952, rk:1, mty:2, ui:-1,
     x:93.0, y:30.0, d:0.0, p:'', cs:{al:True, fd:False}}

客户端 `MonsterInfoBase.parseMonsterInfo` 另外会读 `s`(移速) / `ms`(暴走速度)；
抓包里没有的 `camp` / `state` 客户端不读，故不下发。

## 难度档位

`15_0` 的 `fb.lv` 就是难度序号：fb.lv=2 → 该副本第 2 档怪物前缀。
抓包实测 fb14 lv=2 刷的正是 374xx（373/374/375/427 中的第 2 个）。

## 房间命名（官方实测）

    rn = f"{sceneName}_u{charId}_0_fb"       例: fubennbuolanone_u88343134_0_fb
    15_0 的 scenes[] 每项 = {sn, id, rn}，id 即该幕的 bi（例: 187075）
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ── 官方命令常量 ────────────────────────────────────────────────────────────
CMD_FB_JOIN = "15_0"          # 建本/准入
CMD_BATTLE_INIT = "2301"      # 战场初始化（角色本体）
CMD_MONSTER_JOIN = "2302"     # 单只怪生成
CMD_MONSTER_DEAD = "2431"     # 怪物死亡注销
CMD_SCENE_RESET = "2303"      # 场景重置
CMD_SCENE_DROPS = "2311"      # 地面掉落物
CMD_SCENE_TRAPS = "2321"      # 机关/传送门
CMD_CHAR_PROPS = "2131"       # 角色实时数值
CMD_HP_SYNC = "2421"          # 血量同步
CMD_FB_FINISH = "15_4"        # 通关结算

#: 客户端 MonsterInfoBase.parseMonsterInfo 读取的字段（2302/2301 怪物条目通用）
MONSTER_REQUIRED_FIELDS = ("cti", "lv", "chp", "mhp", "rk", "n")
MONSTER_ENTITY_FIELDS = ("ci", "x", "y", "d", "bot", "mty", "ui", "bt", "cau", "p", "cs")

#: 副本刷怪表（由 build_dungeon_spawn.py 生成）
DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "dungeon_spawn.json")

#: ci 起始值，官方实测为 -4647998 一类的大负数，保证与玩家正数 ci 不冲突
MONSTER_CI_BASE = -4647998.0
#: 每幕独占一段 ci
MONSTER_CI_SCENE_STRIDE = 1000


# ── 房间 / 战场 ID ──────────────────────────────────────────────────────────
def make_room_name(scene_name: str, char_id: int, suffix: str = "fb") -> str:
    """官方房间名格式：{scene}_u{charId}_0_{suffix}

    抓包实测：fubennbuolanone_u88343134_0_fb
    注意本服务端旧格式是 {scene}_{fblv}_{charId}_{suffix}（缺 _u），客户端
    Fb.getRoomNameBySceneName 只按 sn 反查、自身不校验格式，但服务端房间匹配
    会踩坑，故统一为官方格式。
    """
    return f"{scene_name}_u{char_id}_0_{suffix}"


def make_battlefield_id(fb_id: int, scene_index: int, char_id: int = 0) -> int:
    """生成该幕的战场 ID (bi)。

    官方实测 bi 由服务端 DB 分配（fb10=304686、fb14=187075/187076/187077），
    无常量公式可推。这里与 dungeon_engine 保持同一套规则，按 (fbId, 幕序) 稳定
    派生一段互不冲突的连号值，避开常规场景 ID（2403 / 304448 等）。
    """
    if fb_id == 10:
        return 304686
    return 187000 + fb_id * 5 + scene_index


# ── 结构化配置 ──────────────────────────────────────────────────────────────
@dataclass
class MonsterSpawn:
    """单只怪的刷怪配置（坐标使用官方像素坐标，x≈49~155, y≈29~56）。"""

    cti: int                       # 原型 ID（决定模型、动作与客户端本地数值）
    name: str = ""
    x: float = 0.0
    y: float = 0.0
    d: float = 0.0                 # 朝向
    lv: int = 1
    hp: int = 1
    rk: int = 1                    # 1=普通 2=头目/精英
    mty: int = 2
    normal_speed: int = 5
    mad_speed: int = 5


@dataclass
class SceneWave:
    """一幕的刷怪配置。"""

    scene: str = ""
    monsters: List[MonsterSpawn] = field(default_factory=list)
    is_boss_scene: bool = False
    optional: bool = False
    conditions: List[str] = field(default_factory=list)
    boss_kills: List[Dict[str, Any]] = field(default_factory=list)
    pos_source: str = "reconstructed"


@dataclass
class DungeonSpawnConfig:
    """一个副本的刷怪配置（按幕 × 难度）。"""

    fb_id: int
    name: str
    scenes: List[str] = field(default_factory=list)
    min_level: int = 1
    difficulty_map: Dict[int, int] = field(default_factory=dict)
    #: {幕序: {难度: SceneWave}}
    waves: Dict[int, Dict[int, SceneWave]] = field(default_factory=dict)

    def wave(self, scene_index: int, difficulty: int) -> Optional[SceneWave]:
        per_scene = self.waves.get(scene_index)
        if not per_scene:
            return None
        if difficulty in per_scene:
            return per_scene[difficulty]
        key = min(per_scene, key=lambda k: (abs(k - difficulty), k))
        return per_scene[key]


# ── 配置注册表 ──────────────────────────────────────────────────────────────
class DungeonSpawnRegistry:
    """副本刷怪配置注册表 —— 数据来自官方刷怪表 JSON。"""

    def __init__(self) -> None:
        self._configs: Dict[int, DungeonSpawnConfig] = {}

    def register(self, cfg: DungeonSpawnConfig) -> None:
        self._configs[cfg.fb_id] = cfg

    def get(self, fb_id: int) -> Optional[DungeonSpawnConfig]:
        return self._configs.get(fb_id)

    def all_ids(self) -> List[int]:
        return sorted(self._configs)

    @classmethod
    def from_json(cls, path: str = DATA_PATH) -> "DungeonSpawnRegistry":
        """从 server/data/dungeon_spawn.json 装配注册表。"""
        with open(path, "r", encoding="utf-8") as fp:
            doc = json.load(fp)

        reg = cls()
        for d in doc.get("dungeons", []):
            cfg = DungeonSpawnConfig(
                fb_id=int(d["fbId"]),
                name=d.get("name") or "",
                scenes=[s["sn"] for s in (d.get("scenes") or [])],
                min_level=int(d.get("minLevel") or 1),
                difficulty_map={int(k): int(v)
                                for k, v in (d.get("difficultyMap") or {}).items()},
            )
            pool = d.get("monsters") or {}
            for scene in d.get("scenes") or []:
                per_scene: Dict[int, SceneWave] = {}
                for diff_txt, spawns in (scene.get("waves") or {}).items():
                    monsters: List[MonsterSpawn] = []
                    for sp in spawns:
                        mid = int(sp["mId"])
                        st = pool.get(str(mid)) or {}
                        monsters.append(MonsterSpawn(
                            cti=mid,
                            name=st.get("name") or "",
                            x=float(sp.get("x", 0.0)),
                            y=float(sp.get("y", 0.0)),
                            d=float(sp.get("d", 0.0)),
                            lv=int(st.get("lv") or 1),
                            hp=int(st.get("mhp") or 1),
                            rk=int(st.get("rk") or 1),
                            normal_speed=int(st.get("normalSpeed") or 5),
                            mad_speed=int(st.get("madSpeed") or st.get("normalSpeed") or 5),
                        ))
                    per_scene[int(diff_txt)] = SceneWave(
                        scene=scene["sn"],
                        monsters=monsters,
                        is_boss_scene=any(m.rk >= 2 for m in monsters),
                        optional=bool(scene.get("optional")),
                        conditions=list(scene.get("conditions") or []),
                        boss_kills=list(scene.get("bossKills") or []),
                        pos_source=scene.get("posSource") or "reconstructed",
                    )
                cfg.waves[int(scene["idx"])] = per_scene
            reg.register(cfg)
        return reg

    # ── 组包 ────────────────────────────────────────────────────────────
    def build_2302(
        self,
        bi: int,
        spawn: MonsterSpawn,
        ci: float,
        char_level: int = 1,
    ) -> Dict[str, Any]:
        """构造单只怪的 2302 包（字段严格对齐官方抓包）。"""
        return {
            "_cmd": CMD_MONSTER_JOIN,
            "bi": bi,
            "bot": 2,
            "bt": False,
            "cau": False,
            "ci": float(ci),
            "cti": float(spawn.cti),
            "n": spawn.name,
            "lv": int(spawn.lv),
            "chp": int(spawn.hp),
            "mhp": int(spawn.hp),
            "rk": int(spawn.rk),
            "mty": int(spawn.mty),
            "ui": -1,
            "x": float(spawn.x),
            "y": float(spawn.y),
            "d": float(spawn.d),
            "p": "",
            "cs": {"al": True, "fd": False},
            "s": int(spawn.normal_speed),
            "ms": int(spawn.mad_speed),
        }

    def build_scene_monsters(
        self,
        fb_id: int,
        scene_index: int,
        bi: int,
        char_level: int = 1,
        difficulty: int = 2,
    ) -> List[Dict[str, Any]]:
        """一幕的全部 2302 包（每只怪独立一条，ci 每幕独占一段）。"""
        cfg = self.get(fb_id)
        if cfg is None:
            return []
        wave = cfg.wave(scene_index, difficulty)
        if wave is None:
            return []
        base = MONSTER_CI_BASE - scene_index * MONSTER_CI_SCENE_STRIDE
        return [
            self.build_2302(bi, spawn, base - idx, char_level)
            for idx, spawn in enumerate(wave.monsters)
        ]

    @staticmethod
    def build_2431(bi: int, dead_cis: Sequence[float], killer_ci: int = -1) -> Dict[str, Any]:
        """怪物死亡注销包。"""
        return {
            "_cmd": CMD_MONSTER_DEAD,
            "bi": bi,
            "dl": [
                {"ci": float(c), "k": float(killer_ci), "t": -1.0, "type": 2}
                for c in dead_cis
            ],
        }


#: 全局注册表实例（从官方刷怪表装配）
try:
    REGISTRY = DungeonSpawnRegistry.from_json()
except (OSError, ValueError, KeyError, TypeError):
    REGISTRY = DungeonSpawnRegistry()


__all__ = [
    "CMD_FB_JOIN", "CMD_BATTLE_INIT", "CMD_MONSTER_JOIN", "CMD_MONSTER_DEAD",
    "CMD_SCENE_RESET", "CMD_SCENE_DROPS", "CMD_SCENE_TRAPS",
    "CMD_CHAR_PROPS", "CMD_HP_SYNC", "CMD_FB_FINISH",
    "MONSTER_REQUIRED_FIELDS", "MONSTER_ENTITY_FIELDS",
    "MONSTER_CI_BASE", "MONSTER_CI_SCENE_STRIDE", "DATA_PATH",
    "MonsterSpawn", "SceneWave", "DungeonSpawnConfig",
    "DungeonSpawnRegistry", "REGISTRY",
    "make_room_name", "make_battlefield_id",
]
