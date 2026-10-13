"""
龙斗士客户端通讯协议 - AMF3 序列化与反序列化工具模块 (amf3.py)
用于编码与解析 Type 1 (ExtHandler) 扩展消息及系统广播数据。

依据源码:
- 02-反编译源码AS3/完整反编译/scripts/mmo/socketserver/handlers/ExtHandler.as
- 02-反编译源码AS3/完整反编译/scripts/mmo/socketserver/protocol/ExMsgCodec.as
"""

from typing import Any, Dict, List, Optional, Union
import pyamf


def encode_amf3(obj: Any) -> bytes:
    """
    将 Python 对象编码为 ActionScript 3 (AMF3) 二进制字节流。
    """
    stream = pyamf.encode(obj, encoding=pyamf.AMF3)
    return stream.getvalue()


def decode_amf3(data: Union[bytes, bytearray]) -> Any:
    """
    将 AMF3 二进制字节流反序列化为 Python 对象。
    若包含多个对象，返回首个解出对象。
    """
    decoded_generator = pyamf.decode(data, encoding=pyamf.AMF3)
    results = list(decoded_generator)
    if not results:
        return None
    return results[0]


# -------------------------------------------------------------
# 常用协议响应快速构建函数 (Convenience Factory Functions)
# -------------------------------------------------------------

def build_log_ok(user_id: int, user_name: str, last_login_time: float = 0.0) -> Dict[str, Any]:
    """构建 Socket 登录成功响应 (logOK)。"""
    if last_login_time == 0.0:
        last_login_time = 1791561600000.0
    return {
        "_cmd": "logOK",
        "id": user_id,
        "n": user_name,
        "s": 0,
        "lastLoginTime": float(last_login_time),
    }


def build_log_ko(error_msg: str) -> Dict[str, Any]:
    """构建 Socket 登录失败响应 (logKO)。"""
    return {
        "_cmd": "logKO",
        "err": error_msg,
    }


def build_init_user_data() -> Dict[str, Any]:
    """构建 BootAllCommand 必须的 initUserData 响应。"""
    return {
        "_cmd": "initUserData",
    }


def build_server_time_resp(timestamp: Optional[int] = None) -> Dict[str, Any]:
    """构建 getCurrentTime 响应。"""
    import time
    return {
        "_cmd": "getCurrentTime",
        "t": timestamp if timestamp is not None else int(time.time()),
    }


def build_backpack_resp(bag: Optional[List[Dict[str, Any]]] = None, ov: bool = True) -> Dict[str, Any]:
    """构建背包初始化响应 (8_0_0)。ov=True 代表数据分发完成。"""
    return {
        "_cmd": "8_0_0",
        "bag": bag if bag is not None else [],
        "ov": ov,
    }


def build_tasks_init_resp(tasks: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """构建任务列表初始化响应 (10-0)。解开 TaskService 死锁关键。"""
    return {
        "_cmd": "10-0",
        "t": tasks if tasks is not None else [],
    }


def build_task_update_resp(task_id: str, state: int, update_type: int) -> Dict[str, Any]:
    """
    构建任务状态推送 (10-1)。
    update_type:
        0: 更新步骤
        1: 新增任务
        2: 任务完结
    """
    return {
        "_cmd": "10-1",
        "ty": update_type,
        "t": {
            "ti": task_id,
            "st": state,
        },
    }


def build_create_room_resp(room_name: str) -> Dict[str, Any]:
    """构建副本房间创建响应 (13_3)。"""
    return {
        "_cmd": "13_3",
        "rn": room_name,
    }


def build_create_name_resp(result_code: int = 1, names: Optional[str] = None) -> Dict[str, Any]:
    """
    构建起名/改名响应 (103_4)。
    result_code: 1 为成功 (CreateNameResult.SUCCESS)
    """
    resp: Dict[str, Any] = {
        "_cmd": "103_4",
        "r": result_code,
    }
    if names is not None:
        resp["names"] = names
    return resp


def build_spawn_player_resp(player_info: Dict[str, Any]) -> Dict[str, Any]:
    """
    构建战场玩家生成响应 (2301)。
    必须包含 ui (userId) 与主角属性，防止 PlayerManager 报 TypeError #1009。
    """
    return {
        "_cmd": "2301",
        "cl": [player_info],
    }
