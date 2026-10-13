"""
龙斗士客户端通讯协议 - TCP 流式封包与拆包模块 (framing.py)
实现上行数据流粘包/半包拆包、解密与下行帧结构封装。

依据源码:
- 02-反编译源码AS3/完整反编译/scripts/mmo/socketserver/SocketServerClient.as (writeBinaryToSocket / handleBinaryMessage)
- 02-反编译源码AS3/完整反编译/scripts/mmo/socketserver/protocol/BinaryMsgWriter.as
- 02-反编译源码AS3/完整反编译/scripts/mmo/socketserver/protocol/BinaryMsgReader.as
- 02-反编译源码AS3/完整反编译/scripts/mmo/socketserver/command/UpCmd.as
- 02-反编译源码AS3/完整反编译/scripts/mmo/socketserver/command/DownCmd.as
"""

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict, List, Optional, Tuple, Union

from .amf3 import decode_amf3, encode_amf3
from .cipher import do_bytes, undo_bytes


class PacketType(IntEnum):
    SYS = 0x00     # 系统消息
    EXT = 0x01     # AMF3 扩展消息
    EXT_B = 0x02   # 原始字节扩展消息


class UpCmd(IntEnum):
    """客户端上行系统命令字 (基于官方 AS3 UpCmd.as 源码规范)"""
    AS_OBJ = 10002
    HIT = 10011
    JOIN_ROOM = 10012
    LEAVE_ROOM = 10013
    LOGIN = 10016
    PUB_MSG = 10020
    ROUND_TRIP = 10025
    SET_RVARS = 10027
    SET_UVARS = 10028
    XT_REQ = 10033
    XT_REQ_B = 10034


class DownCmd(IntEnum):
    """服务端下行系统命令字 (基于官方 AS3 DownCmd.as 源码规范)"""
    CREATE_RM_KO = 20015
    DATA_OBJ = 20016
    DMN_MSG = 20017
    JOIN_KO = 20018
    JOIN_OK = 20019
    LEAVE_ROOM = 20020
    LOG_KO = 20021
    PUB_MSG = 20026
    R_VARS_UPDATE = 20034
    USER_ENTER_ROOM = 20036
    USER_LEAVE_ROOM = 20037
    USER_VARS_UPDATE = 20039
    XT_RES = 20041
    CROSS_DOMAIN = 20042
    XT_RES_B = 20043


@dataclass
class UplinkPacket:
    """解析后的上行数据包表示"""
    packet_num: int
    msg_type: PacketType
    action: int                        # UpCmd 动作编号 (若为系统包)
    room_id: int                       # 房间 ID / 目标扩展 ID
    cmd: Optional[str] = None          # 扩展消息命令名 (如 '10-0', '13_3', 'initUserData')
    ext_id: Optional[int] = None       # 扩展模块 ID (来自 ExtMap)
    sub_id: Optional[int] = None       # 扩展内部请求序号
    payload: Any = None                # 解构后的数据负载 (字典或字段元组)
    raw_body: Optional[bytes] = None   # 解密后的原始正文字节


def pack_utf(text: str) -> bytes:
    """以 Flash writeUTF 格式打包字符串 (2字节大端长度前缀 + UTF-8 字节)。"""
    encoded = text.encode("utf-8")
    return struct.pack(">H", len(encoded)) + encoded


def unpack_utf(data: bytes, offset: int = 0) -> Tuple[str, int]:
    """以 Flash readUTF 格式解包字符串。返回 (字符串, 新偏移量)。"""
    if len(data) < offset + 2:
        raise ValueError("Buffer underflow reading UTF length prefix")
    length = struct.unpack_from(">H", data, offset)[0]
    offset += 2
    if len(data) < offset + length:
        raise ValueError("Buffer underflow reading UTF payload")
    text = data[offset : offset + length].decode("utf-8")
    return text, offset + length


# -------------------------------------------------------------
# 上行数据流解析 (TCP Stream Parsing)
# -------------------------------------------------------------

def parse_uplink_stream(
    buffer: bytearray,
    char_id: int = 0
) -> Tuple[List[UplinkPacket], bytearray]:
    """
    流式拆包器：从 TCP 缓冲区中解析所有完整的上行数据包。

    上行帧布局:
    +------------------------+-----------------------+-----------------------------+
    | TotalLength (4B BE)    | PacketNum (4B BE)     | Encrypted Body (TotalLen-4) |
    +------------------------+-----------------------+-----------------------------+

    返回:
        (解析出的数据包列表, 剩余未消费的缓冲区字节)
    """
    packets: List[UplinkPacket] = []
    offset = 0
    buf_len = len(buffer)

    while offset + 4 <= buf_len:
        total_length = struct.unpack_from(">I", buffer, offset)[0]
        # 合法性校验：单包最大长度限制 (防止恶意/损坏数据)
        if total_length > 10 * 1024 * 1024 or total_length < 4:
            raise ValueError(f"Invalid packet total_length: {total_length}")

        packet_full_len = 4 + total_length
        if offset + packet_full_len > buf_len:
            # 存在半包，等待后续 TCP 数据到来
            break

        # 提取序号与正文
        packet_num = struct.unpack_from(">I", buffer, offset + 4)[0]
        encrypted_body = buffer[offset + 8 : offset + packet_full_len]

        # 核心解密逻辑:
        # 首包 (packet_num == 0) 完全跳过解密！
        if packet_num == 0:
            decrypted_body = bytes(encrypted_body)
        else:
            decrypted_body = bytes(undo_bytes(packet_num, encrypted_body, char_id))
            if decrypted_body and decrypted_body[0] not in (0, 1, 2) and char_id != 0:
                fallback_dec = bytes(undo_bytes(packet_num, encrypted_body, 0))
                if fallback_dec and fallback_dec[0] in (0, 1, 2):
                    decrypted_body = fallback_dec

        # 解析正文结构
        pkt = _parse_decrypted_body(packet_num, decrypted_body)
        packets.append(pkt)

        offset += packet_full_len

    remaining_buffer = buffer[offset:]
    return packets, remaining_buffer


