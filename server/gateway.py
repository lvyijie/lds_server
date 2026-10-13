"""
核心 TCP 游戏网关服务 (gateway.py) - 终极修复合规版
已修复：
1. 全部 Python 缺少运算符语法错误 (减号与乘号补齐)。
2. 补齐 11_0 消耗品槽位响应，彻底解决 ConsumentSlots Error #1009。
3. 深渊之路入场绝不篡改实体，亚克原生引导；打怪 bi 动态绑定当前房间。
4. 战宠伴生实体补齐 avatar/ati/cl/dnis/mty，骨骼与跟随正常渲染。
5. 修复 27_1 / 27_6 / 10-0 战宠背包容量翻页越界 Error #1125。
6. 修复 27_74 / 27_72 守护槽位解析 f0~1000.swf 及空指针 Error #1009。
7. 修复 27_200 / 27_204 战宠详情及觉醒查询参数解析。
8. 修复 15_20 勇者之路副本面板属性缺失 Error #1069。
9. 修复 pMsg 聊天命令处理与 StatClient (1-2, 1-14) 统计放行。
"""
import struct
import os
import time
import pickle
import asyncio
import logging
import collections
import re
import json
from typing import Any, Dict, List, Optional

from .policy_server import POLICY_REQUEST, POLICY_XML
from .protocol.amf3 import (
    build_log_ok,
    build_spawn_player_resp,
    decode_amf3,
)
from .protocol.framing import (
    DownCmd,
    PacketType,
    UpCmd,
    UplinkPacket,
    build_downlink_ext_packet,
    build_downlink_join_ok,
    build_downlink_user_vars_update,
    parse_uplink_stream,
)
import random
from .player_storage import (
    GLOBAL_EQUIP_CONFIG,
    GLOBAL_INVENTORY_CATALOGS,
    resolve_inventory_type,
    load_player,
    save_player,
    update_task,
    complete_tutorial,
    is_tutorial_completed,
    update_position,
    update_basic,
    add_bag_item,
    add_equip_gem,
    get_equip_gem_list,
    add_equip,
    add_cloth,
    set_pet,
    get_dragon_coin,
    add_dragon_coin,
    change_temp_score,
    get_temp_score,
    set_dragon_coin,
    get_vip_info,
    set_vip_info,
    get_pets,
    add_pet,
    wake_pet,
    level_up_pet,
    set_follow_pet,
    PET_NAMES,
    calc_player_properties,
    calc_pet_guard_stats,
    build_tcp_2301_entity,
    build_user_vars_list,
    get_skills,
    level_up_skill,
    learn_skill,
    get_mounts,
    add_mount,
    use_ride_consumable,
    discard_mount,
    set_default_rider,
    set_guarding_rider,
    set_player_riding,
    set_player_unriding,
    dress_equipment,
    get_equipment_dress_error,
    undress_equipment,
    use_consumable,
    update_clothes,
    get_player_backpack,
    set_equipment_enhance_level,
    get_owned_clothes,
    get_cloth_expiry,
    get_wearing_clothes,
    build_player_corps_str,
    get_guardians,
    set_guardian_pet,
    calc_guard_power,
    swap_guardian_slots,
    one_key_guard,
    get_player_corps_str,
)
from .task_catalog import get_task, can_start_task
from .encyclopedia import get_mall_goods_info, init_encyclopedia
from .dungeon_engine import (
    get_or_create_dungeon,
    get_dungeon,
    remove_dungeon,
)

logger = logging.getLogger("Gateway")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
PKL_PATH = os.path.join(DATA_DIR, "action_presets.pkl")
STRENGTHEN_TEST_COST = 100
STRENGTHEN_TEST_SUCCESS_RATE = 80
CAPTURED_TRIGGERS: Dict[str, List[List[bytes]]] = {}
CAPTURED_SINGLE_FRAMES: Dict[str, bytes] = {}

if os.path.exists(PKL_PATH):
    try:
        with open(PKL_PATH, "rb") as f:
            data = pickle.load(f)
            if isinstance(data, dict):
                CAPTURED_TRIGGERS = data.get("triggers", {})
                CAPTURED_SINGLE_FRAMES = data.get("single_frames", {})
        logger.info(
            "成功加载抓包响应库: %d 个动作触发映射, %d 个单帧索引",
            len(CAPTURED_TRIGGERS),
            len(CAPTURED_SINGLE_FRAMES),
        )
    except Exception as e:
        logger.warning("加载 action_presets.pkl 失败: %s", e)

PET_OFFICIAL_DATA: Dict[str, Any] = {}
OFFICIAL_DATA_PATH = os.path.join(DATA_DIR, "pet_catalog.json")
if os.path.exists(OFFICIAL_DATA_PATH):
    try:
        with open(OFFICIAL_DATA_PATH, "r", encoding="utf-8") as f:
            PET_OFFICIAL_DATA = json.load(f)
    except Exception as e:
        logger.warning("加载 pet_catalog.json 失败: %s", e)

STAGE_RATES = [0.0, 0.06, 0.14, 0.22, 0.31, 0.40, 0.50, 0.60]


def format_pet_for_client(p: Dict[str, Any], char_id: int) -> Dict[str, Any]:
    """格式化战宠数据以对齐官方规范与官方数值公式。"""
    pi = int(p.get("pi", 1))
    pti = int(p.get("pti", 100408))
    pti_str = str(pti)
    official_names = PET_OFFICIAL_DATA.get("names", {})
    official_gifts = PET_OFFICIAL_DATA.get("gifts", {})
    official_skills = PET_OFFICIAL_DATA.get("skills", {})
    official_scores = PET_OFFICIAL_DATA.get("scores", {})

    default_name = official_names.get(pti_str) or PET_NAMES.get(pti, "亚克")
    name = str(p.get("n", p.get("ptn", default_name)))
    if not name or name.startswith("战宠_") or name.startswith("战宠") or name.isdigit() or name == "null":
        name = default_name
    lv = int(p.get("lv", 1 if pti == 100408 else 100))
    exp_coefficient = max(1, int(p.get("ecc", 100) or 100))
    next_exp = int((1.2 * lv ** 3 + 35 * lv ** 2 + 200 * lv + 300) * exp_coefficient / 100)
    ecs = int(p.get("ecs", 0 if pti == 100408 else 5))
    eskill = int(p.get("eskill", 0 if pti == 100408 else 5))

    gift = official_gifts.get(pti_str, {})
    score = official_scores.get(pti_str, {})
    skills_cfg = official_skills.get(pti_str, [32071, 32072, 32073])

    str_gift = int(gift.get("strength", 390))
    itl_gift = int(gift.get("intelligence", 0))
    con_gift = int(gift.get("constitution", 390))
    stm_gift = int(gift.get("stamina", 390))
    vit_gift = int(gift.get("vitality", 390))
    agi_gift = int(gift.get("agility", 390))

    stage_rate = STAGE_RATES[ecs] if ecs < len(STAGE_RATES) else 0.0

    # 官方属性成长公式
    str_val = int((0.5 + 1.71875 * lv) * ((str_gift - 100.0) / 50.0) * (1.24 + 0.1 * lv) * (0.438 + 0.0063 * lv) * (
            stage_rate + 1))
    itl_val = int((0.5 + 1.71875 * lv) * ((itl_gift - 100.0) / 50.0) * (1.24 + 0.1 * lv) * (0.438 + 0.0063 * lv) * (
            stage_rate + 1)) if itl_gift > 0 else 0
    con_val = int((0.46875 + 1.40625 * lv) * ((con_gift - 100.0) / 50.0) * (1.24 + 0.1 * lv) * (0.438 + 0.0063 * lv) * (
            stage_rate + 1))
    stm_val = int(
        (0.625 + 2.5 * lv) * ((stm_gift - 100.0) / 50.0) * (1.24 + 0.1 * lv) * (0.438 + 0.0063 * lv) * (stage_rate + 1))
    vit_val = int((0.40625 + 1.09375 * lv) * ((vit_gift - 100.0) / 50.0) * (1.24 + 0.1 * lv) * (0.438 + 0.0063 * lv) * (
            stage_rate + 1))
    agi_val = int((0.4375 + 1.5625 * lv) * ((agi_gift - 100.0) / 50.0) * (1.24 + 0.1 * lv) * (0.438 + 0.0063 * lv) * (
            stage_rate + 1))

    # 官方战斗数值
    atk_gift = max(str_gift, itl_gift)
    atk_attr = max(str_val, itl_val)
    att_num = int((stage_rate + 1) * (0.390625 + 0.625 * lv) * ((atk_gift - 100.0) / 50.0) * (8.86 + 0.7 * lv) * (
            0.438 + 0.0063 * lv) + atk_attr * 1.2)
    def_num = int((stage_rate + 1) * (6.25 + 12.5 * lv) * ((stm_gift - 100.0) / 50.0) * (8.86 + 0.7 * lv) * (
            0.438 + 0.0063 * lv) + stm_val * 15.0)
    hp_num = int((stage_rate + 1) * (9.375 + 18.75 * lv) * ((con_gift - 100.0) / 50.0) * (8.86 + 0.7 * lv) * (
            0.438 + 0.0063 * lv) + con_val * 25.0)
    mp_num = int((stage_rate + 1) * (5.46875 + 10.9375 * lv) * ((vit_gift - 100.0) / 50.0) * (8.86 + 0.7 * lv) * (
            0.438 + 0.0063 * lv) + vit_val * 15.0)

    # 官方暴击/命中/闪避
    cri_val = round(max(0, agi_val * 3.0), 1)
    hit_val = round(max(50.0, 98.0 + 0.0060 * agi_val - 0.06 * lv), 1)
    eva_val = round(max(5.0, 12.0 + 0.0060 * agi_val - 0.06 * lv), 1)

    mhp = max(100, hp_num)
    chp = int(p.get("chp", mhp))
    mmp = max(50, mp_num)
    cmp = int(p.get("cmp", mmp))
    mnA = max(10, att_num)
    mxA = max(mnA, att_num)

    str_val_s = f"{str_val}#{str_val}"
    itl_val_s = f"{itl_val}#{itl_val}"
    con_val_s = f"{con_val}#{con_val}"
    stm_val_s = f"{stm_val}#{stm_val}"
    vit_val_s = f"{vit_val}#{vit_val}"
    agi_val_s = f"{agi_val}#{agi_val}"
    att_val_s = f"{mnA}#{mxA}"
    def_val_s = f"{def_num}#{def_num}"
    hit_val_s = f"{hit_val}#{hit_val}"
    eva_val_s = f"{eva_val}#{eva_val}"
    cri_val_s = f"{cri_val}#{cri_val}"
    crp_val_s = ".01#.01"

    # 官方战斗力 (pba)
    pba = int(lv * ((str_gift + itl_gift) * 0.05 - 5) * (1 + 0.15 * (10 + lv) * 0.1) * 1.6 * (1 + stage_rate))
    if pba <= 0:
        pba = int(score.get("fightscore", 100) * lv / 100)

    sk_list = []
    for s_id in skills_cfg:
        sk_list.append({"ski": int(s_id), "skl": 1})
    if not sk_list:
        sk_list = [{"ski": 32071, "skl": 1}]

    return {
        "pi": pi,
        "pti": pti,
        "n": name,
        "ptn": name,
        "lv": lv,
        "ecs": ecs,
        "eskill": eskill,
        "isevo": ecs > 0,
        "chp": chp,
        "mhp": mhp,
        "nhp": mhp,
        "mnA": mnA,
        "mxA": mxA,
        "cmp": cmp,
        "mmp": mmp,
        "nmp": mmp,
        "csp": 0,
        "msp": 0,
        "str": str_val_s,
        "itl": itl_val_s,
        "con": con_val_s,
        "stm": stm_val_s,
        "vit": vit_val_s,
        "agi": agi_val_s,
        "att": att_val_s,
        "def": def_val_s,
        "hit": hit_val_s,
        "eva": eva_val_s,
        "cri": cri_val_s,
        "crp": crp_val_s,
        "atE": "0#0",
        "dfE": "0#0",
        "ecc": exp_coefficient,
        "eta": 0,
        "etd": 0,
        "eda": 0,
        "edd": 0,
        "ela": 0,
        "eld": 0,
        "efa": 0,
        "efd": 0,
        "eia": 0,
        "cle": max(0, int(p.get("cle", 0) or 0)),
        "clse": 0,
        "nx": next_exp,
        "nxse": 1,
        "sr": 1,
        "gri": 9,
        "pba": pba,
        "cft": 100,
        "cfv": 60,
        "mft": 100,
        "mfv": 100,
        "loc": 1,
        "bot": 4,
        "t": int(p.get("t", 1)),
        "eid": 0,
        "ui": -1,
        "ci": float(-4120000 - pi),
        "oci": float(char_id),
        "d": "2026-10-10",
        "e": int(p.get("e", 9999999)),
        "sk": sk_list,
        "cdp": 0,
        "corps": 0,
        "cse": 0,
        "mdp": 0,
        "mnAE": 0,
        "mxAE": 0,
        "rmp": 0,
        "rsp": 0,
        "rhp": 0,
    }


def _pet_inventory_for_client(char_id: int) -> tuple[List[Dict[str, Any]], int, str]:
    """Build a pet list and sequence from the same valid, unique client pet IDs."""
    player = load_player(char_id)
    formatted_pets: List[Dict[str, Any]] = []
    seen_ids = set()
    for pet in get_pets(char_id):
        formatted = format_pet_for_client(pet, char_id)
        pet_id = int(formatted["pi"])
        if pet_id <= 0 or pet_id in seen_ids:
            continue
        seen_ids.add(pet_id)
        formatted_pets.append(formatted)

    follow_id = int(player.get("follow_pet_id", 0))
    if follow_id not in seen_ids:
        follow_id = int(formatted_pets[0]["pi"]) if formatted_pets else 0
        if follow_id:
            set_follow_pet(char_id, follow_id)

    sorted_ids = ([follow_id] + [
        int(pet["pi"]) for pet in formatted_pets if int(pet["pi"]) != follow_id
    ]) if follow_id else [int(pet["pi"]) for pet in formatted_pets]
    return formatted_pets, follow_id, "#".join(str(pet_id) for pet_id in sorted_ids)


def _build_guardian_info_payload(char_id: int) -> Dict[str, Any]:
    """Build a complete guardian snapshot from the current saved pet/slot state."""
    guardians = get_guardians(char_id)
    formatted_pets, _, _ = _pet_inventory_for_client(char_id)
    pets_by_id = {int(p["pi"]): p for p in formatted_pets}
    attrs = [
        ("攻击", 0), ("防御", 1), ("生命", 2),
        ("魔法", 3), ("致命等级", 4), ("抗性", 5)
    ]

    gl_list = []
    slot_strs = []
    for slot in range(1, 7):
        attr_name, attr_type = attrs[slot - 1]
        try:
            pet_id = int(guardians.get(str(slot), guardians.get(slot, 0)) or 0)
        except (TypeError, ValueError):
            pet_id = 0
        pet_obj = pets_by_id.get(pet_id)
        if pet_obj:
            slot_strs.append(f"{slot}-{pet_id}")
            pet_level = int(pet_obj.get("lv", 1))
            pet_template_id = int(pet_obj.get("pti", 100408))
            gl_list.append({
                "en": 0,
                "l": 1,
                "ea": f"{attr_name}_{pet_level}",
                "e1": 0,
                "e2": 120,
                "et": attr_type,
                "pi": pet_id,
                "pti": pet_template_id,
                "lv": pet_level,
                "n": str(pet_obj.get("n", "")),
                "ptn": str(pet_obj.get("ptn", "")),
                "ci": float(pet_obj.get("ci", -4120000 - pet_id)),
            })
        else:
            gl_list.append({
                "en": 0,
                "l": 1,
                "ea": f"{attr_name}_1",
                "e1": 0,
                "e2": 120,
                "et": attr_type,
                "pi": 0,
                "pti": 0,
                "lv": 1,
                "n": "",
                "ptn": "",
                "ci": 0.0,
            })

    # GuardView.setProperty in the shipped client pairs each entry in `p`
    # with pl[index] (rather than searching `pl` by pi). Keep guardian pets
    # first in the exact same occupied-slot order as `p`, then append the rest
    # of the inventory for other client consumers.
    guardian_pet_ids = [int(entry.split("-", 1)[1]) for entry in slot_strs]
    ordered_pets = [pets_by_id[pet_id] for pet_id in guardian_pet_ids if pet_id in pets_by_id]
    ordered_ids = set(guardian_pet_ids)
    ordered_pets.extend(pet for pet in formatted_pets if int(pet["pi"]) not in ordered_ids)

    pl_pets = []
    for pet in ordered_pets:
        pet_payload = dict(pet)
        # The guardian panel reads these numeric fields from `pl` and applies
        # the stone rate for the slot's property to the pet currently in it.
        stats = calc_pet_guard_stats(pet)
        pet_payload.update({
            "att": stats["att"],
            "def": stats["def"],
            "cri": stats["cri"],
            "anti": 0,
            "mhp": stats["hp"],
            "mmp": stats["mp"],
        })
        pl_pets.append(pet_payload)

    stone_levels = load_player(char_id).get("guardian_stone_levels", {})
    recommendation_log = []
    stat_keys = ("att", "def", "hp", "mp", "cri", "anti")
    for slot_key, raw_pet_id in guardians.items():
        try:
            pet_id = int(raw_pet_id)
            slot = int(slot_key)
        except (TypeError, ValueError):
            continue
        pet = pets_by_id.get(pet_id)
        if not pet or not 1 <= slot <= 6:
            continue
        key = stat_keys[slot - 1]
        base = 0 if key == "anti" else calc_pet_guard_stats(pet).get(key, 0)
        try:
            stone_level = int(stone_levels.get(str(slot), 1))
        except (TypeError, ValueError, AttributeError):
            stone_level = 1
        rate = max(0, min(10, stone_level)) * 0.01
        recommendation_log.append((slot, pet_id, key, base, round(float(base) * (0.02 + rate), 1), stone_level))

    power = calc_guard_power(char_id)
    logger.info("27_72守护快照: char=%d slots=%s power=%d pet_count=%d bonuses=%s",
                char_id, "#".join(slot_strs), power, len(formatted_pets), recommendation_log)
    return {
        "_cmd": "27_72",
        "c": int(char_id),
        "p": "#".join(slot_strs),
        "ps": "",
        "g": None,
        "s": None,
        "gp": power,
        "res": 0,
        "pl": pl_pets,
        "gl": gl_list,
    }


