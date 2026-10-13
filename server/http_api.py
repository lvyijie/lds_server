"""
HTTP 资源托管与 JSP API 仿真服务 (http_api.py)
基于 aiohttp 实现：
1. 静态资源托管：映射 07-原始SWF/核心downloaded_core 目录官方资源。
2. 动态拦截 start~*.xml / difference.xml / config_union.xml / isp.txt 等。
3. JSP 接口真实模拟 (完全匹配抓包数据)。
4. 增强功能：本地缺失时自动回源官方 CDN (res.100bt.com) 下载并落盘保存，全网缺失下发空 SWF 桩防卡死。
"""

import os
import re
import time
import socket
import asyncio
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
import aiohttp
from aiohttp import web

from .player_storage import (
    load_player,
    add_dragon_coin,
    set_dragon_coin,
    get_dragon_coin,
    set_vip_info,
    get_vip_info,
    add_pet,
    wake_pet,
    level_up_pet,
    get_pets,
    clean_corrupted_data,
    update_player_basic,
    grant_item_to_player,
)
from .encyclopedia import search_encyclopedia, init_encyclopedia

logger = logging.getLogger("HttpServer")

WORKSPACE_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
STATIC_ROOT = os.path.join(WORKSPACE_ROOT, "07-原始SWF", "核心downloaded_core")

SWF_BASE_ROOT = os.path.join(WORKSPACE_ROOT, "07-原始SWF")

GLOBAL_SWF_CACHE: Dict[str, Path] = {}

def init_swf_cache() -> None:
    """递归索引本地所有已下载的真实 SWF 资源 (包括战宠、时装、核心库等全部子目录)，建立多维度文件名映射字典。"""
    global GLOBAL_SWF_CACHE
    if GLOBAL_SWF_CACHE:
        return
    root_p = Path(SWF_BASE_ROOT)
    if not root_p.is_dir():
        root_p = Path(STATIC_ROOT)
    if not root_p.is_dir():
        return
    count = 0
    for p in root_p.rglob("*.swf"):
        if not p.is_file():
            continue
        count += 1
        rel_str = str(p.relative_to(root_p)).replace("\\", "/").lower()
        clean_rel = re.sub(r"~\d+", "", rel_str)
        fname = p.name.lower()
        fname_clean = re.sub(r"~\d+", "", fname)
        fname_stem = fname_clean.replace(".swf", "")

        # 注册全路径映射与纯文件名映射
        GLOBAL_SWF_CACHE[rel_str] = p
        GLOBAL_SWF_CACHE[clean_rel] = p
        GLOBAL_SWF_CACHE[fname] = p
        GLOBAL_SWF_CACHE[fname_clean] = p
        GLOBAL_SWF_CACHE[fname_stem] = p

    logger.info("已建立全局 SWF 智能寻址缓存: %d 个真实文件, %d 个别名索引", count, len(GLOBAL_SWF_CACHE))

init_swf_cache()

# 兜底用的极简合法空 SWF 字节流 (72 字节，避免万一下不到时客户端挂死)
DUMMY_EMPTY_SWF = (
    b'FWS\x04H\x00\x00\x00x\x00\x05P\x00\x00\x0f\xa0\x00\x00'
    b'\x0c\x01\x00\x43\x02\xff\xff\xff\x40\x00\x00\x00'
)

CROSSDOMAIN_XML = """<?xml version="1.0"?>
<!DOCTYPE cross-domain-policy SYSTEM "http://www.adobe.com/xml/dtds/cross-domain-policy.dtd">
<cross-domain-policy>
    <allow-access-from domain="*" />
    <allow-http-request-headers-from domain="*" headers="*" />
</cross-domain-policy>
"""

CONFIG_UNION_XML = """<?xml version="1.0" encoding="utf-8"?>
<config>
  <version>1.0.0</version>
  <debug>false</debug>
  <server>
    <urls>
      <unionLoginSite>http://login.lds.100bt.com/</unionLoginSite>
      <unionServiceSite>http://service.lds.100bt.com/</unionServiceSite>
    </urls>
  </server>
</config>
"""


