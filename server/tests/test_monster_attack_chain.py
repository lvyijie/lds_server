# -*- coding: utf-8 -*-
"""聚焦单测：服务端权威怪物 AI 的「出手 → 判定 → 受伤扣血」一条龙（方案 A）。

只覆盖本次改动，不跑整个测试目录：

    python -m unittest server.tests.test_monster_attack_chain -v

被验证的链路（每一环都有前端源码依据）：

    服务端状态机              下行包                      客户端表现
    ─────────────────────────────────────────────────────────────────────────────
    chase（靠近玩家）    →   2451 cmdMonsterMove          MonsterActionController.onMonsterMove
    pre_attack（站定）   →   2452 cmdMonsterStop          MonsterActionController.onMonsterStop
    attack（判定帧命中） →   2411 cmdPlayerUseSkill       BeHitManager.onServerCheckHit
                                                          → 飘伤害数字 + 受击动作 + 硬直
                         →   2421 cmdPlayerStatusUpdate   → 血条数值

关键断言：
  · 只有判定帧才扣血（旧的「进判定范围就扣血」已删除）
  · 攻击框按 aiscript attBound*（**像素 ÷ gridSize=8 → 格**）判定
  · 2411 的 `sk` 是怪物真实技能 id（MonsterActionMap），否则前端 `beHitRecover`
    在 `SkillConfig.getSkillInfoByID(sk)` 查不到会直接 return，就没有受击表现
  · `p` = "1#H#-<伤害>#0"（命中）/ "0#H#0#0"（闪避），格式对齐 `BeHitManager.parseHPInfo`
  · 受击无敌帧内不重复扣血；前摇期间不扣血
"""
import os
import asyncio
import base64
import json
import struct
import time
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch

from server import gateway, player_storage
from server.dungeon_engine import (
    DungeonInstance,
    get_or_create_dungeon,
    remove_dungeon,
)
from server.gateway import ClientSession
from server.protocol.framing import PacketType, UplinkPacket

FB_ID = 10          # 永望角：首幕是 36202 红波利（新手战士.pcapng 实测）
MONSTER_MID = 36202
#: aiscript `36202` 的 attack1 = 105（MonsterActionMap）；攻击框 72~96px / 0~16px
EXPECTED_SKILL_ID = 105
EXPECTED_BOX_X = (9.0, 12.0)      # 72/8 ~ 96/8
EXPECTED_BOX_Y = (0.0, 2.0)       # 0/8 ~ 16/8
#: ⚠ 只在 workspace 内建测试存档 —— DSH 沙箱不允许写系统临时目录
#: （`server/data/player_*.json` 本来就在 .gitignore 里）
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
#: workspace 根（`server/` 的上一级），用于读客户端数据表
ROOT_DIR = os.path.dirname(os.path.dirname(DATA_DIR))

PLAYER_POS = (60.0, 40.0)
#: 让怪正好落在攻击框里：|dx| = 10 ∈ [9,12]，|dy| = 0 ∈ [0,2]
IN_BOX_OFFSET = 10.0
#: 远在攻击框外
FAR_OFFSET = 55.0