def _treasure_equipment_candidates(job_id: int, preferred_ids: List[int]) -> List[int]:
    """Return strengthenable chest gear the active client occupation can use."""
    occupation = str(job_id)
    compatible = [
        mid
        for mid in preferred_ids
        if occupation in str(GLOBAL_EQUIP_CONFIG.get(mid, {}).get("job", "")).split(",")
    ]
    if compatible:
        return compatible

    return [
        mid
        for mid, equipment in GLOBAL_EQUIP_CONFIG.items()
        if occupation in str(equipment.get("job", "")).split(",")
           and int(equipment.get("strLvLimit", 0)) > 0
           and int(equipment.get("quality", 0)) >= 5
    ]


class ClientSession:
    """维护单个客户端长连接会话状态"""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer
        self.addr = writer.get_extra_info("peername") if writer else None
        self.buffer = bytearray()
        self.user_id: int = 663944508
        self.char_id: int = 88343134
        p_data = load_player(self.char_id)
        self.user_name: str = p_data.get("basic", {}).get("user_name", "LDS883431339")
        cur_pos_room = p_data.get("position", {}).get("last_room", "")
        if is_tutorial_completed(self.char_id):
            self.active_room = cur_pos_room if (
                    cur_pos_room and "shenyuanroad" not in cur_pos_room) else "momomaintown_1"
        else:
            self.active_room = cur_pos_room or f"shenyuanroad_{self.char_id}_0_tr"
        self.task_step: str = "513_done" if is_tutorial_completed(self.char_id) else ""
        self.is_logged_in: bool = False
        self.last_step_push_time: float = 0.0
        self.walk_task_started_time: float = 0.0
        self.fight_attack_count: int = 0
        self.monsters_killed: bool = False
        self.cmd_counters: Dict[str, int] = collections.defaultdict(int)
        self.pet_inventory_bootstrapped: bool = False
        self.pet_panel_inventory_refreshed: bool = False

        self.in_fb: bool = False
        self.fb10_cleared: bool = False
        self.fb_monsters_killed: int = 0
        self.current_room_id: int = 304448 if "shenyuanroad" in self.active_room else 2403

    def send_raw(self, data: bytes) -> None:
        try:
            self.writer.write(data)
        except Exception as e:
            logger.error("发送数据给客户端失败: %s", e)

    async def flush(self) -> None:
        try:
            await self.writer.drain()
        except Exception:
            pass

    def send_ext(self, data_obj: Dict) -> None:
        frame = build_downlink_ext_packet(data_obj)
        self.send_raw(frame)

    def send_props_update(self) -> None:
        props = calc_player_properties(self.char_id)
        basic = load_player(self.char_id).get("basic", {})
        max_sp = max(0, int(basic.get("msp", 100)))
        cur_sp = max(0, min(int(basic.get("csp", max_sp)), max_sp))
        lv = props["lv"]
        self.send_ext({
            "_cmd": "2131",
            "ci": float(self.char_id),
            "n": self.user_name,
            "r": 1,
            "j": props["job_name"],
            "jlv": 0,
            "lv": lv,
            "plv": lv,
            "originaljob": props["originaljob"],
            "mhp": props["mhp"],
            "chp": props["chp"],
            "nhp": props["mhp"],
            "mmp": props["mmp"],
            "cmp": props["cmp"],
            "nmp": props["mmp"],
            "att": props["att"],
            "def": props["def"],
            "str": props["str"],
            "agi": props["agi"],
            "vit": props["vit"],
            "itl": props["itl"],
            "con": props["con"],
            "stm": props["stm"],
            "cri": props["cri"],
            "hit": props["hit"],
            "eva": props["eva"],
            "csp": cur_sp,
            "msp": max_sp,
            "cdp": 0,
            "mdp": 100,
            "mnA": props.get("mnA", 50 + lv * 40),
            "mxA": props.get("mxA", 50 + lv * 45),
            "atE": "0#0",
            "dfE": "0#0",
            "eda": 0, "edd": 0,
            "efa": 0, "efd": 0,
            "eia": 0, "eid": 0,
            "ela": 0, "eld": 0,
            "eta": 0, "etd": 0,
            "ba_t": props.get("ba_t", props.get("ba_lv", 400)),
            "ba_lv": props["ba_lv"],
            "ba_eqp": props.get("ba_eqp", 0),
            "ba_sk": props.get("ba_sk", 0),
            "ba_vp": props.get("ba_vp", 0),
            "ba_pt": props.get("ba_pt", 0),
            "ba_clo": props.get("ba_clo", 0),
            "ba_pg": props.get("ba_pg", 0),
            "ba_cg": props.get("ba_cg", 0),
            "ba_dp": props.get("ba_dp", 0),
            "ba_fp": props.get("ba_fp", 0),
            "exp": props["exp"],
            "cle": props["cle"],
            "nx": props["nx"],
            "tidd": "",
        })

    def grant_dungeon_reward(self, exp_reward: int, coin_reward: int) -> None:
        """Persist dungeon rewards and send the client-side experience/level events."""
        before = load_player(self.char_id)
        old_level = int(before.get("basic", {}).get("level", 1))
        old_skill_ids = {
            int(row.get("id", 0))
            for row in before.get("skills", {}).get("list", [])
            if isinstance(row, dict)
        }
        player = update_basic(
            self.char_id,
            exp_gain=exp_reward,
            coin_gain=coin_reward,
        )
        if exp_reward:
            self.send_ext({"_cmd": "2141", "e": exp_reward})
        new_level = int(player.get("basic", {}).get("level", old_level))
        if new_level > old_level:
            unlocked = [
                row for row in player.get("skills", {}).get("list", [])
                if isinstance(row, dict) and int(row.get("id", 0)) not in old_skill_ids
            ]
            if unlocked:
                self.send_ext({
                    "_cmd": "2503",
                    "list": [
                        {"id": int(row["id"]), "au": True,
                         "up": f"{int(row.get('ck0', 1))}#{int(row.get('ck1', 1))}#{int(row.get('ck2', 0))}#{int(row.get('ck3', 0))}"}
                        for row in unlocked
                    ],
                })
            self.send_ext({"_cmd": "2504", "sp": int(player.get("skills", {}).get("sp", 0))})
            self.send_ext({"_cmd": "2143", **calc_player_properties(self.char_id)})