def sync_fetch_from_cdn(rel_path: str, timeout: int = 10) -> bytes:
    """底层 Socket 直连百田官方 CDN (183.215.57.92 / lds.100bt.com)。"""
    cdn_ips = ("183.215.57.92", "111.6.13.20", "183.204.229.21")
    clean = rel_path if rel_path.startswith("/") else f"/{rel_path}"
    candidates = [clean]
    if not clean.startswith("/play/"):
        candidates.append(f"/play{clean}")
    elif clean.startswith("/play/"):
        candidates.append(clean[5:])

    for path in candidates:
        for ip in cdn_ips:
            try:
                s = socket.create_connection((ip, 80), timeout=timeout)
                req = (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: lds.100bt.com\r\n"
                    "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36\r\n"
                    "Referer: http://lds.100bt.com/play/play.html\r\n"
                    "Accept: */*\r\n"
                    "Connection: close\r\n\r\n"
                )
                s.sendall(req.encode("ascii"))
                buf = bytearray()
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
                s.close()

                head, _, body = bytes(buf).partition(b"\r\n\r\n")
                status = head.decode("latin1", "replace").split("\r\n")[0]
                if "200" in status and len(body) > 150 and b"/lds/404.html" not in body[:4000]:
                    if body[:3] in (b'FWS', b'CWS', b'ZWS') or len(body) > 512:
                        return bytes(body)
            except Exception:
                continue
    return b""


async def download_from_upstream(rel_path: str, local_save_path: Path) -> bytes:
    """
    通过底层 socket 直连百田官方 CDN 节点自动拉取缺失资源并落盘。
    """
    content = await asyncio.to_thread(sync_fetch_from_cdn, rel_path)
    if content:
        local_save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(local_save_path, "wb") as f:
            f.write(content)
        logger.info("【官方 CDN 自动抓取并落盘】%s -> %s (%d 字节)", rel_path, local_save_path.name, len(content))
        return content
    return b""


@web.middleware
async def cors_middleware(request: web.Request, handler) -> web.Response:
    """全局跨域与 OPTIONS 预检支持中间件。"""
    if request.method == "OPTIONS":
        response = web.Response(status=200)
    else:
        try:
            response = await handler(request)
        except web.HTTPException as ex:
            response = ex

    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response


async def handle_crossdomain(request: web.Request) -> web.Response:
    """返回 HTTP 跨域策略文件。"""
    return web.Response(text=CROSSDOMAIN_XML, content_type="text/xml")


async def handle_isp(request: web.Request) -> web.Response:
    """返回 ISP 类型。"""
    return web.Response(text="union", content_type="text/plain")


async def handle_time_pub(request: web.Request) -> web.Response:
    """返回当前时间戳。"""
    return web.Response(text=str(int(time.time() * 1000)), content_type="text/plain")


async def handle_game_login_action(request: web.Request) -> web.Response:
    """模拟 HandleGameLoginRequest.action 登录认证。"""
    response = web.Response(text="loginSuccess", content_type="text/plain")
    now_ts = int(time.time() * 1000)
    response.set_cookie(
        "game_login_token",
        f"663944508#{now_ts}#43d5575c184ffe3e0dc650b9a970c0b2",
        domain="100bt.com",
        path="/",
    )
    return response


