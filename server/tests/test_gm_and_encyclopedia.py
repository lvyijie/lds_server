# -*- coding: utf-8 -*-
import unittest
import asyncio
from pathlib import Path
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from server.http_api import create_http_app
from server.encyclopedia import search_encyclopedia, get_mall_goods_info
from server.player_storage import load_player, update_player_basic, grant_item_to_player, clean_corrupted_data


class TestGMAndEncyclopedia(AioHTTPTestCase):
    async def get_application(self):
        return create_http_app()

    @unittest_run_loop
    async def test_gm_page_and_api(self):
        # 1. 测试 GM 网页访问
        resp = await self.client.request("GET", "/gm")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn("龙斗士 GM 管理后台", text)

        # 2. 测试获取玩家数据
        resp = await self.client.request("GET", "/api/gm/player?cid=88343134")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertIn("basic", data)
        self.assertEqual(data["char_id"], 88343134)

        # 3. 测试更新基础属性 (例如设为 100 级, 9999999 金币)
        update_payload = {
            "cid": 88343134,
            "level": 100,
            "coin": 9999999,
            "dragon_coin": 888888,
            "vip_level": 7,
        }
        resp = await self.client.request("POST", "/api/gm/player/update", json=update_payload)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])
        b = data["player"]["basic"]
        self.assertEqual(b["level"], 100)
        self.assertEqual(b["coin"], 9999999)
        self.assertEqual(b["dragon_coin"], 888888)
        self.assertEqual(b["vip_level"], 7)

        # 4. 测试图鉴检索接口
        resp = await self.client.request("GET", "/api/gm/encyclopedia?category=pet&keyword=波利&page=1&page_size=5")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertGreater(data["total"], 0)
        self.assertTrue(any("波利" in it["name"] for it in data["items"]))

        # 5. 测试一键发放物品
        grant_payload = {
            "cid": 88343134,
            "category": "stuff",
            "item_id": 1543,
            "count": 50
        }
        resp = await self.client.request("POST", "/api/gm/player/grant", json=grant_payload)
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])

        # 6. 测试清洗脏数据
        resp = await self.client.request("POST", "/api/gm/player/clean")
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertTrue(data["success"])

    @unittest_run_loop
    async def test_fight_pet_skin_route_serves_skin_asset_not_view_asset(self):
        skin_path = (
            Path(__file__).resolve().parents[2]
            / "07-原始SWF"
            / "宠物fightpetskin"
            / "f100494~1000.swf"
        )
        resp = await self.client.request(
            "GET",
            "/play/fightpetskin/f100494~1000.swf",
        )

        self.assertEqual(resp.status, 200)
        self.assertEqual(await resp.read(), skin_path.read_bytes())

    def test_encyclopedia_and_mall_lookup(self):
        info = get_mall_goods_info("2300166")
        self.assertIsNotNone(info)
        self.assertEqual(info["id"], 2300166)
        self.assertIn("1543", info["bonus"])


if __name__ == "__main__":
    unittest.main()