class TestMonsterAttackChain(unittest.TestCase):
    def setUp(self):
        self.char_id = 987650001
        self.player_path = os.path.join(DATA_DIR, "player_test_%d.json" % self.char_id)
        self.storage_path_patch = patch(
            "server.player_storage.get_player_file",
            side_effect=lambda _char_id: self.player_path,
        )
        self.storage_path_patch.start()
        self.addCleanup(self.storage_path_patch.stop)
        self.addCleanup(self._remove_test_player_file)
        player_storage.reset_player(self.char_id)
        remove_dungeon(self.char_id)
        self.addCleanup(remove_dungeon, self.char_id)

    def _remove_test_player_file(self):
        for path in (self.player_path, self.player_path + ".tmp"):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    # ── 脚手架 ───────────────────────────────────────────────────────────────
    def make_session(self):
        writer = MagicMock()
        writer.get_extra_info.return_value = ("127.0.0.1", 12345)
        session = ClientSession(MagicMock(), writer)
        session.char_id = self.char_id
        sent = []
        session.send_ext = sent.append
        return session, sent

    def start_dungeon(self, session, fb_id=FB_ID):
        """进副本 + 刷怪 + 把玩家坐标固定下来。"""
        session.in_fb = True
        dungeon = get_or_create_dungeon(self.char_id, fb_id=fb_id, fb_lv=2)
        dungeon.spawn_monsters()
        session.fb_player_pos = PLAYER_POS
        return dungeon

    @staticmethod
    def put_monster_at(dungeon, x, y):
        """取出第一只怪搬到指定坐标，并清掉可能残留的状态机字段。"""
        ci = next(iter(dungeon.monsters))
        mon = dungeon.monsters[ci]
        mon["x"], mon["y"] = float(x), float(y)
        for key in ("ai_state", "ai_state_until", "attack_seq"):
            mon.pop(key, None)
        return ci, mon

    @staticmethod
    def cmds(sent):
        return [pkt.get("_cmd") for pkt in sent]

    @staticmethod
    def last_of(sent, cmd):
        for pkt in reversed(sent):
            if pkt.get("_cmd") == cmd:
                return pkt
        return None

    def player_hp(self):
        basic = player_storage.load_player(self.char_id).get("basic", {})
        return int(basic.get("chp"))

    @staticmethod
    def expire_windup(mon):
        """把前摇倒计时拨到过去，免去 sleep。"""
        mon["ai_state_until"] = time.time() - 1.0

    # ── 1. 数据装配：攻击框必须是「像素 ÷ 8 = 格」 ───────────────────────────
    def test_monster_gets_ai_fields_with_grid_units(self):
        """每只怪都要挂上攻击框（格）/ 出手技能 id / 视野半径等 AI 字段。"""
        dungeon = DungeonInstance(char_id=self.char_id, fb_id=FB_ID, fb_lv=2)
        dungeon.spawn_monsters()
        mon = dungeon.monsters[next(iter(dungeon.monsters))]

        self.assertEqual(mon["mid"], MONSTER_MID)
        # ★ 攻击框：aiscript 是像素，必须除以 gridSize=8
        self.assertEqual(mon["attack_box"]["x"], EXPECTED_BOX_X)
        self.assertEqual(mon["attack_box"]["y"], EXPECTED_BOX_Y)
        # ★ 2411 的 sk 来源：MonsterActionMap 的 attack1
        self.assertEqual(mon["attack_skills"]["attack1"], EXPECTED_SKILL_ID)
        self.assertEqual(mon["attack_keys"], ["attack1"])
        self.assertGreater(mon["attack_interval"], 0.0)
        self.assertGreater(mon["windup"], 0.0)
        # chaseR=520 / sightR=360（像素）⇒ 65 / 45 格
        self.assertEqual(mon["chase_r"], 65.0)
        self.assertEqual(mon["sight_r"], 45.0)
        # 战斗数值非 0，否则判定恒为"未命中"（伤害恒 0）
        self.assertGreater(mon["attack"], 0)
        self.assertGreater(mon["hit"], 0)

    def test_attack_box_conversion_is_not_pixel_scale(self):
        """回归：旧代码把 attBound 像素当成格，判定框被放大 8 倍。"""
        dungeon = DungeonInstance(char_id=self.char_id, fb_id=FB_ID, fb_lv=2)
        dungeon.spawn_monsters()
        box_x = dungeon.monsters[next(iter(dungeon.monsters))]["attack_box"]["x"]
        self.assertEqual(box_x, EXPECTED_BOX_X)
        self.assertNotEqual(box_x, (72.0, 96.0), "忘了除 gridSize 就会得到像素值")

    # ── 2. 旧逻辑必须已被删除 ───────────────────────────────────────────────
    def test_old_reach_based_damage_logic_is_gone(self):
        """「进判定范围就扣血」的旧实现必须彻底移除，避免和状态机并存。"""
        self.assertFalse(hasattr(gateway.ClientSession, "_monster_strike"))
        self.assertFalse(hasattr(gateway, "_monster_damage"))
        self.assertFalse(hasattr(gateway, "_monster_attack_reach"))
        self.assertFalse(hasattr(gateway, "MONSTER_ATTACK_REACH_DEFAULT"))
        self.assertFalse(hasattr(gateway, "MONSTER_ATTACK_REACH_BIG"))
        self.assertTrue(hasattr(gateway.ClientSession, "tick_monster_ai"))
        self.assertTrue(hasattr(gateway.ClientSession, "_monster_step"))
        self.assertTrue(hasattr(gateway.ClientSession, "_monster_resolve_attack"))

    # ── 3. 追击：框外只移动，不扣血 ─────────────────────────────────────────
    def test_chase_sends_move_without_damage(self):
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        self.put_monster_at(dungeon, PLAYER_POS[0] - FAR_OFFSET, PLAYER_POS[1])
        hp_before = self.player_hp()

        with patch.object(gateway.random, "random", return_value=0.0):
            session.tick_monster_ai()

        self.assertIn("2451", self.cmds(sent))
        self.assertNotIn("2411", self.cmds(sent))
        self.assertNotIn("2421", self.cmds(sent))
        self.assertEqual(self.player_hp(), hp_before)

    # ── 4. 进攻击框 ⇒ 只进前摇，不扣血 ───────────────────────────────────────
    def test_entering_box_enters_windup_without_damage(self):
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        ci, mon = self.put_monster_at(
            dungeon, PLAYER_POS[0] - IN_BOX_OFFSET, PLAYER_POS[1])
        hp_before = self.player_hp()

        session.tick_monster_ai()

        self.assertEqual(mon["ai_state"], "pre_attack")
        self.assertIn("2452", self.cmds(sent))          # 站定，准备出手
        self.assertNotIn("2411", self.cmds(sent))       # 前摇内不出手
        self.assertNotIn("2421", self.cmds(sent))
        self.assertEqual(self.player_hp(), hp_before)

    # ── 5. 判定帧 ⇒ 2411（表现）+ 2421（血条）+ 扣血 ─────────────────────────
    def test_judge_frame_sends_2411_and_2421_and_deducts_hp(self):
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        ci, mon = self.put_monster_at(
            dungeon, PLAYER_POS[0] - IN_BOX_OFFSET, PLAYER_POS[1])
        session.tick_monster_ai()                       # → pre_attack
        self.assertEqual(mon["ai_state"], "pre_attack")
        self.expire_windup(mon)
        sent.clear()
        hp_before = self.player_hp()

        with patch.object(gateway.random, "random", return_value=0.0):   # 必命中
            session.tick_monster_ai()

        # ★ 受击表现：2411
        fx = self.last_of(sent, "2411")
        self.assertIsNotNone(fx, "判定帧必须下发 2411，否则客户端没有受击反馈")
        self.assertEqual(fx["req"], float(ci))                       # 攻击者 = 怪
        self.assertEqual(fx["sk"], EXPECTED_SKILL_ID)                # 怪物真实技能 id
        self.assertEqual(len(fx["tlc"]), 1)
        self.assertEqual(fx["tlc"][0]["ci"], float(self.char_id))    # 受击者 = 玩家
        fields = fx["tlc"][0]["p"].split("#")
        self.assertEqual(fields[0], "1", "第 0 段 = 1 才算命中（否则客户端当闪避）")
        self.assertEqual(fields[1], "H")
        self.assertTrue(fields[2].startswith("-"), "负数才是掉血")
        self.assertEqual(fields[3], "0", "原版怪打玩家不暴击")

        # ★ 血条数值：2421
        hp_pkt = self.last_of(sent, "2421")
        self.assertIsNotNone(hp_pkt)
        new_hp = int(hp_pkt["cl"][0]["chp"])
        self.assertEqual(new_hp, hp_before - abs(int(fields[2])))
        self.assertEqual(self.player_hp(), new_hp, "血量必须落存档")

        # 出手后进入冷却
        self.assertEqual(mon["ai_state"], "cooldown")

    # ── 6. 闪避：仍要发 2411（飘"闪避"），但不扣血 ───────────────────────────
    def test_miss_sends_2411_with_miss_field_and_no_damage(self):
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        ci, mon = self.put_monster_at(
            dungeon, PLAYER_POS[0] - IN_BOX_OFFSET, PLAYER_POS[1])
        session.tick_monster_ai()
        # attack=0 ⇒ `_monster_attack_roll` 直接判未命中（数值缺失不凭空扣血）
        mon["attack"] = 0
        self.expire_windup(mon)
        sent.clear()
        hp_before = self.player_hp()

        session.tick_monster_ai()

        fx = self.last_of(sent, "2411")
        self.assertIsNotNone(fx)
        # 官方闪避帧原文（`战士背怪物攻击.pcapng` 实测）：`0#H#-0#0`（是 -0 不是 0）
        self.assertEqual(fx["tlc"][0]["p"], "0#H#-0#0")
        self.assertNotIn("2421", self.cmds(sent))
        self.assertEqual(self.player_hp(), hp_before)

    # ── 7. 无敌帧：短时间内不重复扣血 ───────────────────────────────────────
    def test_behit_invuln_prevents_double_damage(self):
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        ci, mon = self.put_monster_at(
            dungeon, PLAYER_POS[0] - IN_BOX_OFFSET, PLAYER_POS[1])
        session.tick_monster_ai()
        self.expire_windup(mon)
        with patch.object(gateway.random, "random", return_value=0.0):
            session.tick_monster_ai()                   # 第 1 次命中
        hp_after_first = self.player_hp()

        # 立刻再打一次（冷却也拨过去，模拟另一只怪接着输出）
        mon["ai_state"] = "pre_attack"
        self.expire_windup(mon)
        with patch.object(gateway.random, "random", return_value=0.0):
            session.tick_monster_ai()

        self.assertEqual(self.player_hp(), hp_after_first,
                         "无敌帧内不应再扣血（否则多只怪把玩家锁死在硬直里）")

    # ── 8. 死亡：血量归零发 2431 ────────────────────────────────────────────
    def test_lethal_hit_sends_player_dead(self):
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        ci, mon = self.put_monster_at(
            dungeon, PLAYER_POS[0] - IN_BOX_OFFSET, PLAYER_POS[1])
        session.tick_monster_ai()
        # 把玩家血量压到 1，保证这一下必死
        player = player_storage.load_player(self.char_id)
        player.setdefault("basic", {})["chp"] = 1
        player_storage.save_player(player, char_id=self.char_id)
        mon["attack"] = 99999
        mon["hit"] = 10 ** 6
        self.expire_windup(mon)
        sent.clear()

        with patch.object(gateway.random, "random", return_value=0.0):
            session.tick_monster_ai()

        self.assertIn("2431", self.cmds(sent))
        self.assertEqual(self.player_hp(), 0)

    # ── 9. 单只怪脏数据不应让整轮停摆 ───────────────────────────────────────
    def test_broken_monster_does_not_stop_other_monsters(self):
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        cis = list(dungeon.monsters)
        self.assertGreaterEqual(len(cis), 2)
        dungeon.monsters[cis[0]]["ai_state"] = "pre_attack"
        dungeon.monsters[cis[0]]["ai_state_until"] = 0.0
        dungeon.monsters[cis[0]]["attack_box"] = "not-a-box"   # 非法 ⇒ 走兜底框
        session.fb_player_pos = (0.0, 0.0)

        session.tick_monster_ai()          # 不抛异常即通过
        self.assertIsInstance(sent, list)