async def handle_login_jsp(request: web.Request) -> web.Response:
    """处理 /login.jsp 请求。"""
    params = {}
    if request.method == "POST":
        try:
            post_data = await request.post()
            params = dict(post_data)
        except Exception:
            pass
    if not params:
        params = dict(request.query)

    req_type = params.get("type", "char")
    username = params.get("username") or params.get("u") or "663944508"
    now_ts = int(time.time() * 1000)

    logger.info("收到 /login.jsp 请求: type=%s, user=%s", req_type, username)

    if req_type == "char":
        from .player_storage import build_http_char_node, update_basic
        if username and username != "663944508":
            update_basic(88343134, user_name=username)

        char_node = build_http_char_node(88343134)
        xml_content = f"""<r>\n<result>ok</result>\n<t>{now_ts}</t>\n<digest>43d5575c184ffe3e0dc650b9a970c0b2</digest>\n<u>{username}</u>\n<sid>1570d4fb-ec53-41cd-8783-feda8fa66466</sid>\n<state>0</state>\n{char_node}\n<ip>127.0.0.1</ip>\n</r>"""
        return web.Response(text=xml_content, content_type="text/xml", charset="utf-8")
    elif req_type == "server":
        xml_content = """<r><result>success</result><m>0</m><svr>127.0.0.1:8990;127.0.0.1:8001</svr><s id="1" p="8001" ip="127.0.0.1" /><zn>1;1 本地测试服/0/4</zn></r>"""
        return web.Response(text=xml_content, content_type="text/xml", charset="utf-8")

    return web.Response(text="<r><result>success</result></r>", content_type="text/xml")


async def handle_character_jsp(request: web.Request) -> web.Response:
    """处理 /character.jsp 接口。"""
    params = {}
    if request.method == "POST":
        try:
            post_data = await request.post()
            params = dict(post_data)
        except Exception:
            pass
    if not params:
        params = dict(request.query)

    req_type = params.get("type", "list")
    from .player_storage import build_http_char_node, update_basic

    if req_type == "create":
        new_name = params.get("name") or params.get("n") or "LDS8834313497"
        update_basic(88343134, user_name=new_name)
        xml_content = f"<r><result>success</result><name>{new_name}</name><ifa>0</ifa></r>"
    else:
        char_node = build_http_char_node(88343134)
        xml_content = f"""<r>\n<result>success</result>\n{char_node}\n</r>"""
    return web.Response(text=xml_content, content_type="text/xml", charset="utf-8")
