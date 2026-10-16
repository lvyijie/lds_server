# -*- coding: utf-8 -*-
"""
server/char_manager.py
龙斗士：全 24 职业注册表 + 多角色槽位管理 (MAX_PLAYER_SLOT=11) + 会话(sid->cid)桥接模块
"""
import os
import json
import uuid
import copy
from typing import Dict, List, Tuple, Optional

# 复用原有 player_storage 的基础路径与读写函数
from .player_storage import (
    DATA_DIR,
    load_player,
    save_player,
    get_default_player,
    get_player_file,
    starter_cloth_for,
    starter_cloth_str_for,
    default_weapon_for,
    JOB_STARTER_CLOTH,
    JOB_STARTER_CLOTH_FEMALE,
)

ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
MAX_PLAYER_SLOT = 11  # 客户端 SelectCharacterPanel.MAX_PLAYER_SLOT = 11 (2页x7格，第2页用4格)
DEFAULT_CREATE_JOB = 9  # 客户端 DiyPanel.DEFAULT_JOB = 9 (双枪)

# 内存态会话绑定表：记录 login.jsp(type=server) 选中的角色，供 TCP 10016 登录反查
SESSION_TO_CHAR: Dict[str, int] = {}
SESSION_TO_ACCOUNT: Dict[str, str] = {}
ACCOUNT_LAST_CHAR: Dict[str, int] = {}

