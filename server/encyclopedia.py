# -*- coding: utf-8 -*-
"""
游戏原生资源图鉴解析与索引模块 (server/encyclopedia.py)
解析 04-数据JSON/游戏数据 下的原生 XML / 分号数据表:
- 装备: EquipmentDatas_dataClass.xml
- 服饰: ClothesDatas_dataClass.xml
- 战宠: PetData_petDefineClass.xml
- 消耗品: ConsumentDatas_dataClass.xml
- 材料: StuffItemDatas_dataClass.xml
- 商城: MallGoodsData_mallGoodsClass.xml
"""

import os
import re
import logging
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Any

logger = logging.getLogger("Encyclopedia")

# 项目根目录与数据目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "04-数据JSON", "游戏数据")

# 缓存
_INITIALIZED = False
_EQUIPMENTS: List[Dict[str, Any]] = []
_CLOTHES: List[Dict[str, Any]] = []
_PETS: List[Dict[str, Any]] = []
_CONSUMES: List[Dict[str, Any]] = []
_STUFFS: List[Dict[str, Any]] = []
_MALL_GOODS: List[Dict[str, Any]] = []
_MALL_GOODS_MAP: Dict[str, Dict[str, Any]] = {}


def _find_file_recursively(filename: str) -> Optional[str]:
    """在游戏数据目录中递归查找文件"""
    if not os.path.exists(DATA_DIR):
        return None
    for root, _, files in os.walk(DATA_DIR):
        if filename in files:
            return os.path.join(root, filename)
    return None


def _load_equipments():
    """解析装备数据 (分号文本)"""
    global _EQUIPMENTS
    _EQUIPMENTS = []
    filepath = _find_file_recursively("EquipmentDatas_dataClass.xml")
    if not filepath or not os.path.exists(filepath):
        logger.warning("未找到 EquipmentDatas_dataClass.xml")
        return

    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(";")
                if len(parts) >= 5:
                    try:
                        eq_id = int(parts[0])
                        name = parts[1].strip()
                        kind = int(parts[2]) if parts[2].isdigit() else 0
                        level = int(parts[3]) if parts[3].isdigit() else 1
                        quality = int(parts[4]) if parts[4].isdigit() else 1
                        job = parts[6].strip() if len(parts) > 6 else ""
                        desc = parts[14].strip() if len(parts) > 14 else ""
                        _EQUIPMENTS.append({
                            "id": eq_id,
                            "name": name,
                            "category": "equip",
                            "category_name": "装备",
                            "kind": kind,
                            "level": level,
                            "quality": quality,
                            "job": job,
                            "desc": desc or f"穿戴等级: {level}级",
                        })
                    except Exception:
                        pass
        logger.info("成功加载装备图鉴: %d 件", len(_EQUIPMENTS))
    except Exception as e:
        logger.error("加载装备图鉴失败: %s", e)


def _load_clothes():
    """解析服饰数据 (XML)"""
    global _CLOTHES
    _CLOTHES = []
    filepath = _find_file_recursively("ClothesDatas_dataClass.xml")
    if not filepath or not os.path.exists(filepath):
        logger.warning("未找到 ClothesDatas_dataClass.xml")
        return

    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        root = ET.fromstring(content)
        for item in root.iter("clothes"):
            cid = item.get("id")
            if not cid:
                continue
            name = item.get("name", "").strip()
            if not name:
                continue
            _CLOTHES.append({
                "id": int(cid),
                "name": name,
                "category": "cloth",
                "category_name": "时装/服饰",
                "type": item.get("type", "1"),
                "level": int(item.get("level", 1)),
                "quality": int(item.get("quality", 1)),
                "job": item.get("job", ""),
                "desc": item.get("desc", "").strip() or f"时装部件 (ID: {cid})",
            })
        logger.info("成功加载时装图鉴: %d 件", len(_CLOTHES))
    except Exception as e:
        logger.error("加载时装图鉴失败: %s", e)


