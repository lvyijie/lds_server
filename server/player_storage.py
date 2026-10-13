"""
龙斗士玩家角色本地持久化存储模块 (player_storage.py)

规范要求：
1. HTTP 协议字段对齐 http_api.py 的 /login.jsp (cid, n, lv, j, cloth, equip, lr)。
2. TCP 协议字段直接提取自 action_presets.pkl 中的 SYS_10012 实体 (2301) 与 10-0 任务结构。
3. 核心持久化数据：
   - basic: char_id, user_name, level, job, exp, coin
   - position: last_room, x, y
   - tasks: 任务字典，如 {"513": {"step": 8, "st": 1, "ti": "513_8"}}
   - bag: 背包物品列表与装备/服装列表
   - pet: 战宠数据
"""

import os
import json
import time
import logging
import re
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple, Union
import xml.etree.ElementTree as ET
import glob

logger = logging.getLogger("PlayerStorage")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_PLAYER_FILE = os.path.join(DATA_DIR, "player_data.json")

# 确保 data 目录存在
os.makedirs(DATA_DIR, exist_ok=True)


def find_json_file(filename: str) -> Optional[str]:
    """多路径递归查找官方数据表 JSON 文件。"""
    cands = [
        os.path.join(ROOT_DIR, "04-数据JSON", "游戏数据", "02-宠物", filename),
        os.path.join(ROOT_DIR, "04-数据JSON", "游戏数据", "05-物品", filename),
        os.path.join(ROOT_DIR, "04-数据JSON", "原始JSON提取", filename),
        os.path.join(ROOT_DIR, "04-数据JSON", filename),
    ]
    for c in cands:
        if os.path.exists(c):
            return c
    for root, dirs, files in os.walk(ROOT_DIR):
        if filename in files:
            return os.path.join(root, filename)
    return None


def _load_material_id_set(filename: str, id_tag: str) -> set[int]:
    """Load item IDs from an official client material definition table."""
    import re
    import xml.etree.ElementTree as ET

    direct_path = os.path.join(
        ROOT_DIR, "04-数据JSON", "游戏数据", "05-物品", filename
    )
    filepath = direct_path if os.path.exists(direct_path) else find_json_file(filename)
    if not filepath:
        return set()
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as source:
            content = source.read()
        content = re.sub(
            r'encoding=(["\'])utf8\1',
            'encoding="utf-8"',
            content,
            count=1,
            flags=re.IGNORECASE,
        )
        root = ET.fromstring(content)
        if id_tag.startswith("@"):
            attribute = id_tag[1:]
            return {
                int(value)
                for element in root.iter()
                if (value := element.get(attribute)) and value.isdigit()
            }
        return {
            int(node.text)
            for node in root.iter(id_tag)
            if node.text and node.text.strip().isdigit()
        }
    except (OSError, ET.ParseError, ValueError) as exc:
        logger.warning("加载官方物品表 %s 失败: %s", filename, exc)
        return set()


def _load_semicolon_id_set(filename: str) -> set[int]:
    """Load IDs from an official semicolon-delimited client data table."""
    filepath = find_json_file(filename)
    if not filepath:
        return set()
    item_ids = set()
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as source:
            for line in source:
                first_column = line.partition(";")[0].strip()
                if first_column.isdigit():
                    item_ids.add(int(first_column))
    except OSError as exc:
        logger.warning("加载官方数据表 %s 失败: %s", filename, exc)
    return item_ids


GLOBAL_CONSUMENT_IDS = _load_material_id_set(
    "ConsumentDatas_dataClass.xml", "ConsumeId"
)
GLOBAL_TASKITEM_IDS = _load_material_id_set(
    "TaskItemDatas_dataClass.xml", "TaskItemId"
)
GLOBAL_STUFFITEM_IDS = _load_material_id_set(
    "StuffItemDatas_dataClass.xml", "StuffId"
)
GLOBAL_RUBBISHITEM_IDS = _load_material_id_set(
    "RubbishItemDatas_dataClass.xml", "RubbishId"
)
GLOBAL_RIDECONSUMENT_IDS = _load_material_id_set(
    "RideConsumentData_dataClass.xml", "@id"
)
GLOBAL_RIDER_GUARDIAN_IDS = _load_material_id_set(
    "RidePetData__guardianClass.xml", "@id"
)
GLOBAL_FLY_PET_EGG_IDS = _load_material_id_set(
    "FlyPetEggDatas_dataClass.xml", "FlyingPetEggTypeId"
)
GLOBAL_EQUIP_GEM_IDS = _load_semicolon_id_set("EquipGemData_dataClass.xml")

GLOBAL_INVENTORY_CATALOGS: Dict[int, set[int]] = {
    2: GLOBAL_CONSUMENT_IDS,
    3: GLOBAL_TASKITEM_IDS,
    4: GLOBAL_STUFFITEM_IDS,
    5: GLOBAL_RUBBISHITEM_IDS,
    7: GLOBAL_RIDECONSUMENT_IDS,
    113: GLOBAL_RIDECONSUMENT_IDS,
    119: GLOBAL_STUFFITEM_IDS,
    152: GLOBAL_FLY_PET_EGG_IDS,
    200: GLOBAL_EQUIP_GEM_IDS,
}


def resolve_inventory_type(mid: int, requested_type: Optional[int] = None) -> int:
    """Resolve and validate an inventory item's client MaterialTypes value."""
    item_id = int(mid)
    if requested_type is not None:
        material_type = int(requested_type)
        if material_type == 0 and item_id in GLOBAL_EQUIP_CONFIG:
            return 0
        catalog = GLOBAL_INVENTORY_CATALOGS.get(material_type)
        if catalog is None or item_id not in catalog:
            raise ValueError(
                f"Unknown inventory item type/id pair: t={material_type}, mid={item_id}"
            )
        return material_type

    matches = [
        material_type
        for material_type, catalog in GLOBAL_INVENTORY_CATALOGS.items()
        if material_type not in (113, 119) and item_id in catalog
    ]
    if item_id in GLOBAL_EQUIP_CONFIG:
        matches.append(0)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous inventory item mid={item_id}; specify its client material type"
        )
    raise ValueError(f"Unknown inventory item mid={item_id}")


KIND_TO_SLOT: Dict[int, int] = {
    # 武器 (主手 10, 副手 11)
    100: 10, 101: 11, 102: 10, 103: 11, 104: 10, 105: 11,
    106: 10, 107: 11, 108: 10, 109: 11, 110: 10, 111: 11,
    112: 10, 113: 11,
    # 防具与饰品 (对齐官方 EquipmentKind.as)
    200: 6,   # 胸甲 (上衣 CHEST) -> 6
    201: 12,  # 护腿 (裤子 TROUSERS) -> 12
    202: 13,  # 战靴 (鞋子 SHOES) -> 13
    203: 7,   # 护腕 (手套 CUFF) -> 7
    204: 1,   # 头盔 (头部 HEAD) -> 1
    205: 4,   # 护肩 (肩部 SHOULDER) -> 4
    206: 5,   # 披风 (背部 BACK) -> 5
    207: 2,   # 耳环 (EARRING) -> 2
    208: 3,   # 项链 (NECKLACE) -> 3
    209: 8,   # 戒指右 (FINGERRING_RIGHT) -> 8
    210: 9,   # 戒指左 (FINGERRING_LEFT) -> 9
    211: 14,  # 圣龙徽章 (VIP_BADGE) -> 14
}

GLOBAL_EQUIP_CONFIG: Dict[int, Dict[str, Any]] = {}


def init_global_equip_config() -> None:
    """启动时全量载入官方 7535 件装备配置表与扩展属性表，彻底告别写死。"""
    global GLOBAL_EQUIP_CONFIG
    eq_path = find_json_file("33_equipment_data.json")
    app_path = find_json_file("21_equip_ext_base_app.json")

    eq_rows: Dict[str, Any] = {}
    app_rows: Dict[str, Any] = {}
    if eq_path and os.path.exists(eq_path):
        try:
            with open(eq_path, "r", encoding="utf-8") as f:
                eq_rows = json.load(f).get("rows", {})
        except Exception as e:
            logger.warning("加载 33_equipment_data.json 失败: %s", e)

    if app_path and os.path.exists(app_path):
        try:
            with open(app_path, "r", encoding="utf-8") as f:
                app_rows = json.load(f).get("rows", {})
        except Exception as e:
            logger.warning("加载 21_equip_ext_base_app.json 失败: %s", e)

    for mid_str, r in eq_rows.items():
        try:
            mid = int(mid_str)
            kind = int(r.get("kind", 100))
            pos = KIND_TO_SLOT.get(kind, 10 if kind < 200 else 6)
            ba_str = app_rows.get(mid_str, {}).get("baseApp", "")

            att = 0
            def_val = 0
            hp = 0
            mp = 0
            for part in ba_str.split("#"):
                if "+" in part:
                    k, v = part.split("+", 1)
                    try:
                        v_num = int(v.split(".")[0])
                    except Exception:
                        continue
                    if "攻击" in k or "\u653b\u51fb" in k:
                        att += v_num
                    elif "防御" in k or "\u9632\u5fa1" in k:
                        def_val += v_num
                    elif "生命" in k or "\u751f\u547d" in k:
                        hp += v_num
                    elif "魔法" in k or "\u9b54\u6cd5" in k:
                        mp += v_num

            fv = int(r.get("calculateBaLv", 100)) * 10
            GLOBAL_EQUIP_CONFIG[mid] = {
                "id": mid,
                "mid": mid,
                "name": r.get("name", ""),
                "kind": kind,
                "position": pos,
                "level": int(r.get("level", 1)),
                "quality": int(r.get("quality", 1)),
                "job": r.get("job", ""),
                "endurance": int(r.get("endurance", 65)),
                "strLvLimit": int(r.get("strLvLimit", 0)),
                "attack": att,
                "defence": def_val,
                "hp": hp,
                "mp": mp,
                "fightValue": fv,
                "baseApp": ba_str,
            }
        except Exception:
            continue

    logger.info("成功加载官方装备数据表: %d 件装备", len(GLOBAL_EQUIP_CONFIG))


init_global_equip_config()

GLOBAL_EQUIP_TO_CLOTHES: Dict[int, int] = {}
GLOBAL_SKILL_TO_ACTIONS: Dict[str, List[str]] = {}


def init_equip_to_clothes() -> None:
    """载入官方武器时装外观映射表 (从 ClothesDatas_dataClass.xml)。"""
    global GLOBAL_EQUIP_TO_CLOTHES
    import xml.etree.ElementTree as ET
    xml_path = None
    cand = os.path.join(ROOT_DIR, "04-数据JSON", "游戏配置", "07-时装", "ClothesDatas_dataClass.xml")
    if os.path.exists(cand):
        xml_path = cand
    else:
        for root, dirs, files in os.walk(os.path.join(ROOT_DIR, "04-数据JSON")):
            if "ClothesDatas_dataClass.xml" in files:
                xml_path = os.path.join(root, "ClothesDatas_dataClass.xml")
                break
    if xml_path and os.path.exists(xml_path):
        try:
            tree = ET.parse(xml_path)
            for c in tree.getroot().findall(".//clothes"):
                cid_str = c.get("id")
                if not cid_str:
                    continue
                cid = int(cid_str)
                eq_str = c.get("equipid", "")
                if eq_str and eq_str != "0":
                    for part in eq_str.split("#"):
                        if part.isdigit():
                            GLOBAL_EQUIP_TO_CLOTHES[int(part)] = cid
            logger.info("成功加载官方武器外观映射表: %d 条映射", len(GLOBAL_EQUIP_TO_CLOTHES))
        except Exception as e:
            logger.warning("解析 ClothesDatas_dataClass.xml 失败: %s", e)


def init_action_map() -> None:
    """载入官方技能到动作动画 ID 映射表 (从 03_action_map.json)。"""
    global GLOBAL_SKILL_TO_ACTIONS
    p = find_json_file("03_action_map.json")
    if p and os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
                GLOBAL_SKILL_TO_ACTIONS = d.get("skillToActions", {})
            logger.info("成功加载官方技能动作映射表: %d 条映射", len(GLOBAL_SKILL_TO_ACTIONS))
        except Exception as e:
            logger.warning("加载 03_action_map.json 失败: %s", e)


init_equip_to_clothes()
init_action_map()


def get_job_action_ids(job_id: int, skill_ids: Optional[List[int]] = None) -> str:
    """
    将角色的技能 ID 映射为客户端场景用户变量 skills 所需的动作动画 ID 字符串。
    对齐官方抓包 Batch 0 (15006,15007,15008,15002,15005,15003,15009,15015) 与 AS3 CharacterInfoBase.
    """
    if not skill_ids:
        skill_entries = get_job_initial_skills(job_id)
        skill_ids = [s["id"] for s in skill_entries]
    action_ids: List[str] = []
    for sid in skill_ids:
        acts = GLOBAL_SKILL_TO_ACTIONS.get(str(sid), [])
        for a in acts:
            if a not in action_ids:
                action_ids.append(a)
    if not action_ids:
        action_ids = ["15006", "15007", "15008", "15002", "15005", "15003", "15009", "15015"]
    return ",".join(action_ids)