class TestPlanBCreepHitReport(unittest.TestCase):
    """方案 B：客户端自管怪 → `ext 13` 上行 → 服务端 2411/2421。

    帧布局（`SocketServerClient.sendXtMessageByte` → `BinaryMsgWriter.resetMsgBuffer`）：
        [Type=1][XtReqB=10034:2B][roomId:4B][子命令:1B][payload...]
    ⇒ `pkt.ext_id` 是 roomId，**子命令在 payload[0]**。这组用例锁死这个约定。
    """

    def setUp(self):
        self.char_id = 987650002
        self.player_path = os.path.join(DATA_DIR, "player_test_%d.json" % self.char_id)
        self.storage_path_patch = patch(
            "server.player_storage.get_player_file",
            side_effect=lambda _char_id: self.player_path,
        )
        self.storage_path_patch.start()
        self.addCleanup(self.storage_path_patch.stop)
        self.addCleanup(self._cleanup)
        player_storage.reset_player(self.char_id)
        remove_dungeon(self.char_id)
        self.addCleanup(remove_dungeon, self.char_id)

    def _cleanup(self):
        for path in (self.player_path, self.player_path + ".tmp"):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    # ── 脚手架 ───────────────────────────────────────────────────────────────
    def make_session(self):
        writer = MagicMock()
        writer.get_extra_info.return_value = ("127.0.0.1", 12345)
        session = ClientSession(MagicMock(), writer)
        session.char_id = self.char_id
        session.user_name = "LDS%d" % self.char_id
        sent = []
        session.send_ext = sent.append
        return session, sent

    @staticmethod
    def cmds(sent):
        return [pkt.get("_cmd") for pkt in sent]

    @staticmethod
    def last_of(sent, cmd):
        for pkt in reversed(sent):
            if pkt.get("_cmd") == cmd:
                return pkt
        return None

    def start_dungeon(self, session, fb_id=FB_ID):
        session.in_fb = True
        dungeon = get_or_create_dungeon(self.char_id, fb_id=fb_id, fb_lv=2)
        dungeon.spawn_monsters()
        session.fb_player_pos = PLAYER_POS
        return dungeon

    @staticmethod
    def ext13_body(skill_id, monster_ci, targets, direction=4):
        """构造真实的 ext 13 上行体（子命令字节 + **二进制**载荷）。

        布局与抓包实证一致（`netbag/战士被怪物攻击.pcapng`）：
            [skillId:4BE][sourceId:4BE][(dir<<4)+count:1B][targetId:4BE × count]
        ⚠ 不是 AMF3。
        """
        body = struct.pack(">ii", int(skill_id), int(monster_ci))
        body += bytes([((int(direction) & 0x0F) << 4) | (len(targets) & 0x0F)])
        for t in targets:
            body += struct.pack(">i", int(t))
        return bytes([13]) + body

    def player_hp(self):
        basic = player_storage.load_player(self.char_id).get("basic", {})
        return int(basic.get("chp"))

    # ── 1. 出手通告（目标=怪自己）绝不能扣血 ────────────────────────────────
    def test_announcement_with_self_target_does_not_damage(self):
        """`CMAIAttackState` 的 `monsterSkillHit(skill, 怪cId, 怪cId, 4)` ⇒ tl==[m]。

        官方对通告是**回显**一条 `2411`（`tlc` 为空），所以 2411 会出现，
        但绝不能有 `2421`、绝不能掉血。
        """
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        ci = next(iter(dungeon.monsters))
        hp_before = self.player_hp()

        session._handle_hit_report(13, self.ext13_body(455, ci, [ci]))

        echo = self.last_of(sent, "2411")
        self.assertIsNotNone(echo, "官方对出手通告会回显 2411")
        self.assertEqual(echo["tlc"], [], "回显的 tlc 必须为空，否则会凭空飘伤害")
        self.assertNotIn("2421", self.cmds(sent))
        self.assertEqual(self.player_hp(), hp_before)

    # ── 2. 真实命中（目标=玩家）⇒ 2411 + 2421 + 扣血 ────────────────────────
    def test_real_hit_on_player_applies_damage_and_sends_2411(self):
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        ci = next(iter(dungeon.monsters))
        hp_before = self.player_hp()

        with patch.object(gateway.random, "random", return_value=0.0):   # 必命中
            session._handle_hit_report(13, self.ext13_body(455, ci, [self.char_id]))

        fx = self.last_of(sent, "2411")
        self.assertIsNotNone(fx, "真实命中必须下发 2411（受击表现）")
        self.assertEqual(fx["req"], float(ci))                  # 攻击者 = 客户端报的怪
        self.assertEqual(fx["sk"], 455)                         # 用客户端报来的技能 id
        self.assertEqual(fx["tlc"][0]["ci"], float(self.char_id))
        fields = fx["tlc"][0]["p"].split("#")
        self.assertEqual(fields[0], "1")
        self.assertTrue(fields[2].startswith("-"))

        hp_pkt = self.last_of(sent, "2421")
        self.assertIsNotNone(hp_pkt)
        new_hp = int(hp_pkt["cl"][0]["chp"])
        self.assertEqual(new_hp, hp_before - abs(int(fields[2])))
        self.assertEqual(self.player_hp(), new_hp)

    # ── 3. 闪避时仍发 2411("0#H#0#0")，不扣血 ───────────────────────────────
    def test_evaded_hit_sends_miss_bubble_without_damage(self):
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        ci = next(iter(dungeon.monsters))
        dungeon.monsters[ci]["attack"] = 0        # ⇒ 判定为未命中
        hp_before = self.player_hp()

        session._handle_hit_report(13, self.ext13_body(455, ci, [self.char_id]))

        fx = self.last_of(sent, "2411")
        self.assertIsNotNone(fx)
        # 官方闪避帧原文（`战士背怪物攻击.pcapng` 实测）：`0#H#-0#0`（是 -0 不是 0）
        self.assertEqual(fx["tlc"][0]["p"], "0#H#-0#0")
        self.assertNotIn("2421", self.cmds(sent))
        self.assertEqual(self.player_hp(), hp_before)

    # ── 4. 未知来源怪：不崩、不凭空扣血 ─────────────────────────────────────
    def test_unknown_monster_ci_is_ignored(self):
        session, sent = self.make_session()
        self.start_dungeon(session)
        hp_before = self.player_hp()

        session._handle_hit_report(13, self.ext13_body(455, -999999.0, [self.char_id]))

        self.assertNotIn("2421", self.cmds(sent))
        self.assertEqual(self.player_hp(), hp_before)

    # ── 5. 载荷不是 AMF3 时留证据、不崩 ─────────────────────────────────────
    def test_garbage_payload_is_handled(self):
        session, sent = self.make_session()
        self.start_dungeon(session)
        hp_before = self.player_hp()

        session._handle_hit_report(13, bytes([13]) + b"\xff\xff\xff\xff")

        self.assertNotIn("2421", self.cmds(sent))
        self.assertEqual(self.player_hp(), hp_before)

    # ── 6. 子命令 11 不参与怪物结算 ─────────────────────────────────────────
    def test_subcommand_11_does_not_touch_player_hp(self):
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        ci = next(iter(dungeon.monsters))
        hp_before = self.player_hp()

        session._handle_hit_report(11, self.ext13_body(15006, ci, [ci]))

        self.assertNotIn("2421", self.cmds(sent))
        self.assertEqual(self.player_hp(), hp_before)

    # ── 7. 方案 B 下不再启动服务端怪物 AI ───────────────────────────────────
    def test_server_side_monster_ai_is_disabled_by_default(self):
        self.assertFalse(gateway.MONSTER_AI_SERVER_SIDE,
                         "方案 B 应默认关闭服务端怪物 AI")
        session, _sent = self.make_session()
        session.start_monster_ai()
        self.assertIsNone(session.fb_ai_task, "默认模式下不应创建 AI 协程")

    # ── 8. 进副本补满血（item ⑥） ───────────────────────────────────────────
    def test_heal_on_dungeon_entry_fills_hp(self):
        player = player_storage.load_player(self.char_id)
        player.setdefault("basic", {})["chp"] = 154
        player["basic"]["mhp"] = 3825            # 过期上限，实测就是它导致 154/2325
        player_storage.save_player(player, char_id=self.char_id)

        result = gateway.heal_player_on_dungeon_entry(self.char_id)

        self.assertIsNotNone(result)
        props = player_storage.calc_player_properties(self.char_id)
        self.assertEqual(self.player_hp(), int(props["mhp"]))
        self.assertNotEqual(self.player_hp(), 154)

    # ── 9. load_player 必须修正过期的 mhp/chp（⑥ 的根因） ────────────────────
    def test_load_player_syncs_stale_mhp_and_clamps_chp(self):
        props = player_storage.calc_player_properties(self.char_id)
        live_mhp = int(props["mhp"])
        player = player_storage.load_player(self.char_id)
        player.setdefault("basic", {})["mhp"] = live_mhp + 12345   # 过期上限
        player["basic"]["chp"] = live_mhp + 99999                  # 超过上限
        player_storage.save_player(player, char_id=self.char_id)

        reloaded = player_storage.load_player(self.char_id).get("basic", {})

        self.assertEqual(int(reloaded["mhp"]), live_mhp, "过期 mhp 必须被同步")
        self.assertEqual(int(reloaded["chp"]), live_mhp, "chp 必须被钳制到上限")

    # ── 10. 玩家已死时不再重复判定（修复重复 2431） ─────────────────────────
    def test_dead_player_is_not_hit_again(self):
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        ci = next(iter(dungeon.monsters))
        player = player_storage.load_player(self.char_id)
        player.setdefault("basic", {})["chp"] = 0
        player_storage.save_player(player, char_id=self.char_id)
        sent.clear()

        with patch.object(gateway.random, "random", return_value=0.0):
            session._handle_hit_report(13, self.ext13_body(455, ci, [self.char_id]))

        self.assertNotIn("2431", self.cmds(sent), "0 血时不应再下发死亡")
        self.assertNotIn("2411", self.cmds(sent))
        self.assertEqual(self.player_hp(), 0)


    # ── 11. 端到端：走 dispatch_packet 验证子命令路由真的通 ─────────────────
    def test_dispatch_packet_routes_ext13_by_subcommand_byte(self):
        """★ 回归本次最大的坑：子命令在 `payload[0]`，不在 `pkt.ext_id`。

        以前 `ext 11/12` 分支判的是 `pkt.ext_id`（其实是 roomId），
        所以从没匹配上；这里用真实的 XT_B 帧形状走一遍 `dispatch_packet`。
        """
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        ci = next(iter(dungeon.monsters))
        hp_before = self.player_hp()

        pkt = UplinkPacket(
            packet_num=1,
            msg_type=PacketType.EXT,
            action=10034,                 # UpCmd.XtReqB
            room_id=int(session.current_room_id),
            ext_id=int(session.current_room_id),      # ★ roomId，不是子命令
            cmd="XT_B_10034",
            payload=self.ext13_body(455, ci, [self.char_id]),
        )
        with patch.object(gateway.random, "random", return_value=0.0):
            asyncio.run(gateway.GameGateway().dispatch_packet(session, pkt))

        fx = self.last_of(sent, "2411")
        self.assertIsNotNone(fx, "子命令 13 必须被派发到怪命中处理")
        self.assertEqual(fx["sk"], 455)
        self.assertLess(self.player_hp(), hp_before)

    def test_dispatch_packet_ignores_ext13_when_target_is_monster_itself(self):
        """出手通告（tl==[怪自己]）走完整链路也不该扣血。"""
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        ci = next(iter(dungeon.monsters))
        hp_before = self.player_hp()

        pkt = UplinkPacket(
            packet_num=1,
            msg_type=PacketType.EXT,
            action=10034,
            room_id=int(session.current_room_id),
            ext_id=int(session.current_room_id),
            cmd="XT_B_10034",
            payload=self.ext13_body(455, ci, [ci]),
        )
        asyncio.run(gateway.GameGateway().dispatch_packet(session, pkt))

        # 通告会回显 2411（tlc 为空），但绝不能扣血
        echo = self.last_of(sent, "2411")
        self.assertIsNotNone(echo)
        self.assertEqual(echo["tlc"], [])
        self.assertNotIn("2421", self.cmds(sent))
        self.assertEqual(self.player_hp(), hp_before)


    # ── 12. 锁死抓包实证的字节布局 ─────────────────────────────────────────
    def test_creep_hit_binary_layout_matches_official_capture(self):
        """★ 锁死 ext 13 的**二进制**布局（不是 AMF3）。

        官方双向抓包 `netbag/战士被怪物攻击.pcapng` 里 29 条真实帧的两种形态：

            000001c7 ffa7411c 41 ffa7411c  → 通告：目标 = 出手怪自己
            000001c7 ffa7411c 41 0544025e  → 真实命中：目标 = 88343134（玩家）

        其中 `0x1C7 = 455` = mId 3101 的 `attack1`
        （`server/data/monster_actions.json`），与
        `CMAIAttackState.as:71 monsterSkillHit(skillId, 怪cId, 怪cId, 4)` 吻合。

        本条用例一旦失败，说明载荷解析被改回了 AMF3 或偏移被改坏。
        """
        session, _sent = self.make_session()
        self.start_dungeon(session)
        calls = []

        with patch.object(ClientSession, "_apply_creep_hit_to_player",
                          lambda self_, ci, sk: calls.append((ci, sk))):
            # ① 通告帧（目标 = 怪自己）⇒ 不得结算
            announcement = bytes.fromhex("0d") + bytes.fromhex(
                "000001c7ffa7411c41ffa7411c")
            session._handle_hit_report(13, announcement)
            self.assertEqual(calls, [], "目标=怪自己 的出手通告不该结算")

            # ② 真实命中帧（把官方帧里的玩家 id 换成本用例的 char_id）
            real_hit = (bytes.fromhex("0d") + bytes.fromhex("000001c7ffa7411c41")
                        + struct.pack(">i", self.char_id))
            session._handle_hit_report(13, real_hit)

        self.assertEqual(calls, [(-5816036.0, 455)],
                         "必须解析出 sourceId=-5816036 / skillId=455")

    def test_creep_hit_multi_target_frame(self):
        """多目标帧（count>1）也要能全解析出来。"""
        session, _sent = self.make_session()
        self.start_dungeon(session)
        calls = []
        with patch.object(ClientSession, "_apply_creep_hit_to_player",
                          lambda self_, ci, sk: calls.append((ci, sk))):
            frame = (bytes.fromhex("0d") + struct.pack(">ii", 455, -5816036)
                     + bytes([(4 << 4) | 2])
                     + struct.pack(">i", -5816037)
                     + struct.pack(">i", self.char_id))
            session._handle_hit_report(13, frame)
        self.assertEqual(calls, [(-5816036.0, 455)])


    def test_movement_report_offsets_match_official_capture(self):
        """★ 锁死移动上报的偏移：子命令在 `payload[0]`，坐标从偏移 **1** 起。

        官方帧（`netbag/战士被怪物攻击.pcapng`，上行 XtReqB 子命令 1）：

            01 002b 003b 60 06 0004ae96
            ↑  ↑x=43 ↑y=59 ↑(dir<<4)|posType=0x60 ↑speed=6 ↑bi

        ⇒ x=43 / y=59，正是副本出生点（与 `_FB_PLAYER_ANCHOR` 一致）。
        ⚠ 旧代码用 `pkt.ext_id == 1` 且从偏移 0 读，把 `01 00` 当成 x=256，
          永远解不出正确坐标。
        """
        session, _sent = self.make_session()
        session.task_step = ""
        official = bytes.fromhex("01002b003b6006 0004ae96".replace(" ", ""))

        pkt = UplinkPacket(
            packet_num=1,
            msg_type=PacketType.EXT,
            action=10034,
            room_id=304448,
            ext_id=304448,                     # roomId，不是子命令
            cmd="XT_B_10034",
            payload=official,
        )
        asyncio.run(gateway.GameGateway().dispatch_packet(session, pkt))

        self.assertEqual(session.fb_player_pos, (43.0, 59.0),
                         "移动上报必须解出 x=43 y=59（副本出生点）")


    def test_bi_stays_in_sync_across_scene_switch(self):
        """★ 锁死不变式：同幕内 `2301.bi` 必须等于 `2302.bi`（换幕也要跟着换）。

        客户端 `ClientMonsterManager.onMonsterJoinBF` 开头就是：

            if(this._curBattleFieldId != param1.params.bi) { return; }
            //  _curBattleFieldId 来自 2301.bi

        ⇒ 一旦两者不等，客户端会**静默丢弃所有 2302**：不建怪、不跑本地 AI、
        不发 `ext 13`，而服务端日志看不出任何异常（表现就是"怪不出现/不动/不攻击"）。
        所以这是方案 B 的前置不变式，必须锁住。

        服务端的覆盖顺序（`gateway.dispatch_packet` 的副本进房分支）：
            set_current_room(room_name)
            room_id = dungeon.get_current_scene_meta()["id"]
            session.fb_room_override = room_id          # → 2301/2131/2421 的 bi
            session.fb_pending_monsters = dungeon.spawn_monsters()   # → 2302 的 bi
        """
        session, _sent = self.make_session()
        dungeon = get_or_create_dungeon(self.char_id, fb_id=1, fb_lv=2)
        self.assertGreaterEqual(dungeon.total_scenes_count, 1)

        for idx in range(dungeon.total_scenes_count):
            # 模拟进房：先切幕，再算 bi，再刷怪
            dungeon.current_scene_idx = idx
            scene_id = dungeon.get_current_scene_meta()["id"]
            session.fb_room_override = scene_id          # 2301 的 bi 来源
            packets = dungeon.spawn_monsters()           # 2302 的 bi 来源

            self.assertTrue(packets, "第 %d 幕应刷出怪" % idx)
            for p in packets:
                self.assertEqual(
                    p["bi"], session.fb_room_override,
                    "第 %d 幕：2302.bi 必须等于 2301.bi，否则客户端会丢弃该怪" % idx)

    def test_official_2301_has_no_ml_and_no_aiid_in_2302(self):
        """官方 2301 无 `ml`、官方 2302 无 `aiId` ⇒ 我们也不加（避免自造字段破坏兼容）。

        实证：`server/data/captured_packets.pkl` 的 `single_frames["2301"]` /
        `["2302"]`（官方抓包原帧）。2301 的顶层 key 只有 `_cmd/bi/cl/pk`；
        2302 是 18 个字段（见 `_build_2302` 的 docstring），不含 `aiId`。
        客户端 `generateMonster` 用 `packet.aiId ?? info.defaultAI`，
        而 `defaultAI` 由 aiscript 注入的怪物信息提供 ⇒ 不需要额外下发。
        """
        import pickle
        from pathlib import Path as _Path
        from server.protocol.amf3 import decode_amf3

        pkl = _Path(gateway.__file__).resolve().parent / "data" / "captured_packets.pkl"
        if not pkl.is_file():
            self.skipTest("官方抓包 pkl 不存在")
        frames = pickle.loads(pkl.read_bytes())["single_frames"]
        off_2301 = decode_amf3(frames["2301"][5:])
        off_2302 = decode_amf3(frames["2302"][5:])

        self.assertNotIn("ml", off_2301, "官方 2301 没有 ml，我们也不该加")
        self.assertNotIn("aiId", off_2302, "官方 2302 没有 aiId，我们也不该加")
        self.assertEqual(off_2302.get("bot"), 2)
        self.assertEqual(off_2302.get("mty"), 2)


    def test_server_side_ai_can_be_re_enabled(self):
        """回退开关有效：`MONSTER_AI_SERVER_SIDE=True` 时恢复启动服务端状态机。

        文档（`06-文档/方案-怪物AI归属与掉血链路.md`）承诺"一行切回方案 A"，
        这条用例保证这个承诺不落空 —— 万一客户端没自管，用户改一个常量就能救回来。
        """
        session, _sent = self.make_session()

        async def _run():
            session.start_monster_ai()
            task = session.fb_ai_task
            alive = task is not None and not task.done()
            session.stop_monster_ai()
            return alive

        with patch.object(gateway, "MONSTER_AI_SERVER_SIDE", True):
            self.assertTrue(asyncio.run(_run()), "打开开关后必须真的创建 AI 协程")