# ==============================================================================
# 完整 24 职业权威注册表（严格对齐 全角色表.md & 全角色表.json）
# ==============================================================================
JOB_CATALOG: Dict[int, dict] = {
    # === 1..10 基础职业（创角面板可选，createSelectable=True） ===
    1: {
        "jobId": 1, "name": "战士", "displayName": "战士", "enName": "worrior",
        "enumConst": "WORRIOR", "stage": "基础", "baseJobId": 1, "evoJobId": 51,
        "race": 1, "raceName": "人类", "isEvo": False, "createSelectable": True,
        "skinSuffix": "w", "bodyCanvas": "39x102",
        "bodySprite": "DefineSprite_26_mmo.skin.worrior.a600001_2",
        "skillBase": 1000, "aiName": "战士", "defaultAI": 1,
        "stars": {"attack": 7, "defence": 8, "action": 1},
    },
    2: {
        "jobId": 2, "name": "弓箭手", "displayName": "弓箭手", "enName": "archer",
        "enumConst": "ARCHER", "stage": "基础", "baseJobId": 2, "evoJobId": 52,
        "race": 1, "raceName": "人类", "isEvo": False, "createSelectable": True,
        "skinSuffix": "a", "bodyCanvas": "39x102",
        "bodySprite": "DefineSprite_450_mmo.skin.archer.a600001_2",
        "skillBase": 2000, "aiName": "弓箭手", "defaultAI": 2,
        "stars": {"attack": 9, "defence": 6, "action": 2},
    },
    3: {
        "jobId": 3, "name": "法师", "displayName": "法师", "enName": "magician",
        "enumConst": "MAGICIAN", "stage": "基础", "baseJobId": 3, "evoJobId": 53,
        "race": 2, "raceName": "龙族", "isEvo": False, "createSelectable": True,
        "skinSuffix": "m", "bodyCanvas": "46x102",
        "bodySprite": "DefineSprite_576_mmo.skin.magician.a600001_2",
        "skillBase": 3000, "aiName": "法师", "defaultAI": 3,
        "stars": {"attack": 10, "defence": 6, "action": 1},
    },
    4: {
        "jobId": 4, "name": "骑士", "displayName": "骑士", "enName": "knight",
        "enumConst": "KNIGHT", "stage": "基础", "baseJobId": 4, "evoJobId": 54,
        "race": 2, "raceName": "龙族", "isEvo": False, "createSelectable": True,
        "skinSuffix": "k", "bodyCanvas": "64x101",
        "bodySprite": "DefineSprite_168_mmo.skin.knight.a600001_2",
        "skillBase": 4000, "aiName": "骑士", "defaultAI": 4,
        "stars": {"attack": 6, "defence": 10, "action": 3},
    },
    5: {
        "jobId": 5, "name": "枪炮师", "displayName": "枪炮师", "enName": "gunner",
        "enumConst": "GUNNER", "stage": "基础", "baseJobId": 5, "evoJobId": 55,
        "race": 1, "raceName": "人类", "isEvo": False, "createSelectable": True,
        "skinSuffix": "g", "bodyCanvas": "47x101",
        "bodySprite": "DefineSprite_114_mmo.skin.gunner.a600001_2",
        "skillBase": 5000, "aiName": "枪炮师", "defaultAI": 5,
        "stars": {"attack": 8, "defence": 9, "action": 2},
    },
    6: {
        "jobId": 6, "name": "魔剑士", "displayName": "魔剑士", "enName": "spellsword",
        "enumConst": "SPELL_SWORD", "stage": "基础", "baseJobId": 6, "evoJobId": 56,
        "race": 1, "raceName": "人类", "isEvo": False, "createSelectable": True,
        "skinSuffix": "ss", "bodyCanvas": "53x101",
        "bodySprite": "DefineSprite_1576_mmo.skin.spellsword.a600001_2",
        "skillBase": 6000, "aiName": "魔剑士", "defaultAI": 6,
        "stars": {"attack": 9, "defence": 7, "action": 2},
    },
    7: {
        "jobId": 7, "name": "召唤师", "displayName": "召唤师", "enName": "summoner",
        "enumConst": "SUMMONER", "stage": "基础", "baseJobId": 7, "evoJobId": 57,
        "race": 1, "raceName": "人类", "isEvo": False, "createSelectable": True,
        "skinSuffix": "sm", "bodyCanvas": "51x104",
        "bodySprite": "DefineSprite_267_mmo.skin.summoner.a600001_2",
        "skillBase": 7000, "aiName": "召唤师", "defaultAI": 7,
        "stars": {"attack": 9, "defence": 8, "action": 1},
    },
    8: {
        "jobId": 8, "name": "天斩", "displayName": "天斩", "enName": "axe",
        "enumConst": "AXE", "stage": "基础", "baseJobId": 8, "evoJobId": 58,
        "race": 1, "raceName": "人类", "isEvo": False, "createSelectable": True,
        "skinSuffix": "x", "bodyCanvas": "38x100",
        "bodySprite": "DefineSprite_1817_mmo.skin.axe.a600001_2",
        "skillBase": 8000, "aiName": "天斩", "defaultAI": 8,
        "stars": {"attack": 10, "defence": 6, "action": 1},
    },
    9: {
        "jobId": 9, "name": "双枪", "displayName": "双枪", "enName": "doublegunner",
        "enumConst": "DOUBLE_GUNNER", "stage": "基础", "baseJobId": 9, "evoJobId": 59,
        "race": 1, "raceName": "人类", "isEvo": False, "createSelectable": True,
        "skinSuffix": "dg", "bodyCanvas": "54x100",
        "bodySprite": "DefineSprite_2988_mmo.skin.doublegunner.a600001_2",
        "skillBase": 9000, "aiName": "双枪", "defaultAI": 9,
        "stars": {"attack": 10, "defence": 7, "action": 2},
    },
    10: {
        "jobId": 10, "name": "狼人", "displayName": "狼人", "enName": "werwolf",
        "enumConst": "WERWOLF", "stage": "基础", "baseJobId": 10, "evoJobId": 60,
        "race": 1, "raceName": "人类", "isEvo": False, "createSelectable": True,
        "skinSuffix": "ww", "bodyCanvas": "41x104",
        "bodySprite": "DefineSprite_28_mmo.skin.werwolf.a600001_2",
        "skillBase": 10000, "aiName": "狼人", "defaultAI": 9,
        "stars": {"attack": 9, "defence": 8, "action": 2},
    },

    # === 51..60 觉醒职业（createSelectable=False，originalJob 指向 1..10） ===
    51: {
        "jobId": 51, "name": "战士·觉醒", "displayName": "炎龙战士", "enName": "worrior_evo",
        "enumConst": "WORRIOR_EVO", "stage": "觉醒", "baseJobId": 1, "evoJobId": None,
        "race": 1, "raceName": "人类", "isEvo": True, "createSelectable": False,
        "skinSuffix": "w", "bodyCanvas": "39x102",
        "bodySprite": "DefineSprite_26_mmo.skin.worrior.a600001_2",
        "skillBase": 1000, "aiName": "战士", "defaultAI": 1,
    },
    52: {
        "jobId": 52, "name": "弓箭手·觉醒", "displayName": "风龙射手", "enName": "archer_evo",
        "enumConst": "ARCHER_EVO", "stage": "觉醒", "baseJobId": 2, "evoJobId": None,
        "race": 1, "raceName": "人类", "isEvo": True, "createSelectable": False,
        "skinSuffix": "a", "bodyCanvas": "39x102",
        "bodySprite": "DefineSprite_450_mmo.skin.archer.a600001_2",
        "skillBase": 2000, "aiName": "弓箭手", "defaultAI": 2,
    },
    53: {
        "jobId": 53, "name": "法师·觉醒", "displayName": "魔龙法师", "enName": "magician_evo",
        "enumConst": "MAGICIAN_EVO", "stage": "觉醒", "baseJobId": 3, "evoJobId": None,
        "race": 2, "raceName": "龙族", "isEvo": True, "createSelectable": False,
        "skinSuffix": "m", "bodyCanvas": "46x102",
        "bodySprite": "DefineSprite_576_mmo.skin.magician.a600001_2",
        "skillBase": 3000, "aiName": "法师", "defaultAI": 3,
    },
    54: {
        "jobId": 54, "name": "骑士·觉醒", "displayName": "光龙骑士", "enName": "knight_evo",
        "enumConst": "KNIGHT_EVO", "stage": "觉醒", "baseJobId": 4, "evoJobId": None,
        "race": 2, "raceName": "龙族", "isEvo": True, "createSelectable": False,
        "skinSuffix": "k", "bodyCanvas": "64x101",
        "bodySprite": "DefineSprite_168_mmo.skin.knight.a600001_2",
        "skillBase": 4000, "aiName": "骑士", "defaultAI": 4,
    },
    55: {
        "jobId": 55, "name": "枪炮师·觉醒", "displayName": "狂龙炮师", "enName": "gunner_evo",
        "enumConst": "GUNNER_EVO", "stage": "觉醒", "baseJobId": 5, "evoJobId": None,
        "race": 1, "raceName": "人类", "isEvo": True, "createSelectable": False,
        "skinSuffix": "g", "bodyCanvas": "47x101",
        "bodySprite": "DefineSprite_114_mmo.skin.gunner.a600001_2",
        "skillBase": 5000, "aiName": "枪炮师", "defaultAI": 5,
    },
    56: {
        "jobId": 56, "name": "魔剑士·觉醒", "displayName": "暗龙剑士", "enName": "spellsword_evo",
        "enumConst": "SPELL_SWORD_EVO", "stage": "觉醒", "baseJobId": 6, "evoJobId": None,
        "race": 1, "raceName": "人类", "isEvo": True, "createSelectable": False,
        "skinSuffix": "ss", "bodyCanvas": "53x101",
        "bodySprite": "DefineSprite_1576_mmo.skin.spellsword.a600001_2",
        "skillBase": 6000, "aiName": "魔剑士", "defaultAI": 6,
    },
    57: {
        "jobId": 57, "name": "召唤师·觉醒", "displayName": "幻龙召唤", "enName": "summoner_evo",
        "enumConst": "SUMMONER_EVO", "stage": "觉醒", "baseJobId": 7, "evoJobId": None,
        "race": 1, "raceName": "人类", "isEvo": True, "createSelectable": False,
        "skinSuffix": "sm", "bodyCanvas": "51x104",
        "bodySprite": "DefineSprite_267_mmo.skin.summoner.a600001_2",
        "skillBase": 7000, "aiName": "召唤师", "defaultAI": 7,
    },
    58: {
        "jobId": 58, "name": "天斩·觉醒", "displayName": "霸龙天斩", "enName": "axe_evo",
        "enumConst": "AXE_EVO", "stage": "觉醒", "baseJobId": 8, "evoJobId": None,
        "race": 1, "raceName": "人类", "isEvo": True, "createSelectable": False,
        "skinSuffix": "x", "bodyCanvas": "38x100",
        "bodySprite": "DefineSprite_1817_mmo.skin.axe.a600001_2",
        "skillBase": 8000, "aiName": "天斩", "defaultAI": 8,
    },
    59: {
        "jobId": 59, "name": "双枪·觉醒", "displayName": "影龙双枪", "enName": "doublegunner",
        "enumConst": "DOUBLE_GUNNER_EVO", "stage": "觉醒", "baseJobId": 9, "evoJobId": None,
        "race": 1, "raceName": "人类", "isEvo": True, "createSelectable": False,
        "skinSuffix": "dg", "bodyCanvas": "54x100",
        "bodySprite": "DefineSprite_2988_mmo.skin.doublegunner.a600001_2",
        "skillBase": 9000, "aiName": "双枪", "defaultAI": 9,
    },
    60: {
        "jobId": 60, "name": "狼人·觉醒", "displayName": "天诛狼人", "enName": "werwolf_evo",
        "enumConst": "WERWOLF_EVO", "stage": "觉醒", "baseJobId": 10, "evoJobId": None,
        "race": 1, "raceName": "人类", "isEvo": True, "createSelectable": False,
        "skinSuffix": "ww", "bodyCanvas": "41x104",
        "bodySprite": "DefineSprite_28_mmo.skin.werwolf.a600001_2",
        "skillBase": 10000, "aiName": "狼人", "defaultAI": 9,
    },

    # === 104 平行觉醒（闪灵骑士，baseJobId=4，龙族） ===
    104: {
        "jobId": 104, "name": "骑士·平行觉醒", "displayName": "闪灵骑士", "enName": "knightparallel",
        "enumConst": None, "stage": "平行觉醒", "baseJobId": 4, "evoJobId": None,
        "race": 2, "raceName": "龙族", "isEvo": True, "createSelectable": False,
        "skinSuffix": "kp", "bodyCanvas": "65x101",
        "bodySprite": "DefineSprite_1558_mmo.skin.knightparallel.a600001_2",
        "skillBase": 4000, "aiName": "骑士", "defaultAI": 4,
    },

    # === 201..203 特殊职业（毁灭者 / 审判天使 / 龙骑士） ===
    201: {
        "jobId": 201, "name": "毁灭者", "displayName": "毁灭者", "enName": "destory",
        "enumConst": None, "stage": "特殊", "baseJobId": 201, "evoJobId": None,
        "race": 1, "raceName": "人类", "isEvo": True, "createSelectable": False,
        "skinSuffix": "d", "bodyCanvas": "41x104",
        "bodySprite": "DefineSprite_14_mmo.skin.destory.a600001_2",
        "skillBase": 1000, "aiName": "毁灭者", "defaultAI": 10,
    },
    202: {
        "jobId": 202, "name": "审判天使", "displayName": "审判天使", "enName": "trial_angel",
        "enumConst": None, "stage": "特殊", "baseJobId": 202, "evoJobId": None,
        "race": 1, "raceName": "人类", "isEvo": True, "createSelectable": False,
        "skinSuffix": "ta", "bodyCanvas": "46x104",
        "bodySprite": "DefineSprite_84_mmo.skin.trialangel.a600001_2",
        "skillBase": 3000, "aiName": "审判天使", "defaultAI": 10,
    },
    203: {
        "jobId": 203, "name": "龙骑士", "displayName": "龙骑士", "enName": "dragon_knight",
        "enumConst": None, "stage": "特殊", "baseJobId": 203, "evoJobId": None,
        "race": 1, "raceName": "人类", "isEvo": True, "createSelectable": False,
        "skinSuffix": "dk", "bodyCanvas": "59x104",
        "bodySprite": "DefineSprite_32_mmo.skin.dragonknight.a600001_2",
        "skillBase": 4000, "aiName": "龙骑士", "defaultAI": 10,
    },
}