def _parse_decrypted_body(packet_num: int, body: bytes) -> UplinkPacket:
    """解析解密后的 Body 内容。"""
    if len(body) < 1:
        return UplinkPacket(packet_num=packet_num, msg_type=PacketType.SYS, action=0, room_id=-1, raw_body=body)

    msg_type_code = body[0]
    msg_type = PacketType(msg_type_code)

    if msg_type == PacketType.SYS:
        # Type 0 系统包: [Type: 1B][Action: 2B BE][RoomId: 4B BE][Params...]
        if len(body) < 7:
            return UplinkPacket(packet_num=packet_num, msg_type=msg_type, action=0, room_id=-1, raw_body=body)
        action, room_id = struct.unpack_from(">Hi", body, 1)
        payload = _parse_sys_payload(action, body[7:])
        return UplinkPacket(
            packet_num=packet_num,
            msg_type=msg_type,
            action=action,
            room_id=room_id,
            payload=payload,
            raw_body=body,
        )

    elif msg_type == PacketType.EXT:
        # Type 1 扩展包: [Type: 1B][Action: 2B (UpCmd.XtReq / XtReqB)][ExtId: 4B BE]...
        if len(body) < 7:
            return UplinkPacket(packet_num=packet_num, msg_type=msg_type, action=0, room_id=-1, raw_body=body)
        action, ext_id = struct.unpack_from(">Hi", body, 1)

        if action == UpCmd.XT_REQ_B:
            # Type 1 原始字节扩展包 (如移动同步、战斗微操)
            return UplinkPacket(
                packet_num=packet_num,
                msg_type=msg_type,
                action=action,
                room_id=ext_id,
                ext_id=ext_id,
                cmd=f"XT_B_{action}",
                payload=body[7:],
                raw_body=body,
            )

        if len(body) < 9:
            return UplinkPacket(packet_num=packet_num, msg_type=msg_type, action=action, room_id=ext_id, raw_body=body)
        sub_id = struct.unpack_from(">H", body, 7)[0]
        cur_offset = 9
        cmd_str = ""
        try:
            cmd_str, cur_offset = unpack_utf(body, cur_offset)
        except Exception:
            pass

        amf_data = None
        if cur_offset < len(body):
            try:
                amf_data = decode_amf3(body[cur_offset:])
            except Exception:
                pass

        return UplinkPacket(
            packet_num=packet_num,
            msg_type=msg_type,
            action=action,
            room_id=ext_id,
            ext_id=ext_id,
            sub_id=sub_id,
            cmd=cmd_str,
            payload=amf_data,
            raw_body=body,
        )

    else:
        # Type 2 扩展原始字节包
        action, ext_id = struct.unpack_from(">Hi", body, 1) if len(body) >= 7 else (0, -1)
        return UplinkPacket(
            packet_num=packet_num,
            msg_type=msg_type,
            action=action,
            room_id=ext_id,
            ext_id=ext_id,
            raw_body=body,
        )


def _parse_sys_payload(action: int, payload_bytes: bytes) -> Any:
    """根据系统 Action 解析对应参数。"""
    try:
        if action == UpCmd.LOGIN:
            # 依次解析 3 个 UTF 字符串: zone, userName, password (sessionId)
            off = 0
            zone, off = unpack_utf(payload_bytes, off)
            user_name, off = unpack_utf(payload_bytes, off)
            password, off = unpack_utf(payload_bytes, off)
            return {
                "zone": zone,
                "user_name": user_name,
                "password": password,
            }
        elif action == UpCmd.JOIN_ROOM:
            off = 0
            room_name, off = unpack_utf(payload_bytes, off)
            is_new = payload_bytes[off] == 49
            off += 1
            room_id = struct.unpack_from(">i", payload_bytes, off)[0]
            off += 4
            is_temp = payload_bytes[off] == 49
            off += 1
            room_pass, off = unpack_utf(payload_bytes, off)
            return {
                "room_name": room_name,
                "is_new": is_new,
                "room_id": room_id,
                "is_temp": is_temp,
                "room_pass": room_pass,
            }
    except Exception:
        pass
    return payload_bytes


