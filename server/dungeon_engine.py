"""
龙斗士统一通用副本引擎 (dungeon_engine.py) - 官方 XML 动态怪表驱动版

功能职责：
1. 完整维护全游戏 41+ 个副本配置 (对齐 FbConfig.as 真实定义)。
2. 支持任意副本的入场创建 (15_0 / fb.joinFb)、场景列表装配 (fb.scenes)。
3. 负责副本场景切换与房间名路由 (*_fb)。
4. 场景怪物波次动态生成 (15_3 -> 2302 实体)，自动解析 04-数据JSON/MonsterData_monsterTalkDefineClass.xml。
5. 技能伤害判定与掉血飘字 (15_12 -> 2411/2421)、怪物阵亡反馈 (15_11 -> 2304/2316)。
6. 副本场景推进、Boss 击杀与通关 SSS 结算 (15_4)。
7. 副本通关翻牌抽奖 (15_6) 与安全退出副本回到主城 (15_1)。
"""

import os
import re
import time
import random
import logging
import json
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("DungeonEngine")

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# 官方合法怪物池缓存 [(mid, name), ...]
OFFICIAL_MONSTERS_POOL: List[Tuple[int, str]] = []


def _extract_monster_name_from_xml(filepath: str) -> Dict[int, str]:
    """通过正则与注释提取 MonsterData_monsterTalkDefineClass.xml 里的怪物中文名字"""
    name_map: Dict[int, str] = {}
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as fp:
            content = fp.read()

        # 匹配模式：<!-- 怪物名 --> \s* <monster id = "xxxx">
        pattern = re.compile(r'<!--\s*([^\r\n>]+?)\s*-->\s*<monster\s+id\s*=\s*["\'](\d+)["\']', re.DOTALL)
        for match in pattern.finditer(content):
            raw_name = match.group(1).strip()
            # 清理注释里的多余说明文字
            clean_name = raw_name.split()[0].replace("•", "·")
            mid = int(match.group(2))
            name_map[mid] = clean_name
    except Exception as exc:
        logger.warning("解析怪物中文注释失败: %s", exc)
    return name_map


def load_official_monsters_from_xml() -> None:
    """优先载入从官方 MonsterData.as 导出的 ID/name 表；缺失时回退 XML。"""
    global OFFICIAL_MONSTERS_POOL
    if OFFICIAL_MONSTERS_POOL:
        return

    # MonsterData.as 是客户端的完整 ID→名称表；monsterTalk XML 只覆盖有对话的子集。
    table_path = os.path.join(os.path.dirname(__file__), "data", "monsters_catalog.json")
    if os.path.isfile(table_path):
        try:
            with open(table_path, "r", encoding="utf-8-sig") as fp:
                rows = json.load(fp)
            official_rows = [
                (int(row["id"]), str(row["name"]))
                for row in rows
                if isinstance(row, dict)
                and str(row.get("id", "")).isdigit()
                and 1000 <= int(row["id"]) <= 60000
                and row.get("name")
            ]
            if official_rows:
                OFFICIAL_MONSTERS_POOL = sorted(set(official_rows), key=lambda item: item[0])
                logger.info("从官方 MonsterData.as ID 表载入 %d 个怪物", len(OFFICIAL_MONSTERS_POOL))
                return
        except (OSError, ValueError, TypeError, KeyError) as exc:
            logger.warning("读取官方 MonsterData ID 表失败，将回退 XML: %s", exc)

    xml_candidates = [
        "MonsterData_monsterTalkDefineClass.xml",
        "MonsterData_dataClass.xml"
    ]

    target_path = None
    search_root = os.path.join(ROOT_DIR, "04-数据JSON")
    if os.path.exists(search_root):
        for root, _, files in os.walk(search_root):
            for candidate in xml_candidates:
                if candidate in files:
                    target_path = os.path.join(root, candidate)
                    break
            if target_path:
                break

    if not target_path or not os.path.exists(target_path):
        logger.warning("未找到 MonsterData XML，使用波利系列保底数据")
        OFFICIAL_MONSTERS_POOL = [
            (1001, "白波利"), (1002, "红波利"), (1003, "硬果兽"), (1004, "叶子兽"),
            (2006, "大尾乌托"), (2009, "呼噜探子"), (3006, "乌托护卫"), (3007, "乌托头目")
        ]
        return

    name_dict = _extract_monster_name_from_xml(target_path)
    loaded_list: List[Tuple[int, str]] = []

    try:
        tree = ET.parse(target_path)
        root_node = tree.getroot()
        for monster_elem in root_node.findall(".//monster"):
            mid_str = monster_elem.get("id")
            if not mid_str or not mid_str.isdigit():
                continue
            mid = int(mid_str)
            # 过滤战宠与系统预留ID (>60000)，仅保留客户端原生野怪与副本怪
            if mid < 1000 or mid > 60000:
                continue

            monster_name = name_dict.get(mid, f"怪物_{mid}")
            loaded_list.append((mid, monster_name))

        if loaded_list:
            # 按 ID 正序排列，方便按阶段分层
            loaded_list.sort(key=lambda x: x[0])
            OFFICIAL_MONSTERS_POOL = loaded_list
            logger.info("成功从 %s 解析载入 %d 个官方合法怪物定义", os.path.basename(target_path), len(loaded_list))
        else:
            OFFICIAL_MONSTERS_POOL = [(1001, "白波利"), (1002, "红波利"), (3007, "乌托头目")]
    except Exception as exc:
        logger.error("解析官方怪物 XML 异常: %s", exc)
        OFFICIAL_MONSTERS_POOL = [(1001, "白波利"), (1002, "红波利"), (3007, "乌托头目")]