# 职业初始技能库 (对齐各职业官方技能段：来自 AiCharacterPlayerSkillExtConfig_dataClass.xml)
JOB_DEFAULT_SKILLS: Dict[int, List[int]] = {
    # These are the IDs actually embedded in EvoSkillTree_<job> in skillpanelevo.swf.
    # The former hand-written lists omitted tree nodes and mapped several jobs to
    # another class's skill family, making those skills impossible to persist.
    1: [1000, 1001, 1002, 1015, 1006, 1007, 1008, 1009, 1019, 1010, 1020, 1011, 1021, 1012, 1003, 1013, 1004, 1023, 1014, 1005],  # 战士
    2: [2000, 2001, 2002, 2023, 2005, 2015, 2006, 2007, 2008, 2009, 2019, 2010, 2020, 2011, 2021, 2012, 2003, 2013, 2004],  # 弓箭手
    3: [3000, 3001, 3002, 3013, 3004, 3005, 3006, 3016, 3007, 3008, 3017, 3018, 3009, 3010, 3011, 3021, 3012, 3003],  # 法师
    4: [4000, 4001, 4002, 4010, 4011, 4012, 4003, 4004, 4013, 4014, 4023, 4005, 4015, 4006, 4016, 4007, 4008, 4017, 4018, 4009],  # 骑士
    5: [5000, 5001, 5002, 5003, 5019, 5010, 5020, 5011, 5012, 5022, 5013, 5014, 5005, 5015, 5006, 5016, 5007, 5017, 5008, 5018, 5009],  # 炮师
    6: [6000, 6001, 6002, 6003, 6018, 6009, 6019, 6010, 6020, 6011, 6012, 6022, 6013, 6014, 6005, 6015, 6006, 6016, 6007, 6017, 6008],  # 魔剑士
    7: [7000, 7001, 7002, 7008, 7009, 7010, 7011, 7003, 7004, 7005, 7006, 7007],  # 召唤师
    8: [8000, 8001, 8002, 8010, 8020, 8011, 8021, 8012, 8003, 8013, 8004, 8014, 8005, 8015, 8006, 8007, 8008, 8017, 8009, 8018, 8019],  # 天斩
    9: [9000, 9001, 9002, 9010, 9020, 9011, 9003, 9013, 9004, 9014, 9005, 9015, 9006, 9016, 9007, 9008, 9017, 9009, 9018, 9019],  # 双枪
    10: [10000, 10001, 10102, 10124, 10123, 10112, 10101, 10122, 10111, 10110, 10109, 10119, 10108, 10129, 10128, 10117, 10127, 10126, 10115, 10114, 10125, 10103],  # 双枪/新职业技能树
    51: [51029, 51018, 51028, 51007, 51017, 51006, 51027, 51016, 51005, 51015, 51004, 51025, 51014, 51003, 51013, 51024, 51002, 51023, 51012, 51001, 51011, 51000, 51010, 51031, 51030, 51009, 51008, 51019],  # 狂剑士
}

SEEDED_STARTER_EQUIPMENT = {
    112031, 113027, 20001, 20011, 20021, 20041, 20051,
}


def get_job_initial_skills(job_id: int) -> List[Dict[str, int]]:
    """Return the three base actions available to a new character of this job."""
    skill_ids = JOB_DEFAULT_SKILLS.get(job_id, JOB_DEFAULT_SKILLS[1])
    return [
        {"id": sk_id, "ck0": 1, "ck1": 0, "ck2": 0, "ck3": 0}
        for sk_id in skill_ids[:3]
    ]


def get_default_player(char_id: int = 88343134, user_name: str = "LDS8834313497") -> Dict[str, Any]:
    """生成新角色的默认标准初始数据。"""
    now_ms = float(int(time.time() * 1000))
    default_pet = {
        "pi": 1,
        "pti": 100408,
        "ptn": "亚克",
        "n": "亚克",
        "lv": 1,
        "ecs": 0,
        "eskill": 0,
        "e": 0,
        "chp": 855,
        "mhp": 855,
        "mnA": 100,
        "mxA": 150,
        "sk": [{"ski": 32071, "skl": 0}],
        "bot": 4,
        "bi": 2499,
        "isevo": False,
        "eid": 0,
    }

    return {
        "char_id": char_id,
        "user_name": user_name,
        "basic": {
            "char_id": char_id,
            "user_name": user_name,
            "level": 1,
            "job": 1,              # 1: 战士
            "job_name": "战士",
            "exp": 0,
            "coin": 100000000,     # 金币
            "dragon_coin": 999999, # 充值龙币 (圣龙骑士专属)
            "vip_level": 7,        # 圣龙骑士 VIP 等级 (最高 7 级)
            "is_vip": True,        # 激活圣龙骑士
            "is_annual": True,     # 年费
            "is_super_vip": True,  # 圣皇年费会员
            "super_vip_days": 3650,# 剩余有效天数 (10年)
            "gender": 1,           # 1: 男
            "race": 1,
            "mhp": 836,
            "chp": 300,
            "mmp": 180,
            "cmp": 160,
            "msp": 100,
            "csp": 100,
        },
        "position": {
            "last_room": f"shenyuanroad_{char_id}_0_tr",
            "x": 60,
            "y": 40,
        },
        "tasks": {
            "513": {
                "taskId": 513,
                "step": 0,
                "st": 0,          # 0: 进行中/未完成, 1: 已完成
                "ti": "513_0",
                "ad": now_ms,
                "fd": 0.0,
                "ex": "",
                "tm": 0,
            }
        },
        "skills": {
            "sp": 100,
            "list": get_job_initial_skills(1),
        },
        "bag": {
            "cloth": [100011, 100012, 100013, 100081, 100151, 600001],
            "equip": [],
            "gems": {},
            "items": [],
        },
        "mounts": [{"rid": 1, "pid": 100001, "end": 4133980800000}],
        "mounts_seeded": True,
        "default_rider_id": 1,
        "riding_pet_id": 0,
        "riding_rid": 0,
        "guarding_rider_id": -1,
        "pet": default_pet,
        "pets": [default_pet],
        # Temporary score balances are held outside the material bag. Pet
        # experience is score id 4 and is requested by the client with 8_3.
        "temp_scores": {"4": 0},
        "follow_pet_id": 1,
    }


def get_player_file(char_id: int = 88343134) -> str:
    """获取指定角色的存储文件路径。"""
    if char_id == 88343134:
        return DEFAULT_PLAYER_FILE
    return os.path.join(DATA_DIR, f"player_{char_id}.json")