async def handle_start_xml(request: web.Request) -> web.Response:
    """动态响应任意时间戳的 start~*.xml 请求。"""
    xml_path = os.path.join(STATIC_ROOT, "play", "start~1791614625084.xml")
    if os.path.exists(xml_path):
        with open(xml_path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/xml", charset="utf-8")
    return web.Response(status=404, text="start.xml not found")


async def handle_difference_xml(request: web.Request) -> web.Response:
    """动态重写 difference.xml。"""
    xml_path = os.path.join(STATIC_ROOT, "play", "difference~200407814563291.xml")
    if not os.path.exists(xml_path):
        xml_path = os.path.join(STATIC_ROOT, "difference.xml")
    if os.path.exists(xml_path):
        with open(xml_path, "r", encoding="utf-8") as f:
            content = f.read()
        host = request.host
        if ":" in host:
            port = host.split(":")[1]
            content = content.replace("http://login.lds.100bt.com/", f"http://127.0.0.1:{port}/")
            content = content.replace("http://service.lds.100bt.com/", f"http://127.0.0.1:{port}/")
            content = content.replace("http://dispatch.lds.100bt.com/", f"http://127.0.0.1:{port}/")
        return web.Response(text=content, content_type="text/xml", charset="utf-8")
    return web.Response(status=404, text="difference.xml not found")


async def handle_config_union(request: web.Request) -> web.Response:
    """响应 config_union.xml。"""
    return web.Response(text=CONFIG_UNION_XML, content_type="text/xml", charset="utf-8")


async def handle_empty_ok(request: web.Request) -> web.Response:
    """接收客户端上报/统计，静默返回 200 OK。"""
    return web.Response(status=200, text="")


async def handle_client_log(request: web.Request) -> web.Response:
    """接收客户端报错上报并记录详细错误。"""
    try:
        body = await request.text()
        logger.error("【客户端 ActionScript 报错日志】%s", body)
    except Exception as e:
        logger.warning("读取客户端错误日志失败: %s", e)
    return web.Response(status=200, text="")


async def handle_static_fallback(request: web.Request) -> web.Response:
    """
    智能静态资源寻址中间件：
    1. 本地精确查找
    2. 本地版本号去除查找
    3. 本地同目录模糊前缀匹配
    4. 联网自动下载落盘 (res.100bt.com)
    5. 全网缺失 SWF 下发空桩保底防卡死
    """
    raw_path = request.path.lstrip("/")
    clean_path = raw_path.split("?")[0]

    # 关键修复：当客户端错误请求 f0 战宠图标时，重定向到默认合法的亚克图标 f100408，杜绝空 Linkage 导致 #1009 崩溃
    if "fightpeticon/f0" in clean_path:
        clean_path = "play/fightpeticon/f100408~1000.swf"
    # 特殊接口重定向
    if "start~" in clean_path and clean_path.endswith(".xml"):
        return await handle_start_xml(request)
    if "difference" in clean_path and clean_path.endswith(".xml"):
        return await handle_difference_xml(request)
    if "config_union" in clean_path and clean_path.endswith(".xml"):
        return await handle_config_union(request)

    target_file = Path(STATIC_ROOT) / clean_path

    # Pet skins and icons are separate SWFs from pet view models (fightpetview).
    # Never resolve them by basename across directories, since f<id>.swf in fightpetview lacks icon/skin linkage classes.
    normalized_path = clean_path.replace("\\", "/").lower()
    if normalized_path.startswith("play/fightpetskin/"):
        pet_skin_root = Path(SWF_BASE_ROOT) / "宠物fightpetskin"
        requested_name = Path(clean_path).name
        pet_skin_file = pet_skin_root / requested_name
        if not pet_skin_file.is_file():
            unversioned_name = re.sub(r"~\d+(?=\.swf$)", "", requested_name, flags=re.IGNORECASE)
            pet_skin_file = pet_skin_root / unversioned_name
        if not pet_skin_file.is_file():
            base_prefix = Path(requested_name).name.split("~")[0]
            matches = sorted(pet_skin_root.glob(f"{base_prefix}*"))
            pet_skin_file = next((match for match in matches if match.is_file()), pet_skin_file)
        if pet_skin_file.is_file():
            target_file = pet_skin_file
            logger.info(
                "【战宠皮肤精确映射】请求: %s -> %s (%d bytes)",
                clean_path,
                pet_skin_file,
                pet_skin_file.stat().st_size,
            )
    elif normalized_path.startswith("play/fightpeticon/"):
        
        pet_icon_root = Path(STATIC_ROOT) / "play" / "fightpeticon"
        requested_name = Path(clean_path).name
        if "f0" in requested_name:
            requested_name = "f100408~1000.swf"
        pet_icon_file = pet_icon_root / requested_name
        if not pet_icon_file.is_file():
            unversioned_name = re.sub(r"~\d+(?=\.swf$)", "", requested_name, flags=re.IGNORECASE)
            pet_icon_file = pet_icon_root / unversioned_name
        if not pet_icon_file.is_file():
            base_prefix = Path(requested_name).name.split("~")[0]
            if pet_icon_root.is_dir():
                matches = sorted(pet_icon_root.glob(f"{base_prefix}*"))
                pet_icon_file = next((match for match in matches if match.is_file()), pet_icon_file)
        if pet_icon_file.is_file():
            target_file = pet_icon_file
            logger.info(
                "【战宠头像精确映射】请求: %s -> %s (%d bytes)",
                clean_path,
                pet_icon_file,
                pet_icon_file.stat().st_size,
            )
        if not pet_icon_file.is_file():
            base_stem = Path(requested_name).name.split("~")[0].lower()
            # 1. 尝试将 f100519 替换为 p100519 查找
            p_stem = base_stem.replace("f", "p", 1) if base_stem.startswith("f") else base_stem
            if not GLOBAL_SWF_CACHE:
                init_swf_cache()
            if p_stem in GLOBAL_SWF_CACHE:
                pet_icon_file = GLOBAL_SWF_CACHE[p_stem]
            elif base_stem in GLOBAL_SWF_CACHE:
                pet_icon_file = GLOBAL_SWF_CACHE[base_stem]

        if pet_icon_file.is_file():
            target_file = pet_icon_file
            logger.info(
                "【战宠头像精确映射】请求: %s -> %s (%d bytes)",
                clean_path,
                pet_icon_file,
                pet_icon_file.stat().st_size,
            )

    # 1. 本地不存在，尝试去除版本号及模糊匹配
    if not target_file.is_file():
        unversioned = re.sub(r"~\d+", "", clean_path)
        alt_file = Path(STATIC_ROOT) / unversioned
        if alt_file.is_file():
            target_file = alt_file
        else:
            parent = target_file.parent
            base_prefix = target_file.name.split("~")[0]
            if parent.is_dir() and base_prefix:
                for f in parent.glob(f"{base_prefix}*"):
                    if f.is_file():
                        target_file = f
                        break

    # 2. 本地全局 SWF 智能索引查验 (跨目录别名与词根匹配，但对 fightpeticon/fightpetskin 绝不串到 fightpetview)
    if not target_file.is_file() and clean_path.endswith(".swf") and not (
        normalized_path.startswith("play/fightpeticon/") or normalized_path.startswith("play/fightpetskin/")
    ):
        if not GLOBAL_SWF_CACHE:
            init_swf_cache()
        req_norm = clean_path.lower().replace("\\", "/")
        req_unver = re.sub(r"~\d+", "", req_norm)
        req_name = Path(clean_path).name.lower()
        req_name_unver = re.sub(r"~\d+", "", req_name)
        req_stem = req_name_unver.replace(".swf", "").split("~")[0]

        for cand_key in [req_norm, req_unver, req_name, req_name_unver, req_stem]:
            if cand_key in GLOBAL_SWF_CACHE:
                target_file = GLOBAL_SWF_CACHE[cand_key]
                logger.info("【SWF 智能索引命中】请求: %s -> 本地真实文件: %s", clean_path, target_file.name)
                break

    # 3. 如果本地依然没有，自动联网去官方 CDN 抓取并写入硬盘！
    if not target_file.is_file():
        logger.info("【本地缺失，尝试从官方 CDN 联网下载】%s", clean_path)
        save_dest = Path(STATIC_ROOT) / clean_path
        downloaded = await download_from_upstream(clean_path, save_dest)
        if downloaded:
            target_file = save_dest

    # 4. 本地已有或刚下载成功，直接响应
    if target_file.is_file():
        ext = target_file.suffix.lower()
        charset = None
        content_type = "application/octet-stream"
        if ext == ".swf":
            content_type = "application/x-shockwave-flash"
        elif ext == ".xml":
            content_type = "text/xml"
            charset = "utf-8"
        elif ext in [".html", ".htm"]:
            content_type = "text/html"
            charset = "utf-8"
        elif ext == ".js":
            content_type = "application/javascript"
            charset = "utf-8"
        elif ext in [".png", ".jpg", ".jpeg", ".gif"]:
            content_type = f"image/{ext.lstrip('.')}"
        elif ext == ".txt":
            content_type = "text/plain"
            charset = "utf-8"

        with open(target_file, "rb") as f:
            data = f.read()
        headers = {}
        if Path(clean_path).name.lower().startswith("petvalueservice~"):
            # This SWF is patched locally without changing its historical URL;
            # prevent the browser from reusing a cached official copy.
            headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return web.Response(body=data, content_type=content_type, charset=charset, headers=headers)

    # 4. 官方源也已下线且是 SWF：下发 72 字节空 SWF 桩，彻底防止客户端卡死！
    if clean_path.endswith(".swf"):
        logger.warning("【资源彻底缺失，下发空 SWF 桩防卡死】%s", clean_path)
        return web.Response(body=DUMMY_EMPTY_SWF, content_type="application/x-shockwave-flash")

    logger.warning("未找到静态资源: %s (Raw: %s)", clean_path, request.path)
    return web.Response(status=404, text=f"File not found: {clean_path}")


async def handle_api_get_player(request: web.Request) -> web.Response:
    """返回当前玩家的完整持久化 JSON 数据。"""
    cid = int(request.query.get("cid", 88343134))
    data = load_player(cid)
    return web.json_response(data)


async def handle_api_add_dragon_coin(request: web.Request) -> web.Response:
    """刷充值货币 (龙币)。"""
    cid = int(request.query.get("cid", 88343134))
    amount = int(request.query.get("amount", request.query.get("lb", 100000)))
    new_val = add_dragon_coin(cid, amount)
    return web.json_response({"success": True, "cid": cid, "dragon_coin": new_val, "message": f"成功增加 {amount} 龙币，当前余额: {new_val}"})


async def handle_api_set_vip(request: web.Request) -> web.Response:
    """激活或设置圣龙骑士 VIP。"""
    cid = int(request.query.get("cid", 88343134))
    level = int(request.query.get("level", 10))
    vip_data = set_vip_info(cid, level=level, is_vip=True, is_annual=True, is_super_vip=True)
    return web.json_response({"success": True, "cid": cid, "vip": vip_data, "message": f"成功激活圣龙骑士 (Lv.{level}, 年费圣皇)"})


async def handle_api_wake_pet(request: web.Request) -> web.Response:
    """战宠觉醒。"""
    cid = int(request.query.get("cid", 88343134))
    pid = int(request.query.get("pid", 1))
    ecs = int(request.query.get("ecs", 5))
    pet = wake_pet(cid, pid, target_ecs=ecs)
    if pet:
        return web.json_response({"success": True, "cid": cid, "pet": pet, "message": f"战宠 {pet.get('n')} 成功觉醒至 {ecs} 阶"})
    return web.json_response({"success": False, "message": "未找到指定战宠"}, status=404)


async def handle_api_add_pet(request: web.Request) -> web.Response:
    """获取神宠。"""
    cid = int(request.query.get("cid", 88343134))
    pti = int(request.query.get("pti", 100494))
    name = request.query.get("name")
    pet = add_pet(cid, pti, name=name, lv=100, ecs=5)
    return web.json_response({"success": True, "cid": cid, "pet": pet, "message": f"成功获得战宠 {pet.get('n')} (Lv.100, 5阶觉醒)"})


async def handle_gm_page(request: web.Request) -> web.Response:
    """返回 GM 网页控制台。"""
    gm_html_path = os.path.join(os.path.dirname(__file__), "static", "gm.html")
    if os.path.exists(gm_html_path):
        with open(gm_html_path, "r", encoding="utf-8") as f:
            content = f.read()
        return web.Response(text=content, content_type="text/html", charset="utf-8")
    return web.Response(text="GM 页面未找到", status=404)


async def handle_api_gm_player(request: web.Request) -> web.Response:
    """GM API: 获取玩家完整信息"""
    cid = int(request.query.get("cid", 88343134))
    data = load_player(cid)
    return web.json_response(data)


async def handle_api_gm_update(request: web.Request) -> web.Response:
    """GM API: 批量更新基础属性"""
    try:
        body = await request.json()
        cid = int(body.get("cid", 88343134))
        p = update_player_basic(cid, body)
        return web.json_response({"success": True, "player": p, "message": "角色属性更新成功"})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=400)