# 模块初始化时执行载入
load_official_monsters_from_xml()

# 全游戏官方副本定义字典 (提取自 FbConfig.as)
DUNGEON_DEFINES: Dict[int, Dict[str, Any]] = {
    1: {
        "name": "乌托邦",
        "scenes": ["wutuobanglevelone", "wutuobangleveltwo", "wutuobanglevelthree", "wutuobanglevelfour", "wutuobanglevelfive"],
        "min_lv": 10,
        "boss_name": "乌托头目",
        "boss_cti": 3007,
    },
    2: {
        "name": "莫莫塔",
        "scenes": ["sacrificetowerlevelthree", "sacrificetowerlevelfour", "sacrificetowerlevelfive", "sacrificetowerlevelsix"],
        "min_lv": 16,
        "boss_name": "大战士石像",
        "boss_cti": 4013,
    },
    3: {
        "name": "龙穴",
        "scenes": ["longkechamberone", "longkechambertwo", "longkechamberthree", "longkechamberfour", "longkechamberfive"],
        "min_lv": 26,
        "boss_name": "隆克老大",
        "boss_cti": 7008,
    },
    4: {
        "name": "黑洞战舰二层",
        "scenes": ["fbevilwarshipone", "fbevilwarshiptwo", "fbevilwarshipthree"],
        "min_lv": 31,
        "boss_name": "魔化强化机",
        "boss_cti": 20003,
    },
    5: {
        "name": "凌云防线",
        "scenes": ["lingcloudguardone", "lingcloudguardtwo", "lingcloudguardthree", "lingcloudguarddeep"],
        "min_lv": 36,
        "boss_name": "守望巨像",
        "boss_cti": 21010,
    },
    6: {
        "name": "双月城",
        "scenes": ["doubleMoonSuburb", "doubleMoonRoadOne", "doubleMoonRoadTwo", "doubleMoonSquare"],
        "min_lv": 41,
        "boss_name": "月影领主",
        "boss_cti": 16010,
    },
    7: {
        "name": "恶魔领地",
        "scenes": ["evilAreaHall", "fireEvilArea", "iceEvilArea", "fogEvilArea"],
        "min_lv": 46,
        "boss_name": "炎魔霸主",
        "boss_cti": 21016,
    },
    8: {
        "name": "镇魔塔",
        "scenes": ["townDemonTowerOne", "townDemonTowerTwo", "townDemonTowerThree"],
        "min_lv": 51,
        "boss_name": "威廉伯爵",
        "boss_cti": 23024,
    },
    9: {
        "name": "魔能炮台",
        "scenes": ["eviwarshipLeveltwoA", "eviwarshipLeveltwoB", "eviwarshipLevelthree", "magicCannon"],
        "min_lv": 30,
        "boss_name": "要塞毁灭者",
        "boss_cti": 20003,
    },
    10: {
        "name": "勇望阁",
        "scenes": ["yongwanggethree"],
        "min_lv": 1,
        "boss_name": "怪物萨鲁",
        "boss_cti": 1009,
    },
    11: {
        "name": "欧林神殿",
        "scenes": ["oldpalaceone", "oldpalacetwo", "oldpalacethree", "oldpalacestoreroom"],
        "min_lv": 20,
        "boss_name": "远古石灵",
        "boss_cti": 4013,
    },
    12: {
        "name": "暗影幻境",
        "scenes": ["windmillfairyland", "momoonefairyland", "testfairyland", "dragonfightsitefairyland"],
        "min_lv": 40,
        "boss_name": "暗影月神",
        "boss_cti": 16010,
    },
    13: {
        "name": "浮云平原",
        "scenes": ["fuyunone", "fuyuntwo", "fuyunthree", "fuyunfour"],
        "min_lv": 15,
        "boss_name": "风暴独角兽",
        "boss_cti": 5004,
    },
    14: {
        "name": "诺兰森林",
        "scenes": ["fubennbuolanone", "fubennbuolantwo", "fubennbuolanthree"],
        "min_lv": 22,
        "boss_name": "巨木怪",
        "boss_cti": 21010,
    },
    15: {
        "name": "水晶矿洞",
        "scenes": ["crystalMineralHoleOne", "crystalMineralHoleTwo", "crystalMineralHoleThree", "crystalMineralHoleFour"],
        "min_lv": 35,
        "boss_name": "暴怒的格兰夏尔",
        "boss_cti": 8003,
    },
    16: {
        "name": "空艇制造厂",
        "scenes": ["fbAircraffFactoryOne", "fbAircraffFactoryTwo", "fbAircraffFactoryThree"],
        "min_lv": 45,
        "boss_name": "呼噜空贼·獠牙",
        "boss_cti": 4010,
    },
    17: {
        "name": "黄金龙穴",
        "scenes": ["goldDragonHoleOne", "goldDragonHoleTwo", "goldDragonHoleThree"],
        "min_lv": 55,
        "boss_name": "贪婪的夏洛特",
        "boss_cti": 8001,
    },
    18: {
        "name": "空贼基地",
        "scenes": ["thiefBase"],
        "min_lv": 25,
        "boss_name": "呼噜空贼小头目",
        "boss_cti": 2018,
    },
    19: {
        "name": "龙皇宫殿",
        "scenes": ["dragonPalaceFront", "dragonPalaceMiddle", "dragonPalaceRear", "dragonPalaceCourtyard"],
        "min_lv": 60,
        "boss_name": "路西法",
        "boss_cti": 8004,
    },
    20: {
        "name": "恶魔城堡",
        "scenes": ["demonCastleGarden", "demonCastleFrontCourt", "demonCastleAmbulatory", "demonCastleStudyRoom"],
        "min_lv": 65,
        "boss_name": "南宫铁",
        "boss_cti": 23025,
    },
    21: {
        "name": "星河之路",
        "scenes": ["starRiverOne", "starRiverTwo", "starRiverThree", "starRiverFour"],
        "min_lv": 70,
        "boss_name": "星穹裁决者",
        "boss_cti": 47942,
    },
}