# -------------------------------------------------------------
# 下行数据帧封装 (Downlink Packet Framing)
# -------------------------------------------------------------

def build_downlink_ext_packet(data_obj: Dict[str, Any]) -> bytes:
    """
    构建下行 Type 1 (AMF3 扩展响应) 数据帧。
    布局:
    +------------------------+-------------------+----------------------------+
    | BytesNeeded (4B BE)    | TypeCode: 0x01    | AMF3 序列化字节负载        |
    +------------------------+-------------------+----------------------------+
    """
    amf_bytes = encode_amf3(data_obj)
    body = bytes([PacketType.EXT]) + amf_bytes
    bytes_needed = len(body)
    return struct.pack(">I", bytes_needed) + body


def build_downlink_sys_packet(action: int, room_id: int = -1, payload: bytes = b"") -> bytes:
    """
    构建下行 Type 0 (系统消息) 数据帧。
    布局:
    +------------------------+-------------------+--------------------+------------------+----------+
    | BytesNeeded (4B BE)    | TypeCode: 0x00    | Action (2B BE)     | RoomId (4B BE)   | Payload  |
    +------------------------+-------------------+--------------------+------------------+----------+
    """
    header = bytes([PacketType.SYS]) + struct.pack(">Hi", action, room_id)
    body = header + payload
    bytes_needed = len(body)
    return struct.pack(">I", bytes_needed) + body


def build_downlink_join_ok(
    room_id: int,
    room_name: str,
    max_users: int = 100,
    join_data: str = "",
    users: Optional[List[Dict[str, Any]]] = None
) -> bytes:
    """
    构建加入房间成功 (joinOK = 20019) 下行数据包。
    匹配 BinaryMsgReader.as:handleJoinOk 字段顺序。
    支持下发房间内的用户列表及其自定义属性 (UserVariables: mi, ride, skills, avatar, equip, etc.)
    """
    payload = struct.pack(">i", room_id)
    payload += pack_utf(room_name)
    payload += struct.pack(">i", max_users)
    payload += pack_utf(join_data)
    # varCount (0: 无房间变量)
    payload += struct.pack(">H", 0)

    if not users:
        payload += struct.pack(">H", 0)
    else:
        payload += struct.pack(">H", len(users))
        for u in users:
            uid = int(u["id"])
            uname = str(u.get("name", ""))
            uvars = u.get("vars", [])
            payload += struct.pack(">i", uid) + pack_utf(uname) + struct.pack(">H", len(uvars))
            for vname, vtype, vval in uvars:
                payload += pack_utf(str(vname)) + str(vtype).encode("latin1")[:1] + pack_utf(str(vval))

    return build_downlink_sys_packet(DownCmd.JOIN_OK, room_id, payload)


def build_downlink_user_vars_update(
    room_id: int,
    user_id: int,
    vars_list: List[Tuple[str, str, Any]]
) -> bytes:
    """
    构建用户属性变量更新 (USER_VARS_UPDATE = 20039) 数据包。
    匹配 BinaryMsgReader.as:handleUserVarsUpdate 字段顺序。
    """
    payload = struct.pack(">i", user_id)
    payload += struct.pack(">H", len(vars_list))
    for vname, vtype, vval in vars_list:
        payload += pack_utf(str(vname)) + str(vtype).encode("latin1")[:1] + pack_utf(str(vval))
    return build_downlink_sys_packet(DownCmd.USER_VARS_UPDATE, room_id, payload)


# -------------------------------------------------------------
# 客户端上行数据帧构造器 (用于测试或客户端仿真)
# -------------------------------------------------------------

def build_uplink_login_packet(zone: str, user_name: str, session_id: str) -> bytes:
    """构建首包登录请求 (UpCmd.Login = 10016, seq=0, 明文)。"""
    payload = pack_utf(zone) + pack_utf(user_name) + pack_utf(session_id)
    body = bytes([PacketType.SYS]) + struct.pack(">Hi", UpCmd.LOGIN, -1) + payload
    total_length = len(body) + 4
    packet_num = 0
    return struct.pack(">II", total_length, packet_num) + body


def build_uplink_ext_packet(
    seq: int,
    ext_id: int,
    cmd_str: str,
    params: Optional[Dict[str, Any]] = None,
    sub_id: int = 0,
    char_id: int = 0
) -> bytes:
    """构建上行 Type 1 扩展请求包 (带 doBytes 加密)。"""
    amf_bytes = encode_amf3(params if params is not None else {})
    body_header = bytes([PacketType.EXT]) + struct.pack(">HiH", UpCmd.XT_REQ, ext_id, sub_id) + pack_utf(cmd_str)
    plain_body = body_header + amf_bytes

    if seq == 0:
        encrypted_body = plain_body
    else:
        encrypted_body = bytes(do_bytes(seq, plain_body, char_id))

    total_length = len(encrypted_body) + 4
    return struct.pack(">II", total_length, seq) + encrypted_body