async def handle_api_gm_grant(request: web.Request) -> web.Response:
    """GM API: 一键发放物品/宠物"""
    try:
        body = await request.json()
        cid = int(body.get("cid", 88343134))
        category = body.get("category", "item")
        item_id = int(body.get("item_id", 0))
        count = int(body.get("count", 1))
        old_player = load_player(cid)
        old_items = {
            int(item.get("gid", 0)): item
            for item in old_player.get("bag", {}).get("items", [])
        }
        old_pet_ids = {
            int(pet.get("pi", 0))
            for pet in old_player.get("pets", [])
            if isinstance(pet, dict)
        }
        old_cloth_set = set(old_player.get("bag", {}).get("cloth", []))
        p = grant_item_to_player(cid, category=category, item_id=item_id, count=count)
        updated_items = [
            item
            for item in p.get("bag", {}).get("items", [])
            if item != old_items.get(int(item.get("gid", 0)))
        ]
        new_pets = [
            pet
            for pet in p.get("pets", [])
            if isinstance(pet, dict)
            and int(pet.get("pi", 0)) not in old_pet_ids
        ]
        cloth_items = []
        is_cloth_grant = str(category).lower().strip() in ("cloth", "clothes", "1")
        new_clothes_ids = [c_id for c_id in p.get("bag", {}).get("cloth", []) if c_id not in old_cloth_set]
        if is_cloth_grant and not new_clothes_ids:
            new_clothes_ids = [int(item_id)]

        for c_id in new_clothes_ids:
            cloth_items.append({
                "gid": int(c_id),
                "n": 1,
                "t": 1,
                "exp": 7614748800000,
                "skt": 0,
                "gem": "",
                "cn": "",
                "cds": "",
            })
        online_updates = 0
        gateway = request.app.get("game_gateway")
        if updated_items and gateway is not None:
            online_updates = await gateway.notify_player_inventory_update(
                cid, updated_items
            )
        if cloth_items and gateway is not None:
            online_updates = max(
                online_updates,
                await gateway.notify_player_inventory_update(cid, cloth_items),
            )
        if new_pets and gateway is not None:
            online_updates = max(
                online_updates,
                await gateway.notify_player_new_pets(cid, new_pets),
            )
        message = f"成功发放 ID {item_id} x{count}"
        if (updated_items or new_pets or cloth_items) and not online_updates:
            message += "；角色当前未在线，重新登录后可在背包或衣柜中看到"
        elif online_updates and new_pets:
            message += "；战宠已同步到在线客户端"
        elif online_updates and cloth_items:
            message += "；时装已同步到在线客户端衣柜"
        elif online_updates and updated_items:
            message += "；已同步到在线游戏客户端"
        return web.json_response({
            "success": True,
            "player": p,
            "online_updates": online_updates,
            "message": message,
        })
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)}, status=400)