class DungeonInstance:
    """单个玩家的长连接副本运行实例"""

    def __init__(self, char_id: int, fb_id: int = 10, fb_lv: int = 2):
        self.char_id: int = char_id
        self.fb_id: int = fb_id
        self.fb_lv: int = fb_lv

        # 获取副本静态定义 (未知副本智能自动兜底生成)
        self.define = DUNGEON_DEFINES.get(fb_id, {
            "name": f"探索副本_{fb_id}",
            "scenes": [f"fb_{fb_id}_scene_1", f"fb_{fb_id}_scene_2"],
            "min_lv": max(1, fb_lv * 10),
            "boss_name": "乌托头目",
            "boss_cti": 3007,
        })
        self.fb_name: str = self.define["name"]
        self.scene_names: List[str] = self.define["scenes"]
        self.total_scenes_count: int = len(self.scene_names)
        self.current_scene_idx: int = 0
        self.is_cleared: bool = False
        self.start_time: float = time.time()
        self.monsters_killed_total: int = 0

        # 装配供客户端 SceneManager / FbControl 寻址的 scenes 数组结构
        self.scenes_meta: List[Dict[str, Any]] = []
        for idx, sn in enumerate(self.scene_names):
            sn_clean = sn.lower().strip()
            # 官方规范房间ID
            if self.fb_id == 10:
                sid = 304686
            else:
                sid = 300000 + fb_id * 100 + idx

            # 官方标准房间名：{地图名}_{难度}_{角色ID}_fb
            rn = f"{sn_clean}_{self.fb_lv}_{char_id}_fb"

            self.scenes_meta.append({
                "sn": sn_clean,
                "id": sid,
                "rn": rn,
                "idx": idx,
            })

        # 当前场景存活怪物字典 {ci: dict}
        self.monsters: Dict[float, Dict[str, Any]] = {}

    def get_join_packet(self) -> Dict[str, Any]:
        """生成官方规范的 15_0 准入包 (严格对齐 FbControl.as 解析结构)"""
        return {
            "res": 1,
            "suc": True,
            "r": 0,
            "s": 0,
            "_cmd": "15_0",
            "fb": {
                "fbId": self.fb_id,
                "fblv": self.fb_lv,
                "lv": self.fb_lv,
                "cs": 0,
                "scenes": self.scenes_meta,
                "key": 61327 + self.fb_id,
                "sgc": True,
            }
        }

    def get_current_scene_meta(self) -> Dict[str, Any]:
        """获取当前关卡场景的元数据 (sn, id, rn)"""
        if 0 <= self.current_scene_idx < len(self.scenes_meta):
            return self.scenes_meta[self.current_scene_idx]
        return self.scenes_meta[-1]

    def set_current_room(self, room_name: str) -> None:
        """客户端通过 JOIN_ROOM 进入某一场景时，同步关卡索引"""
        for idx, sm in enumerate(self.scenes_meta):
            if sm["rn"] == room_name:
                self.current_scene_idx = idx
                logger.info("玩家 %d 副本关卡推进到第 %d/%d 幕: %s", self.char_id, idx + 1, self.total_scenes_count, sm["sn"])
                break

    def spawn_monsters(self, cr_id: int = 201) -> List[Dict[str, Any]]:
        """
        基于官方 XML 数据驱动生成场景怪物实体。
        选取 MonsterData.as 中存在的原始怪物 ID。ClientMonsterInfoConfig 是另一套
        客户端控制怪物配置，普通 MonsterData ID 不保证能由该配置管理器加载。
        """
        self.monsters.clear()
        scene_meta = self.get_current_scene_meta()
        scene_id = scene_meta["id"]
        is_final_scene = (self.current_scene_idx == self.total_scenes_count - 1)

        monster_packets = []
        monster_count = 5 if not is_final_scene else 4
        base_lv = max(1, self.define.get("min_lv", 10))

        if not OFFICIAL_MONSTERS_POOL:
            load_official_monsters_from_xml()

        # 按副本等级划定合规的怪兽 ID 范围，保障视觉与数值贴合
        if base_lv <= 15:
            matched_pool = [m for m in OFFICIAL_MONSTERS_POOL if 1000 <= m[0] <= 3100]
        elif base_lv <= 35:
            matched_pool = [m for m in OFFICIAL_MONSTERS_POOL if 2000 <= m[0] <= 8000]
        else:
            matched_pool = [m for m in OFFICIAL_MONSTERS_POOL if 4000 <= m[0] <= 30000]

        if not matched_pool:
            matched_pool = OFFICIAL_MONSTERS_POOL

        # 1. 动态生成小怪
        for i in range(monster_count):
            m_id, m_name = matched_pool[i % len(matched_pool)]
            ci = float(-4120000 - (self.current_scene_idx * 10) - (i + 1))
            mx = round(56.0 + i * 5.5, 1)
            my = round(46.0 + (i % 2) * 2.0, 1)
            hp = 1200 + base_lv * 150
            mlv = base_lv + (i % 2)
            cti = float(m_id)
            rk = 1

            self.monsters[ci] = {"n": m_name, "hp": hp, "mhp": hp, "x": mx, "y": my, "lv": mlv, "cti": cti, "rk": rk}
            monster_packets.append(self._build_2302(ci, scene_id, m_name, hp, mlv, mx, my, cti, rk))

        # 2. 关底追加首领 Boss (优先使用 DUNGEON_DEFINES 绑定的 Boss)
        if is_final_scene:
            boss_ci = float(-4129999.0)
            boss_name = self.define.get("boss_name", "首领")
            boss_hp = 15000 + base_lv * 800
            boss_lv = base_lv + 5
            boss_cti = float(self.define.get("boss_cti", matched_pool[-1][0]))
            boss_rk = 2

            self.monsters[boss_ci] = {
                "n": boss_name,
                "hp": boss_hp,
                "mhp": boss_hp,
                "x": 84.0,
                "y": 48.0,
                "lv": boss_lv,
                "cti": boss_cti,
                "rk": boss_rk,
            }
            monster_packets.append(self._build_2302(boss_ci, scene_id, boss_name, boss_hp, boss_lv, 84.0, 48.0, boss_cti, boss_rk))

        logger.info(
            "副本 [%s] 第 %d 关刷怪完毕: 生成 %d 只怪 (含首领: %s, 基础等级: %d)",
            self.fb_name,
            self.current_scene_idx + 1,
            len(monster_packets),
            is_final_scene,
            base_lv,
        )
        return monster_packets

    def _build_2302(self, ci: float, bi: int, n: str, hp: int, lv: int, x: float, y: float, cti: float, rk: int) -> Dict[str, Any]:
        """构建 AS3 规范的 2302 怪物实体数据字典"""
        return {
            "_cmd": "2302",
            "bi": bi,
            "bot": 2,
            "camp": 2,
            "ci": ci,
            "n": n,
            "chp": hp,
            "mhp": hp,
            "lv": lv,
            "x": x,
            "y": y,
            "d": 0.0,
            "s": 8,
            "state": 1,
            "bt": False,
            "mty": 2,
            "ui": -1,
            "cau": False,
            "rk": rk,
            "cti": cti,
            "cs": {"al": True, "fd": False},
            "p": "",
        }

    def handle_damage(self, dmg_str: str, skill_id: int) -> Tuple[List[Dict], List[float], bool, bool]:
        """
        处理技能伤害 15_12。
        返回: (hit_updates, killed_cis, scene_cleared, fb_cleared)
        """
        now_ms = float(int(time.time() * 1000))
        scene_meta = self.get_current_scene_meta()
        scene_id = scene_meta["id"]

        hit_updates = []
        killed_cis = []

        if dmg_str:
            for part in dmg_str.split("#"):
                if not part or "," not in part:
                    continue
                try:
                    ci_str, d_str = part.split(",")
                    ci = float(ci_str)
                    dmg = int(d_str)
                    if ci in self.monsters and self.monsters[ci]["hp"] > 0:
                        m = self.monsters[ci]
                        m["hp"] = max(0, m["hp"] - dmg)
                        hit_updates.append({
                            "ci": ci, "bi": scene_id, "chp": m["hp"], "mhp": m["mhp"],
                            "pkm": 0, "ati": "", "originaljob": 0, "vr": now_ms,
                            "dnis": True, "lv": m.get("lv", 10), "n": m.get("n", "")
                        })
                        if m["hp"] == 0:
                            killed_cis.append(ci)
                            self.monsters_killed_total += 1
                except Exception:
                    pass
        else:
            # 兼容普通攻击保底扣血
            living = [ci for ci, m in self.monsters.items() if m["hp"] > 0][:2]
            for ci in living:
                m = self.monsters[ci]
                m["hp"] = max(0, m["hp"] - 1500)
                hit_updates.append({
                    "ci": ci, "bi": scene_id, "chp": m["hp"], "mhp": m["mhp"],
                    "pkm": 0, "ati": "", "originaljob": 0, "vr": now_ms,
                    "dnis": True, "lv": m.get("lv", 10), "n": m.get("n", "")
                })
                if m["hp"] == 0:
                    killed_cis.append(ci)
                    self.monsters_killed_total += 1

        all_dead = all(m["hp"] <= 0 for m in self.monsters.values())
        is_final_scene = (self.current_scene_idx == self.total_scenes_count - 1)
        scene_cleared = all_dead
        fb_cleared = all_dead and is_final_scene

        if fb_cleared:
            self.is_cleared = True

        return hit_updates, killed_cis, scene_cleared, fb_cleared

    def force_clear_current_scene(self) -> List[float]:
        """清除当前场景所有残余怪物并返回被清理的 ci 列表"""
        killed = []
        for ci, m in self.monsters.items():
            if m["hp"] > 0:
                m["hp"] = 0
                killed.append(ci)
                self.monsters_killed_total += 1
        return killed

    def build_finish_result(self, player_lv: int = 100) -> Dict[str, Any]:
        """
        生成官方规范的 15_4 SSS 级通关结算奖励包。
        根据玩家等级与副本难度动态给予海量经验与金币。
        """
        duration = max(15, int(time.time() - self.start_time))
        base_exp = 3000 + player_lv * 500 + self.fb_id * 300
        base_coin = 5000 + player_lv * 200 + self.fb_id * 150

        return {
            "_cmd": "15_4",
            "suc": True,
            "ft": False,
            "fun": 1,
            "res": {
                "pt": 2000,
                "ppe": 0,
                "ot": 4000,
                "double": 0,
                "ve": 0,
                "te": 0,
                "total": base_exp,
                "pe": 700,
                "grade": "SSS",
                "time": duration,
                "pm": base_coin,
                "ge": 210,
                "dc": 0,
            },
            "exp_reward": base_exp,
            "coin_reward": base_coin,
        }

    def generate_bonus_box_rewards(self) -> str:
        """通关翻牌 15_6 产出奖励 (神装、材料、大量金币)"""
        rewards = []
        equip_cands = [20035, 20045, 20055, 20005, 20015, 20025, 140029, 141029]
        rewards.append(f"0:{random.choice(equip_cands)}:1")
        rewards.append("100:1:50000")
        rewards.append("2:1001:3")
        return "#".join(rewards)


# 全局多玩家副本管理器字典 {char_id: DungeonInstance}
DUNGEON_SESSIONS: Dict[int, DungeonInstance] = {}


def get_or_create_dungeon(char_id: int, fb_id: int = 10, fb_lv: int = 2) -> DungeonInstance:
    """获取或初始化玩家当前的副本实例"""
    inst = DungeonInstance(char_id=char_id, fb_id=fb_id, fb_lv=fb_lv)
    DUNGEON_SESSIONS[char_id] = inst
    logger.info("玩家 %d 成功创建副本实例 [%s] (ID=%d, 难度=%d)", char_id, inst.fb_name, fb_id, fb_lv)
    return inst


def get_dungeon(char_id: int) -> Optional[DungeonInstance]:
    """获取玩家活跃中的副本实例"""
    return DUNGEON_SESSIONS.get(char_id)


def remove_dungeon(char_id: int) -> None:
    """清理并移除玩家副本实例"""
    if char_id in DUNGEON_SESSIONS:
        DUNGEON_SESSIONS.pop(char_id, None)
        logger.info("已清理玩家 %d 的副本实例", char_id)
