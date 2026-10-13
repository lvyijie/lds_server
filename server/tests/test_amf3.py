"""
单元测试 - amf3.py AMF3 序列化与业务包构建测试
"""

import unittest
from server.protocol.amf3 import (
    build_backpack_resp,
    build_create_name_resp,
    build_create_room_resp,
    build_init_user_data,
    build_log_ko,
    build_log_ok,
    build_server_time_resp,
    build_spawn_player_resp,
    build_task_update_resp,
    build_tasks_init_resp,
    decode_amf3,
    encode_amf3,
)


class TestAmf3Module(unittest.TestCase):

    def test_primitive_roundtrip(self):
        """测试基本数据类型的 AMF3 序列化与反序列化"""
        primitives = [
            123,
            -456,
            3.14159,
            "hello dragon",
            True,
            False,
            None,
        ]
        for val in primitives:
            encoded = encode_amf3(val)
            decoded = decode_amf3(encoded)
            if isinstance(val, float):
                self.assertAlmostEqual(val, decoded, places=4)
            else:
                self.assertEqual(val, decoded)

    def test_complex_dict_roundtrip(self):
        """测试嵌套字典、列表复合对象的序列化与还原"""
        obj = {
            "_cmd": "2301",
            "cl": [
                {
                    "bot": 1,
                    "ui": 10001,
                    "ci": 10001,
                    "n": "亚瑟",
                    "x": 350,
                    "y": 480.0,
                    "d": 1,
                    "hp": 2000,
                    "mhp": 2000,
                    "skills": [1, 2, 3],
                    "equip": {"weapon": 101, "cloth": 102},
                }
            ],
        }
        encoded = encode_amf3(obj)
        self.assertIsInstance(encoded, bytes)
        self.assertGreater(len(encoded), 10)

        decoded = decode_amf3(encoded)
        self.assertEqual(decoded["_cmd"], "2301")
        self.assertEqual(len(decoded["cl"]), 1)
        player = decoded["cl"][0]
        self.assertEqual(player["ui"], 10001)
        self.assertEqual(player["n"], "亚瑟")
        self.assertEqual(player["skills"], [1, 2, 3])
        self.assertEqual(player["equip"]["weapon"], 101)

    def test_convenience_builders(self):
        """测试常用协议响应构建函数结构与可序列化性"""
        builders = [
            build_log_ok(10001, "亚瑟", 1710000000),
            build_log_ko("密码错误"),
            build_init_user_data(),
            build_server_time_resp(1710000000),
            build_backpack_resp([], True),
            build_tasks_init_resp([]),
            build_task_update_resp("513_1", 0, 1),
            build_create_room_resp("shenyuanroad_10001"),
            build_create_name_resp(1, "推荐名"),
            build_spawn_player_resp({"ui": 10001, "n": "亚瑟"}),
        ]

        for resp in builders:
            self.assertIn("_cmd", resp)
            encoded = encode_amf3(resp)
            decoded = decode_amf3(encoded)
            self.assertEqual(resp["_cmd"], decoded["_cmd"])
            self.assertEqual(resp, decoded)


if __name__ == "__main__":
    unittest.main()