class TestPortalClearInvariant(unittest.TestCase):
    """传送门（`MONSTER_ALL_CLEAR`）相关不变式。

    客户端 `FbSceneMonsterClearCondition` 只监听 `CurateEvent.MONSTER_ALL_CLEAR`，
    而 `ClientMonsterManager` 有**两条删怪路径，只有一条会派发它**：

    ```as3
    // ① 客户端自己的死亡流程：CMBHMsgSendComponent.onTick → doMonsterDead()
    //    ⚠ 在发 15_11 上行**之前**执行
    if(this._charIdToMonster.length == 0) dispatchEvent(MONSTER_ALL_CLEAR);

    // ② 收到服务端 2304 → onMonsterLeaveBF()
    this._charIdToMonster.remove(_loc2_);      // 只删，不判空、不派发
    ```

    ⇒ 服务端的 `2304` 若抢在①之前到达，最后一只怪会被②吃掉 ⇒ 传送门永不开。
    这批用例锁住"方案 B 下 2304 只能来自 15_11"这个结论。
    """

    def setUp(self):
        self.char_id = 987650003
        self.player_path = os.path.join(DATA_DIR, "player_test_%d.json" % self.char_id)
        self.storage_path_patch = patch(
            "server.player_storage.get_player_file",
            side_effect=lambda _char_id: self.player_path,
        )
        self.storage_path_patch.start()
        self.addCleanup(self.storage_path_patch.stop)
        self.addCleanup(self._cleanup)
        player_storage.reset_player(self.char_id)
        remove_dungeon(self.char_id)
        self.addCleanup(remove_dungeon, self.char_id)

    def _cleanup(self):
        for path in (self.player_path, self.player_path + ".tmp"):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass

    def make_session(self):
        writer = MagicMock()
        writer.get_extra_info.return_value = ("127.0.0.1", 12345)
        session = ClientSession(MagicMock(), writer)
        session.char_id = self.char_id
        session.user_name = "LDS%d" % self.char_id
        sent = []
        session.send_ext = sent.append
        return session, sent

    @staticmethod
    def cmds(sent):
        return [pkt.get("_cmd") for pkt in sent]

    def start_dungeon(self, session, fb_id=FB_ID):
        session.in_fb = True
        dungeon = get_or_create_dungeon(self.char_id, fb_id=fb_id, fb_lv=2)
        dungeon.spawn_monsters()
        session.fb_player_pos = PLAYER_POS
        return dungeon

    # ── 1. 去重：同一只怪只播报一次 ─────────────────────────────────────────
    def test_death_announcement_is_deduplicated(self):
        """★ 实测同一只怪会经 `15_12` 与 `15_11` 两条路被算死 ⇒ 必须去重。

        日志证据：5 只怪发了 9 条 `2304`（4 只重复），`2316` 也重复。
        """
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        scene_id = dungeon.get_current_scene_meta()["id"]
        ci = next(iter(dungeon.monsters))

        session._announce_monster_death(scene_id, ci)
        session._announce_monster_death(scene_id, ci)      # 重复
        session._announce_monster_death(scene_id, ci)      # 重复

        self.assertEqual(self.cmds(sent).count("2304"), 1, "2304 必须只发一次")
        self.assertEqual(self.cmds(sent).count("2316"), 1, "2316 金币也只能一次")

    def test_death_announcement_resets_per_scene(self):
        """换幕要重置去重表（不同幕的 ci 不同，但重置更稳）。"""
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        scene_id = dungeon.get_current_scene_meta()["id"]
        ci = next(iter(dungeon.monsters))

        session._announce_monster_death(scene_id, ci)
        session._announce_monster_death(scene_id + 1, ci)   # 另一幕

        self.assertEqual(self.cmds(sent).count("2304"), 2)

    # ── 2. ★ 方案 B 下 15_12（纯伤害）不得发 2304 ───────────────────────────
    def test_damage_path_does_not_send_2304_in_plan_b(self):
        """★ 关键：方案 B 下 `15_12` 只更新血量，**不能**发 `2304`。

        否则会抢在客户端 `doMonsterDead()` 之前把最后一只怪移出
        `_charIdToMonster` ⇒ `MONSTER_ALL_CLEAR` 永不派发 ⇒ 传送门不开。
        """
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        ci = next(iter(dungeon.monsters))
        # 直接把怪打到只剩 1 血，再用一次伤害打死它
        dungeon.monsters[ci]["hp"] = 1
        scene_id = dungeon.get_current_scene_meta()["id"]

        pkt = UplinkPacket(
            packet_num=1, msg_type=PacketType.EXT, action=10033,
            room_id=scene_id, cmd="15_12",
            payload={"s": 15012, "d": "%d,99999" % int(ci)},
        )
        asyncio.run(gateway.GameGateway().dispatch_packet(session, pkt))

        self.assertEqual(dungeon.monsters[ci]["hp"], 0, "伤害仍要算死")
        self.assertNotIn("2304", self.cmds(sent),
                         "方案 B 下伤害路径不能发 2304（会与客户端删怪竞态）")
        self.assertIn("2316", self.cmds(sent), "金币通知仍要发")

    # ── 3. ★ 15_11（客户端死亡确认）才发 2304 ───────────────────────────────
    def test_creep_dead_path_sends_2304(self):
        """客户端 `15_11` 报死亡时（客户端已本地删完）才发 `2304` —— 事后确认，安全。"""
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        ci = next(iter(dungeon.monsters))
        scene_id = dungeon.get_current_scene_meta()["id"]

        pkt = UplinkPacket(
            packet_num=1, msg_type=PacketType.EXT, action=10033,
            room_id=scene_id, cmd="15_11",
            payload={"x": "%d_50_40#" % int(ci), "ks": False},
        )
        asyncio.run(gateway.GameGateway().dispatch_packet(session, pkt))

        self.assertIn("2304", self.cmds(sent))
        self.assertEqual(dungeon.monsters[ci]["hp"], 0)
        # 15_11 自身要有 ACK（客户端在等）
        self.assertEqual(sent[0].get("_cmd"), "15_11")

    # ── 4. 15_12 + 15_11 都来一遍：合计仍只有一条 2304 ──────────────────────
    def test_both_paths_together_send_single_2304(self):
        """两条真实路径都走一遍，`2304` 合计必须只有 1 条。"""
        session, sent = self.make_session()
        dungeon = self.start_dungeon(session)
        ci = next(iter(dungeon.monsters))
        dungeon.monsters[ci]["hp"] = 1
        scene_id = dungeon.get_current_scene_meta()["id"]

        for cmd, payload in (
            ("15_12", {"s": 15012, "d": "%d,99999" % int(ci)}),
            ("15_11", {"x": "%d_50_40#" % int(ci), "ks": False}),
        ):
            asyncio.run(gateway.GameGateway().dispatch_packet(
                session,
                UplinkPacket(packet_num=1, msg_type=PacketType.EXT, action=10033,
                             room_id=scene_id, cmd=cmd, payload=payload),
            ))

        self.assertEqual(self.cmds(sent).count("2304"), 1)
        self.assertEqual(self.cmds(sent).count("2316"), 1)


