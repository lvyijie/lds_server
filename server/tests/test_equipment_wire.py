"""装备四条链路的**真实 socket 端到端**测试。

与 `test_inventory_progression_fixes.py` 的区别：
  那一份是**进程内**直接调 `GameGateway().dispatch_packet(...)`，验的是处理逻辑；
  这一份是**真的起一个网关、真的走 TCP 报文**（上下行都过 AMF3 编解码 + 加密帧），
  验的是"客户端真正会收到什么"，也就是"点强化到底有没有反应"这个问题本身。

覆盖：
  * `8_2_31` 必须回 `ft`（它决定"强化"按钮的可见性，是本次 bug 的根因）
  * `8_2_5` 的 `cd` 条件串格式
  * `8_2_6` 成功时的 `res`/`st` 与随后的 `8_0.up` 条目（含 `t`）
  * `25_179_11` 装备觉醒的 `r`

帧格式（与 `test_integration.py` 一致）：
    上行 EXT: build_uplink_ext_packet(seq, ext_id, cmd_str, params, char_id)
    下行    : [4B 长度][1B TypeCode][AMF3 body]
"""

import asyncio
import os
import struct
import unittest

from server import char_manager, player_storage
from server.gateway import start_gateway_server
from server.protocol.amf3 import decode_amf3
from server.protocol.framing import (
    PacketType,
    build_uplink_ext_packet,
    build_uplink_login_packet,
)
from server.tests import _tmpdir

ACCOUNT = "player_888"


