"""
测试勇望阁副本 (FB 10) 与新手引导交互链路 (test_fb10_tutorial.py)
"""
import asyncio
import os
import tempfile
import unittest
import struct
from unittest.mock import patch

from server.gateway import start_gateway_server
from server.protocol.amf3 import decode_amf3
from server.protocol.framing import (
    PacketType,
    build_uplink_ext_packet,
    build_uplink_login_packet,
)


class TestFB10AndTutorialFlow(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        from server.player_storage import reset_player
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        storage_patch = patch(
            "server.player_storage.get_player_file",
            side_effect=lambda char_id: os.path.join(
                self.temp_dir.name, f"player_{char_id}.json"
            ),
        )
        storage_patch.start()
        self.addCleanup(storage_patch.stop)
        reset_player(88343134)
        self.gateway_srv = await start_gateway_server(host="127.0.0.1", port=0)
        self.gate_port = self.gateway_srv.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.gateway_srv.close()
        await self.gateway_srv.wait_closed()

    async def _read_one_packet(self, reader):
        len_bytes = await reader.readexactly(4)
        total_len = struct.unpack(">I", len_bytes)[0]
        body = await reader.readexactly(total_len)
        ftype = body[0]
        if ftype == PacketType.EXT:
            return ftype, decode_amf3(body[1:])
        return ftype, body[1:]

    async def test_full_fb10_tutorial_lifecycle(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gate_port)
        try:
            # 策略请求放行
            policy_bytes = await reader.readuntil(b"\x00")
            self.assertIn(b"cross-domain-policy", policy_bytes)

            # 登录
            login_pkt = build_uplink_login_packet("1 浮空岛1区", "hero_tester", "SESSION_FB10")
            writer.write(login_pkt)
            await writer.drain()

            # 读 logOK 和 3个 17_3
            ftype, obj = await self._read_one_packet(reader)
            self.assertEqual(obj["_cmd"], "logOK")
            for _ in range(3):
                await self._read_one_packet(reader)

            # 1. 10-0 (初始状态不包含 513，确保可启动第 0 步开场剧情)
            writer.write(build_uplink_ext_packet(seq=1, ext_id=10, cmd_str="10-0", params={}))
            await writer.drain()

            ftype, obj = await self._read_one_packet(reader)
            self.assertEqual(obj["_cmd"], "10-0")
            # 验证未接取 513 时，t 列表中不含 513
            self.assertFalse(any("513" in str(item.get("ti")) for item in obj["t"]))

            # 消费附带的 2131
            await self._read_one_packet(reader)

            # 2. 10-7 (Step 0 触发检测，服务端必须先下发宠物亚克 27_3)
            writer.write(build_uplink_ext_packet(seq=2, ext_id=10, cmd_str="10-7", params={}))
            await writer.drain()

            ftype, obj = await self._read_one_packet(reader)
            self.assertEqual(obj["_cmd"], "10-7")
            self.assertTrue(obj["s"])

            # 3. 10-3 (Step 0 结束，启动主线 513_1)
            writer.write(build_uplink_ext_packet(seq=3, ext_id=10, cmd_str="10-3", params={"ti": 513}))
            await writer.drain()

            ftype, obj = await self._read_one_packet(reader)
            self.assertEqual(obj["_cmd"], "10-1")
            self.assertEqual(obj["t"]["ti"], "513_1")
            await self._read_one_packet(reader)  # 10-7

            # 4. 15_0 (进入勇望阁副本 FB 10)
            writer.write(build_uplink_ext_packet(seq=4, ext_id=15, cmd_str="15_0", params={"fbId": 10, "fblv": 2, "sgc": True}))
            await writer.drain()

            # 应收到 15_0 (带 fb.scenes 规范结构)
            ftype, obj = await self._read_one_packet(reader)
            self.assertEqual(obj["_cmd"], "15_0")
            self.assertEqual(obj["res"], 1)
            self.assertIn("fb", obj)
            self.assertEqual(obj["fb"]["fbId"], 10)
            self.assertEqual(obj["fb"]["scenes"][0]["sn"], "yongwanggethree")

            # 5. 15_3 (波利王出场动画播放完毕，刷新怪物)
            writer.write(build_uplink_ext_packet(seq=5, ext_id=15, cmd_str="15_3", params={"crId": 201}))
            await writer.drain()

            # 应收到 6 只怪物生成 (2302 x 6)，可包含 15_3 ACK
            spawned_monsters = []
            while len(spawned_monsters) < 6:
                ftype, obj = await self._read_one_packet(reader)
                if obj.get("_cmd") == "15_3":
                    continue
                self.assertEqual(obj["_cmd"], "2302")
                spawned_monsters.append(obj)
            self.assertEqual(len(spawned_monsters), 6)
            self.assertTrue(any(m["n"] == "波利王" for m in spawned_monsters))

            # 6. 15_12 (攻击小怪与波利王)
            writer.write(build_uplink_ext_packet(seq=6, ext_id=15, cmd_str="15_12", params={"s": 15006, "d": "-4129311,600"}))
            await writer.drain()

            # 受到技能 2411, 受到伤害 2421, 怪物阵亡 2304, 掉落金币 2316
            ftype, obj = await self._read_one_packet(reader)
            self.assertEqual(obj["_cmd"], "2411")
            ftype, obj = await self._read_one_packet(reader)
            self.assertEqual(obj["_cmd"], "2421")
            ftype, obj = await self._read_one_packet(reader)
            self.assertEqual(obj["_cmd"], "2304")
            self.assertEqual(obj["ci"], -4129311.0)
            ftype, obj = await self._read_one_packet(reader)
            self.assertEqual(obj["_cmd"], "2316")

            # 7. 击杀剩余所有怪物并验证通关 15_4
            for ci, max_hp in [(-4129313.0, 600), (-4129314.0, 600), (-4129315.0, 600), (-4129316.0, 600), (-4129317.0, 800)]:
                writer.write(build_uplink_ext_packet(seq=7, ext_id=15, cmd_str="15_12", params={"s": 15006, "d": f"{int(ci)},{max_hp}"}))
                await writer.drain()
                # 读 2411, 2421, 2304, 2316
                await self._read_one_packet(reader)
                await self._read_one_packet(reader)
                await self._read_one_packet(reader)
                await self._read_one_packet(reader)

            # 全部怪物死亡后下发的 15_4
            ftype, obj = await self._read_one_packet(reader)
            self.assertEqual(obj["_cmd"], "15_4")
            self.assertTrue(obj["suc"])
            self.assertEqual(obj["res"]["grade"], "SSS")

            # 8. 15_1 (离开副本)
            writer.write(build_uplink_ext_packet(seq=8, ext_id=15, cmd_str="15_1", params={}))
            await writer.drain()
            ftype, obj = await self._read_one_packet(reader)
            self.assertEqual(obj["_cmd"], "15_1")
            self.assertEqual(obj["res"], 1)

            # 9. 17_2 (选宠奖励领取，官方抓包 Batch 1 下发 6 帧: 32_2, 25_763_2, 27_3, 17_2, 2131, 2421)
            writer.write(build_uplink_ext_packet(seq=9, ext_id=17, cmd_str="17_2", params={"id": 1656, "in": 0}))
            await writer.drain()

            received_cmds = {}
            for _ in range(6):
                ftype, packet_obj = await self._read_one_packet(reader)
                if isinstance(packet_obj, dict) and "_cmd" in packet_obj:
                    received_cmds[packet_obj["_cmd"]] = packet_obj

            self.assertIn("17_2", received_cmds)
            self.assertEqual(received_cmds["17_2"]["re"], 5)
            self.assertIn("115:100140:1", received_cmds["17_2"]["bo"])
            self.assertIn("27_3", received_cmds)
            self.assertEqual(received_cmds["27_3"]["fv"]["pti"], 100140)

        finally:
            writer.close()
            await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