def get_job_meta(job_id: int) -> dict:
    """获取任意 24 职业的完整元数据，未知职业回退到 1(战士)"""
    try:
        jid = int(job_id)
    except (ValueError, TypeError):
        jid = 1
    return JOB_CATALOG.get(jid, JOB_CATALOG[1])


def get_default_skills_for_job(job_id: int, level: int = 1) -> List[dict]:
    """返回该职业在指定等级**应该已学会**的技能（ck0=1 已学 / ck1=1 强化 1 级）。

    ⚠ 必须与 `player_storage.get_job_skill_pool()` 同源。若两边结论不一致
    （例如一边按基职号段、一边按本职业号段），换职业刚写好的技能树会在
    下一次 `load_player` 清洗时被冲掉，客户端随后抛
    `TypeError #1009 at SkillCostAssistant$/checkCost()`（跳跃落地必炸）。

    官方设计（用户确认）：**技能随等级逐步解锁，一级只给默认技能**。
    等级门槛取自官方 `SkillTipsEvoMap_dataClass*.xml` 的 `lv` 字段，
    升级由 `player_storage._sync_level_unlocked_skills()` 自动补学。
    """
    from .player_storage import get_job_level_skills

    skills = get_job_level_skills(job_id, level)
    if skills:
        return skills
    meta = get_job_meta(job_id)
    base = meta["skillBase"] or 1000
    return [
        {"id": base,     "ck0": 1, "ck1": 1, "ck2": 0, "ck3": 0},
        {"id": base + 1, "ck0": 1, "ck1": 1, "ck2": 0, "ck3": 0},
        {"id": base + 2, "ck0": 1, "ck1": 1, "ck2": 0, "ck3": 0},
        {"id": base + 3, "ck0": 1, "ck1": 1, "ck2": 0, "ck3": 0},
    ]