class TestSpawnSnapToWalkable(unittest.TestCase):
    """刷怪坐标必须落在可行走格上（否则客户端自管模式下怪会永久卡住）。

    掩码由 `server/build_scene_walkable.py` 从场景 SWF 的 `obstruction` 层生成，
    规则与客户端 `AbstractScene.setMapArray()` 一致（R>127 且不透明 = 可走）。

    实测坏点：`fubennbuolanone`（fb14 幕1）的 `(49,31)` 落在障碍区（周围 7×7 全阻），
    且该坐标在 4 个难度里都出现 ⇒ 玩家走不到、宠物够不到、本幕清不空、传送门不开。
    """

    SCENE = "fubennbuolanone"
    BAD = (49, 31)
    GOOD = (93, 30)

    def setUp(self):
        from server import scene_walkable
        if not scene_walkable.has_mask(self.SCENE):
            self.skipTest("场景掩码未构建（先跑 python -m server.build_scene_walkable）")
        self.walkable = scene_walkable

    # ── 1. 掩码本身与客户端规则一致 ─────────────────────────────────────────
    def test_mask_matches_client_rule(self):
        self.assertTrue(self.walkable.is_walkable(self.SCENE, *self.GOOD))
        self.assertFalse(self.walkable.is_walkable(self.SCENE, *self.BAD))

    def test_unknown_scene_returns_none(self):
        """缺表必须返回 None（未知），而不是 False —— 否则会误判成"不可走"。"""
        self.assertIsNone(self.walkable.is_walkable("__no_such_scene__", 10, 10))

    # ── 2. 吸附 ─────────────────────────────────────────────────────────────
    def test_snap_moves_off_obstacle(self):
        nx, ny, dist = self.walkable.snap(self.SCENE, *self.BAD)
        self.assertGreater(dist or 0, 0.0, "坏点必须被移动")
        self.assertTrue(self.walkable.is_walkable(self.SCENE, nx, ny),
                        "吸附结果必须是可走格")
        self.assertLessEqual(dist, 6.0, "吸附不该跳太远")

    def test_snap_keeps_walkable_point(self):
        nx, ny, dist = self.walkable.snap(self.SCENE, *self.GOOD)
        self.assertEqual((nx, ny), self.GOOD)
        self.assertEqual(dist, 0.0)

    def test_snap_without_mask_is_noop(self):
        x, y, dist = self.walkable.snap("__no_such_scene__", 7, 9)
        self.assertEqual((x, y), (7, 9))
        self.assertIsNone(dist)

    # ── 3. 端到端：刷出来的怪不能落在障碍格 ─────────────────────────────────
    def test_spawned_monsters_are_on_walkable_cells(self):
        """fb14 幕1 的 5 只怪，吸附后全部落在可走格。"""
        dungeon = DungeonInstance(char_id=1, fb_id=14, fb_lv=2)
        dungeon.current_scene_idx = 0
        packets = dungeon.spawn_monsters()
        self.assertTrue(packets)
        for p in packets:
            gx, gy = int(p["x"]), int(p["y"])
            self.assertTrue(
                self.walkable.is_walkable(self.SCENE, gx, gy),
                "怪被刷在不可走格 (%d,%d) ⇒ 客户端自管模式下会永久卡住" % (gx, gy))

    def test_snap_helper_degrades_gracefully(self):
        """`_snap_spawn_to_walkable` 在缺表时原样返回（不能抛异常）。"""
        from server.dungeon_engine import _snap_spawn_to_walkable
        x, y, dist = _snap_spawn_to_walkable("__no_such_scene__", 12.0, 34.0)
        self.assertEqual((x, y), (12.0, 34.0))
        self.assertIsNone(dist)