def _load_pets():
    """解析战宠数据 (XML)"""
    global _PETS
    _PETS = []
    filepath = _find_file_recursively("PetData_petDefineClass.xml")
    if not filepath or not os.path.exists(filepath):
        logger.warning("未找到 PetData_petDefineClass.xml")
        return

    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        root = ET.fromstring(content)
        for item in root.findall("pet"):
            pid = item.get("id")
            if not pid:
                continue
            name = item.get("name", "").strip()
            if not name:
                continue
            des = item.get("des", "").strip()
            evo = item.get("evo", "").strip()
            final_id = item.get("finalevolution", pid)
            _PETS.append({
                "id": int(pid),
                "name": name,
                "category": "pet",
                "category_name": "战宠",
                "level": 1,
                "quality": 5 if evo else 3,
                "evo": evo,
                "final_id": final_id,
                "desc": f"进化阶段: {evo} | {des}" if evo else des,
            })
        logger.info("成功加载战宠图鉴: %d 只", len(_PETS))
    except Exception as e:
        logger.error("加载战宠图鉴失败: %s", e)


def _load_consumes():
    """解析消耗品数据 (XML)"""
    global _CONSUMES
    _CONSUMES = []
    filepath = _find_file_recursively("ConsumentDatas_dataClass.xml")
    if not filepath or not os.path.exists(filepath):
        logger.warning("未找到 ConsumentDatas_dataClass.xml")
        return

    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        # 兼容 encoding="utf8" 语法报错
        content = re.sub(r'encoding=[\'"]utf8[\'"]', 'encoding="utf-8"', content, flags=re.IGNORECASE)
        root = ET.fromstring(content)
        for row in root.findall(".//row"):
            cid = row.findtext("ConsumeId")
            name = row.findtext("Name")
            if not cid or not name:
                continue
            desc = row.findtext("Des", "")
            lv = row.findtext("Level", "1")
            qa = row.findtext("Quality", "1")
            _CONSUMES.append({
                "id": int(cid),
                "name": name.strip(),
                "category": "consume",
                "category_name": "消耗品/药剂",
                "level": int(lv) if lv.isdigit() else 1,
                "quality": int(qa) if qa.isdigit() else 1,
                "desc": desc.strip() or "使用类消耗品",
            })
        logger.info("成功加载消耗品图鉴: %d 项", len(_CONSUMES))
    except Exception as e:
        logger.error("加载消耗品图鉴失败: %s", e)


def _load_stuffs():
    """解析材料数据 (XML)"""
    global _STUFFS
    _STUFFS = []
    filepath = _find_file_recursively("StuffItemDatas_dataClass.xml")
    if not filepath or not os.path.exists(filepath):
        logger.warning("未找到 StuffItemDatas_dataClass.xml")
        return

    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
        content = re.sub(r'encoding=[\'"]utf8[\'"]', 'encoding="utf-8"', content, flags=re.IGNORECASE)
        root = ET.fromstring(content)
        for row in root.findall(".//row"):
            sid = row.findtext("StuffId")
            name = row.findtext("Name")
            if not sid or not name:
                continue
            desc = row.findtext("Des", "")
            lv = row.findtext("Level", "1")
            qa = row.findtext("Quality", "1")
            _STUFFS.append({
                "id": int(sid),
                "name": name.strip(),
                "category": "stuff",
                "category_name": "材料/道具",
                "level": int(lv) if lv.isdigit() else 1,
                "quality": int(qa) if qa.isdigit() else 1,
                "desc": desc.strip() or "任务与强化所需材料",
            })
        logger.info("成功加载材料图鉴: %d 项", len(_STUFFS))
    except Exception as e:
        logger.error("加载材料图鉴失败: %s", e)


