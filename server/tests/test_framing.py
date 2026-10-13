"""
单元测试 - framing.py 流式封包与拆包测试
验证客户端上行握手包与服务端下行帧编解码。
"""

import struct
import unittest
from server.protocol.amf3 import decode_amf3
from server.protocol.framing import (
    DownCmd,
    PacketType,
    UpCmd,
    build_downlink_ext_packet,
    build_downlink_join_ok,
    build_downlink_sys_packet,
    build_uplink_ext_packet,
    build_uplink_login_packet,
    parse_uplink_stream,
)


class TestFramingModule(unittest.TestCase):

    def test_uplink_login_packet_parsing(self):
        """测试首包登录请求 (UpCmd.Login = 10016, seq=0) 的构建与解析"""
        zone = "1 浮空岛1区"
        username = "12345678"
        session_id = "SESSION_ABC_123"

        # 模拟客户端构建首包
        raw_packet = build_uplink_login_packet(zone, username, session_id)
        self.assertGreater(len(raw_packet), 8)

        # 模拟服务端接收流
        buffer = bytearray(raw_packet)
        packets, remaining = parse_uplink_stream(buffer, char_id=0)

        self.assertEqual(len(remaining), 0)
        self.assertEqual(len(packets), 1)

        pkt = packets[0]
        self.assertEqual(pkt.packet_num, 0)
        self.assertEqual(pkt.msg_type, PacketType.SYS)
        self.assertEqual(pkt.action, UpCmd.LOGIN)
        self.assertEqual(pkt.payload["zone"], zone)
        self.assertEqual(pkt.payload["user_name"], username)
        self.assertEqual(pkt.payload["password"], session_id)

    def test_uplink_encrypted_ext_packet_parsing(self):
        """测试后续上行 Type 1 扩展包（带 doBytes 加密与包序号）的解析"""
        seq = 1
        char_id = 10001
        ext_id = 10  # TaskExtension
        cmd_str = "10-0"
        params = {"req": "init"}

        # 模拟客户端发送加密扩展包
        raw_packet = build_uplink_ext_packet(
            seq=seq,
            ext_id=ext_id,
            cmd_str=cmd_str,
            params=params,
            sub_id=1,
            char_id=char_id,
        )

        # 模拟服务端接收并解密解析
        buffer = bytearray(raw_packet)
        packets, remaining = parse_uplink_stream(buffer, char_id=char_id)

        self.assertEqual(len(remaining), 0)
        self.assertEqual(len(packets), 1)

        pkt = packets[0]
        self.assertEqual(pkt.packet_num, seq)
        self.assertEqual(pkt.msg_type, PacketType.EXT)
        self.assertEqual(pkt.action, UpCmd.XT_REQ)
        self.assertEqual(pkt.ext_id, ext_id)
        self.assertEqual(pkt.cmd, cmd_str)
        self.assertEqual(pkt.payload, params)

    def test_sticky_and_partial_packets(self):
        """测试 TCP 粘包与半包流式处理"""
        # 构造两个连续的包
        pkt1 = build_uplink_login_packet("z1", "u1", "s1")
        pkt2 = build_uplink_login_packet("z2", "u2", "s2")
        combined = pkt1 + pkt2

        # 模拟拆包：一次性收到粘包
        packets, rem = parse_uplink_stream(bytearray(combined), char_id=0)
        self.assertEqual(len(packets), 2)
        self.assertEqual(len(rem), 0)
        self.assertEqual(packets[0].payload["user_name"], "u1")
        self.assertEqual(packets[1].payload["user_name"], "u2")

        # 模拟半包：仅收到部分数据
        half_stream = combined[: len(pkt1) + 5]  # 完整第1包 + 第2包的前5字节
        packets_half, rem_half = parse_uplink_stream(bytearray(half_stream), char_id=0)
        self.assertEqual(len(packets_half), 1)
        self.assertEqual(packets_half[0].payload["user_name"], "u1")
        self.assertEqual(len(rem_half), 5)

        # 补全后续半包数据
        rest_stream = rem_half + combined[len(pkt1) + 5 :]
        packets_rest, rem_rest = parse_uplink_stream(rest_stream, char_id=0)
        self.assertEqual(len(packets_rest), 1)
        self.assertEqual(len(rem_rest), 0)
        self.assertEqual(packets_rest[0].payload["user_name"], "u2")

    def test_downlink_ext_packet_framing(self):
        """测试服务端下行 Type 1 AMF3 数据帧封装"""
        data_obj = {
            "_cmd": "logOK",
            "id": 10001,
            "n": "测试玩家",
            "lastLoginTime": 1710000000,
        }
        frame = build_downlink_ext_packet(data_obj)

        # 验证大端总长前缀
        total_len = struct.unpack_from(">I", frame, 0)[0]
        self.assertEqual(total_len, len(frame) - 4)

        # 验证类型码 (0x01)
        type_code = frame[4]
        self.assertEqual(type_code, PacketType.EXT)

        # 反序列化剩余 AMF3 字节
        amf_body = frame[5:]
        decoded = decode_amf3(amf_body)
        self.assertEqual(decoded["_cmd"], "logOK")
        self.assertEqual(decoded["id"], 10001)
        self.assertEqual(decoded["n"], "测试玩家")

    def test_downlink_sys_packet_framing(self):
        """测试服务端下行 Type 0 系统数据帧封装"""
        frame = build_downlink_sys_packet(DownCmd.JOIN_OK, room_id=100)
        total_len = struct.unpack_from(">I", frame, 0)[0]
        self.assertEqual(total_len, len(frame) - 4)
        self.assertEqual(frame[4], PacketType.SYS)
        action, room_id = struct.unpack_from(">Hi", frame, 5)
        self.assertEqual(action, DownCmd.JOIN_OK)
        self.assertEqual(room_id, 100)

    def test_downlink_join_ok_framing(self):
        """测试 joinOK (20019) 专用下行帧封装"""
        frame = build_downlink_join_ok(
            room_id=200,
            room_name="shenyuanroad_200",
            max_users=50,
            join_data="ok",
        )
        total_len = struct.unpack_from(">I", frame, 0)[0]
        self.assertEqual(total_len, len(frame) - 4)
        self.assertEqual(frame[4], PacketType.SYS)
        action, room_id = struct.unpack_from(">Hi", frame, 5)
        self.assertEqual(action, DownCmd.JOIN_OK)
        self.assertEqual(room_id, 200)


if __name__ == "__main__":
    unittest.main()