class TestEquipmentWireRoundTrip(unittest.IsolatedAsyncioTestCase):
    """把整条装备链路走一遍真实 socket。"""

    async def asyncSetUp(self):
        TestEquipmentWireRoundTrip.port_counter = (
            getattr(TestEquipmentWireRoundTrip, "port_counter", 39500) + 50
        )
        self.gate_port = TestEquipmentWireRoundTrip.port_counter + 1

        # 存档重定向到用例临时目录，避免污染真实玩家档
        self.temp_dir_name = _tmpdir.make_case_dir("equipwire")
        self.player_path = os.path.join(self.temp_dir_name, "player.json")
        from unittest.mock import patch
        self._path_patch = patch(
            "server.player_storage.get_player_file",
            side_effect=lambda _char_id: self.player_path,
        )
        self._path_patch.start()
        self.addCleanup(self._path_patch.stop)
        self.addCleanup(_tmpdir.cleanup, self.temp_dir_name)

        # 保证账号 player_888 有一个和它同名的角色（与 test_integration 同一套夹具思路）。
        # ⚠ `info["chars"]` 是 **cid 整数列表**，不是字典列表。
        _acc, info = char_manager.get_or_create_account(ACCOUNT)
        if not (info.get("chars") or []):
            char_manager.create_character_for_account(ACCOUNT, raw_name=ACCOUNT)
        _acc, info = char_manager.get_or_create_account(ACCOUNT)
        self.char_id = int(info["chars"][-1])

        self.gateway_srv = await start_gateway_server(host="127.0.0.1", port=self.gate_port)

    async def asyncTearDown(self):
        self.gateway_srv.close()
        await self.gateway_srv.wait_closed()

    # ── 测试客户端小工具 ──────────────────────────────────────────────────
    async def _login(self, reader, writer):
        """走完登录握手，返回 socket 上的 char_id（0 = 服务端按登录名解析）。"""
        policy = await reader.readuntil(b"\x00")
        self.assertIn(b"cross-domain-policy", policy)
        writer.write(build_uplink_login_packet("1 浮空岛1区", ACCOUNT, "SESSION_TEST"))
        await writer.drain()
        # logOK + 3 个 17_3 通知帧
        got = []
        for _ in range(4):
            got.append(await self._read_ext(reader))
        self.assertEqual(got[0]["_cmd"], "logOK")
        return 0

    async def _read_ext(self, reader):
        len_bytes = await reader.readexactly(4)
        total_len = struct.unpack(">I", len_bytes)[0]
        body = await reader.readexactly(total_len)
        self.assertEqual(body[0], PacketType.EXT, "期望 EXT 帧")
        return decode_amf3(body[1:])

    async def _send_and_collect(self, reader, writer, seq, cmd, params, char_id=0, want=None, limit=12):
        """发一条上行，收下行直到出现 `want`（或其下一条），返回收到的所有包。"""
        writer.write(build_uplink_ext_packet(
            seq=seq, ext_id=8, cmd_str=cmd, params=params, char_id=char_id,
        ))
        await writer.drain()
        packets = []
        for _ in range(limit):
            pkt = await asyncio.wait_for(self._read_ext(reader), timeout=5)
            packets.append(pkt)
            if want is not None and pkt.get("_cmd") == want:
                break
        return packets

    def _make_enhanceable(self):
        """在目标角色档里放一件可强化装备 + 500 金币。"""
        player_storage.reset_player(self.char_id)
        mid = next(
            m for m, meta in player_storage.GLOBAL_EQUIP_CONFIG.items()
            if int(meta.get("strLvLimit", 0)) > 0
        )
        player = player_storage.load_player(self.char_id)
        player["basic"]["coin"] = 500
        player["basic"]["user_name"] = ACCOUNT
        player_storage.save_player(player, char_id=self.char_id)
        return player_storage.add_bag_item(self.char_id, mid=mid, t=0)

    # ── 用例 ─────────────────────────────────────────────────────────────
    async def test_strengthen_button_gate_over_real_socket(self):
        """★ 核心回归：`8_2_31` 必须回 `ft`。

        这就是「点强化但等级不涨」的根因：客户端 `StrengthenPanelBase.as:250`
        用 `_freeTimes`（来自 `ft`）决定"强化"按钮是否可见；
        服务端不回这条包时 `_freeTimes` 是 `undefined`，`undefined < 1` 为 false
        ⇒ 按钮永久隐藏 ⇒ 玩家连点都点不到。
        """
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gate_port)
        try:
            char_id = await self._login(reader, writer)
            packets = await self._send_and_collect(
                reader, writer, seq=10, cmd="8_2_31", params={},
                char_id=char_id, want="8_2_31",
            )
            reply = packets[-1]
            self.assertEqual(reply["_cmd"], "8_2_31")
            self.assertIn("ft", reply, "8_2_31 必须带 ft，否则强化按钮永久隐藏")
            self.assertIsInstance(reply["ft"], int)
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_enhance_full_flow_over_real_socket(self):
        """真实报文走完 `8_2_31 → 8_2_5 → 8_2_26 → 8_2_6`，并核对下发的 `8_0`。"""
        item = self._make_enhanceable()
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gate_port)
        try:
            char_id = await self._login(reader, writer)

            # 1) 开面板：免费次数
            pkts = await self._send_and_collect(
                reader, writer, seq=10, cmd="8_2_31", params={},
                char_id=char_id, want="8_2_31",
            )
            self.assertIn("ft", pkts[-1])

            # 2) 选装备：强化条件
            pkts = await self._send_and_collect(
                reader, writer, seq=11, cmd="8_2_5", params={"gid": item["gid"]},
                char_id=char_id, want="8_2_5",
            )
            cond = pkts[-1]
            self.assertEqual(cond["gid"], item["gid"])
            self.assertEqual(cond["cd"], "100:0:%d" % __import__("server.gateway", fromlist=["x"]).STRENGTHEN_TEST_COST)

            # 3) 成功率
            pkts = await self._send_and_collect(
                reader, writer, seq=12, cmd="8_2_26",
                params={"gid": item["gid"], "li": 0, "useS": False},
                char_id=char_id, want="8_2_26",
            )
            self.assertIsInstance(pkts[-1]["p"], int)

            # 4) 强化（结果随机，两种结果都要自洽）
            pkts = await self._send_and_collect(
                reader, writer, seq=13, cmd="8_2_6",
                params={"gi": item["gid"], "pi": 0, "li": 0, "spec": True, "ob": False},
                char_id=char_id, want="8_2_6",
            )
            result = pkts[-1]
            self.assertEqual(result["_cmd"], "8_2_6")
            self.assertIn(result["res"], (201, 202))
            self.assertIn("st", result)

            saved = player_storage.load_player(self.char_id)
            row = next(r for r in saved["bag"]["items"] if r["gid"] == item["gid"])

            if result["res"] == 201:
                self.assertEqual(row["cl"], 1, "回 201 时存档等级必须 +1")
                self.assertEqual(saved["basic"]["coin"], 400, "成功要扣 100 金币")
                ups = [e for p in pkts if p.get("_cmd") == "8_0" for e in p.get("up", [])]
                entry = next((e for e in ups if int(e.get("gid", -1)) == item["gid"]), None)
                self.assertIsNotNone(entry, "成功必须下发 8_0.up 让客户端更新等级")
                self.assertEqual(entry["cl"], 1)
                self.assertEqual(entry["t"], 0, "up 条目缺 t 会被客户端静默丢弃")
            else:
                self.assertEqual(row["cl"], 0, "回 202 时等级不能变")
                self.assertEqual(saved["basic"]["coin"], 400, "失败也要扣费（固定费用）")
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_awaken_over_real_socket(self):
        """真实报文走 `25_179_11` 装备觉醒，核对 `r` 与存档变换。"""
        player_storage.reset_player(self.char_id)
        equip_id = next(
            m for m in player_storage.GLOBAL_EQUIP_WAKE
            if m in player_storage.GLOBAL_EQUIP_CONFIG
        )
        recipe = player_storage.GLOBAL_EQUIP_WAKE[equip_id]
        item = player_storage.add_bag_item(self.char_id, mid=equip_id, t=0)
        player_storage.set_equipment_enhance_level(self.char_id, item["gid"], 3)
        player_storage.add_bag_item(
            self.char_id, mid=int(recipe["mid"]), count=int(recipe["num"])
        )

        reader, writer = await asyncio.open_connection("127.0.0.1", self.gate_port)
        try:
            char_id = await self._login(reader, writer)
            # `25_179_11` 属于 SmallGameExtension（ext_id=25），不是 8
            writer.write(build_uplink_ext_packet(
                seq=20, ext_id=25, cmd_str="25_179_11", params={"g": item["gid"]},
                char_id=char_id,
            ))
            await writer.drain()
            packets = []
            for _ in range(12):
                pkt = await asyncio.wait_for(self._read_ext(reader), timeout=5)
                packets.append(pkt)
                if pkt.get("_cmd") == "25_179_11":
                    break
            reply = packets[-1]
            self.assertEqual(reply["_cmd"], "25_179_11")
            self.assertEqual(reply["r"], 1, "材料齐备时应觉醒成功（r=1）")

            saved = player_storage.load_player(self.char_id)
            awakened = next(
                (r for r in saved["bag"]["items"]
                 if int(r.get("mid", 0)) == int(recipe["wakeEquipId"])),
                None,
            )
            self.assertIsNotNone(awakened, "存档里必须出现觉醒后的装备")
            self.assertEqual(awakened["cl"], 3, "觉醒必须保留强化等级")
            leftover = sum(
                int(r.get("n", 0) or 0) for r in saved["bag"]["items"]
                if int(r.get("mid", 0)) == int(recipe["mid"]) and int(r.get("t", 0)) != 0
            )
            self.assertEqual(leftover, 0, "觉醒材料应被扣光")

            ups = [e for p in packets if p.get("_cmd") == "8_0" for e in p.get("up", [])]
            added = next(
                (e for e in ups if int(e.get("mid", 0)) == int(recipe["wakeEquipId"])), None
            )
            self.assertIsNotNone(added, "必须下发新装备条目")
            self.assertEqual(added["t"], 0)
            self.assertEqual(added["cl"], 3)
        finally:
            writer.close()
            await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
