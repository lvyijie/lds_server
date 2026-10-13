"""
端到端联调测试 (test_integration.py)
自动模拟客户端测试：
1. HTTP API 接口 (/crossdomain.xml, /login.jsp)
2. TCP 8001 网关握手 (登录首包 -> logOK -> 10-0 任务 -> 13_3 副本 -> 2301 角色入场)
"""

import asyncio
import unittest
from aiohttp import ClientSession as HttpClientSession

from server.gateway import start_gateway_server
from server.http_api import start_http_server
from server.protocol.amf3 import decode_amf3
from server.protocol.framing import (
    DownCmd,
    PacketType,
    UpCmd,
    build_uplink_ext_packet,
    build_uplink_login_packet,
)


class TestFullIntegration(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # 启动测试端口服务 (动态递增端口避免 Windows TIME_WAIT 占用)
        TestFullIntegration.port_counter = getattr(TestFullIntegration, "port_counter", 38200) + 50
        self.http_port = TestFullIntegration.port_counter
        self.gate_port = TestFullIntegration.port_counter + 1
        self.http_runner = await start_http_server(host="127.0.0.1", port=self.http_port)
        self.gateway_srv = await start_gateway_server(host="127.0.0.1", port=self.gate_port)

    async def asyncTearDown(self):
        self.gateway_srv.close()
        await self.gateway_srv.wait_closed()
        await self.http_runner.cleanup()

    async def test_http_login_and_server_list(self):
        """测试 HTTP 选服接口"""
        async with HttpClientSession() as session:
            # 1. crossdomain.xml
            async with session.get(f"http://127.0.0.1:{self.http_port}/crossdomain.xml") as resp:
                self.assertEqual(resp.status, 200)
                text = await resp.text()
                self.assertIn("cross-domain-policy", text)

            # 2. login.jsp (type=char)
            async with session.post(
                f"http://127.0.0.1:{self.http_port}/login.jsp",
                data={"type": "char", "username": "player_888"},
            ) as resp:
                self.assertEqual(resp.status, 200)
                text = await resp.text()
                self.assertIn("<result>ok</result>", text)
                self.assertIn("<u>player_888</u>", text)
                self.assertIn("<char cid=", text)

            # 3. login.jsp (type=server)
            async with session.post(
                f"http://127.0.0.1:{self.http_port}/login.jsp",
                data={"type": "server", "u": "player_888", "cid": "10001"},
            ) as resp:
                self.assertEqual(resp.status, 200)
                text = await resp.text()
                self.assertIn("<result>success</result>", text)
                self.assertIn('p="8001"', text)

    async def test_tcp_gateway_handshake_flow(self):
        """测试 TCP 网关登录握手与 Bootloader 链路"""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.gate_port)

        try:
            # 0. 接收服务端在建连瞬间主动下发的策略放行串 (官方服务端规范)
            policy_bytes = await reader.readuntil(b"\x00")
            self.assertIn(b"cross-domain-policy", policy_bytes)

            # 1. 模拟发送 UpCmd.Login (10016, seq=0)
            login_pkt = build_uplink_login_packet("1 浮空岛1区", "player_888", "SESSION_TEST")
            writer.write(login_pkt)
            await writer.drain()

            # 接收 logOK 响应 (Type 1 AMF3)
            # 下行格式: [4B TotalLen][1B TypeCode (0x01)][AMF3 Body]
            len_bytes = await reader.readexactly(4)
            import struct
            total_len = struct.unpack(">I", len_bytes)[0]
            body_bytes = await reader.readexactly(total_len)
            self.assertEqual(body_bytes[0], PacketType.EXT)

            amf_obj = decode_amf3(body_bytes[1:])
            self.assertEqual(amf_obj["_cmd"], "logOK")
            self.assertEqual(amf_obj["n"], "player_888")

            # 消费登录首包附带的 3 个 17_3 系统通知帧
            for _ in range(3):
                len_bytes = await reader.readexactly(4)
                total_len = struct.unpack(">I", len_bytes)[0]
                body_bytes = await reader.readexactly(total_len)
                self.assertEqual(body_bytes[0], PacketType.EXT)
                amf_17 = decode_amf3(body_bytes[1:])
                self.assertEqual(amf_17["_cmd"], "17_3")

            # 2. 模拟发送 10-0 (初始化任务)
            seq = 1
            char_id = 0
            tasks_req = build_uplink_ext_packet(seq=seq, ext_id=10, cmd_str="10-0", params={}, char_id=char_id)
            writer.write(tasks_req)
            await writer.drain()

            # 接收 10-0 响应
            len_bytes = await reader.readexactly(4)
            total_len = struct.unpack(">I", len_bytes)[0]
            body_bytes = await reader.readexactly(total_len)
            self.assertEqual(body_bytes[0], PacketType.EXT)
            amf_obj = decode_amf3(body_bytes[1:])
            self.assertEqual(amf_obj["_cmd"], "10-0")
            self.assertIsInstance(amf_obj["t"], list)

            # 接收附带下发的主角属性 (2131)
            len_bytes = await reader.readexactly(4)
            total_len = struct.unpack(">I", len_bytes)[0]
            body_bytes = await reader.readexactly(total_len)
            self.assertEqual(body_bytes[0], PacketType.EXT)
            amf_attr = decode_amf3(body_bytes[1:])
            self.assertEqual(amf_attr["_cmd"], "2131")

            # 3. 模拟发送 13_3 (申请进入深渊之路副本房间)
            seq = 3
            fb_req = build_uplink_ext_packet(seq=seq, ext_id=13, cmd_str="13_3", params={"bn": "shenyuanroad"}, char_id=char_id)
            writer.write(fb_req)
            await writer.drain()

            len_bytes = await reader.readexactly(4)
            total_len = struct.unpack(">I", len_bytes)[0]
            body_bytes = await reader.readexactly(total_len)
            amf_obj = decode_amf3(body_bytes[1:])
            self.assertEqual(amf_obj["_cmd"], "13_3")
            self.assertIn("shenyuanroad", amf_obj["rn"])

            # 4. 模拟发送 UpCmd.JoinRoom (通过 do_bytes 加密)
            from server.protocol.cipher import do_bytes
            from server.protocol.framing import pack_utf
            join_body = bytes([PacketType.SYS]) + struct.pack(">Hi", UpCmd.JOIN_ROOM, -1) + pack_utf("shenyuanroad_10001") + bytes([49]) + struct.pack(">i", 1) + bytes([48]) + pack_utf("")
            enc_join_body = bytes(do_bytes(7, join_body, char_id))
            total_join_len = len(enc_join_body) + 4
            join_pkt = struct.pack(">II", total_join_len, 7) + enc_join_body
            writer.write(join_pkt)
            await writer.drain()

            # 接收 joinOK (DownCmd 20019)
            len_bytes = await reader.readexactly(4)
            total_len = struct.unpack(">I", len_bytes)[0]
            body_bytes = await reader.readexactly(total_len)
            self.assertEqual(body_bytes[0], PacketType.SYS)
            action = struct.unpack_from(">H", body_bytes, 1)[0]
            self.assertEqual(action, DownCmd.JOIN_OK)

            # 接收 2301 主角生成 (允许跳过场景广播帧 2303, 2311)
            amf_obj = {}
            while amf_obj.get("_cmd") != "2301":
                len_bytes = await reader.readexactly(4)
                total_len = struct.unpack(">I", len_bytes)[0]
                body_bytes = await reader.readexactly(total_len)
                if body_bytes[0] == PacketType.EXT:
                    amf_obj = decode_amf3(body_bytes[1:])
            self.assertEqual(amf_obj["_cmd"], "2301")
            self.assertEqual(len(amf_obj["cl"]), 1)
            self.assertEqual(amf_obj["cl"][0]["n"], "player_888")

        finally:
            writer.close()
            await writer.wait_closed()


if __name__ == "__main__":
    unittest.main()