class GameGateway:
    """游戏 TCP 网关管理器"""

    def __init__(self):
        self.sessions: Dict[str, ClientSession] = {}

    async def notify_player_inventory_update(
            self, char_id: int, items: List[Dict[str, Any]]
    ) -> int:
        """Push GM-granted inventory changes to active sessions for this player."""
        online_sessions = [
            session
            for session in self.sessions.values()
            if session.char_id == char_id and session.is_logged_in
        ]
        for session in online_sessions:
            session.send_ext({"_cmd": "8_0", "up": items})
            await session.flush()
        return len(online_sessions)

    async def notify_player_new_pets(
            self, char_id: int, pets: List[Dict[str, Any]]
    ) -> int:
        """Push GM-granted pets using the client's new-pet extension response."""
        online_sessions = [
            session
            for session in self.sessions.values()
            if session.char_id == char_id and session.is_logged_in
        ]
        if not online_sessions:
            return 0

        _, follow_id, sequence = _pet_inventory_for_client(char_id)
        for session in online_sessions:
            for pet in pets:
                session.send_ext(
                    {
                        "_cmd": "27_3",
                        "f": follow_id,
                        "s": sequence,
                        "fv": format_pet_for_client(pet, char_id),
                    }
                )
            await session.flush()
        return len(online_sessions)

    async def handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_key = f"{peer[0]}:{peer[1]}"
        session = ClientSession(reader, writer)
        self.sessions[peer_key] = session
        logger.info("客户端连接建立: %s", peer_key)

        try:
            session.send_raw(POLICY_XML)
            await session.flush()

            while not reader.at_eof():
                chunk = await reader.read(4096)
                if not chunk:
                    break

                if POLICY_REQUEST in chunk and not session.is_logged_in and len(session.buffer) == 0:
                    logger.info("收到 %s 的同端口策略探测请求，已在建连时响应放行", peer_key)
                    writer.close()
                    await writer.wait_closed()
                    return

                session.buffer.extend(chunk)
                packets, session.buffer = parse_uplink_stream(session.buffer, char_id=session.char_id)

                for pkt in packets:
                    await self.dispatch_packet(session, pkt)
                    await session.flush()

        except ConnectionResetError:
            logger.info("客户端断开连接 (Reset): %s", peer_key)
        except Exception as e:
            logger.exception("处理客户端 %s 异常: %s", peer_key, e)
        finally:
            self.sessions.pop(peer_key, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("客户端连接已关闭: %s", peer_key)

    async def dispatch_packet(self, session: ClientSession, pkt: UplinkPacket) -> None:
        logger.info(
            "[收到数据包] Seq=%d, Type=%s, Action=%d, Cmd=%s, SubId=%s",
            pkt.packet_num,
            pkt.msg_type.name,
            pkt.action,
            pkt.cmd,
            pkt.sub_id,
        )

        if pkt.msg_type == PacketType.SYS:
            if pkt.action == UpCmd.LOGIN:
                user_info = pkt.payload if isinstance(pkt.payload, dict) else {}
                if user_info.get("user_name"):
                    session.user_name = user_info["user_name"]
                    update_basic(session.char_id, user_name=session.user_name)
                session.is_logged_in = True
                logger.info("【官方客户端登录成功】用户: %s (Zone: %s)", session.user_name, user_info.get("zone"))

                session.send_ext(build_log_ok(session.char_id, session.user_name, float(int(time.time() * 1000))))
                session.send_ext({"_cmd": "17_3", "id": 328})
                session.send_ext({"_cmd": "17_3", "id": 417})
                session.send_ext({"_cmd": "17_3", "id": 476})

            elif pkt.action == UpCmd.JOIN_ROOM:
                room_name = pkt.payload.get("room_name", session.active_room or "momomaintown_1") if isinstance(
                    pkt.payload, dict) else (session.active_room or "momomaintown_1")
                session.active_room = room_name
                logger.info("【加入场景】玩家 %s 请求进入场景: %s", session.user_name, room_name)

                user_vars = build_user_vars_list(session.char_id)
                user_entry = {
                    "id": session.char_id,
                    "name": session.user_name,
                    "vars": user_vars,
                }

                # 1. 副本战斗场景进入
                if room_name.endswith("_fb") or "yongwanggethree" in room_name:
                    dungeon = get_dungeon(session.char_id)
                    if dungeon:
                        dungeon.set_current_room(room_name)
                        scene_meta = dungeon.get_current_scene_meta()
                        room_id = scene_meta["id"]
                    else:
                        room_id = 304686

                    session.in_fb = True
                    session.current_room_id = room_id
                    session.active_room = room_name

                    # 1. 下发官方规范的 JOIN_OK
                    session.send_raw(build_downlink_join_ok(
                        room_id=room_id,
                        room_name=room_name,
                        max_users=100,
                        users=[user_entry]
                    ))

                    # 2. 帧 1：2303 场景重置（告知客户端开始加载新地图资源）
                    session.send_ext({"_cmd": "2303", "bi": room_id})

                    # 3. 帧 2：2311 场景掉落物列表（初始为空）
                    session.send_ext({"_cmd": "2311", "bi": room_id, "il": []})

                    # 4. 帧 3：2301 本地玩家实体（必须注入朝向与出生坐标，让相机聚焦绑定）
                    ent_2301 = build_tcp_2301_entity(session.char_id)
                    ent_2301["bi"] = room_id
                    ent_2301["x"] = 40.0
                    ent_2301["y"] = 48.0
                    ent_2301["d"] = 1.0  # 朝右，确保骨骼方向激活
                    if ent_2301.get("pet"):
                        ent_2301["pet"]["bi"] = room_id
                        ent_2301["pet"]["x"] = 35.0
                        ent_2301["pet"]["y"] = 48.0
                    session.send_ext({"_cmd": "2301", "bi": room_id, "pk": False, "cl": [ent_2301]})

                    # 5. 帧 4：2131 刷新角色实时数值（激活副本战斗 HUD 与连击面板）
                    dyn_props = calc_player_properties(session.char_id)
                    session.send_ext({
                        "_cmd": "2131",
                        "ci": float(session.char_id),
                        "n": session.user_name,
                        "r": 1,
                        "j": dyn_props["job_name"],
                        "jlv": 0,
                        "lv": dyn_props["lv"],
                        "plv": dyn_props["lv"],
                        "originaljob": dyn_props["originaljob"],
                        "mhp": dyn_props["mhp"],
                        "chp": dyn_props["chp"],
                        "mmp": dyn_props["mmp"],
                        "cmp": dyn_props["cmp"],
                        "att": dyn_props["att"],
                        "def": dyn_props["def"],
                        "ba_lv": dyn_props["ba_lv"],
                        "ba_t": dyn_props.get("ba_t", dyn_props.get("ba_lv", 400)),
                        "ba_eqp": dyn_props.get("ba_eqp", 0),
                        "ba_sk": dyn_props.get("ba_sk", 0),
                        "ba_vp": dyn_props.get("ba_vp", 0),
                        "ba_pt": dyn_props.get("ba_pt", 0),
                        "ba_clo": dyn_props.get("ba_clo", 0),
                        "ba_pg": dyn_props.get("ba_pg", 0),
                        "ba_cg": dyn_props.get("ba_cg", 0),
                        "ba_dp": dyn_props.get("ba_dp", 0),
                        "ba_fp": dyn_props.get("ba_fp", 0),
                    })

                    # 6. 帧 5：2321 场景机关与传送门列表
                    session.send_ext({"_cmd": "2321", "bi": room_id, "sl": []})
                    return

                # 2. 常规场景持久化记录
                update_position(session.char_id, last_room=room_name)

                join_batches = CAPTURED_TRIGGERS.get("SYS_10012", [])
                batch_idx = -1

                if "shenyuanroad" in room_name:
                    batch_idx = 0
                elif "momomaintown" in room_name:
                    batch_idx = 5
                elif "newjobfbyongwangge" in room_name:
                    batch_idx = 3 if is_tutorial_completed(session.char_id) else 1
                elif "guardtemple" in room_name:
                    batch_idx = 4
                else:
                    batch_idx = 5 if len(join_batches) > 5 else 0

                if batch_idx >= 0 and join_batches and batch_idx < len(join_batches) and join_batches[batch_idx]:
                    logger.info("下发官方场景入场 5 帧序列 (批次 %d: 场景 %s)", batch_idx, room_name)
                    for f_idx, frame in enumerate(join_batches[batch_idx]):
                        if f_idx == 0:
                            target_room_id = 304448 if "shenyuanroad" in room_name else (
                                2403 if "momomaintown" in room_name else 304537)
                            try:
                                if len(frame) >= 15:
                                    target_room_id = struct.unpack_from(">i", frame, 11)[0]
                            except Exception:
                                pass
                            session.current_room_id = target_room_id
                            dyn_join_ok = build_downlink_join_ok(
                                room_id=target_room_id,
                                room_name=room_name,
                                max_users=200,
                                users=[user_entry]
                            )
                            session.send_raw(dyn_join_ok)
                            continue
                        elif f_idx == 3 and session.user_name:
                            try:
                                obj_2301 = decode_amf3(frame[5:])
                                if isinstance(obj_2301, dict) and "cl" in obj_2301 and obj_2301["cl"]:
                                    ent = obj_2301["cl"][0]
                                    ent["n"] = session.user_name
                                    ent["dn"] = session.user_name
                                    dyn = build_tcp_2301_entity(session.char_id)
                                    ent["lv"] = dyn.get("lv", 1)
                                    ent["plv"] = dyn.get("lv", 1)
                                    ent["chp"] = dyn.get("chp", 836)
                                    ent["mhp"] = dyn.get("mhp", 836)
                                    ent["cl"] = dyn.get("cl", ent.get("cl", []))

                                    if dyn.get("pet"):
                                        ent["pet"] = dict(dyn["pet"])
                                        ent["pet"]["bi"] = target_room_id
                                    elif "pet" in ent:
                                        del ent["pet"]

                                    obj_2301["cl"] = [ent]
                                    session.send_ext(obj_2301)
                                    continue
                            except Exception:
                                pass
                        else:
                            try:
                                obj_f = decode_amf3(frame[5:])
                                if isinstance(obj_f, dict):
                                    cmd_f = obj_f.get("_cmd")
                                    if cmd_f == "2131":
                                        dyn_props = calc_player_properties(session.char_id)
                                        for prop_k in ("lv", "plv", "ba_lv", "ba_t", "ba_eqp", "ba_sk", "ba_vp", "ba_pt", "ba_pg", "ba_clo", "ba_cg", "ba_dp", "ba_fp", "mhp",
                                                       "chp", "mmp", "cmp", "att", "def"):
                                            if prop_k in dyn_props:
                                                obj_f[prop_k] = dyn_props[prop_k]
                                        session.send_ext(obj_f)
                                        continue
                                    elif cmd_f == "2421":
                                        dyn_props = calc_player_properties(session.char_id)
                                        if "bv" in obj_f and isinstance(obj_f["bv"], dict) and "lv" in obj_f["bv"]:
                                            obj_f["bv"]["lv"] = dyn_props["lv"]
                                        if "cl" in obj_f and isinstance(obj_f["cl"], list):
                                            for cl_ent in obj_f["cl"]:
                                                if isinstance(cl_ent, dict) and "lv" in cl_ent:
                                                    cl_ent["lv"] = dyn_props["lv"]
                                        session.send_ext(obj_f)
                                        continue
                            except Exception:
                                pass
                        session.send_raw(frame)
                else:
                    room_id = 304448 if "shenyuanroad" in room_name else 2403
                    session.current_room_id = room_id
                    session.send_raw(
                        build_downlink_join_ok(room_id=room_id, room_name=room_name, max_users=100, users=[user_entry]))
                    ent_2301 = build_tcp_2301_entity(session.char_id)
                    ent_2301["bi"] = room_id
                    if ent_2301.get("pet"):
                        ent_2301["pet"]["bi"] = room_id
                    session.send_ext({"_cmd": "2301", "bi": room_id, "pk": False, "cl": [ent_2301]})

            elif pkt.action == UpCmd.HIT:
                pass

            elif pkt.action == UpCmd.PUB_MSG:
                msg_txt = ""
                if isinstance(pkt.payload, dict):
                    msg_txt = str(pkt.payload.get("txt", pkt.payload.get("msg", "")))
                elif isinstance(pkt.payload, str):
                    msg_txt = pkt.payload

                if msg_txt.startswith("/lb ") or msg_txt.startswith("/龙币 "):
                    try:
                        amt = int(msg_txt.split()[1])
                        new_lb = add_dragon_coin(session.char_id, amt)
                        session.send_ext({"_cmd": "21_7", "ab": 9999900, "lb": new_lb})
                    except Exception:
                        pass
                elif msg_txt.startswith("/vip "):
                    try:
                        vlv = min(10, max(1, int(msg_txt.split()[1])))
                        set_vip_info(session.char_id, level=vlv, is_vip=True)
                        session.send_ext({
                            "_cmd": "4_0",
                            "mt": 8, "mlv": vlv, "ms": 99999,
                            "an": True, "muo": True, "nls": 0,
                            "med": "2035-12-31", "spm": True, "rsmd": 3650
                        })
                        uv_pkt = build_downlink_user_vars_update(
                            room_id=getattr(session, "current_room_id", 2403),
                            user_id=session.char_id,
                            vars_list=[("mi", "s", f"8_{vlv}_true_true")]
                        )
                        session.send_raw(uv_pkt)
                    except Exception:
                        pass
                elif msg_txt.startswith("/pet ") or msg_txt.startswith("/神宠 "):
                    try:
                        pti_val = int(msg_txt.split()[1])
                        add_pet(session.char_id, pti=pti_val, lv=100, ecs=5)
                    except Exception:
                        pass
                elif msg_txt.startswith("/fb ") or msg_txt.startswith("/副本 "):
                    try:
                        fbid = int(msg_txt.split()[1])
                        logger.info("【GM测试】手动强制开启副本 ID=%d", fbid)
                        session.in_fb = True
                        session.fb10_cleared = False
                        dungeon = get_or_create_dungeon(session.char_id, fb_id=fbid, fb_lv=2)
                        first_scene = dungeon.get_current_scene_meta()
                        session.active_room = first_scene["rn"]
                        session.send_ext(dungeon.get_join_packet())
                    except Exception as e:
                        logger.error("GM进本异常: %s", e)

        elif pkt.msg_type == PacketType.EXT:
            cmd = pkt.cmd or ""
            payload = pkt.payload if isinstance(pkt.payload, dict) else {}
            if cmd.startswith("15_") or cmd.startswith("1-"):
                logger.info("【副本前置交互】收到命令: Cmd=%s, Payload=%s", cmd, payload)

            # 1. 玩家公聊/私聊消息 (处理聊天框 GM 指令)
            if cmd == "pMsg":
                msg_txt = str(payload.get("msg", payload.get("txt", payload.get("m", ""))))
                logger.info("【收到聊天内容】: %s", msg_txt)

                if msg_txt.startswith("/fb") or msg_txt.startswith("/副本"):
                    parts = msg_txt.split()
                    fbid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 10
                    fblv = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 2
                    logger.info("【GM进本】玩家 %d 强制开启副本 ID=%d, 难度=%d", session.char_id, fbid, fblv)

                    session.in_fb = True
                    session.fb10_cleared = False
                    dungeon = get_or_create_dungeon(session.char_id, fb_id=fbid, fb_lv=fblv)
                    first_scene = dungeon.get_current_scene_meta()
                    target_room_name = first_scene["rn"]
                    target_room_id = first_scene["id"]
                    target_sn = first_scene["sn"]

                    session.active_room = target_room_name
                    session.current_room_id = target_room_id

                    # 1. 下发官方 15_0 准入包
                    join_pkt = dungeon.get_join_packet()
                    session.send_ext(join_pkt)

                    # 2. 组装用户变量
                    user_vars = build_user_vars_list(session.char_id)
                    user_entry = {
                        "id": int(session.char_id),
                        "name": session.user_name,
                        "vars": user_vars,
                    }

                    # 3. 下发官方房间准入信号 JOIN_OK
                    session.send_raw(build_downlink_join_ok(
                        room_id=target_room_id,
                        room_name=target_room_name,
                        max_users=100,
                        users=[user_entry]
                    ))

                    # 4. 关键帧 1：2303 场景重置（指定官方场景 ID，通知客户端加载新地图）
                    session.send_ext({"_cmd": "2303", "bi": target_room_id})
                    session.send_ext({"_cmd": "2311", "bi": target_room_id, "il": []})

                    # 5. 关键帧 2：2301 本地玩家实体
                    ent_2301 = build_tcp_2301_entity(session.char_id)
                    ent_2301["bi"] = target_room_id
                    ent_2301["ui"] = int(session.char_id)
                    ent_2301["ci"] = float(session.char_id)
                    ent_2301["aai"] = True
                    ent_2301["x"] = 40.0
                    ent_2301["y"] = 48.0
                    ent_2301["d"] = 1.0
                    if ent_2301.get("pet"):
                        ent_2301["pet"]["bi"] = target_room_id
                        ent_2301["pet"]["ui"] = -1
                        ent_2301["pet"]["x"] = 35.0
                        ent_2301["pet"]["y"] = 48.0

                    session.send_ext({
                        "_cmd": "2301",
                        "bi": target_room_id,
                        "pk": False,
                        "cl": [ent_2301]
                    })

                    # 6. 关键帧 3：2131 刷新角色属性 (挂载副本 HUD 与血条)
                    dyn_props = calc_player_properties(session.char_id)
                    session.send_ext({
                        "_cmd": "2131",
                        "ci": float(session.char_id),
                        "n": session.user_name,
                        "r": 1,
                        "j": dyn_props["job_name"],
                        "jlv": 0,
                        "lv": dyn_props["lv"],
                        "plv": dyn_props["lv"],
                        "originaljob": dyn_props["originaljob"],
                        "mhp": dyn_props["mhp"],
                        "chp": dyn_props["chp"],
                        "mmp": dyn_props["mmp"],
                        "cmp": dyn_props["cmp"],
                        "att": dyn_props["att"],
                        "def": dyn_props["def"],
                        "ba_lv": dyn_props["ba_lv"],
                        "ba_t": dyn_props.get("ba_t", dyn_props.get("ba_lv", 400)),
                        "ba_eqp": dyn_props.get("ba_eqp", 0),
                        "ba_sk": dyn_props.get("ba_sk", 0),
                        "ba_vp": dyn_props.get("ba_vp", 0),
                        "ba_pt": dyn_props.get("ba_pt", 0),
                        "ba_pg": dyn_props.get("ba_pg", 0),
                        "ba_clo": dyn_props.get("ba_clo", 0),
                        "ba_cg": dyn_props.get("ba_cg", 0),
                        "ba_dp": dyn_props.get("ba_dp", 0),
                        "ba_fp": dyn_props.get("ba_fp", 0),
                    })
                    session.send_ext({"_cmd": "2321", "bi": target_room_id, "sl": []})

                    # 7. 刷怪
                    monster_packets = dungeon.spawn_monsters(cr_id=201)
                    for m_pkt in monster_packets:
                        session.send_ext(m_pkt)

                    logger.info("【GM进本】成功切换至副本场景 %s (地图名: %s)", target_room_name, target_sn)
                    return

            # 2. 官方行为统计与埋点打点 (StatClient)，直接静默返回放行
            elif cmd in ("1-2", "1_2", "1-14", "1_14", "1-12", "1-18"):
                session.send_ext({"_cmd": cmd, "r": 0, "s": 0})
                return

            elif cmd == "getCurrentTime":
                session.send_ext({"_cmd": "getCurrentTime", "t": str(int(time.time() * 1000))})
            elif cmd == "getRegisterTime":
                session.send_ext({
                    "_cmd": "getRegisterTime",
                    "rs": "2026-10-06",
                    "rt": int(time.time() * 1000)
                })

            elif cmd == "11_0":
                slots = [{"id": 0, "pos": i, "type": 0} for i in range(8)]
                session.send_ext({
                    "_cmd": "11_0",
                    "c": slots,
                    "s": "",
                    "r": 0,
                    "res": 0
                })

            elif cmd == "75_1":
                session.send_ext({
                    "_cmd": "75_1",
                    "r": 0,
                    "res": "1_1_1_1_1_1_1_1",
                    "s": 0
                })

            elif cmd == "105_1":
                session.send_ext({"_cmd": "105_1", "r": 0, "res": 0, "level": 1, "exp": 0})

            elif cmd == "25_762_7":
                session.send_ext({"_cmd": "25_762_7", "r": 0, "res": 0, "state": 0})

            elif cmd == "10-20":
                session.send_ext({"_cmd": "10-20", "r": 0})

            elif cmd == "initUserData":
                session.send_ext({"_cmd": "initUserData", "r": 0})

            elif cmd == "27_0":
                formatted_pets, follow_id, seq_str = _pet_inventory_for_client(session.char_id)
                session.send_ext({
                    "_cmd": "27_0",
                    "r": 0,
                    "list": formatted_pets,
                    "s": seq_str,
                    "f": follow_id,
                })

            elif cmd == "10-0":
                now_ms = float(int(time.time() * 1000))
                p_cur = load_player(session.char_id)
                tasks_list = []
                # Restore the actual persisted task state. The old hardcoded
                # placeholder tasks were not backed by storage and hid real
                # accepted tasks on every login.
                for tid, record in p_cur.get("tasks", {}).items():
                    if not str(tid).isdigit() or not isinstance(record, dict):
                        continue
                    if str(tid) == "513":
                        continue
                    task_ti = str(record.get("ti", f"{tid}_0"))
                    task = {
                        "taskId": int(tid),
                        "st": int(record.get("st", 0) or 0),
                        "ex": record.get("ex", ""),
                        "ad": record.get("ad", now_ms),
                        "ti": task_ti,
                        "tm": int(record.get("tm", 0) or 0),
                    }
                    if record.get("fd"):
                        task["fd"] = record["fd"]
                    tasks_list.append(task)
                if is_tutorial_completed(session.char_id):
                    session.task_step = "513_done"
                    t513 = p_cur.get("tasks", {}).get("513", {})
                    tasks_list.insert(0, {
                        "taskId": 513,
                        "st": 1,
                        "ex": t513.get("ex", ""),
                        "ad": t513.get("ad", now_ms),
                        "fd": t513.get("fd", now_ms),
                        "ti": "513_8",
                        "tm": 1,
                    })
                else:
                    t513 = p_cur.get("tasks", {}).get("513", {})
                    curr_step = t513.get("step", 0)
                    curr_ti = t513.get("ti", "513_0")
                    if curr_step > 0:
                        session.task_step = curr_ti
                        tasks_list.insert(0, {
                            "taskId": 513,
                            "st": 0,
                            "ex": t513.get("ex", ""),
                            "ad": t513.get("ad", now_ms),
                            "ti": curr_ti,
                            "tm": 0,
                            "step": curr_step,
                        })
                    else:
                        session.task_step = "513_0"

                session.send_ext({"_cmd": "10-0", "t": tasks_list})
                session.send_props_update()

            elif cmd == "25_908_1":
                # DragonTreasuerInteract.handleData reads three parallel groups
                # from dt/bs/tt. The generic {s,r} reply causes Error #1010 when
                # it indexes those missing fields after entering the scene.
                session.send_ext({
                    "_cmd": "25_908_1",
                    "dt": "0,0,0",
                    "bs": "0#0#0",
                    "tt": "0#0#0",
                })

            elif cmd == "10-7":
                session.send_ext({"_cmd": "10-7", "s": True, "b": "115:100408:1"})
            elif cmd == "10-3":
                now_sec = time.time()
                now_ms = float(int(now_sec * 1000))
                try:
                    requested_id = int(payload.get("ti", payload.get("id", 0)))
                except (TypeError, ValueError):
                    requested_id = 0
                try:
                    start_type = int(payload.get("ty", 11) or 11)
                except (TypeError, ValueError):
                    start_type = 11
                if requested_id == 513:
                    session.task_step = "513_1"
                    session.walk_task_started_time = now_sec
                    update_task(session.char_id, 513, step=1, st=0, ti="513_1")
                    session.send_ext({
                        "_cmd": "10-1", "ty": 1,
                        "t": {"taskId": 513, "st": 0, "ex": "", "ad": now_ms, "ti": "513_1", "tm": 0, "step": 1}
                    })
                    session.send_ext({"_cmd": "10-7", "s": True, "b": "115:100408:1"})
                else:
                    player = load_player(session.char_id)
                    definition = get_task(requested_id)
                    allowed, reason = can_start_task(requested_id, player)
                    existing = player.get("tasks", {}).get(str(requested_id), {})
                    if not definition or not allowed or int(existing.get("st", 0) or 0) == 1:
                        logger.info("任务接取拒绝: char=%s task=%s reason=%s", session.char_id, requested_id, reason or "completed_or_unknown")
                        session.send_ext({"_cmd": "10-5", "ti": requested_id, "res": ""})
                    elif existing and int(existing.get("st", 0) or 0) == 0:
                        # Duplicate click/auto-start must resend the saved step,
                        # never reset task progress to the beginning.
                        current_ti = str(existing.get("ti", f"{requested_id}_{int(existing.get('step', 0) or 0)}"))
                        session.send_ext({"_cmd": "10-1", "ty": 0, "t": {
                            "taskId": requested_id, "st": 0, "ex": existing.get("ex", ""),
                            "ad": existing.get("ad", now_ms), "ti": current_ti,
                            "tm": int(existing.get("tm", 0) or 0), "step": int(existing.get("step", 0) or 0),
                        }})
                    else:
                        steps = definition.get("steps", [])
                        # A type-0 first row is the normal NPC starter itself.
                        # 10-3 is sent after that NPC click, so persist the first
                        # actionable objective instead of sending the player back
                        # to the same dialogue forever.
                        start_index = 0
                        if start_type == 11 and len(steps) > 1 and int(steps[0].get("ty", -1) or 0) == 0:
                            start_index = 1
                        task_ti = f"{requested_id}_{start_index}"
                        update_task(session.char_id, requested_id, step=start_index, st=0, ti=task_ti)
                        record = load_player(session.char_id).get("tasks", {}).get(str(requested_id), {})
                        session.send_ext({"_cmd": "10-1", "ty": 1, "t": {
                            "taskId": requested_id, "st": 0, "ex": record.get("ex", ""),
                            "ad": record.get("ad", now_ms), "ti": task_ti, "tm": 0, "step": start_index,
                        }})
            elif cmd == "10-2":
                ti = str(payload.get("ti", ""))
                try:
                    trigger_type = int(payload.get("ty", 11) or 11)
                except (TypeError, ValueError):
                    trigger_type = 11
                now_ms = float(int(time.time() * 1000))

                if "513_1" in ti:
                    session.task_step = "513_2"
                    update_task(session.char_id, 513, step=2, st=0, ti="513_2")
                    session.send_ext({
                        "_cmd": "10-1", "ty": 0,
                        "t": {"taskId": 513, "st": 0, "ex": "", "ad": now_ms, "ti": "513_2", "tm": 0, "step": 2}
                    })
                    session.send_ext({"_cmd": "10-7", "s": True, "b": "115:100408:1"})
                elif "513_2" in ti:
                    session.task_step = "513_3"
                    update_task(session.char_id, 513, step=3, st=0, ti="513_3")
                    session.send_ext({
                        "_cmd": "10-1", "ty": 0,
                        "t": {"taskId": 513, "st": 0, "ex": "", "ad": now_ms, "ti": "513_3", "tm": 0, "step": 3}
                    })
                    session.send_ext({"_cmd": "10-7", "s": True, "b": "115:100408:1"})
                elif "513_3" in ti:
                    session.task_step = "513_4"
                    update_task(session.char_id, 513, step=4, st=0, ti="513_4")
                    session.send_ext({
                        "_cmd": "10-1", "ty": 0,
                        "t": {"taskId": 513, "st": 0, "ex": "", "ad": now_ms, "ti": "513_4", "tm": 0, "step": 4}
                    })
                    session.send_ext({"_cmd": "10-7", "s": True, "b": "115:100408:1"})
                elif "513_4" in ti:
                    session.task_step = "513_5"
                    update_task(session.char_id, 513, step=5, st=0, ti="513_5")
                    starter_item = add_bag_item(
                        session.char_id, mid=1001, count=1, t=2
                    )
                    session.send_ext({
                        "_cmd": "10-1", "ty": 0,
                        "t": {"taskId": 513, "st": 0, "ex": "", "ad": now_ms, "ti": "513_5", "tm": 0, "step": 5}
                    })
                    session.send_props_update()
                    session.send_ext({"_cmd": "10-6", "ext": "102:100:5"})
                    session.send_ext({"_cmd": "2504", "sp": 5})
                elif "513_5" in ti:
                    session.task_step = "513_6"
                    update_task(session.char_id, 513, step=6, st=0, ti="513_6")
                    session.send_ext({
                        "_cmd": "10-1", "ty": 0,
                        "t": {"taskId": 513, "st": 0, "ex": "", "ad": now_ms, "ti": "513_6", "tm": 0, "step": 6}
                    })
                    session.send_ext({"_cmd": "10-7", "s": True, "b": "115:100408:1"})
                elif "513_6" in ti:
                    session.task_step = "513_7"
                    update_task(session.char_id, 513, step=7, st=0, ti="513_7")
                    session.send_ext({
                        "_cmd": "10-1", "ty": 0,
                        "t": {"taskId": 513, "st": 0, "ex": "", "ad": now_ms, "ti": "513_7", "tm": 0, "step": 7}
                    })
                    session.send_ext({"_cmd": "10-7", "s": True, "b": "115:100408:1"})
                elif "513_7" in ti:
                    session.task_step = "513_8"
                    update_task(session.char_id, 513, step=8, st=0, ti="513_8")
                    session.send_ext({
                        "_cmd": "10-1", "ty": 0,
                        "t": {"taskId": 513, "st": 0, "ex": "", "ad": now_ms, "ti": "513_8", "tm": 0, "step": 8}
                    })
                    session.send_ext({"_cmd": "10-7", "s": True, "b": "115:100408:1"})
                elif "513_8" in ti:
                    session.task_step = "513_done"
                    complete_tutorial(session.char_id, target_room=f"newjobfbyongwangge_{session.char_id}_0_tr")
                    session.send_ext({
                        "_cmd": "10-1", "ty": 2,
                        "t": {"taskId": 513, "st": 1, "ex": "", "ad": now_ms, "ti": "513_8", "tm": 1, "fd": now_ms}
                    })
                else:
                    match = re.fullmatch(r"(\d+)_(\d+)", ti)
                    if not match:
                        logger.info("忽略无法解析的任务步骤: char=%s ti=%s", session.char_id, ti)
                    else:
                        task_id, step_index = int(match.group(1)), int(match.group(2))
                        definition = get_task(task_id)
                        player = load_player(session.char_id)
                        record = player.get("tasks", {}).get(str(task_id), {})
                        steps = definition.get("steps", []) if definition else []
                        current_step = steps[step_index] if step_index < len(steps) else None
                        saved_index = int(record.get("step", step_index) or 0)
                        # Only accept an event for the persisted current step,
                        # and only when the client trigger matches that step.
                        if definition and record and int(record.get("st", 0) or 0) == 0 and saved_index == step_index and current_step and int(current_step.get("ty", -1) or 0) == trigger_type:
                            next_index = step_index + 1
                            if next_index >= len(steps):
                                update_task(session.char_id, task_id, step=step_index, st=1, ti=ti, tm=1, ex=record.get("ex", ""))
                                finished = load_player(session.char_id).get("tasks", {}).get(str(task_id), {})
                                session.send_ext({"_cmd": "10-1", "ty": 2, "t": {
                                    "taskId": task_id, "st": 1, "ex": finished.get("ex", ""),
                                    "ad": finished.get("ad", now_ms), "fd": finished.get("fd", now_ms),
                                    "ti": ti, "tm": 1,
                                }})
                            else:
                                next_ti = f"{task_id}_{next_index}"
                                update_task(session.char_id, task_id, step=next_index, st=0, ti=next_ti, ex=record.get("ex", ""))
                                updated = load_player(session.char_id).get("tasks", {}).get(str(task_id), {})
                                session.send_ext({"_cmd": "10-1", "ty": 0, "t": {
                                    "taskId": task_id, "st": 0, "ex": updated.get("ex", ""),
                                    "ad": updated.get("ad", now_ms), "ti": next_ti, "tm": 0, "step": next_index,
                                }})
                        else:
                            logger.info("任务步骤未推进: char=%s ti=%s trigger=%s saved=%s expected=%s", session.char_id, ti, trigger_type, saved_index, current_step.get("ty") if current_step else None)

            elif cmd == "10-4":
                try:
                    task_id = int(payload.get("ti", 0))
                except (TypeError, ValueError):
                    task_id = 0
                definition = get_task(task_id)
                player = load_player(session.char_id)
                record = player.get("tasks", {}).get(str(task_id), {})
                if definition and definition.get("canGiveUp") and record and int(record.get("st", 0) or 0) == 0:
                    task_ti = str(record.get("ti", f"{task_id}_0"))
                    step_index = int(record.get("step", 0) or 0)
                    update_task(session.char_id, task_id, step=step_index, st=4, ti=task_ti, ex=record.get("ex", ""))
                    updated = load_player(session.char_id).get("tasks", {}).get(str(task_id), {})
                    session.send_ext({"_cmd": "10-1", "ty": 0, "t": {
                        "taskId": task_id, "st": 4, "ex": updated.get("ex", ""),
                        "ad": updated.get("ad", time.time() * 1000), "ti": task_ti, "tm": 0,
                    }})
                else:
                    session.send_ext({"_cmd": "10-5", "ti": task_id, "res": ""})

            elif cmd == "13_2":
                session.task_step = "513_2"
                session.fight_attack_count = 0
                session.monsters_killed = False
                if "13_2" in CAPTURED_TRIGGERS and CAPTURED_TRIGGERS["13_2"]:
                    for frame in CAPTURED_TRIGGERS["13_2"][0]:
                        session.send_raw(frame)
                else:
                    session.send_ext({"_cmd": "13_2", "r": 0})
                real_bi = getattr(session, "current_room_id", 304448)
                session.send_ext({
                    '_cmd': '2451', 'bi': real_bi, 'ci': -4123506.0,
                    'p': '123,54,8#123,53,8', 'bt': True, 's': 15
                })
                session.send_ext({
                    '_cmd': '2451', 'bi': real_bi, 'ci': -4123507.0,
                    'p': '130,45,1#126,49,4#124,49,4', 'bt': True, 's': 15
                })

            elif cmd == "17_2":
                gift_id = int(payload.get("id", 1657))
                if gift_id == 1656:
                    set_pet(session.char_id, pti=100408, name="亚克")
                    add_pet(session.char_id, pti=100140, lv=1, ecs=0)
                    tr_list = CAPTURED_TRIGGERS.get("17_2", [])
                    if len(tr_list) > 1:
                        for frame in tr_list[1]:
                            session.send_raw(frame)
                    elif "17_2" in CAPTURED_SINGLE_FRAMES:
                        session.send_raw(CAPTURED_SINGLE_FRAMES["17_2"])
                    else:
                        session.send_ext({
                            "_cmd": "17_2", "gi": gift_id, "re": 5, "cn": -1,
                            "bo": "115:100140:1", "suc": True,
                        })
                else:
                    starter_weapon = add_bag_item(
                        session.char_id, mid=100001, count=1, t=0
                    )
                    session.send_ext({"_cmd": "8_0", "up": [starter_weapon]})
                    session.send_ext({
                        "_cmd": "17_2", "gi": gift_id, "re": 5, "cn": -1,
                        "bo": "0:100001:1", "suc": True,
                    })

            elif cmd in ("4_3", "4_4"):
                add_equip(session.char_id, equip_id=110001)
                if cmd in CAPTURED_SINGLE_FRAMES:
                    session.send_raw(CAPTURED_SINGLE_FRAMES[cmd])
                else:
                    session.send_ext({"_cmd": cmd, "s": 0, "r": 0})
                    session.send_ext({
                        "_cmd": "4_5", "id": 110001, "itemId": 110001, "pos": 6, "ci": session.char_id
                    })

            elif cmd == "4_0":
                vip_info = get_vip_info(session.char_id)
                is_v = vip_info.get("is_vip", True)
                v_level = min(10, max(1, int(vip_info.get("vip_level", 7)))) if is_v else 0
                session.send_ext({
                    "_cmd": "4_0",
                    "mt": 8 if is_v else 0,
                    "mlv": v_level,
                    "ms": 99999,
                    "an": vip_info.get("is_annual", True),
                    "muo": True,
                    "nls": 0,
                    "med": "2035-12-31",
                    "spm": vip_info.get("is_super_vip", True),
                    "rsmd": vip_info.get("super_vip_days", 3650),
                    "psn": 60,
                    "prn": 60,
                    "pen": 60,
                    "pcn": 60,
                    "pbn": 500,
                    "lgb": "1:400011:1#1:100014:1#2:400021:1#2:100022:1#3:400031:1#3:100033:1#4:400041:1#4:100042:1#5:400051:1#5:100053:1#6:400061:1#6:100062:1#7:400071:1#7:100073:1",
                    "gb": "",
                    "per": 0.0,
                    "mev": False,
                })

            elif cmd == "4_8":
                session.send_ext({
                    "_cmd": "4_8",
                    "r": 0,
                    "isAnnual": True,
                    "annualDays": 3650,
                    "year": 2026,
                    "gb": "1_1_1_1_1"
                })

            elif cmd == "4_5":
                session.send_ext({"_cmd": "4_5", "gb": "0_0_0"})
            elif cmd == "4_1":
                session.send_ext({"_cmd": "4_1", "r": 0})
            elif cmd == "4_2":
                session.send_ext({"_cmd": "4_2", "rn": 99})
            elif cmd == "4_10":
                session.send_ext({"_cmd": "4_10", "r": 0, "day": 3650})

            elif cmd == "21_3":
                p_cur = load_player(session.char_id)
                db_coin = p_cur.get("basic", {}).get("dragon_coin", 999999)
                session.send_ext({"_cmd": "21_3", "ab": 9999900, "lb": db_coin})

            elif cmd in ("21_1", "21_2"):
                goods_id = payload.get("id")
                gid_raw = str(payload.get("gid", ""))
                buy_items = []
                if goods_id is not None:
                    buy_items.append((str(goods_id), 1))
                elif gid_raw:
                    for part in gid_raw.split(","):
                        if not part:
                            continue
                        if "_" in part:
                            g_id_str, g_num_str = part.split("_")
                            buy_items.append((g_id_str, max(1, int(g_num_str) if g_num_str.isdigit() else 1)))
                        elif part.isdigit():
                            buy_items.append((part, 1))

                effective_bonuses = []
                total_cost = 0
                up_bag_items = []
                clothes_updates = []
                new_pets_added = []
                new_mounts_added = []

                prepared_items = []
                try:
                    for g_id_str, buy_count in buy_items:
                        mall_info = get_mall_goods_info(g_id_str)
                        if not mall_info:
                            raise ValueError(
                                f"商城商品 {g_id_str} 缺少官方商品定义"
                            )
                        rewards = []
                        for bonus in str(mall_info.get("bonus", "")).split("#"):
                            if not bonus:
                                continue
                            parts = bonus.split(":")
                            if len(parts) in (3, 4):
                                material_type, mid, quantity = map(int, parts[:3])
                                extra = parts[3] if len(parts) == 4 else ""
                                if len(parts) == 4 and not extra:
                                    raise ValueError(
                                        f"商城商品 {g_id_str} 奖励扩展字段为空: {bonus}"
                                    )
                            elif len(parts) == 2:
                                mid, quantity = map(int, parts)
                                material_type = resolve_inventory_type(mid)
                                extra = ""
                            else:
                                raise ValueError(
                                    f"商城商品 {g_id_str} 奖励格式无效: {bonus}"
                                )
                            if quantity <= 0:
                                raise ValueError(
                                    f"商城商品 {g_id_str} 奖励数量无效: {bonus}"
                                )

                            if material_type == 0 or material_type in GLOBAL_INVENTORY_CATALOGS:
                                resolve_inventory_type(mid, material_type)
                            elif material_type not in (
                                    1, 7, 11, 13, 100, 102, 113, 115, 117, 124, 135
                            ):
                                raise ValueError(
                                    f"商城商品 {g_id_str} 奖励类型不支持: {material_type}"
                                )
                            rewards.append(
                                (material_type, mid, quantity * buy_count, extra)
                            )
                        if not rewards:
                            raise ValueError(
                                f"商城商品 {g_id_str} 没有有效奖励定义"
                            )
                        prepared_items.append((g_id_str, buy_count, mall_info, rewards))
                except ValueError as exc:
                    logger.error("拒绝处理商城购买: %s", exc)
                    session.send_ext({
                        "_cmd": cmd,
                        "res": 1,
                        "r": 1,
                    })
                    return

                for g_id_str, buy_count, mall_info, rewards in prepared_items:
                    price = mall_info.get("price", 10)
                    total_cost += price * buy_count
                    for m_type, m_id, m_num, extra in rewards:
                        bonus_value = f"{m_type}:{m_id}:{m_num}"
                        if extra:
                            bonus_value += f":{extra}"
                        if m_type == 0:
                            it = add_bag_item(
                                session.char_id, mid=m_id, count=m_num, t=0
                            )
                            up_bag_items.append(it)
                            effective_bonuses.append(bonus_value)
                        elif m_type in (1, 11, 13):
                            expires_at = 7614748800000
                            if m_type in (11, 13) and m_num <= 36000:
                                expires_at = int(time.time() * 1000) + m_num * 86400000
                            add_cloth(
                                session.char_id,
                                cloth_id=m_id,
                                expire_at_ms=expires_at,
                            )
                            clothes_updates.append({
                                "gid": m_id,
                                "n": 1,
                                "t": 1,
                                "exp": expires_at,
                                "skt": 0,
                                "gem": "",
                                "cn": "",
                                "cds": "",
                            })
                            effective_bonuses.append(bonus_value)
                        elif m_type == 102:
                            old_level = int(
                                load_player(session.char_id)
                                .get("basic", {})
                                .get("level", 1)
                            )
                            old_skill_ids = {
                                int(row.get("id", 0))
                                for row in load_player(session.char_id).get("skills", {}).get("list", [])
                                if isinstance(row, dict)
                            }
                            updated_player = update_basic(
                                session.char_id,
                                exp_gain=m_num,
                            )
                            session.send_ext({"_cmd": "2141", "e": m_num})
                            new_level = int(
                                updated_player.get("basic", {}).get(
                                    "level", old_level
                                )
                            )
                            if new_level > old_level:
                                unlocked = [
                                    row for row in updated_player.get("skills", {}).get("list", [])
                                    if isinstance(row, dict) and int(row.get("id", 0)) not in old_skill_ids
                                ]
                                if unlocked:
                                    session.send_ext({
                                        "_cmd": "2503",
                                        "list": [
                                            {"id": int(row["id"]), "au": True,
                                             "up": f"{int(row.get('ck0', 1))}#{int(row.get('ck1', 1))}#{int(row.get('ck2', 0))}#{int(row.get('ck3', 0))}"}
                                            for row in unlocked
                                        ],
                                    })
                                session.send_ext({
                                    "_cmd": "2504",
                                    "sp": int(updated_player.get("skills", {}).get("sp", 0)),
                                })
                                session.send_ext({
                                    "_cmd": "2143",
                                    **calc_player_properties(session.char_id),
                                })
                            effective_bonuses.append(bonus_value)
                        elif m_type == 200:
                            add_equip_gem(session.char_id, m_id, m_num)
                            effective_bonuses.append(bonus_value)
                        elif m_type == 117:
                            p_obj = add_pet(
                                session.char_id,
                                pti=m_id,
                                lv=1,
                                ecs=0,
                            )
                            new_pets_added.append(p_obj)
                            effective_bonuses.append(bonus_value)
                        elif m_type == 113:
                            it = add_bag_item(
                                session.char_id, mid=m_id, count=m_num, t=113
                            )
                            up_bag_items.append(it)
                            mount = add_mount(session.char_id, m_id)
                            if mount:
                                new_mounts_added.append(mount)
                            effective_bonuses.append(bonus_value)
                        elif m_type in (115, 135):
                            p_obj = add_pet(session.char_id, pti=m_id, lv=100, ecs=5)
                            new_pets_added.append(p_obj)
                            effective_bonuses.append(bonus_value)
                        elif m_type == 124:
                            if m_id == 4:
                                change_temp_score(session.char_id, m_id, m_num)
                            effective_bonuses.append(bonus_value)
                        elif m_type == 100:
                            p = load_player(session.char_id)
                            p.setdefault("basic", {})["coin"] = p.get("basic", {}).get("coin", 0) + m_num
                            save_player(p, char_id=session.char_id)
                            effective_bonuses.append(f"100:1:{m_num}")
                        else:
                            it = add_bag_item(
                                session.char_id, mid=m_id, count=m_num, t=m_type
                            )
                            up_bag_items.append(it)
                            effective_bonuses.append(bonus_value)

                if not effective_bonuses:
                    logger.error("商城商品奖励列表为空，拒绝确认购买: %s", buy_items)
                    session.send_ext({"_cmd": cmd, "res": 1, "r": 1})
                    return

                final_bonus_str = "#".join(effective_bonuses)
                new_db = add_dragon_coin(session.char_id, -total_cost)

                session.send_ext({"_cmd": "21_7", "t": 1, "v": -total_cost, "cur": new_db, "ab": 9999900, "lb": new_db})
                if up_bag_items:
                    session.send_ext({"_cmd": "8_0", "up": up_bag_items})
                if any(m_type == 124 for _, _, _, rewards in prepared_items for m_type, _, _, _ in rewards):
                    session.send_ext({
                        "_cmd": "8_3",
                        "ts": f"4:{get_temp_score(session.char_id, 4)}",
                    })
                if clothes_updates:
                    session.send_ext({"_cmd": "8_0", "up": clothes_updates})
                for p_obj in new_pets_added:
                    fmt_pet = format_pet_for_client(p_obj, session.char_id)
                    all_pets = get_pets(session.char_id)
                    s_str = "#".join(str(p["pi"]) for p in all_pets)
                    follow_id = int(load_player(session.char_id).get("follow_pet_id", 0))
                    session.send_ext({"_cmd": "27_3", "f": follow_id, "s": s_str, "fv": fmt_pet})
                for mount in new_mounts_added:
                    session.send_ext({
                        "_cmd": "26_6",
                        "rid": mount["rid"],
                        "pid": mount["pid"],
                        "num": 1,
                        "end": mount["end"],
                    })
                    if int(load_player(session.char_id).get("default_rider_id", -1)) == mount["rid"]:
                        session.send_ext({"_cmd": "26_7", "rid": mount["rid"]})

                first_gid = buy_items[0][0] if buy_items else "10001"
                resp_id = int(first_gid) if first_gid.isdigit() else 10001
                session.send_ext({
                    "_cmd": cmd,
                    "res": 0,
                    "r": 0,
                    "id": resp_id,
                    "cnt": sum(count for _, count in buy_items),
                    "gid": gid_raw or first_gid,
                    "it": final_bonus_str,
                })

            elif cmd == "8_2_0":
                m_type = int(payload.get("t", 1))
                m_id = int(payload.get("id", 10001))
                m_cnt = max(1, int(payload.get("n", 1)))
                cost = 500 * m_cnt
                p = load_player(session.char_id)
                cur_coin = p.get("basic", {}).get("coin", 0)
                new_coin = max(0, cur_coin - cost)
                p["basic"]["coin"] = new_coin
                save_player(p, char_id=session.char_id)
                new_it = add_bag_item(session.char_id, mid=m_id, count=m_cnt, t=m_type)
                session.send_ext({"_cmd": "8_1", "bs": new_coin, "ps": 100000, "es": 50, "hs": 1200})
                session.send_ext({"_cmd": "8_0", "up": [new_it]})
                session.send_ext({"_cmd": "8_2_0", "res": 0, "id": m_id, "n": m_cnt})

            elif cmd == "8_2_24":
                session.send_ext({
                    "_cmd": "8_2_24",
                    "l": get_equip_gem_list(session.char_id),
                })

            elif cmd == "8_2_1":
                sell_str = str(payload.get("sell", ""))
                earned = 0
                p = load_player(session.char_id)
                for part in sell_str.split("#"):
                    if not part:
                        continue
                    tokens = part.split(":")
                    if tokens and tokens[0].isdigit():
                        sgid = int(tokens[0])
                        it = next((x for x in p.get("bag", {}).get("items", []) if x.get("gid") == sgid), None)
                        if it:
                            it["n"] = 0
                            p["bag"]["items"].remove(it)
                            earned += 200
                            session.send_ext({"_cmd": "8_0", "up": [{"gid": sgid, "n": 0}]})
                new_coin = p.get("basic", {}).get("coin", 0) + earned
                p["basic"]["coin"] = new_coin
                save_player(p, char_id=session.char_id)
                session.send_ext({"_cmd": "8_1", "bs": new_coin, "ps": 100000, "es": 50, "hs": 1200})
                session.send_ext({"_cmd": "8_2_1", "res": 0})

            elif cmd == "8_2_5":
                gid = int(payload.get("gid", 0))
                equip = next(
                    (
                        item
                        for item in get_player_backpack(session.char_id)
                        if int(item.get("gid", 0)) == gid
                           and int(item.get("t", 0)) == 0
                    ),
                    None,
                )
                equip_meta = (
                    GLOBAL_EQUIP_CONFIG.get(int(equip.get("mid", 0)))
                    if equip
                    else None
                )
                current_level = int(equip.get("cl", 0)) if equip else 0
                level_limit = int(equip_meta.get("strLvLimit", 0)) if equip_meta else 0
                condition = (
                    f"100:0:{STRENGTHEN_TEST_COST}"
                    if equip and level_limit > current_level
                    else None
                )
                session.send_ext({
                    "_cmd": "8_2_5",
                    "gid": gid if condition is not None else 0,
                    "cd": condition,
                })

            elif cmd == "8_2_6":
                gid = int(payload.get("gi", 0))
                equip = next(
                    (
                        item
                        for item in get_player_backpack(session.char_id)
                        if int(item.get("gid", 0)) == gid
                           and int(item.get("t", 0)) == 0
                    ),
                    None,
                )
                equip_meta = (
                    GLOBAL_EQUIP_CONFIG.get(int(equip.get("mid", 0)))
                    if equip
                    else None
                )
                current_level = int(equip.get("cl", 0)) if equip else 0
                level_limit = int(equip_meta.get("strLvLimit", 0)) if equip_meta else 0
                player = load_player(session.char_id)
                current_coin = int(player.get("basic", {}).get("coin", 0))

                if not equip or not equip_meta or level_limit <= 0:
                    result = 200
                elif current_level >= level_limit:
                    result = 207
                elif current_coin < STRENGTHEN_TEST_COST:
                    result = 101
                else:
                    player.setdefault("basic", {})["coin"] = current_coin - STRENGTHEN_TEST_COST
                    save_player(player, char_id=session.char_id)
                    current_coin -= STRENGTHEN_TEST_COST

                    if random.random() * 100 < STRENGTHEN_TEST_SUCCESS_RATE:
                        result = 201
                        updated_equip = set_equipment_enhance_level(
                            session.char_id,
                            gid,
                            current_level + 1,
                        )
                        if updated_equip is None:
                            result = 200
                        else:
                            session.send_ext({
                                "_cmd": "8_0",
                                "up": [{
                                    "gid": gid,
                                    "mid": int(updated_equip["mid"]),
                                    "cl": int(updated_equip["cl"]),
                                }],
                            })
                    else:
                        result = 202

                    session.send_ext({
                        "_cmd": "8_1",
                        "bs": current_coin,
                        "ps": 100000,
                        "es": 50,
                        "hs": 1200,
                    })

                session.send_ext({"_cmd": "8_2_6", "res": result, "st": 0})

            elif cmd == "8_2_26":
                session.send_ext({
                    "_cmd": "8_2_26",
                    "p": STRENGTHEN_TEST_SUCCESS_RATE,
                })

            elif cmd == "21_10":
                ids = str(payload.get("ids", ""))
                remain_list = [f"{i}#999" for i in ids.split(",") if i]
                session.send_ext({"_cmd": "21_10", "r": 0, "res": remain_list})

            elif cmd == "21_11":
                ids = str(payload.get("ids", ""))
                remain_list = [f"{i}#99" for i in ids.split(",") if i]
                session.send_ext({"_cmd": "21_11", "r": 0, "res": remain_list})

            elif cmd == "21_12":
                session.send_ext({"_cmd": "21_12", "r": 0, "di": []})

            elif cmd == "21_13":
                ids = str(payload.get("ids", ""))
                id_items = [i for i in ids.split(",") if i]
                session.send_ext({
                    "_cmd": "21_13",
                    "r": [f"{i}#0" for i in id_items],
                    "dr": [f"{i}#0" for i in id_items],
                })

            elif cmd == "21_4":
                ab_ex = int(payload.get("ab", 100))
                new_db = add_dragon_coin(session.char_id, ab_ex)
                session.send_ext({"_cmd": "21_4", "r": 0})
                session.send_ext({"_cmd": "21_7", "ab": 9999900, "lb": new_db})

            elif cmd == "8_5":
                session.send_ext({"_cmd": "8_5", "rsc": 99999999})

            elif cmd == "8_0_0":
                logger.info("【背包系统】加载玩家存档动态背包数据")
                player_items = get_player_backpack(session.char_id)
                session.send_ext({
                    "_cmd": "8_0_0",
                    "ov": True,
                    "bag": player_items,
                })

            elif cmd == "8_0_1":
                gid = int(payload.get("gid", 1))
                dress_res = dress_equipment(session.char_id, gid)
                if dress_res:
                    target_item, old_item = dress_res
                    session.send_props_update()
                    props = calc_player_properties(session.char_id)
                    session.send_ext({
                        "_cmd": "2421",
                        "bi": getattr(session, "current_room_id", 2403),
                        "cl": [{
                            "ci": float(session.char_id),
                            "chp": props["chp"],
                            "mhp": props["mhp"],
                            "dnis": True,
                            "vr": float(int(time.time() * 1000)),
                        }]
                    })
                    p_cur = load_player(session.char_id)
                    eq_list = p_cur.get("bag", {}).get("equip", [])
                    equip_str = ",".join(f"{eq}:0:0" for eq in eq_list)
                    uv_pkt = build_downlink_user_vars_update(
                        room_id=getattr(session, "current_room_id", 2403),
                        user_id=session.char_id,
                        vars_list=[("equip", "s", equip_str)]
                    )
                    session.send_raw(uv_pkt)
                    up_items = []
                    if old_item:
                        up_items.append({"gid": old_item["gid"], "t": 0, "pl": 100})
                    up_items.append({"gid": target_item["gid"], "t": 0, "pl": target_item.get("pl", 10)})
                    session.send_ext({"_cmd": "8_0", "up": up_items})
                    pgid = old_item.get("gid", 0) if old_item else 0
                    session.send_ext({
                        "_cmd": "8_0_1",
                        "res": 1,
                        "gid": gid,
                        "cgid": gid,
                        "pgid": pgid,
                    })
                else:
                    dress_error = get_equipment_dress_error(session.char_id, gid)
                    session.send_ext({
                        "_cmd": "8_0_1",
                        "res": dress_error,
                        "gid": gid,
                        "cgid": 0,
                        "pgid": 0,
                    })

            elif cmd == "8_0_2":
                gid = int(payload.get("gid", 1))
                target_item = undress_equipment(session.char_id, gid)
                if target_item:
                    session.send_props_update()
                    props = calc_player_properties(session.char_id)
                    session.send_ext({
                        "_cmd": "2421",
                        "bi": getattr(session, "current_room_id", 2403),
                        "cl": [{
                            "ci": float(session.char_id),
                            "chp": props["chp"],
                            "mhp": props["mhp"],
                            "dnis": True,
                            "vr": float(int(time.time() * 1000)),
                        }]
                    })
                    p_cur = load_player(session.char_id)
                    eq_list = p_cur.get("bag", {}).get("equip", [])
                    equip_str = ",".join(f"{eq}:0:0" for eq in eq_list)
                    uv_pkt = build_downlink_user_vars_update(
                        room_id=getattr(session, "current_room_id", 2403),
                        user_id=session.char_id,
                        vars_list=[("equip", "s", equip_str)]
                    )
                    session.send_raw(uv_pkt)
                    session.send_ext({"_cmd": "8_0", "up": [{"gid": gid, "t": 0, "pl": 100}]})
                    session.send_ext({"_cmd": "8_0_2", "res": 1, "gid": gid})
                else:
                    session.send_ext({"_cmd": "8_0_2", "res": 0, "gid": gid})

            elif cmd == "8_0_3":
                gid = int(payload.get("gid", 1))
                count = int(payload.get("n", 1))
                bag_update = None
                new_mount = None
                backpack = get_player_backpack(session.char_id)
                requested_item = next(
                    (item for item in backpack if int(item.get("gid", 0)) == gid),
                    None,
                )
                if requested_item and int(requested_item.get("t", 0)) == 7:
                    was_first_mount = not get_mounts(session.char_id)
                    bag_update, new_mount = use_ride_consumable(session.char_id, gid)
                    target_item = bag_update
                else:
                    target_item = use_consumable(session.char_id, gid, count)
                if target_item is None:
                    logger.warning(
                        "拒绝使用不存在或非官方消耗品: char=%s gid=%s",
                        session.char_id,
                        gid,
                    )
                    session.send_ext({"_cmd": "8_0_3", "res": 0, "pa": ""})
                    return
                session.send_ext({"_cmd": "8_0_3", "res": 1, "pa": ""})
                session.send_ext({"_cmd": "8_0", "up": [target_item]})
                if new_mount:
                    session.send_ext({
                        "_cmd": "26_6",
                        "rid": new_mount["rid"],
                        "pid": new_mount["pid"],
                        "num": 1,
                        "end": new_mount["end"],
                    })
                    if was_first_mount:
                        session.send_ext({"_cmd": "26_7", "rid": new_mount["rid"]})
                session.send_props_update()

            elif cmd == "8_1":
                p_cur = load_player(session.char_id)
                coin = p_cur.get("basic", {}).get("coin", 100000000)
                session.send_ext({
                    "_cmd": "8_1",
                    "bs": coin,
                    "ps": 100000,
                    "hs": 1200,
                    "es": 50
                })

            elif cmd == "8_3":
                player = load_player(session.char_id)
                scores = player.get("temp_scores", {})
                score_parts = [
                    f"{int(score_id)}:{max(0, int(amount or 0))}"
                    for score_id, amount in sorted(scores.items(), key=lambda item: int(item[0]))
                    if str(score_id).isdigit()
                ]
                if not any(part.startswith("4:") for part in score_parts):
                    score_parts.append("4:0")
                session.send_ext({"_cmd": "8_3", "ts": "_".join(score_parts)})

            elif cmd == "8_1_1":
                logger.info("【角色卡】动态下发 8_1_1 个人面板属性与 VIP 数据")
                props = calc_player_properties(session.char_id)
                basic = load_player(session.char_id).get("basic", {})
                max_sp = max(0, int(basic.get("msp", 100)))
                cur_sp = max(0, min(int(basic.get("csp", max_sp)), max_sp))
                vip_info = get_vip_info(session.char_id)
                v_level = min(10, max(1, int(vip_info.get("vip_level", 7)))) if vip_info.get("is_vip") else 0

                session.send_ext({
                    "_cmd": "8_1_1",
                    "ci": float(session.char_id),
                    "ui": int(session.char_id),
                    "n": session.user_name,
                    "dn": session.user_name,
                    "r": 1,
                    "j": props["job_name"],
                    "originaljob": props["originaljob"],
                    "jlv": 0,
                    "lv": props["lv"],
                    "plv": props["lv"],
                    "chp": props["chp"],
                    "mhp": props["mhp"],
                    "cmp": props["cmp"],
                    "mmp": props["mmp"],
                    "csp": cur_sp,
                    "msp": max_sp,
                    "cdp": 0,
                    "mdp": 100,
                    "str": props["str"],
                    "itl": props["itl"],
                    "con": props["con"],
                    "stm": props["stm"],
                    "vit": props["vit"],
                    "agi": props["agi"],
                    "att": props["att"],
                    "def": props["def"],
                    "cri": props["cri"],
                    "mnA": props.get("mnA"),
                    "mxA": props.get("mxA"),
                    "exp": props["exp"],
                    "cle": props["cle"],
                    "nx": props["nx"],
                    "rhp": 6,
                    "ti": 0,
                    "rg": 0,
                    "rpt": 0,
                    "tidd": "",
                    "dned": -1,
                    "aai": True,
                    "dnis": True,
                    "pkm": 0,
                    "ati": "",
                    "pti": 0,
                    "d": -1,
                    "x": getattr(session, "x", 60),
                    "y": getattr(session, "y", 40),
                    "bi": getattr(session, "current_room_id", 2403),
                    "bot": 1,
                    "cs": {"al": True, "fd": False},
                    "trans": {"it": False},
                    "hit": 98.0036,
                    "eva": 12.0036,
                    "ba_lv": props["ba_lv"],
                    "ba_t": props.get("ba_t", props.get("ba_lv", 400)),
                    "ba_eqp": props.get("ba_eqp", 1),
                    "ba_sk": props.get("ba_sk", 0),
                    "ba_pt": props.get("ba_pt", 0),
                    "ba_clo": props.get("ba_clo", 0),
                    "ba_pg": props.get("ba_pg", 0),
                    "ba_cg": props.get("ba_cg", 0),
                    "ba_dp": props.get("ba_dp", 0),
                    "ba_fp": props.get("ba_fp", 0),
                    "ba_vp": props.get("ba_vp", 0),
                    "charData": {"rt": "2026-10-10 00:00:00"},
                })

                session.send_ext({
                    "_cmd": "4_0",
                    "mt": 8 if vip_info.get("is_vip") else 0,
                    "mlv": v_level,
                    "ms": 99999,
                    "an": vip_info.get("is_annual", True),
                    "muo": True,
                    "nls": 0,
                    "med": "2035-12-31",
                    "spm": vip_info.get("is_super_vip", True),
                    "rsmd": vip_info.get("super_vip_days", 3650),
                    "psn": 60,
                    "prn": 60,
                    "pen": 60,
                    "pcn": 60,
                    "pbn": 500,
                    "lgb": "1:400011:1#1:100014:1#2:400021:1#2:100022:1#3:400031:1#3:100033:1#4:400041:1#4:100042:1#5:400051:1#5:100053:1#6:400061:1#6:100062:1#7:400071:1#7:100073:1",
                    "gb": "",
                    "per": 0.0,
                    "mev": False,
                })

            elif cmd == "65_20":
                session.send_ext({"_cmd": "65_20", "ci": float(session.char_id), "army": ""})
            elif cmd == "8_2":
                session.send_ext({"_cmd": "8_2", "ws": 0})
            elif cmd == "29_2":
                session.send_ext({"_cmd": "29_2", "l": ""})

            elif cmd == "1501":
                sk_info = get_skills(session.char_id)
                sk_list = sk_info.get("list", [])
                sp_val = sk_info.get("sp", 100)
                p_cur = load_player(session.char_id)
                b = p_cur.get("basic", {})
                session.send_ext({
                    "_cmd": "2501",
                    "race": b.get("race", 1),
                    "job": b.get("job", 1),
                    "login": True,
                    "sp": sp_val,
                    "sl": {"sk": sk_list}
                })

            elif cmd == "1502":
                sk_id = int(payload.get("sk", 1000))
                up_val = int(payload.get("up", 1))
                ok, new_lv, new_sp = level_up_skill(session.char_id, sk_id, up_val)
                if ok:
                    session.send_ext({"_cmd": "2502", "id": sk_id, "lv": up_val})
                    session.send_ext({"_cmd": "2504", "sp": new_sp})
                    session.send_props_update()
                else:
                    session.send_ext({"_cmd": "2502", "msg": "技能等级不足、未解锁或已达上限"})

            elif cmd == "1503":
                sk_id = int(payload.get("sk", 1000))
                target = learn_skill(session.char_id, sk_id)
                if target:
                    session.send_ext({
                        "_cmd": "2503",
                        "list": [{
                            "id": sk_id,
                            "au": True,
                            "up": f"{int(target.get('ck0', 1))}#{int(target.get('ck1', 1))}#{int(target.get('ck2', 0))}#{int(target.get('ck3', 0))}",
                        }],
                    })
                else:
                    session.send_ext({"_cmd": "2503", "msg": "技能尚未达到学习等级"})

            elif cmd == "26_3":
                mounts = get_mounts(session.char_id)
                player = load_player(session.char_id)
                default_rid = int(player.get("default_rider_id", -1) or -1)
                guarding_rid = int(player.get("guarding_rider_id", -1) or -1)
                owned_rids = {int(mount["rid"]) for mount in mounts}
                if default_rid not in owned_rids:
                    default_rid = -1
                if guarding_rid not in owned_rids:
                    guarding_rid = -1
                logger.info(
                    "坐骑列表响应: char=%d count=%d default=%d guard=%d mounts=%s",
                    session.char_id, len(mounts), default_rid, guarding_rid,
                    [(mount["rid"], mount["pid"], mount["end"]) for mount in mounts],
                )
                session.send_ext({
                    "_cmd": "26_3",
                    "fi": default_rid,
                    "grid": guarding_rid,
                    "rps": mounts,
                })

            elif cmd == "26_4":
                session.send_ext({"_cmd": "26_4", "lc": "0#0#0"})

            elif cmd == "26_8":
                # Task513 expects rid=-1 when there is no active mount.
                riding_rid = int(load_player(session.char_id).get("riding_rid", 0) or 0)
                session.send_ext({"_cmd": "26_8", "rid": riding_rid if riding_rid > 0 else -1})

            elif cmd == "26_1":
                rid = int(payload.get("rid", payload.get("id", 0)))
                requested_mount = next(
                    (mount for mount in get_mounts(session.char_id) if int(mount["rid"]) == rid),
                    None,
                )
                rider_pid = set_player_riding(session.char_id, rid)
                if rider_pid is None:
                    fail_code = (
                        "5"
                        if requested_mount and int(requested_mount.get("end", 0)) <= int(time.time() * 1000)
                        else "3"
                    )
                    session.send_ext({"_cmd": "26_1", "r": fail_code, "rid": rid})
                else:
                    session.send_ext({"_cmd": "26_1", "r": "1", "rid": rid})
                    uv_pkt = build_downlink_user_vars_update(
                        room_id=getattr(session, "current_room_id", 2403),
                        user_id=session.char_id,
                        vars_list=[("ride", "n", str(rider_pid))]
                    )
                    session.send_raw(uv_pkt)

            elif cmd == "26_2":
                set_player_unriding(session.char_id)
                session.send_ext({"_cmd": "26_2", "r": "1"})
                uv_pkt = build_downlink_user_vars_update(
                    room_id=getattr(session, "current_room_id", 2403),
                    user_id=session.char_id,
                    vars_list=[("ride", "n", "0")]
                )
                session.send_raw(uv_pkt)
                session.send_props_update()

            elif cmd == "26_6":
                rid = int(payload.get("rid", 0))
                was_riding = int(load_player(session.char_id).get("riding_rid", 0) or 0) == rid
                removed = discard_mount(session.char_id, rid)
                session.send_ext({
                    "_cmd": "26_6",
                    "rid": rid,
                    "pid": int(removed.get("pid", -1)) if removed else -1,
                    "num": 0,
                    "end": 0,
                })
                if removed and was_riding:
                    uv_pkt = build_downlink_user_vars_update(
                        room_id=getattr(session, "current_room_id", 2403),
                        user_id=session.char_id,
                        vars_list=[("ride", "n", "0")]
                    )
                    session.send_raw(uv_pkt)

            elif cmd == "26_7":
                rid = int(payload.get("rid", -1))
                if set_default_rider(session.char_id, rid):
                    session.send_ext({"_cmd": "26_7", "rid": rid})
                else:
                    session.send_ext({"_cmd": "26_7", "rid": -1})

            elif cmd == "26_9":
                rid = int(payload.get("rid", -1))
                if set_guarding_rider(session.char_id, rid):
                    session.send_ext({"_cmd": "26_9", "r": "1", "grid": rid})
                else:
                    session.send_ext({"_cmd": "26_9", "r": "3", "grid": -1})

            elif cmd == "26_10":
                owner_id = int(payload.get("cid", session.char_id))
                owner = load_player(owner_id)
                guarding_rid = int(owner.get("guarding_rider_id", -1) or -1)
                session.send_ext({"_cmd": "26_10", "cid": owner_id, "grid": guarding_rid})

            elif cmd == "12_3_1":
                p_cur = load_player(session.char_id)
                owned_clothes = get_owned_clothes(session.char_id)
                wearing_clothes = set(get_wearing_clothes(session.char_id))
                res_parts = [
                    f"{cloth_id}_{1 if cloth_id in wearing_clothes else 0}_0__1791561600000_{get_cloth_expiry(session.char_id, cloth_id)}__"
                    for cloth_id in owned_clothes
                ]
                session.send_ext({
                    "_cmd": "12_3_1",
                    "cid": float(session.char_id),
                    "res": ",".join(res_parts),
                    "cv": int(p_cur.get("bag", {}).get("cv", 0)),
                })

            elif cmd == "12_3_2":
                session.send_ext({
                    "_cmd": "12_3_2",
                    "cid": float(session.char_id),
                    "cv": 0,
                    "res": ",".join(str(c) for c in get_wearing_clothes(session.char_id)),
                })

            elif cmd == "12_3_3":
                vpack = str(payload.get("vpack", ""))
                changed_cloth_ids = update_clothes(session.char_id, vpack)
                vpack = ",".join(str(c) for c in changed_cloth_ids)
                session.send_ext({
                    "_cmd": "12_3_3",
                    "cid": session.char_id,
                    "res": True,
                    "vpack": vpack,
                })
                avatar_str = ",".join(
                    str(c) for c in get_wearing_clothes(session.char_id)
                )
                uv_pkt = build_downlink_user_vars_update(
                    room_id=getattr(session, "current_room_id", 2403),
                    user_id=session.char_id,
                    vars_list=[("avatar", "s", avatar_str)]
                )
                session.send_raw(uv_pkt)

            elif cmd == "78_0":
                session.send_ext({"_cmd": "78_0", "st": 0, "ty": 0})

            # 珍宝天箱系统
            elif cmd == "25_1157_0":
                session.send_ext({
                    "_cmd": "25_1157_0",
                    "i": 1157,
                    "r": 1,
                    "pcd": 0, "ecd": 0, "plc": 0, "elc": 0,
                })

            elif cmd == "25_1157_1":
                lottery_type = int(payload.get("t", 0))
                p_cur = load_player(session.char_id)
                job_id = int(p_cur.get("basic", {}).get("job", 1))
                if lottery_type == 0:
                    pet_cand = [100494, 100491, 100502, 100492, 100503, 100509]
                    sel_pti = random.choice(pet_cand)
                    new_pet = add_pet(session.char_id, pti=sel_pti, lv=100, ecs=5, allow_duplicate=True)
                    fmt_pet = format_pet_for_client(new_pet, session.char_id)
                    all_pets = get_pets(session.char_id)
                    s_str = "#".join(str(p["pi"]) for p in all_pets)

                    follow_id = int(load_player(session.char_id).get("follow_pet_id", 0))
                    session.send_ext({"_cmd": "27_3", "f": follow_id, "s": s_str, "fv": fmt_pet})
                    session.send_ext({"_cmd": "32_2", "tp": 0, "did": 711})
                    session.send_props_update()
                    props = calc_player_properties(session.char_id)
                    session.send_ext({
                        "_cmd": "2421",
                        "bi": getattr(session, "current_room_id", 2403),
                        "cl": [{
                            "ci": float(session.char_id),
                            "chp": props["chp"],
                            "mhp": props["mhp"],
                            "dnis": True,
                            "vr": float(int(time.time() * 1000)),
                        }]
                    })
                    session.send_ext({"_cmd": "25_1157_1", "i": 1157, "r": 1, "e": f"135:{sel_pti}:1", "q": 5})
                else:
                    if job_id in (1, 51):
                        equip_cand = [112031, 113027, 112032, 113028, 20001, 20011, 20021, 20031, 20041, 20051, 20005,
                                      20015, 20025, 20035, 20045, 20055]
                    elif job_id in (2, 52):
                        equip_cand = [120029, 121029, 20005, 20015, 20025, 20035, 20045, 20055]
                    elif job_id in (3, 53):
                        equip_cand = [130029, 131029, 20005, 20015, 20025, 20035, 20045, 20055]
                    else:
                        equip_cand = [140029, 141029, 20005, 20015, 20025, 20035, 20045, 20055]
                    equip_cand = _treasure_equipment_candidates(job_id, equip_cand)
                    sel_eq = random.choice(equip_cand)
                    new_it = add_bag_item(session.char_id, mid=sel_eq, count=1, t=0)
                    session.send_ext({"_cmd": "8_0", "up": [new_it]})
                    session.send_ext({"_cmd": "25_1157_1", "i": 1157, "r": 1, "e": f"0:{sel_eq}:1", "q": 5})

            elif cmd == "25_1157_2":
                lottery_type = int(payload.get("t", 0))
                p_cur = load_player(session.char_id)
                job_id = int(p_cur.get("basic", {}).get("job", 1))
                new_db = add_dragon_coin(session.char_id, -30)
                session.send_ext({"_cmd": "21_7", "t": 1, "v": -30, "cur": new_db, "ab": 9999900, "lb": new_db})
                if lottery_type == 0:
                    pet_cand = [100494, 100491, 100502, 100496, 100497, 100498, 100499, 100500]
                    sel_pti = random.choice(pet_cand)
                    new_pet = add_pet(session.char_id, pti=sel_pti, lv=100, ecs=5, allow_duplicate=True)
                    fmt_pet = format_pet_for_client(new_pet, session.char_id)
                    all_pets = get_pets(session.char_id)
                    s_str = "#".join(str(p["pi"]) for p in all_pets)

                    follow_id = int(load_player(session.char_id).get("follow_pet_id", 0))
                    session.send_ext({"_cmd": "27_3", "f": follow_id, "s": s_str, "fv": fmt_pet})
                    session.send_ext({"_cmd": "32_2", "tp": 0, "did": 711})
                    session.send_props_update()
                    props = calc_player_properties(session.char_id)
                    session.send_ext({
                        "_cmd": "2421",
                        "bi": getattr(session, "current_room_id", 2403),
                        "cl": [{
                            "ci": float(session.char_id),
                            "chp": props["chp"],
                            "mhp": props["mhp"],
                            "dnis": True,
                            "vr": float(int(time.time() * 1000)),
                        }]
                    })
                    session.send_ext({"_cmd": "25_1157_2", "i": 1157, "r": 1, "e": f"135:{sel_pti}:1", "q": 5})
                else:
                    if job_id in (1, 51):
                        equip_cand = [112031, 113027, 112032, 113028, 20001, 20011, 20021, 20031, 20041, 20051, 20005,
                                      20015, 20025, 20035, 20045, 20055]
                    elif job_id in (2, 52):
                        equip_cand = [120029, 121029, 20005, 20015, 20025, 20035, 20045, 20055]
                    elif job_id in (3, 53):
                        equip_cand = [130029, 131029, 20005, 20015, 20025, 20035, 20045, 20055]
                    else:
                        equip_cand = [140029, 141029, 20005, 20015, 20025, 20035, 20045, 20055]
                    equip_cand = _treasure_equipment_candidates(job_id, equip_cand)
                    sel_eq = random.choice(equip_cand)
                    new_it = add_bag_item(session.char_id, mid=sel_eq, count=1, t=0)
                    session.send_ext({"_cmd": "8_0", "up": [new_it]})
                    session.send_ext({"_cmd": "25_1157_2", "i": 1157, "r": 1, "e": f"0:{sel_eq}:1", "q": 5})

            elif cmd == "25_1157_3":
                lottery_type = int(payload.get("t", 0))
                p_cur = load_player(session.char_id)
                job_id = int(p_cur.get("basic", {}).get("job", 1))
                new_db = add_dragon_coin(session.char_id, -298)
                session.send_ext({"_cmd": "21_7", "t": 1, "v": -298, "cur": new_db, "ab": 9999900, "lb": new_db})
                if lottery_type == 0:
                    pet_cand = [100494, 100491, 100502, 100496, 100497, 100498, 100499, 100500]
                    items = []
                    for _ in range(10):
                        sel = random.choice(pet_cand)
                        new_pet = add_pet(session.char_id, pti=sel, lv=100, ecs=5, allow_duplicate=True)
                        fmt_pet = format_pet_for_client(new_pet, session.char_id)
                        all_pets = get_pets(session.char_id)
                        s_str = "#".join(str(p["pi"]) for p in all_pets)
                        items.append(f"135:{sel}:1")
                        follow_id = int(load_player(session.char_id).get("follow_pet_id", 0))
                        session.send_ext({"_cmd": "27_3", "f": follow_id, "s": s_str, "fv": fmt_pet})
                    session.send_ext({"_cmd": "32_2", "tp": 0, "did": 711})
                    session.send_props_update()
                    props = calc_player_properties(session.char_id)
                    session.send_ext({
                        "_cmd": "2421",
                        "bi": getattr(session, "current_room_id", 2403),
                        "cl": [{
                            "ci": float(session.char_id),
                            "chp": props["chp"],
                            "mhp": props["mhp"],
                            "dnis": True,
                            "vr": float(int(time.time() * 1000)),
                        }]
                    })
                    session.send_ext(
                        {"_cmd": "25_1157_3", "i": 1157, "r": 1, "e": "#".join(items), "q": "5_5_5_5_5_5_5_5_5_5"})
                else:
                    if job_id in (1, 51):
                        equip_cand = [112031, 113027, 112032, 113028, 20001, 20011, 20021, 20031, 20041, 20051, 20005,
                                      20015, 20025, 20035, 20045, 20055]
                    elif job_id in (2, 52):
                        equip_cand = [120029, 121029, 20005, 20015, 20025, 20035, 20045, 20055]
                    elif job_id in (3, 53):
                        equip_cand = [130029, 131029, 20005, 20015, 20025, 20035, 20045, 20055]
                    else:
                        equip_cand = [140029, 141029, 20005, 20015, 20025, 20035, 20045, 20055]
                    equip_cand = _treasure_equipment_candidates(job_id, equip_cand)
                    items = []
                    up_list = []
                    for _ in range(10):
                        sel = random.choice(equip_cand)
                        new_it = add_bag_item(session.char_id, mid=sel, count=1, t=0)
                        items.append(f"0:{sel}:1")
                        up_list.append(new_it)
                    session.send_ext({"_cmd": "8_0", "up": up_list})
                    session.send_ext(
                        {"_cmd": "25_1157_3", "i": 1157, "r": 1, "e": "#".join(items), "q": "5_5_5_5_5_5_5_5_5_5"})

            # 战宠面板 (P键)
            elif cmd in ("27_1", "27_6"):
                session.pet_inventory_bootstrapped = True
                formatted_pets, follow_id, seq_str = _pet_inventory_for_client(session.char_id)
                max_capacity = max(100, len(formatted_pets) + 20)
                logger.info(
                    "战宠列表响应: char=%d cmd=%s follow=%d sequence=%s pets=%s sc=%d",
                    session.char_id,
                    cmd,
                    follow_id,
                    seq_str,
                    [(pet["pi"], pet["pti"]) for pet in formatted_pets],
                    max_capacity,
                )
                session.send_ext({
                    "_cmd": cmd,
                    "pl": formatted_pets,
                    "s": seq_str,
                    "f": follow_id,
                    "sc": max_capacity,
                })

            elif cmd == "27_4":
                raw_pid = payload.get("petId", payload.get("pi", payload.get("p")))
                try:
                    pid = int(raw_pid)
                except (TypeError, ValueError):
                    pid = 0
                pets = get_pets(session.char_id)
                pet_info = next((p for p in pets if int(p.get("pi", 0)) == pid), None)
                session.cmd_counters[cmd] += 1
                if session.cmd_counters[cmd] <= 5 or session.cmd_counters[cmd] % 50 == 0:
                    logger.info(
                        "战宠单体详情请求: char=%d count=%d payload=%s requested_pi=%d found=%s",
                        session.char_id,
                        session.cmd_counters[cmd],
                        payload,
                        pid,
                        bool(pet_info),
                    )
                if pet_info is None:
                    session.send_ext({"_cmd": "27_4", "msg": f"战宠不存在: pi={pid}"})
                    return
                session.send_ext({
                    "_cmd": "27_4",
                    "fv": format_pet_for_client(pet_info, session.char_id),
                })

            elif cmd == "27_7":
                player = load_player(session.char_id)
                pets = player.get("pets", [])
                pets_by_id = {
                    int(pet["pi"]): pet
                    for pet in pets
                    if isinstance(pet, dict) and int(pet.get("pi", 0)) > 0
                }
                ordered_ids = []
                for value in str(payload.get("s", "")).split("#"):
                    try:
                        pet_id = int(value)
                    except (TypeError, ValueError):
                        continue
                    if pet_id in pets_by_id and pet_id not in ordered_ids:
                        ordered_ids.append(pet_id)
                ordered_ids.extend(pet_id for pet_id in pets_by_id if pet_id not in ordered_ids)
                player["pets"] = [pets_by_id[pet_id] for pet_id in ordered_ids]
                save_player(player, char_id=session.char_id)
                formatted_pets = [
                    format_pet_for_client(pet, session.char_id)
                    for pet in player["pets"]
                ]
                sequence = "#".join(str(pet_id) for pet_id in ordered_ids)
                logger.info(
                    "战宠顺序响应: char=%d sequence=%s pets=%s",
                    session.char_id,
                    sequence,
                    [(pet["pi"], pet["pti"]) for pet in formatted_pets],
                )
                session.send_ext({
                    "_cmd": "27_7",
                    "pl": formatted_pets,
                    "s": sequence,
                })

            elif cmd == "27_5":
                pid = int(payload.get("pi", payload.get("p", payload.get("petId", 1))))
                logger.info("【战宠跟随】设置出战战宠: pi=%d", pid)
                curr_room = getattr(session, "current_room_id", 2403)

                if pid == 0:
                    set_follow_pet(session.char_id, pet_id=0)
                    session.send_ext({
                        "_cmd": "27_5",
                        "ci": float(session.char_id),
                        "bi": curr_room,
                        "f": 0,
                        "pet": {}
                    })
                    session.send_props_update()
                else:
                    set_follow_pet(session.char_id, pet_id=pid)
                    pets = get_pets(session.char_id)
                    pet_obj = next((p for p in pets if p.get("pi") == pid), None)
                    if not pet_obj and pets:
                        pet_obj = pets[0]
                        pid = pet_obj.get("pi", 1)
                        set_follow_pet(session.char_id, pet_id=pid)

                    formatted_pet = format_pet_for_client(pet_obj, session.char_id) if pet_obj else {}
                    if formatted_pet:
                        formatted_pet["bi"] = curr_room
                        formatted_pet["ci"] = float(-4120000 - pid)

                    session.send_ext({
                        "_cmd": "27_5",
                        "ci": float(session.char_id),
                        "bi": curr_room,
                        "f": pid,
                        "pet": formatted_pet
                    })
                    session.send_props_update()

            elif cmd == "27_151":
                # SummonPetDataService.onLoadMyPetList iterates params.pl
                # without a null guard. Returning the generic {s, r} ACK
                # makes the client dereference a missing list during login.
                player = load_player(session.char_id)
                summon_pets = player.get("summon_pets", [])
                if not isinstance(summon_pets, list):
                    summon_pets = []
                session.send_ext({"_cmd": "27_151", "pl": summon_pets})
                logger.info(
                    "召唤宠物列表响应: char=%d count=%d",
                    session.char_id,
                    len(summon_pets),
                )

            elif cmd in ("27_173", "27_174"):
                corps_str = build_player_corps_str(session.char_id)
                session.send_ext({
                    "_cmd": cmd,
                    "r": 0,
                    "s": 0,
                    "corps": corps_str
                })

            elif cmd == "27_74":
                et = payload.get("et")
                pet_id = int(payload.get("p", payload.get("n", 0)))
                # The client sends only et (slot index) and p (pet instance ID).
                # Treating an unrelated o=0 field as one-key mode overwrote all slots.
                is_all = bool(payload.get("is_all")) or et == -1
                old_guardians = dict(get_guardians(session.char_id))

                if is_all:
                    guardians = one_key_guard(session.char_id)
                else:
                    slot = int(et) + 1 if et is not None else int(payload.get("gp", 1))
                    if 1 <= slot <= 6:
                        guardians = set_guardian_pet(session.char_id, slot=slot, pet_id=pet_id)
                    else:
                        guardians = get_guardians(session.char_id)

                gp = calc_guard_power(session.char_id)
                logger.info("27_74守护槽更新: char=%d request=%s old=%s new=%s",
                            session.char_id, payload, old_guardians, guardians)
                session.send_ext({
                    "_cmd": "27_74",
                    "r": 0,
                    "s": 0,
                    "res": 0,
                    "gp": gp
                })
                # Push the authoritative slot + per-pet numeric snapshot with the
                # acknowledgement. This makes the panel recalculate immediately
                # from the pet now occupying each slot, even if its follow-up 27_72
                # request is delayed or coalesced by the client.
                session.send_ext(_build_guardian_info_payload(session.char_id))
                session.send_props_update()

            elif cmd == "27_75":
                try:
                    # PetValueClient sends zero-based array indexes.
                    slot_a = int(payload.get("et1", -1)) + 1
                    slot_b = int(payload.get("et2", -1)) + 1
                    old_guardians = dict(get_guardians(session.char_id))
                    guardians = swap_guardian_slots(session.char_id, slot_a, slot_b)
                    logger.info("27_75守护槽交换: char=%d request=%s old=%s new=%s",
                                session.char_id, payload, old_guardians, guardians)
                    session.send_ext({"_cmd": "27_75", "r": 0, "s": 0})
                    session.send_ext(_build_guardian_info_payload(session.char_id))
                    session.send_props_update()
                except (TypeError, ValueError) as exc:
                    session.send_ext({"_cmd": "27_75", "r": 1, "s": 0, "msg": str(exc)})

            elif cmd == "27_72":
                session.send_ext(_build_guardian_info_payload(session.char_id))

            elif cmd == "27_200":
                requested_ids = str(payload.get("petId", "") or payload.get("p", "") or "")
                formatted_pets, _, _ = _pet_inventory_for_client(session.char_id)
                pets_map = {int(p["pi"]): p for p in formatted_pets}

                # The login-time 27_1 can arrive before the fight-pet panel is
                # created. The panel then asks 27_200 for only pi=1; its batch
                # listener refreshes existing rows and cannot build the owned
                # list. Re-send the full list once on that panel-specific
                # starter-pet request, even if login already sent 27_1.
                panel_starter_refresh = (
                    requested_ids.strip() == "1"
                    and not session.pet_panel_inventory_refreshed
                )
                if panel_starter_refresh:
                    session.pet_panel_inventory_refreshed = True
                if not session.pet_inventory_bootstrapped or panel_starter_refresh:
                    session.pet_inventory_bootstrapped = True
                    follow_id = int(load_player(session.char_id).get("follow_pet_id", 0))
                    if follow_id not in pets_map:
                        follow_id = int(formatted_pets[0]["pi"]) if formatted_pets else 0
                    sequence = ([follow_id] + [
                        int(pet["pi"]) for pet in formatted_pets if int(pet["pi"]) != follow_id
                    ]) if follow_id else []
                    session.send_ext({
                        "_cmd": "27_1",
                        "pl": formatted_pets,
                        "s": "#".join(str(pid) for pid in sequence),
                        "f": follow_id,
                        "sc": max(100, len(formatted_pets) + 20),
                    })
                    logger.info(
                        "战宠列表补发: char=%d trigger=27_200 requested=%s reason=%s count=%d follow=%d",
                        session.char_id,
                        requested_ids,
                        "panel-starter-refresh" if panel_starter_refresh else "first-batch-bootstrap",
                        len(formatted_pets),
                        follow_id,
                    )

                returned_pets = []
                if requested_ids:
                    for token in re.split(r"[#,_]", requested_ids):
                        if token and token.isdigit():
                            pid = int(token)
                            if pid in pets_map:
                                returned_pets.append(pets_map[pid])

                if not returned_pets:
                    returned_pets = formatted_pets

                logger.info(
                    "战宠批量详情响应: char=%d requested=%s returned=%s",
                    session.char_id,
                    requested_ids,
                    [(pet["pi"], pet["pti"]) for pet in returned_pets],
                )
                session.send_ext({"_cmd": "27_200", "fv": returned_pets})

            elif cmd == "27_204":
                session.send_ext({
                    "_cmd": "27_204",
                    "r": 0,
                    "s": 0,
                    "pi": "",
                    "item": "",
                    "res": 0
                })

            elif cmd == "27_203":
                pid = int(payload.get("p", 1))
                ecs = int(payload.get("e", 5))
                updated_pet = wake_pet(session.char_id, pet_id=pid, target_ecs=ecs)
                if not updated_pet:
                    updated_pet = add_pet(session.char_id, pti=100494, lv=100, ecs=ecs)
                session.send_ext({
                    "_cmd": "27_203",
                    "ss": True,
                    "fv": format_pet_for_client(updated_pet, session.char_id),
                })
                session.send_props_update()

            elif cmd in ("27_201", "27_202"):
                pid = int(payload.get("p", 1))
                if cmd == "27_201":
                    updated_pet = level_up_pet(
                        session.char_id,
                        pet_id=pid,
                        exp_amount=max(0, int(payload.get("e", 0))),
                    )
                else:
                    updated_pet = level_up_pet(
                        session.char_id,
                        pet_id=pid,
                        target_lv=max(1, int(payload.get("l", 1))),
                    )
                if updated_pet:
                    session.send_ext({
                        "_cmd": cmd,
                        "ss": True,
                        "fv": format_pet_for_client(updated_pet, session.char_id),
                    })
                    session.send_ext({
                        "_cmd": "8_3",
                        "ts": f"4:{get_temp_score(session.char_id, 4)}",
                    })
                    session.send_props_update()
                else:
                    session.send_ext({"_cmd": cmd, "ss": False})

            # 全副本通用引擎
            elif cmd in ("15_0", "fb.joinFb"):
                fbid = int(payload.get("fbId", 10))
                fblv = int(payload.get("fblv", payload.get("lv", 2)))
                session.in_fb = True
                session.fb10_cleared = False
                dungeon = get_or_create_dungeon(session.char_id, fb_id=fbid, fb_lv=fblv)
                first_scene = dungeon.get_current_scene_meta()
                session.active_room = first_scene["rn"]
                session.send_ext(dungeon.get_join_packet())

            elif cmd == "15_3":
                crid = payload.get("crId", 201)
                logger.info(
                    "[FB调试] 收到15_3: char=%s crId=%s room=%s bi=%s",
                    session.char_id,
                    crid,
                    session.active_room,
                    session.current_room_id,
                )
                session.send_ext({"_cmd": "15_3", "r": 0, "s": 0, "crId": crid})
                if "shenyuanroad" in session.active_room:
                    real_bi = getattr(session, "current_room_id", 304448)
                    monster_packets = [{
                        "_cmd": "2302", "bot": 2, "bi": real_bi, "ci": -4123506.0,
                        "cti": 26937.0, "n": "小蝙蝠", "lv": 1, "chp": 57030, "mhp": 57030,
                        "x": 120.0, "y": 54.0, "d": 0.0, "p": "", "rk": 1, "mty": 2, "ui": -1,
                        "cau": False, "bt": False, "cs": {"al": True, "fd": False}
                    }, {
                        "_cmd": "2302", "bot": 2, "bi": real_bi, "ci": -4123507.0,
                        "cti": 26937.0, "n": "小蝙蝠", "lv": 1, "chp": 57030, "mhp": 57030,
                        "x": 130.0, "y": 45.0, "d": 0.0, "p": "", "rk": 1, "mty": 2, "ui": -1,
                        "cau": False, "bt": False, "cs": {"al": True, "fd": False}
                    }]
                else:
                    dungeon = get_dungeon(session.char_id)
                    if not dungeon:
                        dungeon = get_or_create_dungeon(session.char_id, fb_id=10, fb_lv=2)
                    monster_packets = dungeon.spawn_monsters(crid)
                    logger.info(
                        "[FB调试] 生成怪物: char=%s fbId=%s fbLv=%s scene=%s room=%s "
                        "expectedBi=%s count=%s entities=%s",
                        session.char_id,
                        dungeon.fb_id,
                        dungeon.fb_lv,
                        dungeon.get_current_scene_meta(),
                        session.active_room,
                        dungeon.get_current_scene_meta().get("id"),
                        len(monster_packets),
                        [
                            {
                                "cmd": pkt.get("_cmd"),
                                "ci": pkt.get("ci"),
                                "cti": pkt.get("cti"),
                                "name": pkt.get("n"),
                                "bi": pkt.get("bi"),
                            }
                            for pkt in monster_packets
                        ],
                    )
                for m_pkt in monster_packets:
                    session.send_ext(m_pkt)

            elif cmd == "15_13":
                target_cis = payload.get("t") or []
                if not isinstance(target_cis, list):
                    target_cis = []
                resp_l = []
                for tci in target_cis:
                    try:
                        ci_val = float(tci)
                        resp_l.append({
                            "ti": ci_val,
                            "sl": [
                                {"mn": 80, "smn": 0, "sk": 15006, "mx": 100, "smn": 0},
                                {"mn": 80, "smn": 0, "sk": 15007, "mx": 100, "smn": 0},
                                {"mn": 98, "smn": 0, "sk": 15008, "mx": 100, "smn": 0},
                                {"mn": 134, "smn": 0, "sk": 15009, "mx": 150, "smn": 0},
                                {"mn": 240, "smn": 0, "sk": 15015, "mx": 260, "smn": 0},
                            ]
                        })
                    except Exception:
                        pass
                session.send_ext({"_cmd": "15_13", "s": True, "l": resp_l})

            elif cmd == "15_12":
                skill_id = payload.get("s", 15006)
                dmg_str = str(payload.get("d", ""))
                dungeon = get_dungeon(session.char_id)
                if not dungeon:
                    dungeon = get_or_create_dungeon(session.char_id, fb_id=10, fb_lv=2)
                scene_meta = dungeon.get_current_scene_meta()
                scene_id = scene_meta["id"]

                hit_updates, killed_cis, scene_cleared, fb_cleared = dungeon.handle_damage(dmg_str, skill_id)

                tlc_list = [{"ci": u["ci"], "p": "1#H#-17#0"} for u in hit_updates]
                session.send_ext({
                    '_cmd': '2411',
                    'bi': scene_id,
                    'sk': int(skill_id),
                    'req': float(session.char_id),
                    'tlc': tlc_list,
                    'tli': [],
                    'tls': []
                })

                if hit_updates:
                    cl_items = [
                        {
                            "ci": u["ci"],
                            "bi": scene_id,
                            "chp": u["chp"],
                            "mhp": u["mhp"],
                            "lv": u.get("lv", 10),
                            "n": u.get("n", ""),
                            "pkm": 0,
                            "ati": "",
                            "originaljob": 0,
                            "dnis": True,
                            "vr": float(int(time.time() * 1000)),
                        }
                        for u in hit_updates
                    ]
                    session.send_ext({
                        '_cmd': '2421',
                        'bi': scene_id,
                        'cl': cl_items,
                    })

                for kci in killed_cis:
                    session.send_ext({'ui': -1, 'bot': 2, 'ci': kci, '_cmd': '2304', 'bi': scene_id})
                    session.send_ext({'q': 5, 'c': float(session.char_id), 't': 100, '_cmd': '2316', 'id': 1, 'eq': 0})

                if fb_cleared and not session.fb10_cleared:
                    session.fb10_cleared = True
                    props = calc_player_properties(session.char_id)
                    fin_pkt = dungeon.build_finish_result(props["lv"])
                    session.grant_dungeon_reward(fin_pkt["exp_reward"], fin_pkt["coin_reward"])
                    update_position(session.char_id, last_room="momomaintown_1")
                    session.send_ext(fin_pkt)
                    p_after = load_player(session.char_id)
                    session.send_ext(
                        {"_cmd": "8_1", "bs": p_after.get("basic", {}).get("coin", 0), "ps": 100000, "hs": 1200,
                         "es": 50})
                    session.send_props_update()

            elif cmd == "15_11":
                x_val = str(payload.get("x", ""))
                session.send_ext({"_cmd": "15_11", "s": True})
                dungeon = get_dungeon(session.char_id)
                if dungeon:
                    scene_meta = dungeon.get_current_scene_meta()
                    scene_id = scene_meta["id"]
                    for part in x_val.split("#"):
                        if not part:
                            continue
                        sub_tokens = part.split("_")
                        if sub_tokens:
                            try:
                                m_ci = float(sub_tokens[0])
                                if m_ci in dungeon.monsters:
                                    dungeon.monsters[m_ci]["hp"] = 0
                                    session.send_ext({'ui': -1, 'bot': 2, 'ci': m_ci, '_cmd': '2304', 'bi': scene_id})
                                    session.send_ext(
                                        {'q': 5, 'c': float(session.char_id), 't': 100, '_cmd': '2316', 'id': 1,
                                         'eq': 0})
                            except Exception:
                                pass

                    all_dead = all(m["hp"] <= 0 for m in dungeon.monsters.values())
                    is_final = (dungeon.current_scene_idx == dungeon.total_scenes_count - 1)
                    if all_dead and is_final and not session.fb10_cleared:
                        session.fb10_cleared = True
                        props = calc_player_properties(session.char_id)
                        fin_pkt = dungeon.build_finish_result(props["lv"])
                        session.grant_dungeon_reward(fin_pkt["exp_reward"], fin_pkt["coin_reward"])
                        update_position(session.char_id, last_room="momomaintown_1")
                        session.send_ext(fin_pkt)
                        p_after = load_player(session.char_id)
                        session.send_ext(
                            {"_cmd": "8_1", "bs": p_after.get("basic", {}).get("coin", 0), "ps": 100000, "hs": 1200,
                             "es": 50})
                        session.send_props_update()

            elif cmd == "15_4":
                session.fb10_cleared = True
                dungeon = get_dungeon(session.char_id)
                props = calc_player_properties(session.char_id)
                if dungeon:
                    fin_pkt = dungeon.build_finish_result(props["lv"])
                    session.grant_dungeon_reward(fin_pkt["exp_reward"], fin_pkt["coin_reward"])
                else:
                    fin_pkt = {
                        "_cmd": "15_4",
                        "suc": True,
                        "ft": False,
                        "fun": 1,
                        "res": {
                            "pt": 2000, "ppe": 0, "ot": 4000, "double": 0, "ve": 0,
                            "te": 0, "total": 2730, "pe": 700, "grade": "SSS",
                            "time": 32, "pm": 200, "ge": 210, "dc": 0
                        },
                        "exp_reward": 2730,
                        "coin_reward": 200
                    }
                    session.grant_dungeon_reward(2730, 200)
                session.send_ext(fin_pkt)
                p_after = load_player(session.char_id)
                session.send_ext(
                    {"_cmd": "8_1", "bs": p_after.get("basic", {}).get("coin", 0), "ps": 100000, "hs": 1200, "es": 50})
                session.send_props_update()

            elif cmd == "15_6":
                dungeon = get_dungeon(session.char_id)
                reward_str = dungeon.generate_bonus_box_rewards() if dungeon else "0:20035:1#100:1:50000"
                recovery_items = [
                    add_bag_item(session.char_id, mid=1001, count=3, t=2)
                ]
                session.send_ext({"_cmd": "8_0", "up": recovery_items})
                session.send_ext({"_cmd": "15_6", "r": 0, "it": reward_str, "suc": True})

            elif cmd == "15_1":
                session.in_fb = False
                session.fb10_cleared = False
                remove_dungeon(session.char_id)
                update_position(session.char_id, last_room="momomaintown_1")
                session.send_ext({"_cmd": "15_1", "res": 1, "r": 0, "s": 0})


            elif cmd in ("15_5", "15_7", "15_18"):

                if cmd == "15_5":

                    # 响应副本难度解锁：必须兼容数组与字符串格式

                    fbid = int(payload.get("fbId", payload.get("id", 1)))

                    session.send_ext({

                        "_cmd": "15_5",

                        "r": 0,

                        "s": 0,

                        "res": 1,

                        "fbId": fbid,

                        "id": fbid,

                        "levels": [1, 2, 3, 4],

                        "lvs": [1, 2, 3, 4],

                        "openLvs": "1#2#3#4",

                        "lv": 2,

                        "maxLv": 4,

                    })


                elif cmd == "15_7":

                    session.send_ext({"_cmd": "15_7", "r": 0, "res": []})


                elif cmd == "15_18":

                    fbid = int(payload.get("fbId", payload.get("id", 1)))

                    lv = int(payload.get("level", payload.get("fblv", 2)))

                    session.send_ext({

                        "_cmd": "15_18",

                        "r": 0,

                        "s": 0,

                        "fbId": fbid,

                        "id": fbid,

                        "level": lv,

                        "fblv": lv,

                        "t": 10,  # 剩余进入次数 (匹配面板上的“今天还可以进入10次副本”)

                        "rt": 10,

                        "maxTimes": 10,

                        "max": 10,

                    })


            elif cmd == "15_20":

                ids_str = str(payload.get("ids", "") or payload.get("id", ""))

                res_list = []

                all_fids = [

                    10, 14, 16, 1, 31, 2, 32, 11, 15, 18, 3, 33, 9, 4,

                    25, 13, 5, 12, 6, 22, 17, 7, 23, 19, 8, 20, 21, 26,

                    27, 24, 28, 29, 30, 34, 35

                ]

                target_entries = []

                if ids_str:

                    for token in re.split(r"[,;_]", ids_str):

                        parts = token.split(":", 1)

                        if parts[0].isdigit():

                            fid = int(parts[0])

                            # The panel requests entries as "fbId:difficulty".
                            # Preserve that difficulty because the client indexes
                            # the response by "fbId_difficulty" on each tab.
                            level = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 2

                            target_entries.append((fid, level))

                if not target_entries:
                    target_entries = [(fid, 2) for fid in all_fids]

                for fid, level in target_entries:
                    res_list.append({

                        "id": fid,

                        "fbId": fid,

                        "level": level,

                        "fblv": level,

                        "rt": 10,  # 必须有 rt (剩余次数 _leaveTimes)

                        "t": 10,

                        "times": 10,

                        "maxTimes": 10,

                        # FbRecordIcon2015Base reads the record for the
                        # requested difficulty after locating this entry.

                        "record": {

                            "Lv1": "SSS",

                            "Lv2": "SSS",

                            "Lv3": "SSS",

                            "Lv4": "SSS",

                            "Lv5": "SSS"

                        },

                        "grade": "SSS",

                        "star": 3,

                        "open": True,

                        "isOpen": True,

                        "lock": False,

                        "isEvo": False,

                        "isEvo2": False,

                        "first": False,

                        "canSweep": True,

                    })

                session.send_ext({

                    "_cmd": "15_20",

                    "r": 0,

                    "s": 0,

                    "list": res_list

                })

            elif cmd == "15_19":
                cost = 10
                curr_coin = get_dragon_coin(session.char_id)
                if curr_coin >= cost:
                    set_dragon_coin(session.char_id, curr_coin - cost)
                    session.send_ext({"_cmd": "15_19", "r": 1})
                    session.send_props_update()
                else:
                    session.send_ext({"_cmd": "15_19", "r": 201})

            elif cmd in ("12_1", "12_2"):
                session.send_ext({"_cmd": cmd, "s": 0, "r": 0})

            elif cmd.startswith("XT_B_"):
                if cmd == "XT_B_10034" and isinstance(pkt.payload, (bytes, bytearray)):
                    raw_p = bytes(pkt.payload)
                    real_bi = getattr(session, "current_room_id", 304448)

                    if session.task_step == "513_1" and len(raw_p) >= 5 and raw_p[0] == 1:
                        grid_x, grid_y = struct.unpack(">HH", raw_p[1:5])
                        now_sec = time.time()
                        if 27 <= grid_x <= 40 and (now_sec - session.walk_task_started_time > 3.0):
                            session.task_step = "513_2"
                            session.last_step_push_time = now_sec
                            now_ms = float(int(now_sec * 1000))
                            session.send_ext({
                                "_cmd": "10-1", "ty": 0,
                                "t": {"taskId": 513, "st": 0, "ex": "", "ad": now_ms, "ti": "513_2", "tm": 0, "step": 2}
                            })
                            session.send_ext({"_cmd": "10-7", "s": True, "b": "115:100408:1"})

                    elif session.task_step == "513_2" and not session.monsters_killed and len(raw_p) >= 1 and raw_p[
                        0] in (11, 12, 13, 14, 15, 16):
                        session.fight_attack_count += 1
                        now_sec = time.time()
                        now_ms = float(int(now_sec * 1000))
                        session.send_ext({'_cmd': '2411', 'bi': real_bi, 'ci': float(session.char_id), 'sk': 32001})
                        if session.fight_attack_count in (1, 2):
                            rem_hp = 38000 if session.fight_attack_count == 1 else 15000
                            session.send_ext({
                                '_cmd': '2421', 'bi': real_bi,
                                'cl': [
                                    {'ci': -4123506.0, 'bi': real_bi, 'chp': rem_hp, 'pkm': 0, 'ati': '',
                                     'originaljob': 0, 'vr': now_ms},
                                    {'ci': -4123507.0, 'bi': real_bi, 'chp': rem_hp, 'pkm': 0, 'ati': '',
                                     'originaljob': 0, 'vr': now_ms},
                                ]
                            })
                        elif session.fight_attack_count >= 3:
                            session.last_step_push_time = now_sec
                            session.monsters_killed = True
                            session.send_ext({
                                '_cmd': '2421', 'bi': real_bi,
                                'cl': [
                                    {'ci': -4123506.0, 'bi': real_bi, 'chp': 0, 'pkm': 0, 'ati': '', 'originaljob': 0,
                                     'vr': now_ms},
                                    {'ci': -4123507.0, 'bi': real_bi, 'chp': 0, 'pkm': 0, 'ati': '', 'originaljob': 0,
                                     'vr': now_ms},
                                ]
                            })
                            session.send_ext({'ui': -1, 'bot': 2, 'ci': -4123506.0, '_cmd': '2304', 'bi': real_bi})
                            session.send_ext({'ui': -1, 'bot': 2, 'ci': -4123507.0, '_cmd': '2304', 'bi': real_bi})
                            session.task_step = "513_3"
                            update_task(session.char_id, 513, step=3, st=0, ti="513_3")
                            session.send_ext({
                                "_cmd": "10-1", "ty": 0,
                                "t": {"taskId": 513, "st": 0, "ex": "", "ad": now_ms, "ti": "513_3", "tm": 0, "step": 3}
                            })
                            session.send_ext({"_cmd": "10-7", "s": True, "b": "115:100408:1"})

            elif cmd in CAPTURED_TRIGGERS and cmd not in ("1-2", "1-14", "19-0", "65_2", "8_1_1", "65_20", "15_0",
                                                          "15_1", "15_3", "15_4", "15_6", "15_11", "15_12", "15_13",
                                                          "15_19", "15_20", "27_1", "27_4", "27_5", "27_6", "27_200",
                                                          "10-20", "initUserData"):
                batches = CAPTURED_TRIGGERS[cmd]
                idx = session.cmd_counters[cmd]
                batch = batches[min(idx, len(batches) - 1)]
                session.cmd_counters[cmd] += 1
                for frame in batch:
                    session.send_raw(frame)
            elif cmd in CAPTURED_SINGLE_FRAMES:
                session.send_raw(CAPTURED_SINGLE_FRAMES[cmd])
            else:
                session.send_ext({"_cmd": cmd, "s": 0, "r": 0})


async def start_gateway_server(host: str = "0.0.0.0", port: int = 8990, ports: tuple = None):
    gateway = GameGateway()
    if ports is None:
        srv = await asyncio.start_server(gateway.handle_connection, host, port)
        logger.info("TCP 游戏网关已在 [%s:%d] 监听", host, port)
        return srv
    servers = []
    for p in ports:
        try:
            srv = await asyncio.start_server(gateway.handle_connection, host, p)
            servers.append(srv)
            logger.info("TCP 游戏网关已在 [%s:%d] 监听", host, p)
        except Exception as e:
            logger.warning("TCP 网关无法绑定端口 %d: %s", p, e)
    return gateway, servers


async def start_gateway_servers(host: str = "0.0.0.0", ports: tuple = (8990, 8001)):
    return await start_gateway_server(host=host, ports=ports)