# ==============================================================================
# 账号与多角色槽位持久化 (server/data/accounts.json)
# ==============================================================================
def load_accounts() -> dict:
    if not os.path.exists(ACCOUNTS_FILE):
        default_data = {
            "next_cid": 88343135,
            "users": {
                "663944508": {
                    "sid": "1570d4fb-default-session",
                    "chars": [88343134],
                }
            },
        }
        save_accounts(default_data)
        return default_data
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "users" not in data:
            data = {"next_cid": 88343135, "users": data}
        if "next_cid" not in data:
            data["next_cid"] = 88343135
        return data
    except Exception:
        return {
            "next_cid": 88343135,
            "users": {"663944508": {"sid": str(uuid.uuid4()), "chars": [88343134]}},
        }


def save_accounts(data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_or_create_account(u: str, refresh_sid: bool = False) -> Tuple[str, dict]:
    """
    获取或初始化账号：
    - 默认账号 663944508 自带原存档角色 88343134；
    - 其他新账号若无角色，默认也是 [88343134] 或空列表 []（此处给空列表可直接触发建号，
      若你想每个新账号默认带 88343134，可将新账号 chars 设为 [] 或克隆角色）。
    """
    account_id = (u or "663944508").strip()
    acc_data = load_accounts()
    users = acc_data.setdefault("users", {})

    if account_id not in users:
        # 如果是默认多多号，保留 88343134；新多多号给空角色列表（直接进创角面板）
        init_chars = [88343134] if account_id == "663944508" else []
        users[account_id] = {
            "sid": str(uuid.uuid4()),
            "chars": init_chars,
        }
        save_accounts(acc_data)
    elif refresh_sid or not users[account_id].get("sid"):
        users[account_id]["sid"] = str(uuid.uuid4())
        save_accounts(acc_data)

    sid = users[account_id]["sid"]
    SESSION_TO_ACCOUNT[sid] = account_id
    return account_id, users[account_id]


def resolve_account_by_sid(sid: str, fallback_u: str = "") -> str:
    if sid and sid in SESSION_TO_ACCOUNT:
        return SESSION_TO_ACCOUNT[sid]
    acc_data = load_accounts()
    for u, info in acc_data.get("users", {}).items():
        if info.get("sid") == sid:
            SESSION_TO_ACCOUNT[sid] = u
            return u
    return (fallback_u or "663944508").strip()


def bind_session_char(sid: str, cid: int, u: str = "") -> None:
    """在 login.jsp(type=server) 或刚创建角色时，绑定当前会话 sid -> 角色 cid"""
    cid_int = int(cid)
    if sid:
        SESSION_TO_CHAR[sid] = cid_int
    acc_u = u or resolve_account_by_sid(sid)
    if acc_u:
        ACCOUNT_LAST_CHAR[acc_u] = cid_int
        acc_data = load_accounts()
        if acc_u in acc_data.get("users", {}):
            acc_data["users"][acc_u]["last_cid"] = cid_int
            save_accounts(acc_data)


def resolve_char_by_session(sid: str = "", user_name: str = "") -> int:
    """
    供 gateway.py 在收到 TCP 10016 (UpCmd.Login) 时反查角色 cid：
    客户端 10016 载荷为: zone + user_name(角色昵称) + password(即 sid)
    """
    # 1. 优先查内存 sid -> cid 强绑定
    if sid and sid in SESSION_TO_CHAR and int(SESSION_TO_CHAR[sid]) > 0:
        return int(SESSION_TO_CHAR[sid])

    acc_data = load_accounts()
    users = acc_data.get("users", {})

    # 2. 查 accounts.json 中 sid 对应的 last_cid
    for u, info in users.items():
        if sid and info.get("sid") == sid:
            if "last_cid" in info and info["last_cid"] in info.get("chars", []):
                return int(info["last_cid"])
            if u in ACCOUNT_LAST_CHAR:
                return int(ACCOUNT_LAST_CHAR[u])

    # 3. 精确匹配角色昵称 user_name (TryConnectSocketCommand 传的是 UserInfo.nickname)
    if user_name:
        for u, info in users.items():
            for cid in info.get("chars", []):
                p = load_player(int(cid))
                if p.get("basic", {}).get("user_name") == user_name:
                    return int(cid)

    # 4. 退化兜底：取该 sid 对应账号的最后一个角色（刚创建的角色）或第一个角色
    for u, info in users.items():
        if sid and info.get("sid") == sid and info.get("chars"):
            return int(info["chars"][-1])

    return 88343134


# ==============================================================================
# 创建角色、删除角色、转职/觉醒与 XML 节点生成
# ==============================================================================
def _is_name_taken(name: str, acc_data: dict) -> bool:
    for u, info in acc_data.get("users", {}).items():
        for cid in info.get("chars", []):
            p = load_player(int(cid))
            if p.get("basic", {}).get("user_name") == name:
                return True
    return False


def create_character_for_account(
    u: str,
    sid: str = "",
    job: int = DEFAULT_CREATE_JOB,
    race: Optional[int] = None,
    gender: int = 1,
    pet_id: int = 0,
    raw_name: str = "",
    cc_code: str = "",
) -> Tuple[str, int, str]:
    """
    处理 character.jsp(type=create)：
    - 校验 11 个角色位置上限 (MAX_PLAYER_SLOT = 11)
    - 支持通过特权码 cc_code 直接填入 51..60 / 104 / 201..203 创建觉醒或特殊职业！
    - 自动生成全局唯一昵称（因客户端 DiyPanel 不含昵称输入框，name 恒为 ""）
    - 初始化独立存档文件 player_{cid}.json 并追加到 chars 列表末尾
    """
    account_id = (u or resolve_account_by_sid(sid) or "663944508").strip()
    acc_data = load_accounts()
    users = acc_data.setdefault("users", {})
    user_info = users.setdefault(
        account_id, {"sid": sid or str(uuid.uuid4()), "chars": []}
    )
    chars: List[int] = user_info.setdefault("chars", [])

    # 1. 校验 11 槽位上限
    if len(chars) >= MAX_PLAYER_SLOT:
        return ("max_char_num_reached", 0, "")

    # 2. 解析职业 ID：若特权码 cc_code 输入了合法的 24 职业 ID（如 201 毁灭者、202 审判天使、203 龙骑士、104 闪灵骑士、51..60 觉醒），优先使用！
    target_job = int(job) if str(job).isdigit() else DEFAULT_CREATE_JOB
    cc_str = (cc_code or "").strip()
    if cc_str.isdigit() and int(cc_str) in JOB_CATALOG:
        target_job = int(cc_str)
    if target_job not in JOB_CATALOG:
        target_job = DEFAULT_CREATE_JOB

    meta = get_job_meta(target_job)

    # 3. 分配全局唯一 cid
    new_cid = int(acc_data.get("next_cid", 88343135))
    while os.path.exists(get_player_file(new_cid)) or new_cid == 88343134:
        new_cid += 1
    acc_data["next_cid"] = new_cid + 1

    # 4. 生成全局唯一角色昵称
    candidate_name = (raw_name or "").strip()
    if not candidate_name or _is_name_taken(candidate_name, acc_data):
        slot_no = len(chars) + 1
        candidate_name = f"{meta['displayName']}{new_cid % 10000:04d}"
        if _is_name_taken(candidate_name, acc_data):
            candidate_name = f"LDS{new_cid}{slot_no:02d}"

    # 5. 构建新角色存档数据
    player = copy.deepcopy(get_default_player(new_cid))
    basic = player.setdefault("basic", {})
    basic["char_id"] = new_cid
    basic["user_name"] = candidate_name
    basic["job"] = target_job
    basic["original_job"] = int(meta["baseJobId"] or target_job)
    # 严格遵循种族规则：法师(3/53)与骑士(4/54/104)为 2(龙族)，其余为 1(人类)
    basic["race"] = int(meta["race"])
    basic["gender"] = int(gender) if int(gender) in (1, 2) else 1
    # 若直接创建觉醒/特殊职业则给 65 级，基础职业给 1 级（保留你原有默认等级也可）
    if meta["isEvo"] and int(basic.get("level", 1)) < 40:
        basic["level"] = 65

    # 5b. 按职业写入初始时装与默认武器
    # 客户端按「职业后缀」请求皮肤 SWF（双枪要 a100013_dg，战士才是 a100013_w），
    # 沿用 get_default_player 里那套战士飞空装会让非战士角色全部变白色模型。
    cloth_list = starter_cloth_for(target_job, basic["gender"])
    basic["avatar"] = ",".join(str(c) for c in cloth_list)
    basic["equip"] = default_weapon_for(target_job)
    bag = player.setdefault("bag", {})
    bag["cloth"] = list(cloth_list)
    bag["wearing_cloth"] = list(cloth_list)
    # 同步把默认武器登记进背包，避免 UI 找不到
    if basic["equip"] not in bag.setdefault("equip", []):
        bag["equip"].append(basic["equip"])

    # 初始化对应职业技能树
    player["skills"] = {
        "sp": 100,
        "list": get_default_skills_for_job(target_job),
    }

    # 如果创角传了初始战宠 petId 且大于 0，记录到 basic
    if pet_id and int(pet_id) > 0:
        basic["init_pet_id"] = int(pet_id)

    save_player(player, char_id=new_cid)

    # 6. 铁律：新角色必须 append 到列表末尾（客户端 RefreshCharacterListCommand 取 char[length-1]）
    chars.append(new_cid)
    user_info["last_cid"] = new_cid
    save_accounts(acc_data)

    # 7. 预绑定会话 sid -> new_cid，确保创角后直接进服能切入新角色
    bind_session_char(user_info.get("sid", sid), new_cid, account_id)
    if sid and sid != user_info.get("sid"):
        bind_session_char(sid, new_cid, account_id)

    return ("success", new_cid, candidate_name)


def delete_character_from_account(u: str, cid: int, sid: str = "") -> List[int]:
    """处理 character.jsp(type=delete)：移除角色并回收位置"""
    account_id = (u or resolve_account_by_sid(sid) or "663944508").strip()
    acc_data = load_accounts()
    user_info = acc_data.get("users", {}).get(account_id)
    if not user_info:
        return []

    cid_int = int(cid)
    chars: List[int] = user_info.get("chars", [])
    if cid_int in chars:
        chars.remove(cid_int)
        if user_info.get("last_cid") == cid_int:
            user_info["last_cid"] = chars[-1] if chars else 0
        save_accounts(acc_data)

        # 删除对应角色存档文件。
        # 这里原本对 88343134 做了硬编码保护（`if cid_int != 88343134`），
        # 已被移除 —— 那个"默认模板角色"没有任何特殊性，用户在选角界面删掉它
        # 就应该真的消失，而不是删完下次启动又冒出一个战士。
        p_file = get_player_file(cid_int)
        if os.path.exists(p_file):
            try:
                os.remove(p_file)
            except OSError:
                pass
    return chars


_STARTER_CLOTH_ID_SET: Optional[set] = None


def _all_starter_cloth_ids() -> set:
    """全部职业（男/女）初始装 id 的并集 —— 用于识别「与职业绑定的初始套部件」。"""
    global _STARTER_CLOTH_ID_SET
    if _STARTER_CLOTH_ID_SET is None:
        s = set()
        for tbl in (JOB_STARTER_CLOTH, JOB_STARTER_CLOTH_FEMALE):
            for ids in tbl.values():
                s.update(int(x) for x in ids)
        _STARTER_CLOTH_ID_SET = s
    return _STARTER_CLOTH_ID_SET


def _is_stale_starter_cloth(wearing, job_id: int, gender: int) -> bool:
    """判断当前穿戴是否为「别的职业」的整套初始装（转职后残留 → 白色模型）。

    只认「完全由初始装 id 组成、但不是本职业那一套」的情况，
    玩家自购时装（含非初始装 id）不会被误判。
    """
    wear = {int(x) for x in (wearing or []) if str(x).lstrip("-").isdigit()}
    if not wear:
        return True
    if wear == {int(x) for x in starter_cloth_for(job_id, gender)}:
        return False
    return wear.issubset(_all_starter_cloth_ids())


def change_character_job(char_id: int, new_job_id: int) -> dict:
    cid = int(char_id)
    target_job = int(new_job_id)
    if target_job not in JOB_CATALOG:
        raise ValueError(f"未知的职业 ID: {target_job}")

    meta = get_job_meta(target_job)
    player = load_player(cid)
    basic = player.setdefault("basic", {})
    bag = player.setdefault("bag", {})

    old_job = int(basic.get("job", 0) or 0)
    gender = basic.get("gender", 1)

    basic["job"] = target_job
    basic["original_job"] = int(meta["baseJobId"] or target_job)
    basic["race"] = int(meta["race"])
    # 技能按当前角色等级解锁（一级只给默认技能），不是把整棵树一次塞满。
    player["skills"] = {
        "sp": 100,
        "list": get_default_skills_for_job(target_job, int(basic.get("level", 1) or 1)),
    }

    # ⚠ 转职必须同步换掉初始时装与默认武器。
    # 客户端按「职业后缀」请求皮肤 SWF（战士 a100013_w / 双枪 a100013_dg），
    # 沿用旧职业的时装 id 会请求到根本不存在的文件 → 服务端下发空 SWF 桩 → 白色模型。
    # 规则：职业变了、或当前穿的就是别职业整套初始装时，
    #       用新职业初始装替换「职业绑定的初始套部件」，但保留玩家自购时装。
    wearing = [
        int(x) for x in (bag.get("wearing_cloth") or bag.get("cloth") or [])
        if str(x).lstrip("-").isdigit()
    ]
    if old_job != target_job or _is_stale_starter_cloth(wearing, target_job, gender):
        starter = starter_cloth_for(target_job, gender)
        starter_ids = _all_starter_cloth_ids()
        keep = [x for x in wearing if x not in starter_ids]
        merged = list(starter) + [x for x in keep if x not in starter]
        basic["avatar"] = ",".join(str(c) for c in merged)
        basic["equip"] = default_weapon_for(target_job)
        bag["cloth"] = list(merged)
        bag["wearing_cloth"] = list(merged)
        if basic["equip"] not in bag.setdefault("equip", []):
            bag["equip"].append(basic["equip"])

    save_player(player, char_id=cid)
    return basic


def build_http_char_node_v2(char_id: int) -> str:
    """
    生成单个角色的 <char> XML 节点。

    ⚠ 字段必须同时写成 XML 子元素，不能只写成属性！
    SelectCharacterPanel.as:198 读的是 `char.cid / char.n / char.r / char.j /
    char.lv / char.g / char.lr / char.cloth / char.equip / char.soul / char.oj`
    —— AS3 E4X 里 `xml.n` 只匹配子元素，属性要写 `xml.@n`。
    只有属性时这些取值都是「空 XMLList」：
      · int(空 XMLList) -> 0        ⇒ 选角永远拿到 cid=0（会生成 player_0.json）
      · String(空 XMLList) -> ""    ⇒ UserInfo.nickname 变成空串
      · 空串会被当成 SharedObject 名 → SharedObject.getLocal("") 抛
        Error #2134: 无法创建 SharedObject（HudFirstClkAssistant 首点红点）
    因此属性与子元素双写，兼容两种读取方式（旧版 build_http_char_node 即如此）。
    """
    cid = int(char_id)
    player = load_player(cid)
    basic = player.get("basic", {})

    job_id = int(basic.get("job", 1))
    meta = get_job_meta(job_id)

    name = basic.get("user_name") or f"{meta['displayName']}{cid % 10000:04d}"
    race = int(basic.get("race", meta["race"]))
    orig_job = int(basic.get("original_job", meta["baseJobId"] or job_id))
    lv = int(basic.get("level", 1))
    gender = str(basic.get("gender", 1))  # "1" = 男, "2" = 女
    last_room = basic.get("last_room") or "new_main_city_1"

    # 获取当前穿戴时装串与武器外显 ID
    # ⚠ 兜底必须按职业取！所有职业都用战士的 100011/100012/100013 会让非战士角色
    #   请求 a100011_<job>~1000.swf 这类并不存在的文件 → 空 SWF 桩 → 白色模型。
    bag = player.get("bag", {})
    wearing = bag.get("wearing_cloth") or bag.get("cloth") or []
    # 自动修复旧存档里的秘银套(100751)、雪山套(100301)或单件600001为真正的1级初始装
    wrong_legacy_ids = {100751, 100301, 102071}
    if not wearing or wearing == [600001] or any(x in wrong_legacy_ids for x in wearing) or (
            job_id not in (1, 51) and 100011 in wearing):
        wearing = starter_cloth_for(job_id, basic.get("gender", 1))
        bag["cloth"] = list(wearing)
        bag["wearing_cloth"] = list(wearing)
        basic["avatar"] = ",".join(str(x) for x in wearing)
        # ⚠ 只在存档文件**本来就在磁盘上**时才回写。用户手动删掉存档 = 想让这个角色
        #   消失，如果这里无条件 save_player，选角界面扫一遍就会把文件重新写回来。
        #   （`load_player` 对缺失文件只返回内存模板，不再落盘。）
        if os.path.exists(get_player_file(cid)):
            save_player(player, char_id=cid)
    cloth = ",".join(str(x) for x in wearing)
    equip = str(basic.get("equip") or default_weapon_for(job_id))

    return (
        f'<char cid="{cid}" n="{name}" r="{race}" lv="{lv}" j="{job_id}" oj="{orig_job}" g="{gender}">'
        f'<cid>{cid}</cid>'
        f'<n>{name}</n>'
        f'<r>{race}</r>'
        f'<lv>{lv}</lv>'
        f'<j>{job_id}</j>'
        f'<oj>{orig_job}</oj>'
        f'<g>{gender}</g>'
        f'<cloth>{cloth}</cloth>'
        f'<equip>{equip}</equip>'
        f'<soul>0</soul>'
        f'<lr>{last_room}</lr>'
        f'<ag>0</ag>'
        f'</char>'
    )


def build_account_char_list_xml(u: str, sid: str = "") -> Tuple[str, str, str]:
    """
    按账号顺序构建最多 11 个 <char> 节点（下标即角色位置 0..10）
    返回: (account_id, sid, char_nodes_xml)
    """
    account_id = (u or resolve_account_by_sid(sid) or "663944508").strip()
    account_id, user_info = get_or_create_account(account_id)
    active_sid = sid or user_info["sid"]
    SESSION_TO_ACCOUNT[active_sid] = account_id

    chars: List[int] = user_info.get("chars", [])[:MAX_PLAYER_SLOT]
    nodes_xml = "".join(build_http_char_node_v2(cid) for cid in chars)
    return account_id, active_sid, nodes_xml