def calc_player_properties(char_id: int = 88343134, player_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    全动态属性公式：
    1. 基础属性完全由 [职业 + 等级] 动态计算；
    2. 装备加成直接遍历玩家 bag['equip'] 从官方 7535 条 GLOBAL_EQUIP_CONFIG 累加；
    3. 技能战力直接遍历玩家 skills['list'] 的真实加点层数；
    4. VIP 战力由真实 vip_level 动态计算。
    """
    if player_data is None:
        filepath = get_player_file(char_id)
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    player_data = json.load(f)
            except Exception:
                player_data = None
    if player_data is None:
        player_data = get_default_player(char_id)

    b = player_data.get("basic", {})
    lv = max(1, min(100, int(b.get("level", 1))))
    job_id = int(b.get("job", 1))
    user_name = str(b.get("user_name", "LDS8834313497"))

    job_names = {
        1: "战士", 2: "弓箭手", 3: "法师", 4: "骑士", 5: "枪炮师",
        6: "魔剑士", 7: "召唤师", 8: "狂战士", 9: "双枪", 10: "狼人",
        201: "毁灭者", 202: "审判天使", 203: "龙骑士"
    }
    job_name = job_names.get(job_id, b.get("job_name", "战士"))

    # 1. 基础等级成长 (随等级动态公式)
    base_mhp = 800 + lv * 150
    base_mmp = 180 + lv * 30
    base_att = 50 + lv * 40
    base_def = 300 + lv * 35
    base_stat = 20 + lv * 10
    cri_val = round(10.0 + lv * 0.2, 1)
    hit_val = round(98.0 + lv * 0.1, 1)
    eva_val = round(12.0 + lv * 0.1, 1)

    # 2. 动态读取官方装备属性累加（不写死任何一件装备）
    eq_hp = 0
    eq_att = 0
    eq_def = 0
    ba_eqp = 0
    equip_ids = player_data.get("bag", {}).get("equip", [])

    for eq_id in equip_ids:
        eq_meta = GLOBAL_EQUIP_CONFIG.get(int(eq_id))
        if eq_meta:
            eq_att += int(eq_meta.get("attack", 0))
            eq_def += int(eq_meta.get("defence", 0))
            eq_hp  += int(eq_meta.get("hp", 0))
            ba_eqp += int(eq_meta.get("fightValue", 150))
        else:
            eq_att += 100
            eq_def += 50
            eq_hp += 500
            ba_eqp += 150

    # 战斗力分项使用一套统一权重，官方原始装备/宠物/服饰分值作为输入。
    # 等级贡献给稳定底分；养成项随真实装备、出战宠物和守护配置变化。
    # 最终总值封顶 200 万，避免异常存档或活动数值把角色战力顶穿。
    ba_lv = lv * 2000

    # 3. 技能战力加成 (从存档真实技能列表中累加)
    skills_list = player_data.get("skills", {}).get("list", [])
    ba_sk = sum(max(0, int(s.get("ck1", 0))) * 50 for s in skills_list if isinstance(s, dict))

    # 4. VIP 战力加成 (1~7级)
    v_lv = max(0, min(7, int(b.get("vip_level", 7))))
    ba_vp = v_lv * 5000 if b.get("is_vip", True) else 0

    # 官方 calculateBaLv 与宠物 pba、守护总分和服饰战力换算到统一量级。
    ba_eqp = min(1_000_000, ba_eqp * 12)
    pets = [pet for pet in player_data.get("pets", []) if isinstance(pet, dict)]
    follow_id = int(player_data.get("follow_pet_id", 0) or 0)
    follow_pet = next((pet for pet in pets if int(pet.get("pi", 0) or 0) == follow_id), None)
    ba_pt = min(250_000, _pet_battle_power(follow_pet) * 12) if follow_pet else 0

    guardians = player_data.get("guardians") or player_data.get("guard_slots") or {}
    pets_by_id = {int(pet.get("pi", 0) or 0): pet for pet in pets}
    guard_raw = 0
    if isinstance(guardians, dict):
        for pet_id in guardians.values():
            try:
                pet = pets_by_id.get(int(pet_id))
            except (TypeError, ValueError):
                pet = None
            if pet:
                pet_lv = max(1, min(100, int(pet.get("lv", 1) or 1)))
                pet_evo = max(0, min(7, int(pet.get("ecs", 0) or 0)))
                guard_raw += pet_lv * 150 + pet_evo * 500
    ba_pg = min(350_000, guard_raw * 3)
    ba_clo = min(300_000, _clothes_battle_power(player_data) * 8)
    ba_cg = 0
    ba_dp = 0
    ba_fp = 0

    total_mhp = base_mhp + eq_hp
    total_att = base_att + eq_att
    total_def = base_def + eq_def
    ba_parts = {
        "ba_lv": ba_lv, "ba_eqp": ba_eqp, "ba_sk": ba_sk, "ba_vp": ba_vp,
        "ba_pt": ba_pt, "ba_pg": ba_pg, "ba_clo": ba_clo,
        "ba_cg": ba_cg, "ba_dp": ba_dp, "ba_fp": ba_fp,
    }
    raw_ba_total = sum(ba_parts.values())
    if raw_ba_total > 2_000_000:
        scale = 2_000_000 / raw_ba_total
        ba_parts = {key: int(value * scale) for key, value in ba_parts.items()}
    ba_lv = ba_parts["ba_lv"]
    ba_eqp = ba_parts["ba_eqp"]
    ba_sk = ba_parts["ba_sk"]
    ba_vp = ba_parts["ba_vp"]
    ba_pt = ba_parts["ba_pt"]
    ba_pg = ba_parts["ba_pg"]
    ba_clo = ba_parts["ba_clo"]
    ba_cg = ba_parts["ba_cg"]
    ba_dp = ba_parts["ba_dp"]
    ba_fp = ba_parts["ba_fp"]
    ba_t = sum(ba_parts.values())

    exp = int(b.get("total_exp", b.get("exp", 0)))
    current_exp = int(b.get("exp", 0))
    nx_exp = 99999 if lv >= 100 else (lv * 1000 + 500)

    # 守护槽按客户端 setGuardPet 规则折算：宠物属性 * (2% + 守护石等级%)。
    guard_slots = player_data.get("guardians") or player_data.get("guard_slots") or {}
    pets_dict = {int(p.get("pi", 0)): p for p in player_data.get("pets", []) if isinstance(p, dict)}

    guard_att_add = 0
    guard_def_add = 0
    guard_hp_add = 0
    guard_mp_add = 0
    guard_cri_add = 0

    for slot_id, pet_key in guard_slots.items():
        try:
            pid = int(pet_key)
        except (ValueError, TypeError):
            continue

        if pid in pets_dict:
            pet = pets_dict[pid]
            pet_stats = calc_pet_guard_stats(pet)
            stone_levels = player_data.get("guardian_stone_levels", {})
            try:
                stone_level = int(stone_levels.get(str(slot_id), 1)) if isinstance(stone_levels, dict) else 1
            except (TypeError, ValueError):
                stone_level = 1
            rate = 0.02 + max(0, min(10, stone_level)) * 0.01
            slot_str = str(slot_id)
            if slot_str == "1":
                guard_att_add += int(pet_stats["att"] * rate)
            elif slot_str == "2":
                guard_def_add += int(pet_stats["def"] * rate)
            elif slot_str == "3":
                guard_hp_add += int(pet_stats["hp"] * rate)
            elif slot_str == "4":
                guard_mp_add += int(pet_stats["mp"] * rate)
            elif slot_str == "5":
                guard_cri_add += round(pet_stats["cri"] * rate, 1)

    # 兵团援助套装效果折算 (传入当前 player_data，防止循环 load_player 造成递归深度爆炸)
    corps_bonus = calc_player_corps_bonus(char_id, player_data=player_data)

    total_att += guard_att_add + corps_bonus["att"]
    total_def += guard_def_add + corps_bonus["def"]
    total_mhp += guard_hp_add + corps_bonus["hp"]
    base_mmp += guard_mp_add + corps_bonus["mp"]
    cri_val = round(cri_val + guard_cri_add + corps_bonus["cri"], 1)

    min_att = int(total_att * 0.95)
    max_att = total_att

    return {
        "ci": float(char_id),
        "n": user_name,
        "dn": user_name,
        "lv": lv,
        "plv": lv,
        "j": job_name,
        "originaljob": job_id,
        "mhp": total_mhp,
        "chp": total_mhp,
        "mmp": base_mmp,
        "cmp": base_mmp,
        "att": f"{min_att}#{max_att}",
        "def": f"{total_def}#{total_def}",
        "str": f"{base_stat}#{base_stat}",
        "agi": f"{base_stat}#{base_stat}",
        "vit": f"{base_stat}#{base_stat}",
        "itl": f"{base_stat}#{base_stat}",
        "con": f"{base_stat}#{base_stat}",
        "stm": f"{base_stat}#{base_stat}",
        "cri": f"{cri_val}#{cri_val}",
        "hit": f"{hit_val}#{hit_val}",
        "eva": f"{eva_val}#{eva_val}",
        "atE": "0#0",
        "dfE": "0#0",
        "ba_lv": ba_lv,
        "ba_t": ba_t,
        "ba_vp": ba_vp,
        "ba_sk": ba_sk,
        "ba_eqp": ba_eqp,
        "ba_pt": ba_pt,
        "ba_pg": ba_pg,
        "ba_clo": ba_clo,
        "ba_cg": ba_cg,
        "ba_dp": ba_dp,
        "ba_fp": ba_fp,
        "exp": exp,
        "cle": current_exp,
        "nx": nx_exp,
        "job_name": job_name,
        "mnA": min_att,
        "mxA": max_att,
    }


def load_player(char_id: int = 88343134) -> Dict[str, Any]:
    """
    加载指定角色的存档 JSON 数据。若不存在则初始化默认存档并保存。
    若玩家等级大于 1，自动清洗新手任务状态与主城房间，防止 HUD 锁死。
    """
    filepath = get_player_file(char_id)
    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "basic" in data:
                    needs_save = False

                    # 自动补全龙币、VIP与战宠拓展字段
                    b = data.setdefault("basic", {})
                    if "temp_scores" not in data:
                        data["temp_scores"] = {}
                        needs_save = True
                    scores = data["temp_scores"]
                    if not isinstance(scores, dict):
                        data["temp_scores"] = scores = {}
                        needs_save = True
                    pet_exp = max(0, int(scores.get("4", 0) or 0))
                    if scores.get("4") != pet_exp:
                        scores["4"] = pet_exp
                        needs_save = True
                    if "msp" not in b:
                        b["msp"] = 100
                        needs_save = True
                    if "csp" not in b:
                        b["csp"] = b["msp"]
                        needs_save = True
                    b.setdefault("dragon_coin", 999999)
                    v_lvl = int(b.get("vip_level", 7))
                    if v_lvl > 10:
                        b["vip_level"] = 10
                        needs_save = True
                    b.setdefault("vip_level", 7)
                    b.setdefault("is_vip", True)
                    b.setdefault("is_annual", True)
                    b.setdefault("is_super_vip", True)
                    b.setdefault("super_vip_days", 3650)

                    p_def = None
                    if "pets" not in data or not data["pets"]:
                        if data.get("pet"):
                            p = dict(data["pet"])
                            p.setdefault("ecs", 5)
                            p.setdefault("eskill", 5)
                            data["pets"] = [p]
                        else:
                            p_def = get_default_player(char_id)
                            data["pets"] = p_def["pets"]
                            data["pet"] = p_def["pet"]
                        needs_save = True
                    if data.get("pets"):
                        for p in data["pets"]:
                            p_name = str(p.get("n", ""))
                            p_pti = p.get("pti")
                            if not p_name or p_name.startswith("战宠_") or p_name.startswith("战宠") or p_name.isdigit():
                                official_n = PET_OFFICIAL_NAMES.get(str(p_pti)) or PET_NAMES.get(p_pti)
                                if official_n:
                                    p["n"] = official_n
                                    p["ptn"] = official_n
                                    needs_save = True
                        data.setdefault("follow_pet_id", data["pets"][0].get("pi", 1))
                    else:
                        data.setdefault("follow_pet_id", 0)

                    if data.get("pet"):
                        pet_obj = data["pet"]
                        p_name = str(pet_obj.get("n", ""))
                        p_pti = pet_obj.get("pti")
                        if not p_name or p_name.startswith("战宠_") or p_name.startswith("战宠") or p_name.isdigit():
                            official_n = PET_OFFICIAL_NAMES.get(str(p_pti)) or PET_NAMES.get(p_pti)
                            if official_n:
                                pet_obj["n"] = official_n
                                pet_obj["ptn"] = official_n
                                needs_save = True

                    job_id = int(b.get("job", 1))
                    expected_skills = set(JOB_DEFAULT_SKILLS.get(job_id, JOB_DEFAULT_SKILLS[1]))
                    skills_data = data.get("skills")
                    if not isinstance(skills_data, dict):
                        skills_data = {"sp": 100, "list": get_job_initial_skills(job_id)}
                        data["skills"] = skills_data
                        needs_save = True
                    current_skills = skills_data.get("list", [])
                    if not isinstance(current_skills, list):
                        current_skills = []
                    # Valid skill IDs include advanced-job ranges (for example 51000+).
                    # The old >=15000 check erased those skills on every login.
                    preserved_skills = []
                    seen_skill_ids = set()
                    for skill in current_skills:
                        if not isinstance(skill, dict):
                            continue
                        try:
                            skill_id = int(skill.get("id", 0) or 0)
                        except (TypeError, ValueError):
                            continue
                        if skill_id not in expected_skills or skill_id in seen_skill_ids:
                            continue
                        seen_skill_ids.add(skill_id)
                        try:
                            preserved_skills.append({
                                "id": skill_id,
                                "ck0": max(0, int(skill.get("ck0", 0) or 0)),
                                "ck1": max(0, int(skill.get("ck1", 0) or 0)),
                                "ck2": max(0, int(skill.get("ck2", 0) or 0)),
                                "ck3": max(0, int(skill.get("ck3", 0) or 0)),
                            })
                        except (TypeError, ValueError):
                            continue
                    if not preserved_skills:
                        preserved_skills = get_job_initial_skills(job_id)
                    if skills_data.get("list") != preserved_skills:
                        skills_data["list"] = preserved_skills
                        needs_save = True
                    try:
                        saved_sp = max(0, int(skills_data.get("sp", 100)))
                    except (TypeError, ValueError):
                        saved_sp = 100
                    if skills_data.get("sp") != saved_sp:
                        skills_data["sp"] = saved_sp
                        needs_save = True
                    if _sync_level_unlocked_skills(data):
                        needs_save = True

                    if "mounts" not in data:
                        data["mounts"] = []
                        needs_save = True
                    if not data.get("mounts_seeded", False):
                        if not data["mounts"]:
                            data["mounts"] = [{
                                "rid": 1,
                                "pid": 100001,
                                "end": 4133980800000,
                            }]
                            data.setdefault("default_rider_id", 1)
                        data["mounts_seeded"] = True
                        needs_save = True
                    if "default_rider_id" not in data:
                        data["default_rider_id"] = (
                            int(data["mounts"][0].get("rid", -1))
                            if data["mounts"] and isinstance(data["mounts"][0], dict)
                            else -1
                        )
                        needs_save = True
                    data.setdefault("guarding_rider_id", -1)
                    data.setdefault("riding_rid", 0)
                    data.setdefault("riding_pet_id", 0)

                    bag = data.setdefault("bag", {})
                    if "gems" not in bag:
                        bag["gems"] = {}
                        needs_save = True
                    if "items" not in bag:
                        if not p_def:
                            p_def = get_default_player(char_id)
                        bag["items"] = p_def["bag"]["items"]
                        bag["equip"] = p_def["bag"]["equip"]
                        needs_save = True

                    items = bag.setdefault("items", [])
                    cleaned_items = []
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        mid = int(it.get("mid", 0))
                        t = int(it.get("t", 0))

                        if mid == 100140 or mid in PET_NAMES:
                            pets_list = data.setdefault("pets", [])
                            if not any(p.get("pti") == mid for p in pets_list):
                                new_pi = max([p.get("pi", 0) for p in pets_list], default=0) + 1
                                p_name = PET_NAMES.get(mid, f"战宠_{mid}")
                                pets_list.append({
                                    "pi": new_pi,
                                    "pti": mid,
                                    "ptn": p_name,
                                    "n": p_name,
                                    "lv": 100,
                                    "ecs": 5,
                                    "eskill": 5,
                                    "e": 9999999,
                                    "chp": 120000,
                                    "mhp": 120000,
                                    "mnA": 15000,
                                    "mxA": 25000,
                                    "sk": [{"ski": 32071, "skl": 1}],
                                    "bot": 4,
                                    "bi": 2499,
                                    "isevo": True,
                                    "t": 1,
                                    "eid": 0,
                                })
                            needs_save = True
                            continue

                        if t == 2 and mid == 4:
                            data.setdefault("orphaned_inventory", []).append({
                                **dict(it),
                                "quarantine_reason": "legacy temporary pet-experience reward",
                            })
                            needs_save = True
                            continue

                        try:
                            try:
                                correct_type = resolve_inventory_type(mid, t)
                            except ValueError:
                                if t not in (0, 2, 3, 4):
                                    raise
                                correct_type = resolve_inventory_type(mid)
                            if correct_type != t:
                                it["t"] = correct_type
                                needs_save = True
                        except ValueError as exc:
                            data.setdefault("orphaned_inventory", []).append({
                                **dict(it),
                                "quarantine_reason": str(exc),
                            })
                            needs_save = True
                            continue

                        if correct_type == 0:
                            if it.get("si") != "":
                                it["si"] = ""
                                needs_save = True
                        elif it.get("pl", 0) not in (102, 103):
                            it["pl"] = 102
                            needs_save = True
                        cleaned_items.append(it)
                    bag["items"] = cleaned_items

                    legacy_equipped_mids = {
                        int(it.get("mid", 0))
                        for it in cleaned_items
                        if (
                            int(it.get("mid", 0)) in SEEDED_STARTER_EQUIPMENT
                            and int(it.get("pl", 0) or 0) in range(1, 16)
                        )
                    }
                    if len(legacy_equipped_mids) >= 5:
                        for it in cleaned_items:
                            if int(it.get("mid", 0)) in legacy_equipped_mids:
                                it["pl"] = 100
                        needs_save = True
                    else:
                        legacy_equipped_mids.clear()

                    equip_list = bag.setdefault("equip", [])
                    cleaned_equip = [
                        eq for eq in equip_list
                        if eq in GLOBAL_EQUIP_CONFIG and eq not in legacy_equipped_mids
                    ]
                    if len(cleaned_equip) != len(equip_list):
                        bag["equip"] = cleaned_equip
                        needs_save = True

                    curr_lv = int(b.get("level", 1))
                    if curr_lv > 1:
                        tasks = data.setdefault("tasks", {})
                        t513 = tasks.setdefault("513", {})
                        if t513.get("st") != 1 or t513.get("ti") != "513_8":
                            now_ms = float(int(time.time() * 1000))
                            tasks["513"] = {
                                "taskId": 513,
                                "step": 8,
                                "st": 1,
                                "ti": "513_8",
                                "ad": t513.get("ad", now_ms),
                                "fd": now_ms,
                                "ex": "",
                                "tm": 1,
                            }
                            needs_save = True

                        pos = data.setdefault("position", {})
                        cur_room = str(pos.get("last_room", ""))
                        if "shenyuanroad" in cur_room or not cur_room:
                            pos["last_room"] = "momomaintown_1"
                            pos["x"] = 60
                            pos["y"] = 40
                            needs_save = True

                        # 动态同步更新合理血量
                        calc_props = calc_player_properties(char_id, player_data=data)
                        if b.get("mhp", 0) < 2000:
                            b["mhp"] = calc_props["mhp"]
                            b["chp"] = calc_props["chp"]
                            b["mmp"] = calc_props["mmp"]
                            b["cmp"] = calc_props["cmp"]
                            needs_save = True

                    if needs_save:
                        save_player(data, char_id=char_id)

                    return data
        except Exception as e:
            logger.warning("读取角色存档 %s 失败，将恢复默认: %s", filepath, e)

    default_data = get_default_player(char_id=char_id)
    save_player(default_data, char_id=char_id)
    return default_data


def save_player(player_data: Dict[str, Any], char_id: Optional[int] = None) -> bool:
    """
    保存角色数据到本地 JSON 文件 (原子写入保护)。
    """
    cid = char_id or player_data.get("char_id") or player_data.get("basic", {}).get("char_id", 88343134)
    filepath = get_player_file(cid)
    tmp_path = filepath + ".tmp"

    try:
        data_str = json.dumps(player_data, ensure_ascii=False, indent=2)
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(data_str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, filepath)
        except PermissionError:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(data_str)
        return True
    except Exception as e:
        logger.error("保存角色存档到 %s 失败: %s", filepath, e)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return False


def reset_player(char_id: int = 88343134) -> Dict[str, Any]:
    """重置指定角色数据为初始新号状态。"""
    default_data = get_default_player(char_id=char_id)
    save_player(default_data, char_id=char_id)
    logger.info("已重置角色 %s 存档数据", char_id)
    return default_data


# -------------------------------------------------------------
# 字段更新辅助接口 (Gateway / HTTP 联动调用)
# -------------------------------------------------------------

def update_task(
    char_id: int,
    task_id: Union[str, int],
    step: int,
    st: int,
    ti: str,
    tm: int = 0,
    ex: str = "",
    fd: Optional[float] = None,
) -> Dict[str, Any]:
    """更新角色指定任务状态 (对齐 AS3 UserTask: ti, st, ex, ad, fd, tm)。"""
    player = load_player(char_id)
    tid_str = str(task_id)
    now_ms = float(int(time.time() * 1000))

    task_entry = player.setdefault("tasks", {}).setdefault(tid_str, {
        "taskId": int(tid_str) if tid_str.isdigit() else 513,
        "ad": now_ms,
    })

    task_entry["step"] = step
    task_entry["st"] = st
    task_entry["ti"] = ti
    task_entry["tm"] = tm
    task_entry["ex"] = ex
    if st == 1:
        task_entry["fd"] = fd if fd is not None else now_ms

    save_player(player, char_id=char_id)
    return player


def is_tutorial_completed(char_id: int = 88343134) -> bool:
    """检查新手教程 (任务 513) 是否已经完结。"""
    player = load_player(char_id)
    task_513 = player.get("tasks", {}).get("513", {})
    return task_513.get("st") == 1 or task_513.get("ti") == "513_8" and task_513.get("step") == 8


def complete_tutorial(char_id: int = 88343134, target_room: Optional[str] = None) -> Dict[str, Any]:
    """标记完成新手教程。"""
    player = load_player(char_id)
    now_ms = float(int(time.time() * 1000))

    player.setdefault("tasks", {})["513"] = {
        "taskId": 513,
        "step": 8,
        "st": 1,
        "ti": "513_8",
        "ad": player.get("tasks", {}).get("513", {}).get("ad", now_ms),
        "fd": now_ms,
        "ex": "",
        "tm": 1,
    }

    if not target_room:
        target_room = f"newjobfbyongwangge_{char_id}_0_tr"

    player.setdefault("position", {})["last_room"] = target_room
    save_player(player, char_id=char_id)
    logger.info("玩家 %d 新手引导已完成，位置更新为: %s", char_id, target_room)
    return player


def update_position(char_id: int, last_room: str, x: Optional[int] = None, y: Optional[int] = None) -> Dict[str, Any]:
    """更新角色当前所在场景房间及坐标。"""
    player = load_player(char_id)
    pos = player.setdefault("position", {})
    pos["last_room"] = last_room
    if x is not None:
        pos["x"] = x
    if y is not None:
        pos["y"] = y
    save_player(player, char_id=char_id)
    return player


def _apply_experience(basic: Dict[str, Any], exp_gain: int) -> Tuple[int, int]:
    """Add total/current experience and apply level thresholds; return old/new levels."""
    old_level = max(1, int(basic.get("level", 1)))
    total_exp = int(basic.get("total_exp", basic.get("exp", 0))) + exp_gain
    current_exp = int(basic.get("exp", 0)) + exp_gain
    level = old_level
    while level < 100:
        next_exp = level * 1000 + 500
        if current_exp < next_exp:
            break
        current_exp -= next_exp
        level += 1
    basic["total_exp"] = total_exp
    basic["exp"] = current_exp
    basic["level"] = level
    return old_level, level


def update_basic(
    char_id: int,
    level: Optional[int] = None,
    exp_gain: int = 0,
    coin_gain: int = 0,
    user_name: Optional[str] = None,
) -> Dict[str, Any]:
    """更新基础属性：经验、等级、龙币、名字。"""
    player = load_player(char_id)
    basic = player.setdefault("basic", {})
    old_level = max(1, int(basic.get("level", 1) or 1))
    if level is not None:
        basic["level"] = level
    if exp_gain > 0:
        _apply_experience(basic, exp_gain)
    new_level = max(1, int(basic.get("level", old_level) or old_level))
    if new_level > old_level:
        skills_data = player.setdefault("skills", {"sp": 100, "list": []})
        try:
            current_skill_points = max(0, int(skills_data.get("sp", 100)))
        except (TypeError, ValueError):
            current_skill_points = 100
        # Client SkillHudManager exposes the learned-skill prompt at level 12;
        # SkillDataService expects the server to award SP when the level rises.
        skills_data["sp"] = current_skill_points + (new_level - old_level)
    _sync_level_unlocked_skills(player)
    if coin_gain > 0:
        basic["coin"] = basic.get("coin", 0) + coin_gain
    if user_name is not None:
        basic["user_name"] = user_name
        player["user_name"] = user_name
    save_player(player, char_id=char_id)
    return player


def add_bag_item(
    char_id: int,
    mid: int,
    count: int = 1,
    gid: Optional[int] = None,
    item_type: Optional[int] = None,
    t: Optional[int] = None,
) -> Dict[str, Any]:
    """向角色背包添加道具/装备，返回新加/更新的物品对象。"""
    requested_type = t if t is not None else item_type
    if count <= 0:
        raise ValueError(f"Inventory item count must be positive: {count}")
    real_t = resolve_inventory_type(mid, requested_type)
    is_eq = real_t == 0
    player = load_player(char_id)
    items = player.setdefault("bag", {}).setdefault("items", [])

    real_pl = 100 if is_eq else 102

    existing = next(
        (
            it for it in items
            if it.get("mid") == mid
            and int(it.get("t", real_t)) == real_t
            and not is_eq
        ),
        None,
    )
    if existing:
        existing["n"] = existing.get("n", 1) + count
        save_player(player, char_id=char_id)
        logger.info("玩家 %d 道具 mid=%d 叠加数量至 %d", char_id, mid, existing["n"])
        return existing
    else:
        if gid is None:
            used_gids = {it.get("gid", 0) for it in items}
            gid = 1
            while gid in used_gids:
                gid += 1
        new_entry = {
            "gid": gid,
            "mid": mid,
            "n": count,
            "t": real_t,
            "pl": real_pl,
        }
        if is_eq:
            max_endurance = max(0, int(GLOBAL_EQUIP_CONFIG[mid].get("endurance", 65)))
            new_entry["si"] = ""
            new_entry["cd"] = 0
            new_entry["cl"] = 0
            new_entry["tsk"] = 3
            new_entry["ce"] = max_endurance
            new_entry["ia"] = False
            new_entry["cb"] = True
            new_entry["egs"] = ""
            new_entry["bp"] = {
                "lp": 0.0,
                "ep": 0.36,
                "sp": 0.28,
                "bp": 0.36,
            }
        items.append(new_entry)
        save_player(player, char_id=char_id)
        logger.info("玩家 %d 背包获得物品 mid=%d x%d (gid=%d, t=%d, pl=%d)", char_id, mid, count, gid, real_t, real_pl)
        return new_entry


def add_equip_gem(char_id: int, gem_id: int, count: int = 1) -> Dict[str, int]:
    """Add an official equipment gem to its dedicated client gem inventory."""
    if count <= 0:
        raise ValueError(f"Equipment gem count must be positive: {count}")
    resolve_inventory_type(gem_id, 200)
    player = load_player(char_id)
    gems = player.setdefault("bag", {}).setdefault("gems", {})
    key = str(int(gem_id))
    gems[key] = int(gems.get(key, 0)) + int(count)
    save_player(player, char_id=char_id)
    return {"g": int(gem_id), "c": gems[key]}


def get_equip_gem_list(char_id: int) -> List[Dict[str, int]]:
    """Build the full gem-count vector expected by command 8_2_24."""
    player = load_player(char_id)
    owned_gems = player.get("bag", {}).get("gems", {})
    return [
        {"g": gem_id, "c": int(owned_gems.get(str(gem_id), 0))}
        for gem_id in sorted(GLOBAL_EQUIP_GEM_IDS)
    ]


def add_equip(char_id: int, equip_id: int) -> Dict[str, Any]:
    """更新角色已装备列表。"""
    player = load_player(char_id)
    equip_list = player.setdefault("bag", {}).setdefault("equip", [])
    if equip_id not in equip_list:
        equip_list.append(equip_id)
        save_player(player, char_id=char_id)
    return player


def add_cloth(
    char_id: int,
    cloth_id: int,
    expire_at_ms: Optional[int] = None,
) -> Dict[str, Any]:
    """更新角色已拥有服饰列表。"""
    player = load_player(char_id)
    bag = player.setdefault("bag", {})
    cloth_list = bag.setdefault("cloth", [])
    if "wearing_cloth" not in bag:
        bag["wearing_cloth"] = list(cloth_list)
    expiry_map = bag.setdefault("cloth_expiry", {})
    if cloth_id not in cloth_list:
        cloth_list.append(cloth_id)
    if expire_at_ms is not None:
        expiry_map[str(cloth_id)] = int(expire_at_ms)
    save_player(player, char_id=char_id)
    return player


def get_cloth_expiry(char_id: int, cloth_id: int) -> int:
    player = load_player(char_id)
    return int(
        player.get("bag", {}).get("cloth_expiry", {}).get(str(cloth_id), 7614748800000)
    )


def get_owned_clothes(char_id: int = 88343134) -> List[int]:
    """获取已拥有服饰 ID；wearing_cloth 单独保存当前穿戴状态。"""
    player = load_player(char_id)
    return list(player.get("bag", {}).get("cloth", []))


def get_wearing_clothes(char_id: int = 88343134) -> List[int]:
    """获取当前穿戴服饰，兼容尚未迁移的旧存档。"""
    player = load_player(char_id)
    bag = player.get("bag", {})
    return list(bag.get("wearing_cloth", bag.get("cloth", [])))


PET_NAMES = {
    100408: "亚克",
    100140: "小炎狮",
    100335: "天极雷神",
    100375: "克利夫",
    100491: "天照真龙王",
    100492: "不朽·天照龙王",
    100494: "暗灭帝尊",
    100495: "不朽·暗灭帝尊",
    100496: "机神·朱雀骑士",
    100497: "机神·青龙女帝",
    100498: "机神·白虎战神",
    100499: "机神·玄武剑圣",
    100500: "机神·太阳贤者",
    100501: "机神·暗月死神",
    100502: "无极东皇",
    100503: "不朽·无极东皇",
    100504: "能天使加西亚",
    100505: "力天使奥菲丁",
    100506: "座天使伊瑟拉",
    100507: "智天使乌利尔",
    100508: "炽天使加百列",
    100509: "华光龙帝",
    100510: "次元‧华光龙帝",
    100511: "飓风龙皇",
    100512: "次元·飓风龙皇",
    100513: "爆炎龙神",
    100514: "次元·爆炎龙神",
    100515: "怒海龙王",
    100516: "次元·怒海龙王",
    100517: "漆黑龙帝",
    100518: "次元·漆黑龙帝",
    100519: "地核天尊",
    100520: "次元·地核天尊",
    100521: "汪洋女帝",
    100522: "次元·汪洋女帝",
    100523: "轰极雷神",
    100524: "次元·轰极雷神",
    100525: "天火炎王",
    100526: "次元·天火炎王",
    100527: "风魔神羽",
    100528: "次元·风魔神羽",
}

PET_OFFICIAL_NAMES: Dict[str, str] = {}
PET_OFFICIAL_GIFTS: Dict[str, Dict[str, Any]] = {}
PET_OFFICIAL_SCORES: Dict[str, Dict[str, Any]] = {}
_OFFICIAL_DATA_PATH = os.path.join(DATA_DIR, "pet_catalog.json")
if os.path.exists(_OFFICIAL_DATA_PATH):
    try:
        with open(_OFFICIAL_DATA_PATH, "r", encoding="utf-8") as f:
            _pod = json.load(f)
            PET_OFFICIAL_NAMES = _pod.get("names", {})
            PET_OFFICIAL_GIFTS = _pod.get("gifts", {})
            PET_OFFICIAL_SCORES = _pod.get("scores", {})
            for _pid_str, _pname in PET_OFFICIAL_NAMES.items():
                if _pid_str.isdigit() and _pname:
                    PET_NAMES[int(_pid_str)] = _pname
    except Exception as _e:
        logger.warning("加载 pet_catalog.json 失败: %s", _e)


GLOBAL_CLOTHES_BA: Dict[int, int] = {}
GLOBAL_CLOTHES_SUIT_BA: List[Tuple[set[int], List[Tuple[int, int]]]] = []


def _load_battle_power_clothes() -> None:
    """Load clothing and set battle-power values from the official client tables."""
    import re

    app_path = find_json_file("ClothesDatas_appDataClass.xml")
    suit_path = find_json_file("ClothesDatas_suitDataClass.xml")
    if app_path:
        try:
            root = ET.parse(app_path).getroot()
            for node in root.iter("clothes"):
                cloth_id = int(node.get("id", 0) or 0)
                match = re.search(r"战斗力\s*\+\s*(\d+)", node.get("app", ""))
                if cloth_id and match:
                    GLOBAL_CLOTHES_BA[cloth_id] = int(match.group(1))
        except (OSError, ET.ParseError, ValueError) as exc:
            logger.warning("加载服饰战力表失败: %s", exc)
    if suit_path:
        try:
            with open(suit_path, "r", encoding="utf-8", errors="replace") as source:
                for line in source:
                    columns = line.strip().split(";")
                    if len(columns) < 5:
                        continue
                    ids = {int(value) for value in columns[2].split(",") if value.isdigit()}
                    thresholds = []
                    for effect in columns[4].split("="):
                        count_text, sep, bonuses = effect.partition(":")
                        if not sep or not count_text.isdigit():
                            continue
                        match = re.search(r"战斗力\s*\+\s*(\d+)", bonuses)
                        if match:
                            thresholds.append((int(count_text), int(match.group(1))))
                    if ids and thresholds:
                        GLOBAL_CLOTHES_SUIT_BA.append((ids, thresholds))
        except OSError as exc:
            logger.warning("加载服饰套装战力表失败: %s", exc)


_load_battle_power_clothes()


def _pet_battle_power(pet: Dict[str, Any]) -> int:
    """Use the same gift/stage formula as the client pet detail response."""
    pti = int(pet.get("pti", 100408) or 100408)
    level = max(1, min(100, int(pet.get("lv", 1) or 1)))
    evo = max(0, min(7, int(pet.get("ecs", 0) or 0)))
    gift = PET_OFFICIAL_GIFTS.get(str(pti), {})
    strength = int(gift.get("strength", 390))
    intelligence = int(gift.get("intelligence", 0))
    stage_rate = [0.0, 0.06, 0.14, 0.22, 0.31, 0.40, 0.50, 0.60][evo]
    value = int(level * ((strength + intelligence) * 0.05 - 5) * (1 + 0.15 * (10 + level) * 0.1) * 1.6 * (1 + stage_rate))
    if value <= 0:
        value = int(int(PET_OFFICIAL_SCORES.get(str(pti), {}).get("fightscore", 100)) * level / 100)
    return max(0, value)


def _clothes_battle_power(player_data: Dict[str, Any]) -> int:
    bag = player_data.get("bag", {})
    wearing = bag.get("wearing_cloth", bag.get("cloth", []))
    try:
        worn_ids = {int(value) for value in wearing}
    except (TypeError, ValueError):
        worn_ids = set()
    raw = sum(GLOBAL_CLOTHES_BA.get(cloth_id, 0) for cloth_id in worn_ids)
    for ids, thresholds in GLOBAL_CLOTHES_SUIT_BA:
        count = len(ids & worn_ids)
        raw += sum(value for needed, value in thresholds if count >= needed)
    return raw


def calc_pet_guard_stats(pet: Dict[str, Any]) -> Dict[str, float]:
    """Return the exact combat fields used by the guardian recommendation panel."""
    pti = int(pet.get("pti", 100408) or 100408)
    level = max(1, min(100, int(pet.get("lv", 1) or 1)))
    evo = max(0, min(7, int(pet.get("ecs", 0) or 0)))
    gift = PET_OFFICIAL_GIFTS.get(str(pti), {})
    strength = int(gift.get("strength", 390))
    intelligence = int(gift.get("intelligence", 0))
    constitution = int(gift.get("constitution", 390))
    stamina = int(gift.get("stamina", 390))
    vitality = int(gift.get("vitality", 390))
    agility = int(gift.get("agility", 390))
    stage_rate = [0.0, 0.06, 0.14, 0.22, 0.31, 0.40, 0.50, 0.60][evo]
    level_factor = (1.24 + 0.1 * level) * (0.438 + 0.0063 * level) * (stage_rate + 1)
    str_value = int((0.5 + 1.71875 * level) * ((strength - 100.0) / 50.0) * level_factor)
    int_value = int((0.5 + 1.71875 * level) * ((intelligence - 100.0) / 50.0) * level_factor) if intelligence > 0 else 0
    con_value = int((0.46875 + 1.40625 * level) * ((constitution - 100.0) / 50.0) * level_factor)
    stm_value = int((0.625 + 2.5 * level) * ((stamina - 100.0) / 50.0) * level_factor)
    vit_value = int((0.40625 + 1.09375 * level) * ((vitality - 100.0) / 50.0) * level_factor)
    agi_value = int((0.4375 + 1.5625 * level) * ((agility - 100.0) / 50.0) * level_factor)
    attack_gift = max(strength, intelligence)
    attack_attr = max(str_value, int_value)
    common = (stage_rate + 1) * (8.86 + 0.7 * level) * (0.438 + 0.0063 * level)
    return {
        "att": max(10, int(common * (0.390625 + 0.625 * level) * ((attack_gift - 100.0) / 50.0) + attack_attr * 1.2)),
        "def": int(common * (6.25 + 12.5 * level) * ((stamina - 100.0) / 50.0) + stm_value * 15.0),
        "hp": max(100, int(common * (9.375 + 18.75 * level) * ((constitution - 100.0) / 50.0) + con_value * 25.0)),
        "mp": max(50, int(common * (5.46875 + 10.9375 * level) * ((vitality - 100.0) / 50.0) + vit_value * 15.0)),
        "cri": round(max(0, agi_value * 3.0), 1),
    }

GLOBAL_CORPS_SUITS: List[Dict[str, Any]] = []

def init_pet_corps_suits() -> None:
    """加载官方 38 个兵团援助套装定义 PetGuardSuitDatas_dataClass.xml。"""
    global GLOBAL_CORPS_SUITS
    xml_path = find_json_file("PetGuardSuitDatas_dataClass.xml")
    if not xml_path or not os.path.exists(xml_path):
        return
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        suits = []
        for row in root.findall("row"):
            sid = row.find("SuitId")
            stype = row.find("Type")
            pids = row.find("PIds")
            evo_classes = row.find("EvoClass")
            name = row.find("SuitName")
            app = row.find("App")
            if sid is not None and pids is not None and sid.text and pids.text:
                suits.append({
                    "id": int(sid.text.strip()),
                    "type": int(stype.text.strip()) if stype is not None and stype.text else 0,
                    "name": name.text.strip() if name is not None and name.text else "",
                    "pids": [int(x.strip()) for x in pids.text.strip().split(",") if x.strip().isdigit()],
                    "evo_classes": [int(x.strip()) for x in evo_classes.text.strip().split(",") if x.strip().isdigit()] if evo_classes is not None and evo_classes.text else [],
                    "app": app.text.strip() if app is not None and app.text else "",
                })
        GLOBAL_CORPS_SUITS = suits
        logger.info("已加载官方兵团援助套装 %d 个", len(GLOBAL_CORPS_SUITS))
    except Exception as e:
        logger.warning("加载 PetGuardSuitDatas_dataClass.xml 失败: %s", e)


def calc_player_corps_bonus(char_id: int = 88343134, player_data: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """计算玩家当前激活的所有兵团援助套装属性总加成。"""
    import re
    if not GLOBAL_CORPS_SUITS:
        init_pet_corps_suits()
    player = player_data if player_data is not None else load_player(char_id)
    guarded_ids = _guard_corps_pet_ids(player)
    guarded_ptis = _guarded_corps_ptis(player, guarded_ids)
    corps_bonus = {"att": 0, "def": 0, "hp": 0, "mp": 0, "cri": 0}

    for suit in GLOBAL_CORPS_SUITS:
        if suit.get("type") != 0:
            continue
        pids = suit.get("pids", [])
        evo_classes = suit.get("evo_classes", [])
        matched = [
            pti for index, pti in enumerate(pids)
            if pti in guarded_ptis and (
                index >= len(evo_classes)
                or guarded_ptis[pti] >= evo_classes[index]
            )
        ]
        matched_count = len(matched)
        app = suit.get("app", "")
        if matched_count <= 0 or not app:
            continue
        suit_buffs = {}
        for segment in app.split("="):
            parts = segment.split(":")
            if len(parts) == 2 and parts[0].isdigit():
                need_cnt = int(parts[0])
                if matched_count >= need_cnt:
                    for b in parts[1].split("#"):
                        m = re.match(r"([^+\d]+)\+(\d+)", b.strip())
                        if m:
                            k, v = m.group(1), int(m.group(2))
                            suit_buffs[k] = v
        for k, v in suit_buffs.items():
            if "攻击" in k:
                corps_bonus["att"] += v
            elif "防御" in k:
                corps_bonus["def"] += v
            elif "生命" in k:
                corps_bonus["hp"] += v
            elif "魔法" in k:
                corps_bonus["mp"] += v
            elif "致命" in k:
                corps_bonus["cri"] += v

    return corps_bonus


def get_player_corps_str(char_id: int = 88343134, player_data: Optional[Dict[str, Any]] = None) -> str:
    """计算玩家兵团援助套装激活字符串 (对齐官方 format: suitId_type:pti,pti#...)。"""
    if not GLOBAL_CORPS_SUITS:
        init_pet_corps_suits()
    player = player_data if player_data is not None else load_player(char_id)
    guarded_ids = _guard_corps_pet_ids(player)
    guarded_ptis = _guarded_corps_ptis(player, guarded_ids)
    matched_parts = []
    for suit in GLOBAL_CORPS_SUITS:
        evo_classes = suit.get("evo_classes", [])
        matched = [
            str(pti) for index, pti in enumerate(suit["pids"])
            if pti in guarded_ptis and (
                index >= len(evo_classes)
                or guarded_ptis[pti] >= evo_classes[index]
            )
        ]
        if matched:
            matched_parts.append(f"{suit['id']}_{suit['type']}:{','.join(matched)}")
    return "#".join(matched_parts)


def _guard_corps_pet_ids(player: Dict[str, Any]) -> set[int]:
    """Client corps helper counts only guardian pets and configured assist pets."""
    ids: set[int] = set()
    guardians = player.get("guardians") or player.get("guard_slots") or {}
    if isinstance(guardians, dict):
        for pet_id in guardians.values():
            try:
                if int(pet_id) > 0:
                    ids.add(int(pet_id))
            except (TypeError, ValueError):
                continue
    assists = player.get("guardian_assists", player.get("guard_assists", []))
    if isinstance(assists, dict):
        assists = list(assists.values())
    if isinstance(assists, (list, tuple, set)):
        for item in assists:
            value = item.get("pi", item.get("piId", 0)) if isinstance(item, dict) else item
            try:
                if int(value) > 0:
                    ids.add(int(value))
            except (TypeError, ValueError):
                continue
    return ids


def _guarded_corps_ptis(player: Dict[str, Any], guarded_ids: set[int]) -> Dict[int, int]:
    """Map guarded template IDs to their best evolution class, matching client suit checks."""
    result: Dict[int, int] = {}
    for pet in player.get("pets", []):
        if not isinstance(pet, dict):
            continue
        try:
            if int(pet.get("pi", 0) or 0) not in guarded_ids:
                continue
            pti = int(pet.get("pti", 0) or 0)
            evo_class = int(pet.get("ecs", 0) or 0)
        except (TypeError, ValueError):
            continue
        if pti > 0:
            result[pti] = max(result.get(pti, 0), evo_class)
    return result


def get_guardians(char_id: int = 88343134) -> Dict[str, int]:
    """获取玩家战宠守护阵位（1-6 槽位映射到 pet_id）。"""
    player = load_player(char_id)
    return player.get("guardians") or player.get("guard_slots", {})


def set_guardian_pet(char_id: int = 88343134, slot: int = 1, pet_id: int = 0) -> Dict[str, int]:
    """设置或卸下指定槽位（1-6）的守护战宠。"""
    player = load_player(char_id)
    guardians = player.setdefault("guardians", dict(player.get("guard_slots", {})))
    slot_key = str(slot)

    if pet_id > 0:
        for k, v in list(guardians.items()):
            # Legacy JSON may contain pet IDs as strings. Normalize while
            # moving a pet so it cannot remain assigned to its old slot too.
            try:
                same_pet = int(v) == pet_id
            except (TypeError, ValueError):
                same_pet = False
            if same_pet:
                del guardians[k]
        guardians[slot_key] = pet_id
    else:
        if slot_key in guardians:
            del guardians[slot_key]

    player["guard_slots"] = dict(guardians)
    save_player(player, char_id=char_id)
    return guardians


def swap_guardian_slots(char_id: int = 88343134, slot_a: int = 1, slot_b: int = 2) -> Dict[str, int]:
    """Swap two guardian slots (slot numbers are 1-based internally)."""
    if not (1 <= slot_a <= 6 and 1 <= slot_b <= 6):
        raise ValueError("guardian slot must be between 1 and 6")
    player = load_player(char_id)
    current = player.get("guardians") or player.get("guard_slots") or {}
    guardians = {str(k): int(v) for k, v in current.items() if int(v or 0) > 0}
    key_a, key_b = str(slot_a), str(slot_b)
    pet_a, pet_b = guardians.get(key_a, 0), guardians.get(key_b, 0)
    if pet_b:
        guardians[key_a] = pet_b
    else:
        guardians.pop(key_a, None)
    if pet_a:
        guardians[key_b] = pet_a
    else:
        guardians.pop(key_b, None)
    player["guardians"] = guardians
    player["guard_slots"] = dict(guardians)
    save_player(player, char_id=char_id)
    return guardians


def calc_guard_power(char_id: int = 88343134) -> int:
    """计算守护神总战力。"""
    player = load_player(char_id)
    guardians = player.get("guardians", player.get("guard_slots", {}))
    pets = player.get("pets", [])
    pets_by_id = {int(p.get("pi", 0)): p for p in pets}
    total_power = 0
    for slot_str, pet_id in guardians.items():
        if pet_id in pets_by_id:
            pet = pets_by_id[pet_id]
            lv = int(pet.get("lv", 100))
            ecs = int(pet.get("ecs", 5))
            total_power += int(lv * 150 + ecs * 500)
    return max(0, total_power)


def one_key_guard(char_id: int = 88343134) -> Dict[str, int]:
    """一键守护：挑选最强战宠自动填满 6 个守护神槽位。"""
    player = load_player(char_id)
    pets = [p for p in player.get("pets", []) if isinstance(p, dict) and p.get("pi")]
    pets.sort(key=lambda p: (int(p.get("lv", 1)), int(p.get("ecs", 0))), reverse=True)
    guardians = {}
    for idx, pet in enumerate(pets[:6]):
        slot = idx + 1
        guardians[str(slot)] = int(pet["pi"])
    player["guardians"] = guardians
    player["guard_slots"] = dict(guardians)
    save_player(player, char_id=char_id)
    return guardians


def get_dragon_coin(char_id: int = 88343134) -> int:
    """获取玩家充值货币 (龙币) 余额。"""
    player = load_player(char_id)
    return player.get("basic", {}).get("dragon_coin", 999999)


def add_dragon_coin(char_id: int, delta: int) -> int:
    """充值或消费龙币。"""
    player = load_player(char_id)
    b = player.setdefault("basic", {})
    cur = b.get("dragon_coin", 999999)
    new_val = max(0, cur + delta)
    b["dragon_coin"] = new_val
    save_player(player, char_id=char_id)
    logger.info("玩家 %d 龙币变动: %d -> %d (delta=%d)", char_id, cur, new_val, delta)
    return new_val


def change_temp_score(char_id: int, score_id: int, delta: int) -> int:
    """Apply a delta to a persisted temporary-score balance and return it."""
    player = load_player(char_id)
    scores = player.setdefault("temp_scores", {})
    key = str(int(score_id))
    current = max(0, int(scores.get(key, 0) or 0))
    updated = max(0, current + int(delta))
    scores[key] = updated
    save_player(player, char_id=char_id)
    return updated


def get_temp_score(char_id: int, score_id: int) -> int:
    player = load_player(char_id)
    scores = player.get("temp_scores", {})
    return max(0, int(scores.get(str(int(score_id)), 0) or 0)) if isinstance(scores, dict) else 0


def set_dragon_coin(char_id: int, amount: int) -> int:
    """直接设置龙币余额。"""
    player = load_player(char_id)
    b = player.setdefault("basic", {})
    new_val = max(0, amount)
    b["dragon_coin"] = new_val
    save_player(player, char_id=char_id)
    logger.info("玩家 %d 龙币设为: %d", char_id, new_val)
    return new_val


def get_vip_info(char_id: int = 88343134) -> Dict[str, Any]:
    """获取玩家圣龙骑士 VIP 信息。"""
    player = load_player(char_id)
    b = player.get("basic", {})
    v_lv = max(0, min(10, int(b.get("vip_level", 7))))
    is_v = b.get("is_vip", True) and v_lv > 0
    return {
        "is_vip": is_v,
        "vip_level": v_lv,
        "is_annual": b.get("is_annual", True),
        "is_super_vip": b.get("is_super_vip", True),
        "super_vip_days": b.get("super_vip_days", 3650),
    }


def set_vip_info(
    char_id: int,
    level: int = 7,
    is_vip: bool = True,
    is_annual: bool = True,
    is_super_vip: bool = True,
    days: int = 3650,
) -> Dict[str, Any]:
    """设置或刷圣龙骑士 VIP 等级 (限制 0~10 级)。"""
    player = load_player(char_id)
    b = player.setdefault("basic", {})
    clamped_lv = max(0, min(10, int(level)))
    b["vip_level"] = clamped_lv
    b["is_vip"] = is_vip and clamped_lv > 0
    b["is_annual"] = is_annual
    b["is_super_vip"] = is_super_vip
    b["super_vip_days"] = days
    save_player(player, char_id=char_id)
    logger.info("玩家 %d VIP 状态已设置: Lv=%d, is_vip=%s", char_id, clamped_lv, b["is_vip"])
    return b


def get_pets(char_id: int = 88343134) -> List[Dict[str, Any]]:
    """获取玩家拥有的所有战宠列表。"""
    player = load_player(char_id)
    return player.get("pets", [])


def add_pet(
    char_id: int,
    pti: int,
    name: Optional[str] = None,
    lv: int = 100,
    ecs: int = 5,
    allow_duplicate: bool = False,
) -> Dict[str, Any]:
    """向角色添加或获得新战宠 (默认高等级高觉醒)。"""
    player = load_player(char_id)
    pets = player.setdefault("pets", [])
    official_n = PET_OFFICIAL_NAMES.get(str(pti)) or PET_NAMES.get(pti)
    pet_name = name or official_n or f"战宠_{pti}"

    if not allow_duplicate:
        for p in pets:
            if p.get("pti") == pti:
                p["ecs"] = max(p.get("ecs", 0), ecs)
                p["eskill"] = max(p.get("eskill", 0), ecs)
                p["lv"] = max(p.get("lv", 1), lv)
                p["isevo"] = p["ecs"] > 0
                save_player(player, char_id=char_id)
                return p

    new_pi = max([p.get("pi", 0) for p in pets], default=0) + 1
    new_pet = {
        "pi": new_pi,
        "pti": pti,
        "ptn": pet_name,
        "n": pet_name,
        "lv": lv,
        "ecs": ecs,
        "eskill": ecs,
        "e": 9999999,
        "chp": 120000,
        "mhp": 120000,
        "mnA": 15000,
        "mxA": 25000,
        "sk": [{"ski": 32071, "skl": 1}],
        "bot": 4,
        "bi": 2499,
        "isevo": ecs > 0,
        "eid": 0,
    }
    pets.append(new_pet)
    if not player.get("pet") or len(pets) == 1:
        player["pet"] = new_pet
        player["follow_pet_id"] = new_pi
    save_player(player, char_id=char_id)
    logger.info("玩家 %d 获得战宠: %s (ID=%d, pi=%d, ecs=%d)", char_id, pet_name, pti, new_pi, ecs)
    return new_pet


def wake_pet(char_id: int, pet_id: int, target_ecs: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """战宠觉醒升级 (提升阶位 ecs 与全属性)。"""
    player = load_player(char_id)
    pets = player.get("pets", [])
    target = next((p for p in pets if p.get("pi") == pet_id), None)
    if not target and player.get("pet") and player["pet"].get("pi") == pet_id:
        target = player["pet"]
        pets.append(target)
    if not target:
        return None

    cur_ecs = target.get("ecs", 0)
    if target_ecs is not None and target_ecs > cur_ecs:
        new_ecs = min(5, target_ecs)
    else:
        new_ecs = min(5, cur_ecs + 1)
    target["ecs"] = new_ecs
    target["eskill"] = new_ecs
    target["isevo"] = new_ecs > 0
    pti = target.get("pti")
    official_n = PET_OFFICIAL_NAMES.get(str(pti)) or PET_NAMES.get(pti)
    if official_n:
        target["n"] = official_n
        target["ptn"] = official_n

    if player.get("pet") and player["pet"].get("pi") == pet_id:
        player["pet"].update(target)

    save_player(player, char_id=char_id)
    logger.info("玩家 %d 战宠 %s (pi=%d) 成功觉醒至 %d 阶！", char_id, target.get("n"), pet_id, new_ecs)
    return target


def _pet_exp_for_level(level: int, coefficient: int = 100) -> int:
    level = max(1, int(level))
    base = int(1.2 * level ** 3 + 35 * level ** 2 + 200 * level + 300)
    return max(1, base * max(1, int(coefficient)) // 100)


def _pet_exp_to_level(pet: Dict[str, Any], target_lv: int) -> int:
    current_lv = max(1, int(pet.get("lv", 1)))
    target_lv = min(100, max(current_lv, int(target_lv)))
    coefficient = int(pet.get("ecc", 100) or 100)
    needed = -max(0, int(pet.get("cle", 0) or 0))
    for level in range(current_lv, target_lv):
        needed += _pet_exp_for_level(level, coefficient)
    return max(0, needed)


def level_up_pet(
    char_id: int,
    pet_id: int,
    target_lv: Optional[int] = None,
    exp_amount: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Apply pet experience using the client's per-level experience curve."""
    player = load_player(char_id)
    pets = player.get("pets", [])
    target = next((p for p in pets if p.get("pi") == pet_id), None)
    if not target and player.get("pet") and player["pet"].get("pi") == pet_id:
        target = player["pet"]
        pets.append(target)
    if not target:
        return None

    cur_lv = max(1, int(target.get("lv", 1)))
    cur_exp = max(0, int(target.get("cle", 0) or 0))
    coefficient = int(target.get("ecc", 100) or 100)
    if target_lv is not None:
        new_lv = min(100, max(cur_lv, int(target_lv)))
        spent = _pet_exp_to_level(target, new_lv)
        available = get_temp_score(char_id, 4)
        if spent > available:
            return None
        cur_exp = 0
    else:
        spent = min(max(0, int(exp_amount or 0)), get_temp_score(char_id, 4))
        remaining = spent
        new_lv = cur_lv
        while new_lv < 100:
            need = max(0, _pet_exp_for_level(new_lv, coefficient) - cur_exp)
            if remaining < need:
                cur_exp += remaining
                remaining = 0
                break
            remaining -= need
            new_lv += 1
            cur_exp = 0
        spent -= remaining
        if spent <= 0:
            return None

    target["lv"] = new_lv
    target["cle"] = cur_exp
    target["ecc"] = coefficient
    target["mhp"] = max(int(target.get("mhp", 855)), 50000 + new_lv * 1000)
    target["chp"] = target["mhp"]

    if player.get("pet") and player["pet"].get("pi") == pet_id:
        player["pet"].update(target)

    player.setdefault("temp_scores", {})["4"] = max(
        0, int(player.get("temp_scores", {}).get("4", 0) or 0) - spent
    )
    save_player(player, char_id=char_id)
    logger.info(
        "玩家 %d 战宠 %s (pi=%d) 使用宠物经验 %d，升级至 Lv.%d，当前经验=%d",
        char_id, target.get("n"), pet_id, spent, new_lv, cur_exp,
    )
    return target


def set_follow_pet(char_id: int, pet_id: int) -> Optional[Dict[str, Any]]:
    """设置跟随出战战宠。"""
    player = load_player(char_id)
    pets = player.get("pets", [])
    target = next((p for p in pets if p.get("pi") == pet_id), None)
    if target:
        player["pet"] = target
        player["follow_pet_id"] = pet_id
        save_player(player, char_id=char_id)
        logger.info("玩家 %d 设置出战战宠: %s (pi=%d)", char_id, target.get("n"), pet_id)
        return target
    return None


def set_pet(
    char_id: int,
    pti: int = 100408,
    name: str = "亚克",
    lv: int = 1,
) -> Dict[str, Any]:
    """设置角色出战战宠。"""
    player = load_player(char_id)
    player["pet"] = {
        "eid": 0,
        "bot": 4,
        "bi": 2499,
        "isevo": False,
        "lv": lv,
        "pti": pti,
        "ptn": name,
        "n": name,
        "chp": 855,
        "mhp": 855,
        "pi": 1,
        "sk": [{"ski": 32071, "skl": 0}],
    }
    save_player(player, char_id=char_id)
    logger.info("玩家 %d 激活战宠: %s (ID=%d)", char_id, name, pti)
    return player


# -------------------------------------------------------------
# 协议层节点与实体生成函数 (Strict Real Keys)
# -------------------------------------------------------------

def build_http_char_node(char_id: int = 88343134) -> str:
    """生成 HTTP 层 /login.jsp 与 /character.jsp 所需的 <char> XML 节点。"""
    player = load_player(char_id)
    b = player.get("basic", {})
    bag = player.get("bag", {})
    pos = player.get("position", {})

    cid = b.get("char_id", char_id)
    n = b.get("user_name", "LDS8834313497")
    r = b.get("race", 1)
    lv = b.get("level", 1)
    j = b.get("job", 1)
    g = b.get("gender", 1)
    cloth_str = ",".join(str(x) for x in bag.get(
        "wearing_cloth",
        bag.get("cloth", [100011, 100012, 100013, 100081, 100151, 600001]),
    ))
    weapon_cloth_id = 400004 if j == 2 else 400002
    eq_list = bag.get("equip", [])
    for eq in eq_list:
        if eq in GLOBAL_EQUIP_TO_CLOTHES:
            weapon_cloth_id = GLOBAL_EQUIP_TO_CLOTHES[eq]
            break
        elif eq in (400002, 400004):
            weapon_cloth_id = eq
            break
    equip_str = str(weapon_cloth_id)
    lr = pos.get("last_room", f"shenyuanroad_{cid}_0_tr")
    oj = b.get("job", 1)

    return f"""<char cid="{cid}">
\t<cid>{cid}</cid>
\t<n>{n}</n>
\t<r>{r}</r>
\t<lv>{lv}</lv>
\t<j>{j}</j>
\t<g>{g}</g>
\t<cloth>{cloth_str}</cloth>
\t<equip>{equip_str}</equip>
\t<soul>0</soul>
\t<lr>{lr}</lr>
\t<ag>0</ag>
\t<oj>{oj}</oj>
</char>"""


def build_tcp_2301_entity(char_id: int = 88343134) -> Dict[str, Any]:
    """从持久化数据中提取并构建 TCP 层的 2301 实体字典对象。"""
    player = load_player(char_id)
    b = player.get("basic", {})
    pos = player.get("position", {})
    props = calc_player_properties(char_id, player_data=player)

    cid = float(b.get("char_id", char_id))
    name = str(b.get("user_name", "LDS8834313497"))
    lv = int(props["lv"])

    entity: Dict[str, Any] = {
        "tidd": "",
        "bot": 1,
        "dned": -1,
        "cmp": props["cmp"],
        "bi": 304448,
        "aai": True,
        "lv": lv,
        "ba_sk": props.get("ba_sk", 0),
        "dn": name,
        "mmp": props["mmp"],
        "eva": 12.003599166870117,
        "hit": 98.00360107421875,
        "pkm": 1,
        "ui": int(cid),
        "ba_pt": props.get("ba_pt", 0),
        "ati": "",
        "ba_lv": props["ba_lv"],
        "ba_clo": props.get("ba_clo", 0),
        "ba_t": props.get("ba_t", props.get("ba_lv", 41)),
        "dnis": True,
        "pti": 0,
        "d": -1,
        "ba_pg": props.get("ba_pg", 0),
        "ba_cg": props.get("ba_cg", 0),
        "ba_dp": props.get("ba_dp", 0),
        "ba_fp": props.get("ba_fp", 0),
        "ci": cid,
        "ba_vp": props.get("ba_vp", 0),
        "j": props["job_name"],
        "chp": props["chp"],
        "n": name,
        "cs": {"al": True, "fd": False},
        "r": "人类",
        "mhp": props["mhp"],
        "jlv": 0,
        "ti": 0,
        "rg": 0,
        "rpt": 0,
        "x": pos.get("x", 60),
        "y": pos.get("y", 40),
        "originaljob": props["originaljob"],
        "ba_eqp": props.get("ba_eqp", 1),
        "rhp": 6,
        "plv": lv,
        "trans": {"it": False},
        "mnA": props.get("mnA"),
        "mxA": props.get("mxA"),
        "att": props["att"],
        "def": props["def"],
        "cri": props["cri"],
    }

    try:
        follow_pid = int(player.get("follow_pet_id", 1) or 0)
    except (TypeError, ValueError):
        follow_pid = 0
    pets = player.get("pets", [])
    pet_obj = next((p for p in pets if int(p.get("pi", 0) or 0) == follow_pid), None) or player.get("pet")
    if pet_obj and follow_pid > 0:
        pet_data = dict(pet_obj)
        pet_pi = pet_data.get("pi", 1)
        pet_pti = int(pet_data.get("pti", 100408))
        pet_name = str(pet_data.get("n", pet_data.get("ptn", "")))
        if not pet_name or pet_name.startswith("战宠_") or pet_name.startswith("战宠") or pet_name.isdigit():
            pet_name = PET_OFFICIAL_NAMES.get(str(pet_pti)) or PET_NAMES.get(pet_pti, "亚克")
        pet_data["pi"] = pet_pi
        pet_data["pti"] = pet_pti
        pet_data["ptn"] = pet_name
        pet_data["n"] = pet_name
        pet_data["ci"] = float(-4120000 - pet_pi)
        pet_data["oci"] = float(char_id)
        pet_data["bot"] = 4
        pet_data["ui"] = -1
        pet_data["bi"] = entity.get("bi", 304448)
        pet_data.setdefault("sk", [{"ski": 32071, "skl": 0}])
        entity["pet"] = pet_data

    return entity


def clean_corrupted_data(char_id: int = 88343134) -> Dict[str, Any]:
    """清洗异常脏数据 (如非法服饰ID与错误物品ID，规范战宠命名与默认出战)"""
    player = load_player(char_id)
    bag = player.setdefault("bag", {})
    cloth_list = bag.get("cloth", [])
    valid_clothes = [c for c in cloth_list if isinstance(c, int) and 100000 <= c < 1000000]
    if len(valid_clothes) < 6:
        for def_c in [100011, 100012, 100013, 100081, 100151, 600001]:
            if def_c not in valid_clothes:
                valid_clothes.append(def_c)
    wearing_clothes = bag.get("wearing_cloth", valid_clothes)
    bag["cloth"] = valid_clothes
    bag["wearing_cloth"] = [
        c for c in wearing_clothes
        if isinstance(c, int) and c in valid_clothes
    ]

    items = bag.get("items", [])
    valid_items = [it for it in items if isinstance(it, dict) and it.get("mid", 0) < 9000000]
    bag["items"] = valid_items

    pets = [p for p in player.get("pets", []) if isinstance(p, dict)]
    for pet in pets:
        if int(pet.get("pti", 0)) == 100408:
            pet["ptn"] = "亚克"
            pet["n"] = "亚克"

    pet_ids = {int(pet.get("pi", 0)) for pet in pets if int(pet.get("pi", 0)) > 0}
    follow_id = int(player.get("follow_pet_id", 0) or 0)
    if follow_id not in pet_ids:
        legacy_pet_id = int((player.get("pet") or {}).get("pi", 0) or 0)
        follow_id = legacy_pet_id if legacy_pet_id in pet_ids else (int(pets[0]["pi"]) if pets else 0)
        player["follow_pet_id"] = follow_id
    selected_pet = next((pet for pet in pets if int(pet.get("pi", 0)) == follow_id), None)
    if selected_pet:
        player["pet"] = selected_pet

    save_player(player, char_id=char_id)
    logger.info("角色 %d 异常数据清洗完毕: 服饰 %d 件, 物品 %d 种", char_id, len(valid_clothes), len(valid_items))
    return player


def update_player_basic(char_id: int, updates: Dict[str, Any]) -> Dict[str, Any]:
    """批量更新角色基础属性 (等级, 金币, 龙币, 经验, VIP等)"""
    player = load_player(char_id)
    b = player.setdefault("basic", {})

    for k, v in updates.items():
        if k in ("level", "lv"):
            b["level"] = max(1, min(100, int(v)))
        elif k in ("coin", "gold"):
            b["coin"] = max(0, int(v))
        elif k in ("dragon_coin", "lb", "longbi"):
            b["dragon_coin"] = max(0, int(v))
        elif k in ("exp",):
            b["exp"] = max(0, int(v))
        elif k in ("vip_level", "mlv"):
            b["vip_level"] = max(0, min(10, int(v)))
            b["is_vip"] = b["vip_level"] > 0
        elif k in ("job",):
            b["job"] = int(v)
            job_map = {1: "战士", 2: "弓箭手", 3: "法师", 4: "圣骑士", 5: "祭司", 6: "魔剑士", 7: "狂战士"}
            b["job_name"] = job_map.get(b["job"], "战士")
        elif k in ("user_name", "n"):
            b["user_name"] = str(v)
            player["user_name"] = str(v)
        elif k in ("mhp", "chp", "mmp", "cmp"):
            b[k] = max(1, int(v))

    save_player(player, char_id=char_id)
    logger.info("角色 %d 基础属性已更新: %s", char_id, updates)
    return player


def grant_item_to_player(char_id: int, category: str, item_id: int, count: int = 1) -> Dict[str, Any]:
    """统一发放物品接口 (装备, 时装, 消耗品, 材料, 战宠)"""
    cat = category.lower().strip()
    if count <= 0:
        raise ValueError(f"Grant count must be positive: {count}")
    if cat in ("equip", "equipment", "0"):
        for _ in range(count):
            add_bag_item(char_id, mid=item_id, count=1, t=0)
    elif cat in ("cloth", "clothes", "1"):
        add_cloth(char_id, cloth_id=item_id, expire_at_ms=7614748800000)
    elif cat in ("pet", "115", "135"):
        for _ in range(count):
            add_pet(
                char_id,
                pti=item_id,
                lv=100,
                ecs=5,
                allow_duplicate=True,
            )
    elif cat in ("consume", "consumable", "consument", "2"):
        add_bag_item(char_id, mid=item_id, count=count, t=2)
    elif cat in ("stuff", "material", "4"):
        add_bag_item(char_id, mid=item_id, count=count, t=4)
    elif cat in ("mall", "mall_goods", "10"):
        from server.encyclopedia import get_mall_goods_info
        mall_info = get_mall_goods_info(str(item_id))
        if mall_info:
            for bonus in str(mall_info.get("bonus", "")).split("#"):
                if not bonus:
                    continue
                parts = bonus.split(":")
                if len(parts) >= 3:
                    m_type, m_id, m_num = int(parts[0]), int(parts[1]), int(parts[2])
                    if m_type in (1, 11, 13):
                        add_cloth(char_id, cloth_id=m_id, expire_at_ms=7614748800000)
                    elif m_type == 0:
                        for _ in range(count * m_num):
                            add_bag_item(char_id, mid=m_id, count=1, t=0)
                    elif m_type in (115, 135):
                        add_pet(char_id, pti=m_id, lv=100, ecs=5)
                    elif m_type == 100:
                        p = load_player(char_id)
                        p.setdefault("basic", {})["coin"] = p.get("basic", {}).get("coin", 0) + m_num * count
                        save_player(p, char_id=char_id)
                    else:
                        add_bag_item(char_id, mid=m_id, count=count * m_num, t=m_type)
        else:
            add_bag_item(char_id, mid=item_id, count=count)
    elif cat in ("item", "auto"):
        add_bag_item(char_id, mid=item_id, count=count)
    else:
        raise ValueError(f"Unsupported GM grant category: {category}")

    return load_player(char_id)


# -------------------------------------------------------------
# 核心功能模块: 技能、坐骑、背包装备与场景用户变量
# -------------------------------------------------------------

def build_user_vars_list(char_id: int = 88343134) -> List[Tuple[str, str, Any]]:
    """
    构造用户变量 (UserVariables) 列表，直接用于 JOIN_OK 和 USER_VARS_UPDATE。
    """
    player = load_player(char_id)
    b = player.get("basic", {})
    bag = player.get("bag", {})

    v_lv = max(0, min(10, int(b.get("vip_level", 7))))
    is_v = b.get("is_vip", True) and v_lv > 0
    v_type = 8 if is_v else 0
    is_an = str(b.get("is_annual", True)).lower()
    is_sp = str(b.get("is_super_vip", True)).lower()
    mi_str = f"{v_type}_{v_lv}_{is_an}_{is_sp}"

    riding_id = int(player.get("riding_pet_id", 0))

    skills_entries = player.get("skills", {}).get("list", [])
    job_id = int(b.get("job", 1))
    if skills_entries:
        sk_ids = [int(s.get("id")) for s in skills_entries if s.get("id")]
        sk_list = get_job_action_ids(job_id, sk_ids)
    else:
        sk_list = get_job_action_ids(job_id)

    weapon_cloth_id = 400002
    eq_list = bag.get("equip", [])
    for eq in eq_list:
        if eq in GLOBAL_EQUIP_TO_CLOTHES:
            weapon_cloth_id = GLOBAL_EQUIP_TO_CLOTHES[eq]
            break
        elif eq in (400002, 400004):
            weapon_cloth_id = eq
            break
    equip_str = f"{weapon_cloth_id}:0:0"

    cloth_list = bag.get(
        "wearing_cloth",
        bag.get("cloth", [100011, 100012, 100013, 100081, 100151, 600001]),
    )
    avatar_str = ",".join(str(c) for c in cloth_list)

    return [
        ("contract_st", "s", "0#0"),
        ("skills", "s", sk_list),
        ("fly_wing", "s", "0"),
        ("cv", "n", "0"),
        ("equip", "s", equip_str),
        ("avatar", "s", avatar_str),
        ("mi", "s", mi_str),
        ("ride", "n", str(riding_id)),
        ("float", "n", "0"),
    ]


def get_skills(char_id: int = 88343134) -> Dict[str, Any]:
    """获取玩家掌握的技能列表与剩余技能点 (用于 1501 -> 2501)。"""
    player = load_player(char_id)
    sk = player.get("skills")
    if not sk or not sk.get("list"):
        job_id = int(player.get("basic", {}).get("job", 1))
        return {
            "sp": 100,
            "list": get_job_initial_skills(job_id)
        }
    return sk


def _load_skill_rank_limits() -> Dict[Tuple[int, int], int]:
    """Read explicit maxLevelN rank caps from the official skill descriptions."""
    limits: Dict[Tuple[int, int], int] = {}
    path = find_json_file("SkillExtXMLInfo_xmlClass.xml")
    if not path:
        return limits
    try:
        root = ET.parse(path).getroot()
        for description in root.iter("skillDescription"):
            id_node = description.find("id")
            if id_node is None:
                continue
            skill_id = int(id_node.get("value", 0) or 0)
            for node in description:
                name = node.tag
                if name.startswith("maxLevel") and name[8:].isdigit():
                    limits[(skill_id, int(name[8:]))] = int(node.get("value", 0) or 0)
    except (OSError, ET.ParseError, ValueError) as exc:
        logger.warning("加载官方技能等级上限失败: %s", exc)
    return limits


GLOBAL_SKILL_RANK_LIMITS = _load_skill_rank_limits()


def _load_skill_level_requirements() -> Dict[Tuple[int, int], int]:
    """Load the original per-skill learning/upgrade levels from official Evo tips."""
    requirements: Dict[Tuple[int, int], int] = {}
    tips_dir = os.path.join(ROOT_DIR, "04-数据JSON", "游戏数据", "01-技能")
    for path in glob.glob(os.path.join(tips_dir, "SkillTipsEvoMap_dataClass*.xml")):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as source:
                content = source.read()
            for match in re.finditer(r"<tips\b([^>]*)>", content):
                attrs = {key: value for key, _quote, value in re.findall(
                    r"([A-Za-z_][\w.-]*)\s*=\s*(['\"])(.*?)\2", match.group(1)
                )}
                skill_id = int(attrs.get("skillId", 0) or 0)
                enhance_level = int(attrs.get("enhanceLevel", 1) or 1)
                # Empty lv values are used by the original data for basic actions.
                required_level = int(attrs.get("lv", 0) or 1)
                key = (skill_id, enhance_level)
                requirements[key] = min(requirements.get(key, required_level), required_level)
        except (OSError, ValueError) as exc:
            logger.warning("加载技能等级配置失败 %s: %s", os.path.basename(path), exc)
    return requirements


GLOBAL_SKILL_LEVEL_REQUIREMENTS = _load_skill_level_requirements()


def _sync_level_unlocked_skills(player: Dict[str, Any]) -> List[Dict[str, int]]:
    """Persist skills whose official learning level has been reached."""
    basic = player.setdefault("basic", {})
    job_id = int(basic.get("job", 1) or 1)
    level = max(1, int(basic.get("level", 1) or 1))
    skills_data = player.setdefault("skills", {"sp": 0, "list": get_job_initial_skills(job_id)})
    current = skills_data.setdefault("list", [])
    if not isinstance(current, list):
        current = []
        skills_data["list"] = current
    known = {
        int(row.get("id", 0)): row
        for row in current
        if isinstance(row, dict) and str(row.get("id", "")).lstrip("-").isdigit()
    }
    newly_learned: List[Dict[str, int]] = []
    for skill_id in JOB_DEFAULT_SKILLS.get(job_id, JOB_DEFAULT_SKILLS[1]):
        if skill_id in known:
            continue
        required_level = GLOBAL_SKILL_LEVEL_REQUIREMENTS.get((skill_id, 0))
        if required_level is None:
            # Most older skill families start at enhanceLevel 1.
            required_level = GLOBAL_SKILL_LEVEL_REQUIREMENTS.get((skill_id, 1), 1)
        if level < required_level:
            continue
        learned = {"id": skill_id, "ck0": 1, "ck1": 1, "ck2": 0, "ck3": 0}
        current.append(learned)
        known[skill_id] = learned
        newly_learned.append(learned)
    return newly_learned


def level_up_skill(char_id: int, skill_id: int, up_tier: int = 1) -> Tuple[bool, int, int]:
    """升级指定技能 (扣减技能点，提升技能对应栏位层数，更新战力)。"""
    player = load_player(char_id)
    sk_data = player.setdefault("skills", {"sp": 100, "list": []})
    try:
        sp = max(0, int(sk_data.get("sp", 0)))
    except (TypeError, ValueError):
        sp = 0
    sk_list = sk_data.setdefault("list", [])

    job_id = int(player.get("basic", {}).get("job", 1))
    if skill_id not in JOB_DEFAULT_SKILLS.get(job_id, JOB_DEFAULT_SKILLS[1]) or up_tier not in (1, 2, 3) or sp <= 0:
        return False, up_tier, sp
    target = next((s for s in sk_list if isinstance(s, dict) and int(s.get("id", 0) or 0) == skill_id), None)
    if not target or int(target.get("ck0", 0) or 0) <= 0:
        return False, up_tier, sp

    tier_key = f"ck{up_tier}"
    try:
        current_tier = max(0, int(target.get(tier_key, 0) or 0))
    except (TypeError, ValueError):
        current_tier = 0
    tier_limit = GLOBAL_SKILL_RANK_LIMITS.get((skill_id, up_tier))
    if tier_limit is not None and current_tier >= tier_limit:
        return False, up_tier, sp
    player_level = max(1, int(player.get("basic", {}).get("level", 1) or 1))
    required_level = GLOBAL_SKILL_LEVEL_REQUIREMENTS.get((skill_id, current_tier + 1)) if up_tier == 1 else None
    if required_level is not None and player_level < required_level:
        return False, up_tier, sp
    target[tier_key] = current_tier + 1
    new_sp = sp - 1
    sk_data["sp"] = new_sp

    save_player(player, char_id=char_id)
    logger.info("玩家 %d 技能 %d (%s) 升级至 Lv.%d，剩余 SP=%d", char_id, skill_id, tier_key, target[tier_key], new_sp)
    return True, up_tier, new_sp


def learn_skill(char_id: int, skill_id: int) -> Dict[str, Any]:
    """学习新技能 (用于 1503 -> 2503)。"""
    player = load_player(char_id)
    sk_data = player.setdefault("skills", {"sp": 100, "list": []})
    sk_list = sk_data.setdefault("list", [])

    job_id = int(player.get("basic", {}).get("job", 1))
    if skill_id not in JOB_DEFAULT_SKILLS.get(job_id, JOB_DEFAULT_SKILLS[1]):
        return {}
    player_level = max(1, int(player.get("basic", {}).get("level", 1) or 1))
    required_level = GLOBAL_SKILL_LEVEL_REQUIREMENTS.get((skill_id, 0))
    if required_level is None:
        required_level = GLOBAL_SKILL_LEVEL_REQUIREMENTS.get((skill_id, 1), 1)
    if player_level < required_level:
        return {}

    target = next((s for s in sk_list if s.get("id") == skill_id), None)
    if not target:
        target = {"id": skill_id, "ck0": 1, "ck1": 1, "ck2": 0, "ck3": 0}
        sk_list.append(target)
    else:
        target["ck0"] = 1

    save_player(player, char_id=char_id)
    logger.info("玩家 %d 成功学习技能 %d", char_id, skill_id)
    return target


RIDE_PERMANENT_END_MS = 4133980800000  # 2101-01-01; client treats years >2100 as permanent.


def get_mounts(char_id: int = 88343134) -> List[Dict[str, Any]]:
    """Return only valid, persisted mount instances for 26_3."""
    player = load_player(char_id)
    mounts = player.get("mounts", [])
    if not isinstance(mounts, list):
        return []
    valid = []
    seen_rids = set()
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        try:
            rid = int(mount.get("rid", 0))
            pid = int(mount.get("pid", 0))
            end = int(mount.get("end", RIDE_PERMANENT_END_MS))
        except (TypeError, ValueError):
            continue
        if rid <= 0 or rid in seen_rids or pid not in GLOBAL_RIDECONSUMENT_IDS:
            continue
        seen_rids.add(rid)
        valid.append({"rid": rid, "pid": pid, "end": end})
    return valid


def add_mount(char_id: int, pid: int, end_ms: int = RIDE_PERMANENT_END_MS) -> Optional[Dict[str, Any]]:
    """Add a permanent owned mount, deduplicating an already-owned template."""
    pid = int(pid)
    if pid not in GLOBAL_RIDECONSUMENT_IDS:
        return None
    player = load_player(char_id)
    mounts = player.setdefault("mounts", [])
    if not isinstance(mounts, list):
        mounts = player["mounts"] = []
    existing = next(
        (item for item in mounts if isinstance(item, dict) and int(item.get("pid", 0)) == pid),
        None,
    )
    if existing:
        return None
    used = [int(item.get("rid", 0)) for item in mounts if isinstance(item, dict)]
    rid = max(used, default=0) + 1
    mount = {"rid": rid, "pid": pid, "end": int(end_ms)}
    mounts.append(mount)
    if int(player.get("default_rider_id", -1)) <= 0:
        player["default_rider_id"] = rid
    save_player(player, char_id=char_id)
    logger.info("玩家 %d 获得坐骑: rid=%d pid=%d end=%d", char_id, rid, pid, end_ms)
    return mount


def use_ride_consumable(char_id: int, gid: int) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Consume one type-7 mount item and persist its mount instance."""
    player = load_player(char_id)
    items = player.setdefault("bag", {}).setdefault("items", [])
    target = next((item for item in items if int(item.get("gid", 0)) == int(gid)), None)
    if not target or int(target.get("t", 0)) != 7:
        return None, None
    pid = int(target.get("mid", 0))
    if pid not in GLOBAL_RIDECONSUMENT_IDS:
        return None, None
    mounts = player.setdefault("mounts", [])
    if any(isinstance(item, dict) and int(item.get("pid", 0)) == pid for item in mounts):
        return None, None
    cur_n = max(1, int(target.get("n", 1)))
    target["n"] = cur_n - 1
    if target["n"] <= 0:
        items.remove(target)
        bag_update = {"gid": int(gid), "n": 0}
    else:
        bag_update = target
    used = [int(item.get("rid", 0)) for item in mounts if isinstance(item, dict)]
    rid = max(used, default=0) + 1
    mount = {"rid": rid, "pid": pid, "end": RIDE_PERMANENT_END_MS}
    mounts.append(mount)
    if int(player.get("default_rider_id", -1)) <= 0:
        player["default_rider_id"] = rid
    save_player(player, char_id=char_id)
    logger.info("玩家 %d 使用坐骑道具 gid=%d 获得坐骑 rid=%d pid=%d", char_id, gid, rid, pid)
    return bag_update, mount


def set_default_rider(char_id: int, rid: int) -> bool:
    player = load_player(char_id)
    if rid > 0 and not any(int(m["rid"]) == int(rid) for m in get_mounts(char_id)):
        return False
    player["default_rider_id"] = int(rid) if rid > 0 else -1
    save_player(player, char_id=char_id)
    return True


def set_guarding_rider(char_id: int, rid: int) -> bool:
    player = load_player(char_id)
    if rid > 0:
        mount = next((m for m in get_mounts(char_id) if int(m["rid"]) == int(rid)), None)
        if not mount or int(mount["pid"]) not in GLOBAL_RIDER_GUARDIAN_IDS:
            return False
    player["guarding_rider_id"] = int(rid) if rid > 0 else -1
    save_player(player, char_id=char_id)
    return True


def discard_mount(char_id: int, rid: int) -> Optional[Dict[str, Any]]:
    player = load_player(char_id)
    mounts = player.setdefault("mounts", [])
    target = next((item for item in mounts if isinstance(item, dict) and int(item.get("rid", 0)) == int(rid)), None)
    if not target:
        return None
    mounts.remove(target)
    if int(player.get("default_rider_id", -1)) == rid:
        player["default_rider_id"] = -1
    if int(player.get("guarding_rider_id", -1)) == rid:
        player["guarding_rider_id"] = -1
    if int(player.get("riding_rid", 0)) == rid:
        player["riding_rid"] = 0
        player["riding_pet_id"] = 0
    save_player(player, char_id=char_id)
    return target


def set_player_riding(char_id: int, rid: int) -> Optional[int]:
    """Store rider instance id and return its template id for the user variable."""
    mount = next((item for item in get_mounts(char_id) if int(item["rid"]) == int(rid)), None)
    if not mount or int(mount.get("end", 0)) <= int(time.time() * 1000):
        return None
    player = load_player(char_id)
    player["riding_rid"] = int(rid)
    player["riding_pet_id"] = int(mount["pid"])
    save_player(player, char_id=char_id)
    logger.info("玩家 %d 上马，坐骑 rid=%d pid=%d", char_id, rid, mount["pid"])
    return int(mount["pid"])


def set_player_unriding(char_id: int) -> int:
    player = load_player(char_id)
    player["riding_rid"] = 0
    player["riding_pet_id"] = 0
    save_player(player, char_id=char_id)
    logger.info("玩家 %d 下马恢复步行", char_id)
    return 0


def get_player_backpack(char_id: int = 88343134) -> List[Dict[str, Any]]:
    """构造玩家背包全量物品列表 (用于 8_0_0 初始化)。"""
    player = load_player(char_id)
    bag = player.get("bag", {})
    items = bag.get("items", [])
    equip_ids = set(int(mid) for mid in bag.get("equip", []) if str(mid).isdigit())

    result = []
    used_gids = set()
    occupied_slots = set()
    equipped_mids = set()
    backpack_changed = False

    for it in items:
        entry = it
        original_entry = deepcopy(entry)
        gid = entry.get("gid", 1)
        used_gids.add(gid)
        mid = int(entry.get("mid", 0))
        t = int(entry.get("t", 0))

        t = resolve_inventory_type(mid, t)
        is_eq = t == 0
        if is_eq:
            entry["t"] = 0
            entry["si"] = ""
            entry.setdefault("cl", 0)
            entry.setdefault("tsk", 3)
            max_endurance = max(0, int(GLOBAL_EQUIP_CONFIG[mid].get("endurance", 65)))
            try:
                current_endurance = int(entry.get("ce", max_endurance))
            except (TypeError, ValueError):
                current_endurance = max_endurance
            entry["ce"] = max(0, min(current_endurance, max_endurance))
            entry.setdefault("ia", False)
            entry.setdefault("cb", True)
            entry.setdefault("egs", "")
            if not entry.get("bp"):
                entry["bp"] = {"lp": 0.0, "ep": 0.36, "sp": 0.28, "bp": 0.36}
            elif "ei" in entry["bp"] and entry["bp"]["ei"] == 0:
                del entry["bp"]["ei"]
            entry.setdefault("cd", 0)
            if "ei" in entry and entry["ei"] == 0:
                del entry["ei"]
            entry.setdefault("bs", 0)
            entry.setdefault("ss", 0)
            entry.setdefault("ch", 0)
            entry.setdefault("rs", 0)
            entry.setdefault("ec", 0)
            eq_meta = GLOBAL_EQUIP_CONFIG.get(mid, {})
            pos = eq_meta.get("position", 10)
            saved_slot = int(entry.get("pl", 0) or 0)
            if saved_slot == pos and saved_slot not in occupied_slots:
                entry["pl"] = pos
                occupied_slots.add(pos)
                equipped_mids.add(mid)
            else:
                entry["pl"] = 100
        else:
            if t == 0:
                entry["t"] = 2
            if entry.get("pl", 0) not in (102, 103):
                entry["pl"] = 102

        result.append(entry)
        if entry != original_entry:
            backpack_changed = True

    for eq_mid in equip_ids:
        if eq_mid in GLOBAL_EQUIP_CONFIG and eq_mid not in equipped_mids:
            matching_item = next((it for it in result if int(it.get("mid", 0)) == eq_mid), None)
            eq_meta = GLOBAL_EQUIP_CONFIG.get(eq_mid, {})
            pos = int(eq_meta.get("position", 10))
            if pos in occupied_slots:
                continue
            if matching_item is not None:
                matching_item["pl"] = pos
                occupied_slots.add(pos)
                equipped_mids.add(eq_mid)
                backpack_changed = True
                continue
            next_gid = 1
            while next_gid in used_gids:
                next_gid += 1
            used_gids.add(next_gid)
            result.append({
                "gid": next_gid,
                "mid": eq_mid,
                "pl": pos,
                "t": 0,
                "n": 1,
                "cl": 0,
                "tsk": 3,
                "ce": max(0, int(eq_meta.get("endurance", 65))),
                "ia": False,
                "cb": True,
                "egs": "",
                "bp": {
                    "lp": 0.0,
                    "ep": 0.36,
                    "sp": 0.28,
                    "bp": 0.36,
                },
                "si": "",
                "cd": 0,
                "bs": 0,
                "ss": 0,
                "ch": 0,
                "rs": 0,
                "ec": 0,
            })
            occupied_slots.add(pos)
            equipped_mids.add(eq_mid)
            backpack_changed = True

    normalized_equip = sorted(equipped_mids)
    if bag.get("equip", []) != normalized_equip:
        bag["equip"] = normalized_equip
        backpack_changed = True
    if backpack_changed:
        save_player(player, char_id=char_id)

    return result


def dress_equipment(char_id: int, gid: int) -> Optional[Tuple[Dict[str, Any], Optional[Dict[str, Any]]]]:
    """穿戴装备 (用于 8_0_1，根据官方部位动态更新对应 pl 与 bag.equip，返回 (target, old_item))。"""
    player = load_player(char_id)
    items = player.setdefault("bag", {}).setdefault("items", [])
    target = next((it for it in items if it.get("gid") == gid), None)
    if not target:
        return None

    mid = int(target.get("mid", 0))
    eq_meta = GLOBAL_EQUIP_CONFIG.get(mid, {})
    basic = player.get("basic", {})
    if mid not in GLOBAL_EQUIP_CONFIG:
        return None
    if int(eq_meta.get("level", 1)) > int(basic.get("level", 1)):
        return None
    allowed_jobs = {
        int(job_id)
        for job_id in str(eq_meta.get("job", "")).split(",")
        if job_id.isdigit()
    }
    if allowed_jobs and int(basic.get("job", 1)) not in allowed_jobs:
        return None
    slot = eq_meta.get("position", 10)

    old_item = next((it for it in items if it.get("pl") == slot and it.get("gid") != gid), None)
    if old_item:
        old_item["pl"] = 100

    target["pl"] = slot
    target["t"] = 0
    target["si"] = ""

    equipped_mids = {
        int(it.get("mid", 0))
        for it in items
        if int(it.get("pl", 0) or 0) in range(1, 16)
    }
    player["bag"]["equip"] = sorted(equipped_mids)

    save_player(player, char_id=char_id)
    logger.info("玩家 %d 成功穿上装备 gid=%d mid=%d 到槽位 %d (替换旧装备: %s)", char_id, gid, mid, slot, old_item.get("gid") if old_item else "无")
    return target, old_item


def get_equipment_dress_error(char_id: int, gid: int) -> int:
    """Return the client MaterialResult code for an invalid 8_0_1 equipment request."""
    player = load_player(char_id)
    target = next(
        (it for it in player.get("bag", {}).get("items", []) if int(it.get("gid", 0)) == gid),
        None,
    )
    if target is None:
        return 11

    mid = int(target.get("mid", 0))
    eq_meta = GLOBAL_EQUIP_CONFIG.get(mid)
    if eq_meta is None or int(target.get("t", 0)) != 0:
        return 18
    if int(eq_meta.get("level", 1)) > int(player.get("basic", {}).get("level", 1)):
        return 15

    allowed_jobs = {
        int(job_id)
        for job_id in str(eq_meta.get("job", "")).split(",")
        if job_id.isdigit()
    }
    if allowed_jobs and int(player.get("basic", {}).get("job", 1)) not in allowed_jobs:
        return 14
    return 0


def set_equipment_enhance_level(
    char_id: int,
    gid: int,
    level: int,
) -> Optional[Dict[str, Any]]:
    """Persist an equipment's enhance level, enforcing its official per-item limit."""
    player = load_player(char_id)
    target = next(
        (
            item
            for item in player.get("bag", {}).get("items", [])
            if int(item.get("gid", 0)) == gid
        ),
        None,
    )
    if target is None:
        return None

    mid = int(target.get("mid", 0))
    equip_meta = GLOBAL_EQUIP_CONFIG.get(mid)
    if equip_meta is None or int(target.get("t", 0)) != 0:
        return None
    max_level = int(equip_meta.get("strLvLimit", 0))
    if max_level <= 0 or level < 0 or level > max_level:
        return None

    target["cl"] = level
    save_player(player, char_id=char_id)
    return target


def undress_equipment(char_id: int, gid: int) -> Optional[Dict[str, Any]]:
    """脱下装备 (用于 8_0_2，将 pl 置为 100，从 bag.equip 移除)。"""
    player = load_player(char_id)
    items = player.setdefault("bag", {}).setdefault("items", [])
    target = next((it for it in items if it.get("gid") == gid), None)
    if not target:
        return None

    mid = int(target.get("mid", 0))
    target["pl"] = 100
    target["t"] = 0
    target["si"] = ""

    player["bag"]["equip"] = sorted({
        int(it.get("mid", 0))
        for it in items
        if int(it.get("pl", 0) or 0) in range(1, 16)
    })

    save_player(player, char_id=char_id)
    logger.info("玩家 %d 成功脱下装备 gid=%d mid=%d 回到背包", char_id, gid, mid)
    return target


def use_consumable(char_id: int, gid: int, count: int = 1) -> Optional[Dict[str, Any]]:
    """使用消耗品/药水/经验丹 (用于 8_0_3)。"""
    player = load_player(char_id)
    items = player.setdefault("bag", {}).setdefault("items", [])
    target = next((it for it in items if it.get("gid") == gid), None)
    if not target:
        return None

    mid = int(target.get("mid", 0))
    if int(target.get("t", 0)) != 2 or mid not in GLOBAL_CONSUMENT_IDS:
        return None

    cur_n = int(target.get("n", 1))
    count = max(1, min(int(count), cur_n))
    new_n = max(0, cur_n - count)
    target["n"] = new_n
    if new_n <= 0:
        items.remove(target)

    b = player.setdefault("basic", {})
    health_restore = {1001: 500, 1002: 1500, 1003: 2500}.get(mid, 0)
    if health_restore:
        b["chp"] = min(
            int(b.get("mhp", 0)),
            int(b.get("chp", 0)) + health_restore * count,
        )

    save_player(player, char_id=char_id)
    logger.info("玩家 %d 使用道具 gid=%d mid=%d x%d (剩余 %d)", char_id, gid, mid, count, new_n)
    return target


def update_clothes(char_id: int, vpack_str: str) -> List[int]:
    """Toggle the requested owned clothes and return the IDs that changed."""
    player = load_player(char_id)
    requested_ids = []
    for s in vpack_str.split(","):
        s = s.strip()
        if s.isdigit():
            requested_ids.append(int(s))
    bag = player.setdefault("bag", {})
    owned_ids = set(bag.get("cloth", []))
    wearing_ids = list(dict.fromkeys(
        bag.get("wearing_cloth", bag.get("cloth", []))
    ))
    changed_ids = []
    for cloth_id in dict.fromkeys(requested_ids):
        if cloth_id not in owned_ids:
            continue
        changed_ids.append(cloth_id)
        if cloth_id in wearing_ids:
            wearing_ids.remove(cloth_id)
        else:
            wearing_ids.append(cloth_id)
    bag["wearing_cloth"] = wearing_ids
    save_player(player, char_id=char_id)
    logger.info(
        "玩家 %d 身穿服饰变更: changed=%s wearing=%s",
        char_id,
        changed_ids,
        wearing_ids,
    )
    return changed_ids


def build_player_corps_str(char_id: int) -> str:
    """生成客户端 27_173 所需的 corps 字符串 (格式: 套装ID_类型:宠物1,宠物2#...)"""
    return get_player_corps_str(char_id)
