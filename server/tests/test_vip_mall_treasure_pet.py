"""
单元测试：龙币商城、圣龙骑士 VIP、珍宝天箱与战宠觉醒系统 (test_vip_mall_treasure_pet.py)
"""
import unittest
import asyncio
from server.player_storage import (
    load_player,
    reset_player,
    get_dragon_coin,
    add_dragon_coin,
    set_dragon_coin,
    get_vip_info,
    set_vip_info,
    get_pets,
    add_pet,
    wake_pet,
    level_up_pet,
    set_follow_pet,
)
from server.gateway import GameGateway, ClientSession
from server.protocol.framing import PacketType, UpCmd, UplinkPacket


class MockStreamWriter:
    def __init__(self):
        self.packets = []
        self.is_closed = False

    def write(self, data: bytes):
        self.packets.append(data)

    async def drain(self):
        pass

    def close(self):
        self.is_closed = True

    async def wait_closed(self):
        pass

    def get_extra_info(self, name: str):
        return ("127.0.0.1", 12345)


class TestVipMallTreasurePet(unittest.TestCase):
    def setUp(self):
        self.char_id = 99998888
        self.player = reset_player(self.char_id)

    def test_dragon_coin_storage_operations(self):
        """测试龙币余额查询、增加、扣除与直接设定。"""
        init_coin = get_dragon_coin(self.char_id)
        self.assertGreaterEqual(init_coin, 999999)

        new_coin = add_dragon_coin(self.char_id, 50000)
        self.assertEqual(new_coin, init_coin + 50000)

        after_lottery = add_dragon_coin(self.char_id, -30)
        self.assertEqual(after_lottery, new_coin - 30)

        set_coin = set_dragon_coin(self.char_id, 888888)
        self.assertEqual(set_coin, 888888)
        self.assertEqual(get_dragon_coin(self.char_id), 888888)

    def test_vip_storage_operations(self):
        """测试圣龙骑士 VIP 状态设定与查询。"""
        vip = get_vip_info(self.char_id)
        self.assertTrue(vip["is_vip"])
        self.assertEqual(vip["vip_level"], 7)
        self.assertTrue(vip["is_annual"])
        self.assertTrue(vip["is_super_vip"])

        set_vip_info(self.char_id, level=7, is_vip=True, is_annual=False)
        updated = get_vip_info(self.char_id)
        self.assertEqual(updated["vip_level"], 7)
        self.assertFalse(updated["is_annual"])

    def test_pet_awakening_and_management(self):
        """测试战宠获取、出战、满阶觉醒与升级。"""
        god_pet = add_pet(self.char_id, pti=100494, name="暗灭帝尊", lv=100, ecs=0)
        self.assertEqual(god_pet["pti"], 100494)
        self.assertEqual(god_pet["n"], "暗灭帝尊")
        pid = god_pet["pi"]

        awakened = wake_pet(self.char_id, pet_id=pid, target_ecs=5)
        self.assertIsNotNone(awakened)
        self.assertEqual(awakened["ecs"], 5)
        self.assertEqual(awakened["eskill"], 5)
        self.assertTrue(awakened["isevo"])
        self.assertGreaterEqual(awakened["mhp"], 200000)

        followed = set_follow_pet(self.char_id, pet_id=pid)
        self.assertIsNotNone(followed)
        self.assertEqual(followed["pi"], pid)

        leveled = level_up_pet(self.char_id, pet_id=pid, target_lv=100)
        self.assertEqual(leveled["lv"], 100)

    def test_gateway_vip_and_mall_packets(self):
        """测试网关对 VIP (4_0, 4_5) 与商城 (21_3, 21_2) 协议的响应。"""
        gateway = GameGateway()
        writer = MockStreamWriter()
        session = ClientSession(None, writer)
        session.char_id = self.char_id

        async def run_flow():
            # 1. VIP 初始化 (4_0)
            pkt_4_0 = UplinkPacket(packet_num=1, msg_type=PacketType.EXT, action=10, room_id=1, cmd="4_0", payload={})
            await gateway.dispatch_packet(session, pkt_4_0)
            await session.flush()
            self.assertTrue(any(b"4_0" in p for p in writer.packets))
            writer.packets.clear()

            # 2. 商城查询龙币 (21_3)
            pkt_21_3 = UplinkPacket(packet_num=2, msg_type=PacketType.EXT, action=10, room_id=1, cmd="21_3", payload={})
            await gateway.dispatch_packet(session, pkt_21_3)
            await session.flush()
            self.assertTrue(any(b"21_3" in p for p in writer.packets))
            writer.packets.clear()

            # 3. 珍宝天箱数据初始化 (25_1157_0) -> 必须下发 pcd/ecd = 0 以关闭等待遮罩
            pkt_box = UplinkPacket(packet_num=3, msg_type=PacketType.EXT, action=10, room_id=1, cmd="25_1157_0", payload={})
            await gateway.dispatch_packet(session, pkt_box)
            await session.flush()
            self.assertTrue(any(b"25_1157_0" in p for p in writer.packets))
            writer.packets.clear()

            # 4. 珍宝天箱抽奖 (25_1157_1) -> 免费神宠单抽
            pkt_lottery = UplinkPacket(packet_num=4, msg_type=PacketType.EXT, action=10, room_id=1, cmd="25_1157_1", payload={"t": 0})
            await gateway.dispatch_packet(session, pkt_lottery)
            await session.flush()
            self.assertTrue(any(b"25_1157_1" in p for p in writer.packets))
            writer.packets.clear()

            # 5. 战宠觉醒材料查询 (27_204) 与 执行觉醒 (27_203)
            pkt_wake_need = UplinkPacket(packet_num=5, msg_type=PacketType.EXT, action=10, room_id=1, cmd="27_204", payload={"p": 1, "e": 5})
            await gateway.dispatch_packet(session, pkt_wake_need)
            await session.flush()
            self.assertTrue(any(b"27_204" in p for p in writer.packets))
            writer.packets.clear()

            pkt_wake_exec = UplinkPacket(packet_num=6, msg_type=PacketType.EXT, action=10, room_id=1, cmd="27_203", payload={"p": 1, "e": 5})
            await gateway.dispatch_packet(session, pkt_wake_exec)
            await session.flush()
            self.assertTrue(any(b"27_203" in p for p in writer.packets))
            # 6. 商城购买商品 (21_2)
            pkt_buy = UplinkPacket(packet_num=7, msg_type=PacketType.EXT, action=10, room_id=1, cmd="21_2", payload={"gid": "2300170_1", "vn": 1})
            await gateway.dispatch_packet(session, pkt_buy)
            await session.flush()
            self.assertTrue(any(b"21_2" in p for p in writer.packets))
            self.assertTrue(any(b"21_7" in p for p in writer.packets))
            writer.packets.clear()

            # 7. 背包与身穿装备初始化 (8_0_0) -> 必须下发 ov=True 帧
            pkt_bag = UplinkPacket(packet_num=8, msg_type=PacketType.EXT, action=10, room_id=1, cmd="8_0_0", payload={})
            await gateway.dispatch_packet(session, pkt_bag)
            await session.flush()
            self.assertTrue(any(b"8_0_0" in p for p in writer.packets))
            writer.packets.clear()

            # 8. 服饰列表 (12_3_1, 12_3_2, 12_3_3)
            pkt_clothes = UplinkPacket(packet_num=9, msg_type=PacketType.EXT, action=10, room_id=1, cmd="12_3_1", payload={"cid": self.char_id})
            await gateway.dispatch_packet(session, pkt_clothes)
            await session.flush()
            self.assertTrue(any(b"12_3_1" in p for p in writer.packets))
            writer.packets.clear()

            pkt_wearing = UplinkPacket(packet_num=10, msg_type=PacketType.EXT, action=10, room_id=1, cmd="12_3_2", payload={"cid": self.char_id})
            await gateway.dispatch_packet(session, pkt_wearing)
            await session.flush()
            self.assertTrue(any(b"12_3_2" in p for p in writer.packets))
            writer.packets.clear()

            # 9. 战宠列表与单宠查询 (27_1, 27_200, 27_4)
            pkt_pets = UplinkPacket(packet_num=11, msg_type=PacketType.EXT, action=10, room_id=1, cmd="27_1", payload={})
            await gateway.dispatch_packet(session, pkt_pets)
            await session.flush()
            self.assertTrue(any(b"27_1" in p for p in writer.packets))
            writer.packets.clear()

            pkt_pet_batch = UplinkPacket(packet_num=12, msg_type=PacketType.EXT, action=10, room_id=1, cmd="27_200", payload={"petId": "1#2"})
            await gateway.dispatch_packet(session, pkt_pet_batch)
            await session.flush()
            self.assertTrue(any(b"27_200" in p for p in writer.packets))
            writer.packets.clear()

        asyncio.run(run_flow())


if __name__ == "__main__":
    unittest.main()
