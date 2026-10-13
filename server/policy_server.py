"""
Flash Socket 安全策略服务 (policy_server.py)
监听 TCP 843 端口，响应 Flash Player 发送的 <policy-file-request/> 请求。

具备 Windows 权限自适应与容错能力：
- 若缺乏管理员权限导致 843 端口绑定失败，优雅捕获并记录提示，不中断主进程。
- 提供跨域 XML 策略内容生成函数，供网关 8001 端口作同端口策略兜底响应。
"""

import asyncio
import logging

logger = logging.getLogger("PolicyServer")

POLICY_REQUEST = b"<policy-file-request/>"

POLICY_XML = (
    b'<?xml version="1.0"?>\n'
    b'<!DOCTYPE cross-domain-policy SYSTEM "/xml/dtds/cross-domain-policy.dtd">\n'
    b'<cross-domain-policy>\n'
    b'    <allow-access-from domain="*" to-ports="*" />\n'
    b'</cross-domain-policy>\0'
)


async def handle_policy_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """处理策略端口 TCP 客户端连接。"""
    try:
        data = await asyncio.wait_for(reader.read(1024), timeout=5.0)
        if POLICY_REQUEST in data:
            logger.info("收到来自 %s 的 Flash 843 策略请求，已响应放行策略 XML", writer.get_extra_info("peername"))
            writer.write(POLICY_XML)
            await writer.drain()
    except (asyncio.TimeoutError, ConnectionResetError):
        pass
    except Exception as e:
        logger.debug("处理 843 策略连接异常: %s", e)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def start_policy_server(host: str = "0.0.0.0", port: int = 843) -> asyncio.AbstractServer | None:
    """
    启动 TCP 843 端口策略服务。
    若端口权限不足 (如 Windows 非管理员)，自动容错并打印提示。
    """
    try:
        server = await asyncio.start_server(handle_policy_client, host, port)
        logger.info("Flash 843 策略服务已成功启动并在 [%s:%d] 监听", host, port)
        return server
    except (PermissionError, OSError) as e:
        logger.warning(
            "====================================================================\n"
            "[提示] 无法绑定 TCP 843 端口: %s\n"
            "原因说明: 在 Windows/Linux 上绑定低于 1024 的特权端口通常需要管理员身份运行。\n"
            "容错机制已生效: 游戏网关 (8001 端口) 已内建同端口策略探测兜底。\n"
            "若后续 Flash 播放器报 SecurityError #2048，可尝试右键『以管理员身份运行』本服务。\n"
            "====================================================================",
            e,
        )
        return None
