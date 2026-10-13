import asyncio
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from server import player_storage
from server.dungeon_engine import DUNGEON_DEFINES, DungeonInstance, remove_dungeon
from server.gateway import ClientSession, GameGateway, format_pet_for_client
from server.protocol.framing import PacketType, UplinkPacket


class TestInventoryProgressionFixes(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.player_path = os.path.join(self.temp_dir.name, "player.json")
        self.storage_path_patch = patch(
            "server.player_storage.get_player_file",
            side_effect=lambda _char_id: self.player_path,
        )
        self.storage_path_patch.start()
        self.addCleanup(self.storage_path_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)
        self.char_id = 987654321
        player_storage.reset_player(self.char_id)

    def make_session(self):
        writer = MagicMock()
        writer.get_extra_info.return_value = ("127.0.0.1", 12345)
        session = ClientSession(MagicMock(), writer)
        session.char_id = self.char_id
        sent = []
        session.send_ext = sent.append
        return session, sent

    def make_packet(self, cmd, payload=None):
        return UplinkPacket(
            packet_num=1,
            msg_type=PacketType.EXT,
            action=10033,
            room_id=304448,
            cmd=cmd,
            payload=payload or {},
        )

    def test_backpack_equipment_instances_and_durability(self):
        player = player_storage.load_player(self.char_id)
        mid = 112031
        player["bag"]["items"] = [
            {"gid": 41, "mid": mid, "t": 0, "n": 1, "pl": 100},
            {"gid": 42, "mid": mid, "t": 0, "n": 1, "pl": 100},
        ]
        player["bag"]["equip"] = [mid]
        player_storage.save_player(player, char_id=self.char_id)

        items = player_storage.get_player_backpack(self.char_id)
        equipped = [item for item in items if 1 <= item["pl"] <= 15]
        self.assertEqual([item["gid"] for item in equipped], [41])
        self.assertEqual(items[0]["ce"], player_storage.GLOBAL_EQUIP_CONFIG[mid]["endurance"])
        saved = player_storage.load_player(self.char_id)
        self.assertEqual(saved["bag"]["equip"], [mid])
        self.assertEqual([item["pl"] for item in saved["bag"]["items"]], [
            player_storage.GLOBAL_EQUIP_CONFIG[mid]["position"],
            100,
        ])

    def test_new_player_does_not_start_with_seeded_endgame_equipment(self):
        player = player_storage.load_player(self.char_id)

        self.assertEqual(player["bag"]["equip"], [])
        self.assertEqual(player["bag"]["items"], [])
        self.assertEqual(player["basic"]["csp"], player["basic"]["msp"])

    def test_legacy_seeded_equipment_is_preserved_but_unequipped(self):
        player = player_storage.load_player(self.char_id)
        seeded_ids = sorted(player_storage.SEEDED_STARTER_EQUIPMENT)
        player["bag"]["items"] = [
            {
                "gid": 90 + index,
                "mid": mid,
                "t": 0,
                "n": 1,
                "pl": player_storage.GLOBAL_EQUIP_CONFIG[mid]["position"],
            }
            for index, mid in enumerate(seeded_ids)
        ]
        player["bag"]["equip"] = seeded_ids
        player_storage.save_player(player, char_id=self.char_id)

        saved = player_storage.load_player(self.char_id)

        self.assertEqual(
            {item["mid"] for item in saved["bag"]["items"]},
            player_storage.SEEDED_STARTER_EQUIPMENT,
        )
        self.assertTrue(all(item["pl"] == 100 for item in saved["bag"]["items"]))
        self.assertEqual(saved["bag"]["equip"], [])

    def test_tutorial_gift_adds_level_one_weapon_as_equipment(self):
        session, sent = self.make_session()
        gateway = GameGateway()

        asyncio.run(gateway.dispatch_packet(
            session,
            self.make_packet("17_2", {"id": 1657}),
        ))

        player = player_storage.load_player(self.char_id)
        weapon = next(item for item in player["bag"]["items"] if item["mid"] == 100001)
        self.assertEqual(weapon["t"], 0)
        self.assertEqual(weapon["pl"], 100)
        self.assertEqual([packet["_cmd"] for packet in sent], ["8_0", "17_2"])
        self.assertEqual(sent[-1]["bo"], "0:100001:1")

    def test_tutorial_pet_gift_is_stored_as_a_pet(self):
        session, _ = self.make_session()

        asyncio.run(GameGateway().dispatch_packet(
            session,
            self.make_packet("17_2", {"id": 1656}),
        ))

        player = player_storage.load_player(self.char_id)
        self.assertIn(100140, [pet["pti"] for pet in player["pets"]])
        self.assertNotIn(100140, [item["mid"] for item in player["bag"]["items"]])

    def test_equipment_level_requirement_is_enforced_by_server(self):
        session, sent = self.make_session()
        gateway = GameGateway()
        item = player_storage.add_bag_item(
            self.char_id,
            mid=100004,
            count=1,
            t=0,
        )

        asyncio.run(gateway.dispatch_packet(
            session,
            self.make_packet("8_0_1", {"gid": item["gid"]}),
        ))

        self.assertEqual(sent[-1]["_cmd"], "8_0_1")
        self.assertEqual(sent[-1]["res"], 15)
        saved = player_storage.load_player(self.char_id)
        self.assertEqual(saved["bag"]["items"][0]["pl"], 100)

    def test_level_one_character_can_equip_level_one_weapon(self):
        session, sent = self.make_session()
        gateway = GameGateway()
        item = player_storage.add_bag_item(
            self.char_id,
            mid=100001,
            count=1,
            t=0,
        )

        asyncio.run(gateway.dispatch_packet(
            session,
            self.make_packet("8_0_1", {"gid": item["gid"]}),
        ))

        self.assertEqual(sent[-1]["_cmd"], "8_0_1")
        self.assertEqual(sent[-1]["res"], 1)

    def test_consumable_requires_matching_official_material_definition(self):
        potion = player_storage.add_bag_item(
            self.char_id,
            mid=1001,
            count=2,
            t=2,
        )

        self.assertEqual(potion["t"], 2)
        self.assertEqual(potion["pl"], 102)
        self.assertEqual(potion["n"], 2)
        backpack_item = next(
            item for item in player_storage.get_player_backpack(self.char_id)
            if item["gid"] == potion["gid"]
        )
        self.assertEqual(backpack_item["t"], 2)
        self.assertEqual(backpack_item["pl"], 102)
        with self.assertRaisesRegex(ValueError, "Unknown inventory item type/id pair"):
            player_storage.add_bag_item(self.char_id, mid=1519, count=1, t=2)

    def test_ambiguous_inventory_id_requires_explicit_client_type(self):
        with self.assertRaisesRegex(ValueError, "Ambiguous inventory item"):
            player_storage.add_bag_item(self.char_id, mid=100004, count=1)

        mount = player_storage.add_bag_item(
            self.char_id, mid=100004, count=1, t=7
        )
        self.assertEqual(mount["t"], 7)

    def test_inventory_material_types_match_every_loaded_client_catalog(self):
        for material_type, item_ids in player_storage.GLOBAL_INVENTORY_CATALOGS.items():
            for item_id in item_ids:
                self.assertEqual(
                    player_storage.resolve_inventory_type(item_id, material_type),
                    material_type,
                    f"bad catalog pair t={material_type}, mid={item_id}",
                )
        self.assertEqual(player_storage.resolve_inventory_type(1519), 4)
        self.assertEqual(player_storage.resolve_inventory_type(1001, 2), 2)
        with self.assertRaisesRegex(ValueError, "Unknown inventory item"):
            player_storage.add_bag_item(self.char_id, mid=12018, count=1)

    def test_stuff_item_type_is_preserved_and_legacy_consument_misclassification_is_repaired(self):
        player = player_storage.load_player(self.char_id)
        player["bag"]["items"] = [
            {"gid": 1, "mid": 1519, "t": 2, "n": 1, "pl": 102},
            {"gid": 2, "mid": 2076, "t": 2, "n": 1, "pl": 102},
        ]
        player_storage.save_player(player, char_id=self.char_id)

        repaired = player_storage.load_player(self.char_id)
        self.assertEqual(
            [(item["mid"], item["t"]) for item in repaired["bag"]["items"]],
            [(1519, 4), (2076, 4)],
        )
        self.assertEqual(
            [(item["mid"], item["t"]) for item in player_storage.get_player_backpack(self.char_id)],
            [(1519, 4), (2076, 4)],
        )

    def test_legacy_pet_bonus_and_temp_score_rows_do_not_enter_material_list(self):
        player = player_storage.load_player(self.char_id)
        player["bag"]["items"] = [
            {"gid": 1, "mid": 100335, "t": 2, "n": 1, "pl": 102},
            {"gid": 2, "mid": 100375, "t": 2, "n": 1, "pl": 102},
            {"gid": 3, "mid": 4, "t": 2, "n": 300000, "pl": 102},
        ]
        player_storage.save_player(player, char_id=self.char_id)

        repaired = player_storage.load_player(self.char_id)
        self.assertEqual(repaired["bag"]["items"], [])
        self.assertTrue(
            {100335, 100375}.issubset(
                {pet["pti"] for pet in repaired["pets"]}
            )
        )
        self.assertEqual(
            [(item["mid"], item["n"]) for item in repaired["orphaned_inventory"]],
            [(4, 300000)],
        )

    def test_wearing_clothes_do_not_replace_owned_clothes(self):
        initial_worn = player_storage.get_wearing_clothes(self.char_id)
        owned = player_storage.get_owned_clothes(self.char_id)
        player_storage.add_cloth(self.char_id, 100999)

        changed = player_storage.update_clothes(self.char_id, "100999")

        self.assertEqual(changed, [100999])
        self.assertEqual(
            player_storage.get_wearing_clothes(self.char_id),
            initial_worn + [100999],
        )
        self.assertEqual(
            player_storage.get_owned_clothes(self.char_id),
            owned + [100999],
        )
        self.assertEqual(player_storage.update_clothes(self.char_id, "100999"), [100999])
        self.assertEqual(
            player_storage.get_wearing_clothes(self.char_id),
            initial_worn,
        )
        self.assertEqual(player_storage.update_clothes(self.char_id, ""), [])

    def test_new_player_only_has_base_actions_and_no_skill_points_spent(self):
        skills = player_storage.get_skills(self.char_id)["list"]

        self.assertEqual(
            [skill["id"] for skill in skills],
            player_storage.JOB_DEFAULT_SKILLS[1][:3],
        )
        self.assertTrue(all(skill["ck0"] == 1 for skill in skills))
        self.assertTrue(all(skill["ck1"] == 0 for skill in skills))

    def test_legacy_all_skills_seed_is_reduced_without_touching_real_allocations(self):
        player = player_storage.load_player(self.char_id)
        player["skills"]["list"] = [
            {"id": skill_id, "ck0": 1, "ck1": 1, "ck2": 0, "ck3": 0}
            for skill_id in player_storage.JOB_DEFAULT_SKILLS[1]
        ]
        player_storage.save_player(player, char_id=self.char_id)

        skills = player_storage.load_player(self.char_id)["skills"]["list"]

        self.assertEqual(
            [skill["id"] for skill in skills],
            player_storage.JOB_DEFAULT_SKILLS[1][:3],
        )
        self.assertTrue(all(skill["ck1"] == 0 for skill in skills))

    def test_pet_protocol_has_fight_pet_type_and_real_template_id(self):
        packet_pet = format_pet_for_client(
            {"pi": 1, "pti": 100408, "n": "亚克", "lv": 1},
            self.char_id,
        )

        self.assertEqual(packet_pet["t"], 1)
        self.assertEqual(packet_pet["bot"], 4)
        self.assertEqual(packet_pet["pti"], 100408)
        self.assertEqual(packet_pet["oci"], float(self.char_id))
        self.assertNotEqual(packet_pet["ci"], 0)

    def test_pet_treasure_preserves_follow_id_and_only_selects_available_skin(self):
        session, sent = self.make_session()
        choices = []

        def choose_pet(candidates):
            choices.append(candidates)
            return candidates[0]

        with patch("server.gateway.random.choice", side_effect=choose_pet):
            asyncio.run(GameGateway().dispatch_packet(
                session,
                self.make_packet("25_1157_1", {"t": 0}),
            ))

        new_pet_packet = next(packet for packet in sent if packet.get("_cmd") == "27_3")
        self.assertEqual(new_pet_packet["f"], 1)
        self.assertNotIn(100517, choices[0])

    def test_follow_pet_response_includes_current_battlefield(self):
        session, sent = self.make_session()
        session.current_room_id = 304448
        pet = player_storage.add_pet(
            self.char_id,
            pti=100496,
            lv=100,
            ecs=5,
            allow_duplicate=True,
        )

        asyncio.run(GameGateway().dispatch_packet(
            session,
            self.make_packet("27_5", {"petId": pet["pi"]}),
        ))

        follow_response = next(packet for packet in sent if packet.get("_cmd") == "27_5")
        self.assertEqual(follow_response["bi"], 304448)
        self.assertEqual(follow_response["f"], pet["pi"])
        self.assertEqual(follow_response["pet"]["pti"], 100496)

    def test_pet_list_sequence_only_references_returned_unique_pets(self):
        player = player_storage.load_player(self.char_id)
        player["pets"] = [
            {"pi": 1, "pti": 100408, "n": "亚克"},
            {"pi": 2, "pti": 100494, "n": "暗灭帝尊"},
            {"pi": 2, "pti": 100496, "n": "重复编号"},
        ]
        player["follow_pet_id"] = 99
        player_storage.save_player(player, char_id=self.char_id)
        session, sent = self.make_session()

        asyncio.run(GameGateway().dispatch_packet(
            session,
            self.make_packet("27_1"),
        ))

        response = next(packet for packet in sent if packet.get("_cmd") == "27_1")
        returned_ids = {pet["pi"] for pet in response["pl"]}
        sequence_ids = [int(pet_id) for pet_id in response["s"].split("#")]
        self.assertEqual(returned_ids, {1, 2})
        self.assertEqual(set(sequence_ids), returned_ids)
        self.assertEqual(len(sequence_ids), len(set(sequence_ids)))
        self.assertEqual(response["f"], 1)

    def test_pet_sequence_update_returns_complete_ordered_pet_list(self):
        player = player_storage.load_player(self.char_id)
        player["pets"] = [
            {"pi": 1, "pti": 100408, "n": "亚克", "lv": 1, "ecs": 0},
            {"pi": 2, "pti": 100494, "n": "暗灭帝尊", "lv": 100, "ecs": 5},
        ]
        player_storage.save_player(player, char_id=self.char_id)
        session, sent = self.make_session()

        asyncio.run(GameGateway().dispatch_packet(
            session,
            self.make_packet("27_7", {"s": "2#1"}),
        ))

        response = next(packet for packet in sent if packet.get("_cmd") == "27_7")
        self.assertEqual(response["s"], "2#1")
        self.assertEqual(
            [pet["pi"] for pet in response["pl"]],
            [2, 1],
        )
        self.assertEqual(
            [pet["pti"] for pet in response["pl"]],
            [100494, 100408],
        )

    def test_property_packets_preserve_current_stamina_and_stamina_attribute(self):
        player = player_storage.load_player(self.char_id)
        player["basic"]["csp"] = 37
        player["basic"]["msp"] = 100
        player_storage.save_player(player, char_id=self.char_id)
        session, sent = self.make_session()
        session.send_props_update()
        self.assertEqual(sent[-1]["csp"], 37)
        self.assertEqual(sent[-1]["msp"], 100)

        asyncio.run(GameGateway().dispatch_packet(
            session,
            self.make_packet("8_1_1"),
        ))
        detail = next(packet for packet in sent if packet.get("_cmd") == "8_1_1")
        self.assertIn("stm", detail)
        self.assertEqual(detail["csp"], 37)
        self.assertEqual(detail["msp"], 100)

    def test_backpack_completion_and_dynamic_clothes_response(self):
        session, sent = self.make_session()
        gateway = GameGateway()

        asyncio.run(gateway.dispatch_packet(session, self.make_packet("8_0_0")))
        self.assertTrue(sent[-1]["ov"])

        player_storage.add_cloth(self.char_id, 100999)
        player_storage.update_clothes(self.char_id, "100011")
        asyncio.run(gateway.dispatch_packet(session, self.make_packet("12_3_1")))
        clothes_packet = sent[-1]
        self.assertIn("100999_0_", clothes_packet["res"])
        self.assertIn("100011_1_", clothes_packet["res"])

    def test_mall_purchase_adds_catalog_rewards_not_exp_as_quantity(self):
        session, sent = self.make_session()
        gateway = GameGateway()
        with patch(
            "server.gateway.get_mall_goods_info",
            return_value={"price": 5, "bonus": "2:1001:2#1:100999:1"},
        ):
            asyncio.run(gateway.dispatch_packet(
                session,
                self.make_packet("21_1", {"id": 80001, "exp": 3}),
            ))

        player = player_storage.load_player(self.char_id)
        consumable = next(
            item for item in player["bag"]["items"] if item["mid"] == 1001
        )
        self.assertEqual(consumable["n"], 2)
        self.assertIn(100999, player["bag"]["cloth"])
        self.assertEqual(sent[-1]["cnt"], 1)

    def test_mall_legacy_reward_type_is_inferred_from_official_catalog(self):
        session, sent = self.make_session()
        with patch(
            "server.gateway.get_mall_goods_info",
            return_value={"price": 5, "bonus": "1519:2"},
        ):
            asyncio.run(GameGateway().dispatch_packet(
                session,
                self.make_packet("21_2", {"id": 80004}),
            ))

        player = player_storage.load_player(self.char_id)
        self.assertEqual(
            [(item["mid"], item["t"], item["n"]) for item in player["bag"]["items"]],
            [(1519, 4, 2)],
        )
        self.assertEqual(sent[-1]["it"], "4:1519:2")

    def test_mall_invalid_reward_rejects_purchase_before_any_grant(self):
        session, sent = self.make_session()
        with patch(
            "server.gateway.get_mall_goods_info",
            return_value={"price": 5, "bonus": "2:1001:2#2:9999999:1"},
        ):
            asyncio.run(GameGateway().dispatch_packet(
                session,
                self.make_packet("21_2", {"id": 80005}),
            ))

        player = player_storage.load_player(self.char_id)
        self.assertEqual(player["bag"]["items"], [])
        self.assertEqual(player["basic"]["dragon_coin"], 999999)
        self.assertEqual(sent, [{"_cmd": "21_2", "res": 1, "r": 1}])

    def test_mall_clothing_purchase_updates_wardrobe_and_clothes_service(self):
        session, sent = self.make_session()
        with patch(
            "server.gateway.get_mall_goods_info",
            return_value={"price": 5, "bonus": "11:100241:7"},
        ):
            asyncio.run(GameGateway().dispatch_packet(
                session,
                self.make_packet("21_2", {"id": 80002}),
            ))

        player = player_storage.load_player(self.char_id)
        self.assertIn(100241, player["bag"]["cloth"])
        clothing_delta = next(
            packet for packet in sent
            if packet.get("_cmd") == "8_0" and packet.get("up")
        )
        clothing = clothing_delta["up"][0]
        self.assertEqual(clothing["t"], 1)
        self.assertEqual(clothing["gid"], 100241)
        self.assertGreater(clothing["exp"], 0)
        self.assertGreater(
            player_storage.get_cloth_expiry(self.char_id, 100241),
            0,
        )
        asyncio.run(GameGateway().dispatch_packet(
            session,
            self.make_packet("12_3_1"),
        ))
        self.assertIn("100241_0_", sent[-1]["res"])

    def test_mall_pet_star_and_pet_experience_rewards_are_not_bag_materials(self):
        session, sent = self.make_session()
        with patch(
            "server.gateway.get_mall_goods_info",
            return_value={
                "price": 5,
                "bonus": "117:100375:1#124:4:300000#4:1519:2",
            },
        ):
            asyncio.run(GameGateway().dispatch_packet(
                session,
                self.make_packet("21_2", {"id": 80003}),
            ))

        player = player_storage.load_player(self.char_id)
        self.assertIn(100375, [pet["pti"] for pet in player["pets"]])
        self.assertEqual(
            [(item["mid"], item["t"], item["n"]) for item in player["bag"]["items"]],
            [(1519, 4, 2)],
        )
        self.assertTrue(any(packet.get("_cmd") == "27_3" for packet in sent))

    def make_enhanceable_equipment(self):
        mid = next(
            mid
            for mid, meta in player_storage.GLOBAL_EQUIP_CONFIG.items()
            if int(meta.get("strLvLimit", 0)) > 0
        )
        player = player_storage.load_player(self.char_id)
        player["basic"]["coin"] = 500
        player_storage.save_player(player, char_id=self.char_id)
        item = player_storage.add_bag_item(self.char_id, mid=mid)
        return item, int(player_storage.GLOBAL_EQUIP_CONFIG[mid]["strLvLimit"])

    def test_nuolan_dungeon_uses_scene_names_from_official_scene_config(self):
        self.assertEqual(
            DUNGEON_DEFINES[14]["scenes"],
            ["fubennbuolanone", "fubennbuolantwo", "fubennbuolanthree"],
        )

    def test_dungeon_monster_packet_uses_client_speed_field(self):
        packet = DungeonInstance(char_id=1, fb_id=10, fb_lv=2).spawn_monsters()[0]

        self.assertEqual(packet["_cmd"], "2302")
        self.assertEqual(packet["bot"], 2)
        self.assertEqual(packet["s"], 8)
        self.assertNotIn("speed", packet)

    def test_equipment_treasure_chest_rewards_match_player_occupation(self):
        session, sent = self.make_session()

        with patch("server.gateway.random.choice", side_effect=lambda choices: choices[0]):
            asyncio.run(GameGateway().dispatch_packet(
                session,
                self.make_packet("25_1157_1", {"t": 1}),
            ))

        reward_id = int(sent[-1]["e"].split(":")[1])
        reward = next(
            item for item in player_storage.get_player_backpack(self.char_id)
            if int(item["mid"]) == reward_id
        )
        equipment = player_storage.GLOBAL_EQUIP_CONFIG[reward_id]
        self.assertEqual(reward["t"], 0)
        self.assertTrue(reward["cb"])
        self.assertIn("1", str(equipment["job"]).split(","))
        self.assertGreater(equipment["strLvLimit"], 0)
        self.assertGreaterEqual(equipment["quality"], 5)

    def test_strengthening_updates_equipment_and_charges_fixed_cost(self):
        session, sent = self.make_session()
        item, _limit = self.make_enhanceable_equipment()
        gateway = GameGateway()

        asyncio.run(gateway.dispatch_packet(
            session,
            self.make_packet("8_2_5", {"gid": item["gid"]}),
        ))
        condition = sent[-1]
        self.assertEqual(condition["_cmd"], "8_2_5")
        self.assertEqual(condition["gid"], item["gid"])
        self.assertEqual(condition["cd"], "100:0:100")

        asyncio.run(gateway.dispatch_packet(
            session,
            self.make_packet("8_2_26", {"gid": item["gid"], "li": 0}),
        ))
        self.assertEqual(sent[-1], {"_cmd": "8_2_26", "p": 80})

        with patch("server.gateway.random.random", return_value=0.0):
            asyncio.run(gateway.dispatch_packet(
                session,
                self.make_packet("8_2_6", {
                    "gi": item["gid"],
                    "pi": 0,
                    "li": 0,
                }),
            ))

        saved = player_storage.load_player(self.char_id)
        upgraded = next(
            row for row in saved["bag"]["items"]
            if row["gid"] == item["gid"]
        )
        self.assertEqual(upgraded["cl"], 1)
        self.assertEqual(saved["basic"]["coin"], 400)
        self.assertEqual(sent[-1], {"_cmd": "8_2_6", "res": 201, "st": 0})
        equipment_delta = next(
            packet for packet in sent
            if packet.get("_cmd") == "8_0"
        )
        self.assertEqual(
            equipment_delta["up"][0],
            {"gid": item["gid"], "mid": item["mid"], "cl": 1},
        )

    def test_strengthening_rejects_level_above_official_equipment_limit(self):
        session, sent = self.make_session()
        item, limit = self.make_enhanceable_equipment()
        player_storage.set_equipment_enhance_level(
            self.char_id,
            item["gid"],
            limit,
        )
        before_coin = player_storage.load_player(self.char_id)["basic"]["coin"]

        asyncio.run(GameGateway().dispatch_packet(
            session,
            self.make_packet("8_2_6", {"gi": item["gid"], "pi": 0, "li": 0}),
        ))

        after = player_storage.load_player(self.char_id)
        self.assertEqual(after["basic"]["coin"], before_coin)
        self.assertEqual(sent[-1], {"_cmd": "8_2_6", "res": 207, "st": 0})
        self.assertIsNone(
            player_storage.set_equipment_enhance_level(
                self.char_id,
                item["gid"],
                limit + 1,
            )
        )

    def test_strengthening_failure_keeps_level_and_charges_fixed_cost(self):
        session, sent = self.make_session()
        item, _limit = self.make_enhanceable_equipment()

        with patch("server.gateway.random.random", return_value=0.99):
            asyncio.run(GameGateway().dispatch_packet(
                session,
                self.make_packet("8_2_6", {"gi": item["gid"], "pi": 0, "li": 0}),
            ))

        saved = player_storage.load_player(self.char_id)
        unchanged = next(
            row for row in saved["bag"]["items"]
            if row["gid"] == item["gid"]
        )
        self.assertEqual(unchanged["cl"], 0)
        self.assertEqual(saved["basic"]["coin"], 400)
        self.assertEqual(sent[-1], {"_cmd": "8_2_6", "res": 202, "st": 0})

    def test_dungeon_finish_packet_grants_exp_and_level_up(self):
        session, sent = self.make_session()
        remove_dungeon(self.char_id)

        asyncio.run(GameGateway().dispatch_packet(
            session,
            self.make_packet("15_4"),
        ))

        saved = player_storage.load_player(self.char_id)
        self.assertGreater(saved["basic"]["level"], 1)
        self.assertGreater(saved["basic"]["total_exp"], 0)
        self.assertIn("2143", [packet["_cmd"] for packet in sent])

    def test_experience_reward_updates_level_and_sends_level_event(self):
        session, sent = self.make_session()

        session.grant_dungeon_reward(2000, 10)

        basic = player_storage.load_player(self.char_id)["basic"]
        self.assertEqual(basic["level"], 2)
        self.assertEqual(basic["exp"], 500)
        self.assertEqual(basic["total_exp"], 2000)
        self.assertEqual([packet["_cmd"] for packet in sent], ["2141", "2143"])
        self.assertEqual(sent[-1]["lv"], 2)

    def test_official_health_potion_is_consumed_using_its_catalog_type(self):
        session, sent = self.make_session()
        player = player_storage.load_player(self.char_id)
        player["bag"]["items"] = [
            {"gid": 71, "mid": 1001, "t": 2, "n": 1, "pl": 102},
        ]
        player_storage.save_player(player, char_id=self.char_id)

        gateway = GameGateway()
        asyncio.run(gateway.dispatch_packet(
            session,
            self.make_packet("8_0_3", {"gid": 71, "n": 99}),
        ))

        saved = player_storage.load_player(self.char_id)
        self.assertEqual(saved["basic"]["chp"], 800)
        self.assertFalse(saved["bag"]["items"])
        self.assertEqual(
            [packet["_cmd"] for packet in sent],
            ["8_0_3", "8_0", "2131"],
        )

    def test_invalid_consumable_type_is_not_used_or_deleted(self):
        session, sent = self.make_session()
        player = player_storage.load_player(self.char_id)
        player["bag"]["items"] = [
            {"gid": 71, "mid": 1519, "t": 4, "n": 1, "pl": 102},
        ]
        player_storage.save_player(player, char_id=self.char_id)

        asyncio.run(GameGateway().dispatch_packet(
            session,
            self.make_packet("8_0_3", {"gid": 71, "n": 1}),
        ))

        saved = player_storage.load_player(self.char_id)
        self.assertEqual(saved["bag"]["items"][0]["mid"], 1519)
        self.assertEqual(sent, [{"_cmd": "8_0_3", "res": 0, "pa": ""}])


if __name__ == "__main__":
    unittest.main()