class TestSceneEntranceInvariants(unittest.TestCase):
    """场景入口点（`super(x,y)` / 配置 `initPoint`）相关不变式。

    入口点 = 玩家进入该场景时站的位置 ⇒ **必然是这块地图上可走的地面**。
    它是「刷怪位锚点」和「掩码正确性」的权威真值：

    * 早期刷怪坐标是从一份**硬编码的 13 个诺兰密林坐标**循环取的
      （`build_dungeon_spawn.slots_to_positions`，实测 1694/1694 = 100%），
      **完全没看目标地图** ⇒ 怪刷在墙里/背景上。
    * 早期掩码只靠图像特征挑，121 个有入口的场景里 **57 个（47%）入口点不可走**
      ⇒ 掩码全错（用它吸附会把怪丢到随机位置）。
    """

    ENT_PATH = os.path.join(DATA_DIR, "scene_entrances.json")
    MASK_PATH = os.path.join(DATA_DIR, "scene_walkable.json")
    SPAWN_PATH = os.path.join(DATA_DIR, "dungeon_spawn.json")

    @classmethod
    def setUpClass(cls):
        for p in (cls.ENT_PATH, cls.MASK_PATH, cls.SPAWN_PATH):
            if not os.path.isfile(p):
                raise unittest.SkipTest("缺数据文件 %s（先跑对应 build_* 脚本）" % p)
        with open(cls.ENT_PATH, encoding="utf-8") as fp:
            cls.entrances = (json.load(fp).get("scenes") or {})
        with open(cls.MASK_PATH, encoding="utf-8") as fp:
            cls.masks = (json.load(fp).get("scenes") or {})
        with open(cls.SPAWN_PATH, encoding="utf-8") as fp:
            cls.spawn = json.load(fp)

    def _mask_walk(self, sn):
        rec = self.masks[sn]
        cols, rows = int(rec["w"]), int(rec["h"])
        bits = base64.b64decode(rec["mask"])

        def walk(c, r):
            if not (0 <= c < cols and 0 <= r < rows):
                return False
            i = c * rows + r
            return bool((bits[i >> 3] >> (i & 7)) & 1)
        return cols, rows, walk

    # ── 1. 入口点覆盖率 ─────────────────────────────────────────────────────
    def test_entrance_covers_almost_all_dungeon_scenes(self):
        used = {s["sn"] for d in (self.spawn.get("dungeons") or [])
                for s in (d.get("scenes") or [])}
        self.assertTrue(used)
        covered = [sn for sn in used if sn in self.entrances]
        ratio = len(covered) / float(len(used))
        self.assertGreaterEqual(
            ratio, 0.98,
            "入口点覆盖率只有 %.0f%%（%d/%d）；副本场景必须能拿到本图入口点，"
            "否则只能回落到抄别人的坐标" % (ratio * 100, len(covered), len(used)))

    # ── 2. ★ 掩码必须通过入口点硬校验 ───────────────────────────────────────
    def test_every_mask_has_walkable_entrance(self):
        """★ 掩码表里凡是能对上入口点的，入口点必须可走。

        这是**最强的一条**：入口点必然可走（玩家站的地方），
        掩码若把它算成障碍 ⇒ 掩码挑错了图层/错位 ⇒ 绝不能拿来吸附。
        实测早期 121 个里有 57 个不满足。
        """
        checked = bad = 0
        for sn in self.masks:
            ent = self.entrances.get(sn)
            if not ent:
                continue
            checked += 1
            _cols, _rows, walk = self._mask_walk(sn)
            if not walk(int(round(ent["gx"])), int(round(ent["gy"]))):
                bad += 1
                self.fail("场景 %s 的掩码把入口点 (%d,%d) 算成障碍 ⇒ 掩码错位"
                          % (sn, round(ent["gx"]), round(ent["gy"])))
        self.assertGreater(checked, 50, "能校验的掩码太少（%d），数据可能没生成" % checked)
        self.assertEqual(bad, 0)

    # ── 3. 不再用"抄别人地图"的坐标 ────────────────────────────────────────
    def test_no_dungeon_scene_uses_borrowed_coordinates(self):
        """★ 任何一幕都不该再是 `reconstructed`（= 抄诺兰密林的坐标池）。"""
        borrowed = [s["sn"] for d in (self.spawn.get("dungeons") or [])
                    for s in (d.get("scenes") or [])
                    if (s.get("posSource") or "") == "reconstructed"]
        self.assertEqual(
            borrowed, [],
            "这些幕的刷怪坐标还是从别的图抄来的（会落到墙里）：%s" % borrowed[:10])

    # ── 4. 入口布局：坐标必须贴着本图入口点 ─────────────────────────────────
    def test_entrance_layout_is_near_scene_entrance(self):
        """`posSource == "entrance"` 的幕，每个刷怪点都要贴近本场景入口点。"""
        checked = 0
        for d in (self.spawn.get("dungeons") or []):
            for s in (d.get("scenes") or []):
                if (s.get("posSource") or "") != "entrance":
                    continue
                ent = self.entrances.get(s["sn"])
                if not ent:
                    continue
                for wv in (s.get("waves") or {}).values():
                    for sp in wv:
                        checked += 1
                        dx = abs(sp["x"] - ent["gx"])
                        dy = abs(sp["y"] - ent["gy"])
                        self.assertLessEqual(
                            dx, 25.0,
                            "%s 的怪 %s 离入口点 x 方向 %.1f 格（入口是这张图唯一保证可走的地方）"
                            % (s["sn"], sp["mId"], dx))
                        self.assertLessEqual(
                            dy, 10.0,
                            "%s 的怪 %s 离入口点 y 方向 %.1f 格（y 是纵深，偏多就掉进背景）"
                            % (s["sn"], sp["mId"], dy))
        self.assertGreater(checked, 100, "入口布局的刷怪点太少（%d）" % checked)

    # ── 5. 刷怪点必须在场景尺寸内 ──────────────────────────────────────────
    def test_spawn_points_are_within_scene_bounds(self):
        xml_path = os.path.join(ROOT_DIR, "04-data-json", "game-data", "06-scenes",
                                "SceneData_dataClass.xml")
        if not os.path.isfile(xml_path):
            self.skipTest("缺 SceneData_dataClass.xml")
        sizes = {}
        for node in ET.parse(xml_path).getroot().iter():
            nm = node.get("nm")
            if not nm:
                continue
            try:
                sizes[nm] = (float(node.get("width") or 0) / 8.0,
                             float(node.get("height") or 0) / 8.0)
            except (TypeError, ValueError):
                continue
        oob = []
        for d in (self.spawn.get("dungeons") or []):
            for s in (d.get("scenes") or []):
                b = sizes.get(s["sn"])
                if not b:
                    continue
                for wv in (s.get("waves") or {}).values():
                    for sp in wv:
                        if not (0 <= sp["x"] < b[0] and 0 <= sp["y"] < b[1]):
                            oob.append((s["sn"], sp["mId"], sp["x"], sp["y"]))
        self.assertEqual(oob[:5], [], "刷怪点越界: %s" % oob[:5])


if __name__ == "__main__":
    unittest.main(verbosity=2)
