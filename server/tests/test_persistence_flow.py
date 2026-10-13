import unittest
import os
import asyncio
from unittest.mock import MagicMock

from server.player_storage import (
    load_player,
    reset_player,
    is_tutorial_completed,
    build_http_char_node,
    get_player_file,
)
from server.gateway import ClientSession, GameGateway
from server.protocol.framing import UplinkPacket, PacketType, UpCmd
from server.protocol.amf3 import decode_amf3


class TestPersistenceFlow(unittest.TestCase):

    def setUp(self):
        self.cid = 88343134
        # 重置为纯新号测试
        reset_player(self.cid)

    def tearDown(self):
        pass

    def test_end_to_end_tutorial_and_persistence(self):
        # 1. 验证新号初始状态
        p = load_player(self.cid)
        self.assertFalse(is_tutorial_completed(self.cid))
        self.assertIn("shenyuanroad", p["position"]["last_room"])

        # 2. 模拟客户端连入 Gateway
        mock_writer = MagicMock()
        mock_writer.get_extra_info.return_value = ("127.0.0.1", 12345)
        mock_reader = MagicMock()
        session = ClientSession(mock_reader, mock_writer)
        gateway = GameGateway()

        sent_packets = []
        def capture_send(data):
            sent_packets.append(data)
        session.send_raw = capture_send

        # 3. 客户端发送 10-0 (新号)
        pkt_10_0 = UplinkPacket(
            packet_num=1,
            msg_type=PacketType.EXT,
            action=10033,
            room_id=304448,
            cmd="10-0",
            payload={}
        )
        asyncio.run(gateway.dispatch_packet(session, pkt_10_0))
        self.assertEqual(session.task_step, "513_0")

        # 4. 模拟 17_2 领取新手武器礼包
        pkt_17_2_wp = UplinkPacket(
            packet_num=2,
            msg_type=PacketType.EXT,
            action=10033,
            room_id=304448,
            cmd="17_2",
            payload={"id": 1657}
        )
        asyncio.run(gateway.dispatch_packet(session, pkt_17_2_wp))

        # 检查背包已持久化可由 1 级角色装备的新手武器
        p_cur = load_player(self.cid)
        mids = [it["mid"] for it in p_cur["bag"]["items"]]
        self.assertIn(100001, mids)

        # 5. 模拟推进任务到 513_8 并完结
        for step_idx in range(1, 9):
            pkt_step = UplinkPacket(
                packet_num=10 + step_idx,
                msg_type=PacketType.EXT,
                action=10033,
                room_id=304448,
                cmd="10-2",
                payload={"ti": f"513_{step_idx}"}
            )
            asyncio.run(gateway.dispatch_packet(session, pkt_step))

        self.assertEqual(session.task_step, "513_done")
        self.assertTrue(is_tutorial_completed(self.cid))

        # 6. 模拟选宠礼包 17_2 (id=1656)
        pkt_17_2_pet = UplinkPacket(
            packet_num=30,
            msg_type=PacketType.EXT,
            action=10033,
            room_id=304448,
            cmd="17_2",
            payload={"id": 1656}
        )
        asyncio.run(gateway.dispatch_packet(session, pkt_17_2_pet))

        # 7. 模拟副本通关 15_4
        pkt_fb_clear = UplinkPacket(
            packet_num=31,
            msg_type=PacketType.EXT,
            action=10033,
            room_id=304448,
            cmd="15_4",
            payload={}
        )
        asyncio.run(gateway.dispatch_packet(session, pkt_fb_clear))

        # 8. 检查持久化数据
        p_final = load_player(self.cid)
        self.assertTrue(is_tutorial_completed(self.cid))
        self.assertEqual(p_final["tasks"]["513"]["st"], 1)
        self.assertEqual(p_final["tasks"]["513"]["ti"], "513_8")
        self.assertIn("newjobfbyongwangge", p_final["position"]["last_room"])
        self.assertIsNotNone(p_final["pet"])
        self.assertEqual(p_final["pet"]["pti"], 100408)
        self.assertEqual(p_final["basic"]["level"], 2)
        self.assertEqual(p_final["basic"]["exp"], 1230)
        self.assertEqual(p_final["basic"]["total_exp"], 2730)
        self.assertEqual(p_final["basic"]["coin"], 100000200)

        # 9. 模拟下次登录 (Next Login)
        # HTTP /login.jsp 动态生成的节点应为 newjobfbyongwangge
        node = build_http_char_node(self.cid)
        self.assertIn("<lr>newjobfbyongwangge_", node)

        # 新 Session 连入时 10-0 应直接识别 513 已完成，跳过新手剧情
        new_session = ClientSession(mock_reader, mock_writer)
        self.assertEqual(new_session.task_step, "513_done")
        sent_packets.clear()
        new_session.send_raw = capture_send

        asyncio.run(gateway.dispatch_packet(new_session, pkt_10_0))
        self.assertEqual(new_session.task_step, "513_done")

        # 检查发送给客户端的 10-0 包首项是否为 513 st=1
        frame_10_0 = sent_packets[0]
        amf_10_0 = decode_amf3(frame_10_0[5:])
        tasks = amf_10_0["t"]
        self.assertEqual(tasks[0]["taskId"], 513)
        self.assertEqual(tasks[0]["st"], 1)
        self.assertEqual(tasks[0]["ti"], "513_8")


if __name__ == "__main__":
    unittest.main()
