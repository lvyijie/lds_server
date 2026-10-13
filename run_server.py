"""
《龙斗士》官方客户端本地仿真服务端统一启动入口 (run_server.py)
在同一 asyncio 事件循环中并发运行：
1. TCP 843  - Flash Socket 安全策略服务 (支持非管理员权限容错与同端口兜底)
2. HTTP 80 / 8000 - 官方资源托管与 login.jsp / character.jsp API (映射 07-原始SWF/核心downloaded_core)
3. TCP 8990 / 8001 - 核心游戏长连接网关 (严格对接官方 8990 端口与真实抓包响应驱动)
"""

import sys
import asyncio
import logging
import signal

from server.policy_server import start_policy_server
from server.http_api import start_http_servers
from server.gateway import start_gateway_servers


def setup_logging():
    """配置控制台与文件日志。"""
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("server.log", mode="a", encoding="utf-8"),
    ]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def print_banner(http_ports, gateway_ports):
    banner = f"""
================================================================================
           ★ 《龙斗士》官方客户端本地仿真服务端已启动 ★
================================================================================
  [1] Flash 策略服务 : TCP 843 端口 (若权限受限，已自动由网关端口兜底)
  [2] HTTP 官方资源服务: 端口 {http_ports} (支持 http://lds.100bt.com 或 http://127.0.0.1:8000)
      - 入口页面     : http://lds.100bt.com/play/play.html
      - 本地备用入口 : http://127.0.0.1:8000/play/play.html
      - 静态资源路径 : 07-原始SWF/核心downloaded_core
  [3] 游戏 TCP 长连接: 端口 {gateway_ports} (官方 8990 + 本地 8001)
      - 驱动机制     : 真实抓包 (新手战士.pcapng) 序列驱动 + doBytes 流式解密
  [4] GM 后台管理系统: http://127.0.0.1:8000/gm 或 http://127.0.0.1/gm
      - 动态调控属性 : 等级 / 职业 / 金币 / 充值龙币 / 圣龙骑士 VIP
      - 游戏原生图鉴 : 装备 / 服饰 / 战宠 / 消耗品 / 材料 / 商城 一键检索与发放
===============================================================================
  推荐使用 Flash 浏览器打开:
  C:\\jiasu\\Release\\CefFlashBrowser.exe http://lds.100bt.com/play/play.html
================================================================================
  按 Ctrl+C 即可安全停止服务端。
================================================================================
"""
    print(banner)


async def main():
    setup_logging()

    # 预加载游戏原生全图鉴
    from server.encyclopedia import init_encyclopedia
    init_encyclopedia()

    # 1. 启动 843 策略服务
    policy_srv = await start_policy_server(host="0.0.0.0", port=843)

    # 2. 启动游戏网关服务 (8990 和 8001)
    gateway, gateway_servers = await start_gateway_servers(host="0.0.0.0", ports=(8990, 8001))

    # 3. 启动 HTTP 服务 (尝试 80 和 8000), exposing the live gateway to GM APIs.
    http_sites = await start_http_servers(
        host="0.0.0.0",
        ports=(80, 8000),
        game_gateway=gateway,
    )

    http_ports = [s._port for s in http_sites]
    gateway_ports = [s.sockets[0].getsockname()[1] for s in gateway_servers if s.sockets]
    print_banner(http_ports, gateway_ports)

    stop_event = asyncio.Event()

    def handle_stop():
        stop_event.set()

    loop = asyncio.get_running_loop()
    try:
        loop.add_signal_handler(signal.SIGINT, handle_stop)
        loop.add_signal_handler(signal.SIGTERM, handle_stop)
    except (NotImplementedError, AttributeError):
        pass

    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        logging.info("正在平滑关闭服务...")
        if policy_srv:
            policy_srv.close()
            await policy_srv.wait_closed()
        for srv in gateway_servers:
            srv.close()
            await srv.wait_closed()
        logging.info("服务端已全部安全停止。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