async def handle_api_gm_clean(request: web.Request) -> web.Response:
    """GM API: 清理背包脏数据"""
    cid = int(request.query.get("cid", 88343134))
    p = clean_corrupted_data(cid)
    return web.json_response({"success": True, "player": p, "message": "脏数据清洗成功"})


async def handle_api_gm_encyclopedia(request: web.Request) -> web.Response:
    """GM API: 查询游戏原生资源图鉴"""
    category = request.query.get("category", "all")
    keyword = request.query.get("keyword", "")
    page = int(request.query.get("page", 1))
    page_size = int(request.query.get("page_size", 20))
    result = search_encyclopedia(category=category, keyword=keyword, page=page, page_size=page_size)
    return web.json_response(result)


async def handle_pay_page(request: web.Request) -> web.Response:
    """官方充值页面本地仿真桩，支持一键直接加龙币"""
    cid = int(request.query.get("cid", 88343134))
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>百田充值中心 (本地单机仿真)</title>
<style>
body {{ font-family: sans-serif; background: #1e293b; color: #fff; text-align: center; padding-top: 50px; }}
.box {{ background: #0f172a; max-width: 480px; margin: 0 auto; padding: 30px; border-radius: 12px; border: 1px solid #334155; }}
button {{ background: #f59e0b; color: #000; border: none; font-weight: bold; font-size: 16px; padding: 12px 24px; border-radius: 6px; cursor: pointer; }}
</style></head>
<body>
<div class="box">
<h2>百田龙斗士充值中心 (本地仿真)</h2>
<p style="color:#94a3b8; margin: 20px 0;">当前角色: {cid}</p>
<button onclick="fetch('/api/add_lb?cid={cid}&amount=100000').then(()=>alert('充值 100,000 龙币成功！'))">一键免费充值 100,000 龙币</button>
<p style="margin-top:20px;"><a href="/gm" style="color:#38bdf8;">返回 GM 管理后台</a></p>
</div>
</body></html>"""
    return web.Response(text=html, content_type="text/html", charset="utf-8")


def create_http_app(game_gateway=None) -> web.Application:
    """创建并配置 aiohttp Web 应用。"""
    init_swf_cache()
    app = web.Application(middlewares=[cors_middleware])
    app["game_gateway"] = game_gateway

    # GM 控制台页面与原生图鉴 API
    app.router.add_get("/gm", handle_gm_page)
    app.router.add_get("/play/gm", handle_gm_page)
    app.router.add_get("/api/gm/player", handle_api_gm_player)
    app.router.add_post("/api/gm/player/update", handle_api_gm_update)
    app.router.add_post("/api/gm/player/grant", handle_api_gm_grant)
    app.router.add_post("/api/gm/player/clean", handle_api_gm_clean)
    app.router.add_get("/api/gm/encyclopedia", handle_api_gm_encyclopedia)

    # 官方充值仿真页面
    app.router.add_route("*", "/pay", handle_pay_page)
    app.router.add_route("*", "/pay.html", handle_pay_page)
    app.router.add_route("*", "/play/pay.html", handle_pay_page)

    # GM 与后台辅助管理 API (旧版兼容)
    app.router.add_get("/api/player", handle_api_get_player)
    app.router.add_get("/api/add_dragon_coin", handle_api_add_dragon_coin)
    app.router.add_get("/api/add_lb", handle_api_add_dragon_coin)
    app.router.add_get("/api/set_vip", handle_api_set_vip)
    app.router.add_get("/api/wake_pet", handle_api_wake_pet)
    app.router.add_get("/api/add_pet", handle_api_add_pet)

    # 显式注册已知 API 与策略接口
    app.router.add_get("/crossdomain.xml", handle_crossdomain)
    app.router.add_get("/play/crossdomain.xml", handle_crossdomain)
    app.router.add_get("/isp.txt", handle_isp)
    app.router.add_get("/play/isp.txt", handle_isp)
    app.router.add_get("/time/pub", handle_time_pub)
    app.router.add_get("/play/time/pub", handle_time_pub)
    app.router.add_route("*", "/login.jsp", handle_login_jsp)
    app.router.add_route("*", "/play/login.jsp", handle_login_jsp)
    app.router.add_route("*", "/character.jsp", handle_character_jsp)
    app.router.add_route("*", "/play/character.jsp", handle_character_jsp)
    app.router.add_route("*", "/HandleGameLoginRequest.action", handle_game_login_action)
    app.router.add_route("*", "/stat.jsp", handle_empty_ok)
    app.router.add_route("*", "/pvrecord.jsp", handle_empty_ok)
    app.router.add_route("*", "/clientlogcollect.jsp", handle_client_log)
    app.router.add_route("*", "/play/clientlogcollect.jsp", handle_client_log)
    app.router.add_route("*", "/wx/sendTempleteMessage.jsonp", handle_empty_ok)
    app.router.add_get("/config_union.xml", handle_config_union)
    app.router.add_get("/play/config_union.xml", handle_config_union)

    # 兜底通用资源路由
    app.router.add_route("*", "/{path:.*}", handle_static_fallback)

    return app


async def start_http_server(
    host: str = "0.0.0.0",
    port: int = 8000,
    ports: tuple = None,
    game_gateway=None,
):
    app = create_http_app(game_gateway=game_gateway)
    runner = web.AppRunner(app)
    await runner.setup()
    if ports is None:
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info("HTTP 资源服务已在 [http://%s:%d] 监听", host, port)
        return runner
    sites = []
    for p in ports:
        try:
            s = web.TCPSite(runner, host, p)
            await s.start()
            sites.append(s)
            logger.info("HTTP 资源服务已在 [http://%s:%d] 监听", host, p)
        except Exception as e:
            logger.warning("无法在端口 %d 启动 HTTP 服务: %s", p, e)
    return sites


async def start_http_servers(
    host: str = "0.0.0.0",
    ports: tuple = (80, 8000),
    game_gateway=None,
):
    return await start_http_server(
        host=host,
        ports=ports,
        game_gateway=game_gateway,
    )