def _load_mall_goods():
    """解析商城商品数据 (分号文本)"""
    global _MALL_GOODS, _MALL_GOODS_MAP
    _MALL_GOODS = []
    _MALL_GOODS_MAP = {}
    filepath = _find_file_recursively("MallGoodsData_mallGoodsClass.xml")
    if not filepath or not os.path.exists(filepath):
        logger.warning("未找到 MallGoodsData_mallGoodsClass.xml")
        return

    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(";")
                if len(parts) >= 8:
                    try:
                        gid = parts[0].strip()
                        bonus = parts[1].strip()
                        name = parts[4].strip()
                        price_rmb = int(parts[5]) if parts[5].isdigit() else 0
                        price_gold = int(parts[6]) if parts[6].isdigit() else 0
                        price_vip = int(parts[7]) if parts[7].isdigit() else 0
                        desc = parts[26].strip() if len(parts) > 26 else ""

                        item_obj = {
                            "id": int(gid),
                            "name": name,
                            "category": "mall",
                            "category_name": "商城商品",
                            "bonus": bonus,
                            "price": price_rmb or price_gold or price_vip or 10,
                            "price_rmb": price_rmb,
                            "price_gold": price_gold,
                            "price_vip": price_vip,
                            "desc": desc or f"内含奖励: {bonus}",
                        }
                        _MALL_GOODS.append(item_obj)
                        _MALL_GOODS_MAP[gid] = item_obj
                    except Exception:
                        pass
        logger.info("成功加载商城商品: %d 件", len(_MALL_GOODS))
    except Exception as e:
        logger.error("加载商城商品失败: %s", e)


def init_encyclopedia():
    """启动时初始化全部图鉴与索引"""
    global _INITIALIZED
    if _INITIALIZED:
        return
    logger.info("正在初始化游戏原生资源图鉴索引...")
    _load_equipments()
    _load_clothes()
    _load_pets()
    _load_consumes()
    _load_stuffs()
    _load_mall_goods()
    _INITIALIZED = True
    logger.info(
        "游戏图鉴初始化完毕: 装备 %d, 时装 %d, 战宠 %d, 消耗品 %d, 材料 %d, 商城 %d",
        len(_EQUIPMENTS),
        len(_CLOTHES),
        len(_PETS),
        len(_CONSUMES),
        len(_STUFFS),
        len(_MALL_GOODS),
    )


def get_mall_goods_info(gid: str) -> Optional[Dict[str, Any]]:
    """根据商城商品 GID 查询商品详细配置"""
    if not _INITIALIZED:
        init_encyclopedia()
    return _MALL_GOODS_MAP.get(str(gid))


def search_encyclopedia(
    category: str = "all",
    keyword: str = "",
    page: int = 1,
    page_size: int = 20,
) -> Dict[str, Any]:
    """
    通用图鉴检索接口
    :param category: 分类 (all, equip, cloth, pet, consume, stuff, mall)
    :param keyword: 搜索关键字 (名称包含或ID匹配)
    :param page: 页码 (从1开始)
    :param page_size: 每页数量
    :return: { "total": int, "items": List[Dict], "page": int, "page_size": int }
    """
    if not _INITIALIZED:
        init_encyclopedia()

    pool: List[Dict[str, Any]] = []
    if category == "all":
        pool = _EQUIPMENTS + _CLOTHES + _PETS + _CONSUMES + _STUFFS + _MALL_GOODS
    elif category == "equip":
        pool = _EQUIPMENTS
    elif category == "cloth":
        pool = _CLOTHES
    elif category == "pet":
        pool = _PETS
    elif category == "consume":
        pool = _CONSUMES
    elif category == "stuff":
        pool = _STUFFS
    elif category == "mall":
        pool = _MALL_GOODS

    keyword = keyword.strip().lower()
    if keyword:
        filtered = []
        for item in pool:
            if keyword in str(item.get("id", "")).lower() or keyword in str(item.get("name", "")).lower():
                filtered.append(item)
    else:
        filtered = pool

    total = len(filtered)
    start_idx = max(0, (page - 1) * page_size)
    end_idx = start_idx + page_size
    paged_items = filtered[start_idx:end_idx]

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": paged_items,
    }
