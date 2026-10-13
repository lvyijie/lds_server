import unittest
import os
import json
from server.player_storage import (
    load_player,
    save_player,
    reset_player,
    update_task,
    complete_tutorial,
    is_tutorial_completed,
    update_position,
    update_basic,
    add_bag_item,
    add_equip,
    set_pet,
    build_http_char_node,
    build_tcp_2301_entity,
    get_player_file,
)


class TestPlayerStorage(unittest.TestCase):

    def setUp(self):
        # 使用测试专属 char_id
        self.test_cid = 99999999
        self.filepath = get_player_file(self.test_cid)
        if os.path.exists(self.filepath):
            os.remove(self.filepath)

    def tearDown(self):
        if os.path.exists(self.filepath):
            os.remove(self.filepath)

    def test_default_creation(self):
        p = load_player(self.test_cid)
        self.assertEqual(p["basic"]["char_id"], self.test_cid)
        self.assertEqual(p["basic"]["level"], 1)
        self.assertEqual(p["basic"]["job"], 1)
        self.assertIn("shenyuanroad", p["position"]["last_room"])
        self.assertFalse(is_tutorial_completed(self.test_cid))

    def test_http_char_node(self):
        node = build_http_char_node(self.test_cid)
        self.assertIn(f'<char cid="{self.test_cid}">', node)
        self.assertIn(f'<cid>{self.test_cid}</cid>', node)
        self.assertIn('<lv>1</lv>', node)
        self.assertIn('<j>1</j>', node)
        self.assertIn('<cloth>100011,100012,100013,100081,100151,600001</cloth>', node)
        self.assertIn('<equip>400002</equip>', node)
        self.assertIn('<lr>shenyuanroad', node)

    def test_task_progression_and_completion(self):
        # 推进到 513_1
        update_task(self.test_cid, 513, step=1, st=0, ti="513_1")
        p = load_player(self.test_cid)
        self.assertEqual(p["tasks"]["513"]["step"], 1)
        self.assertEqual(p["tasks"]["513"]["st"], 0)
        self.assertFalse(is_tutorial_completed(self.test_cid))

        # 完成 513
        complete_tutorial(self.test_cid, target_room="momomaintown_1")
        self.assertTrue(is_tutorial_completed(self.test_cid))
        p = load_player(self.test_cid)
        self.assertEqual(p["tasks"]["513"]["st"], 1)
        self.assertEqual(p["position"]["last_room"], "momomaintown_1")

        # 检查 HTTP 节点反映最新 last_room
        node = build_http_char_node(self.test_cid)
        self.assertIn('<lr>momomaintown_1</lr>', node)

    def test_bag_and_pet(self):
        add_bag_item(self.test_cid, mid=100004, count=1, t=7)
        add_equip(self.test_cid, equip_id=110001)
        set_pet(self.test_cid, pti=100408, name="小火狮")

        p = load_player(self.test_cid)
        self.assertEqual(len(p["bag"]["items"]), 1)
        self.assertEqual(p["bag"]["items"][0]["mid"], 100004)
        self.assertIn(110001, p["bag"]["equip"])
        self.assertIsNotNone(p["pet"])
        self.assertEqual(p["pet"]["pti"], 100408)

        # 检查 TCP 2301 实体
        ent = build_tcp_2301_entity(self.test_cid)
        self.assertEqual(ent["ui"], self.test_cid)
        self.assertIn("pet", ent)
        self.assertEqual(ent["pet"]["pti"], 100408)


if __name__ == "__main__":
    unittest.main()
