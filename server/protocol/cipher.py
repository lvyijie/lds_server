"""
龙斗士客户端通讯协议 - 加解密与包序号处理模块 (cipher.py)
移植自客户端 AS3 与 Adobe FlasCC (Crossbridge) CModule 中的 doBytes 与 MsgSeq 算法。

源码依据:
- 06-文档/加密模块-CModule关键函数/doBytes.as
- 02-反编译源码AS3/完整反编译/scripts/mmo/socketserver/SocketServerClient.as
- 02-反编译源码AS3/完整反编译/scripts/mmo/socketserver/DefaultMsgSeq.as
"""

from typing import Union


def to_int32(val: int) -> int:
    """将整数转换为 32 位有符号整数 (-2147483648 ~ 2147483647)。"""
    val = val & 0xFFFFFFFF
    return val - 0x100000000 if val >= 0x80000000 else val


def to_int8(val: int) -> int:
    """将整数转换为 8 位有符号整数 (-128 ~ 127)。"""
    val = val & 0xFF
    return val - 0x100 if val >= 0x80 else val


def c_rem(a: int, b: int) -> int:
    """C 语言风格的整除求余（截断取整，向零取整）。"""
    if b == 0:
        return 0
    sign = -1 if a < 0 else 1
    return sign * (abs(a) % abs(b))


def do_bytes(seq: int, data: Union[bytearray, bytes], char_id: int = 0) -> bytearray:
    """
    客户端上行包加密算法 (doBytes)。
    等价于 Flash 客户端内部调用的 CModule doBytes 函数。

    参数:
        seq: 当前上行包的包序号 (packetNum)
        data: 待加密的原始字节流
        char_id: 当前玩家角色 ID (UserInfo.characterId)，未创角/未选角阶段为 0

    返回:
        加密后的 bytearray
    """
    buf = bytearray(data)
    length = len(buf)
    if length == 0:
        return buf

    # 4 字节密钥小端序: [seq & 0xFF, (seq >> 8) & 0xFF, (seq >> 16) & 0xFF, (seq >> 24) & 0xFF]
    u_seq = seq & 0xFFFFFFFF
    key = [
        u_seq & 0xFF,
        (u_seq >> 8) & 0xFF,
        (u_seq >> 16) & 0xFF,
        (u_seq >> 24) & 0xFF,
    ]

    # 分支 A: char_id == 0 (未创角/选角前阶段，极简 4 字节循环异或)
    if char_id == 0:
        for i in range(length):
            buf[i] ^= key[i % 4]
        return buf

    # 分支 B: char_id != 0 (正式游戏运行阶段，复合置换加解密)
    loc24 = u_seq >> 8
    loc25 = u_seq >> 16
    loc26 = u_seq >> 24

    # 计算 32 位累加校验和 s 与掩码 loc8
    s = to_int32(to_int32(to_int32(loc26 + seq) + loc24) + loc25)
    loc8 = to_int32(s ^ -74)  # -74 即 0xFFFFFFB6
    s_8 = to_int8(s)
    b_6 = to_int8(loc8)
    loc7 = b_6
    if s_8 >= 0:
        loc7 = to_int32(0 - loc7)

    # 1. 奇偶掩码选择性异或
    for i in range(length):
        if ((loc7 + i) & 1) == 0:
            buf[i] ^= key[i % 4]

    # 2. 全体字节异或常数掩码
    mask8 = loc8 & 0xFF
    for i in range(length):
        buf[i] ^= mask8

    # 3. 对称块置换 (对合变换)
    if seq != 0:
        rem = c_rem(to_int32(length + seq), 4)
        offset = abs(rem)
        if offset != 0 and length >= 4:
            blocks = length // 4
            for k in range(blocks):
                idx1 = k * 4
                idx2 = k * 4 + offset
                buf[idx1], buf[idx2] = buf[idx2], buf[idx1]

    return buf


def undo_bytes(seq: int, data: Union[bytearray, bytes], char_id: int = 0) -> bytearray:
    """
    客户端上行包解密算法 (undoBytes)。
    由于 doBytes 中置换操作位于 XOR 之后，解密时先执行逆向置换，再逆向执行异或。

    参数:
        seq: 当前包序号
        data: 收到的密文字节流
        char_id: 角色 ID
    """
    buf = bytearray(data)
    length = len(buf)
    if length == 0:
        return buf

    u_seq = seq & 0xFFFFFFFF
    key = [
        u_seq & 0xFF,
        (u_seq >> 8) & 0xFF,
        (u_seq >> 16) & 0xFF,
        (u_seq >> 24) & 0xFF,
    ]

    # 分支 A: char_id == 0 自身自逆
    if char_id == 0:
        for i in range(length):
            buf[i] ^= key[i % 4]
        return buf

    # 分支 B: char_id != 0 严格逆序还原
    loc24 = u_seq >> 8
    loc25 = u_seq >> 16
    loc26 = u_seq >> 24

    s = to_int32(to_int32(to_int32(loc26 + seq) + loc24) + loc25)
    loc8 = to_int32(s ^ -74)
    s_8 = to_int8(s)
    b_6 = to_int8(loc8)
    loc7 = b_6
    if s_8 >= 0:
        loc7 = to_int32(0 - loc7)

    # 1. 逆向块置换（交换相同索引恢复原位）
    if seq != 0:
        rem = c_rem(to_int32(length + seq), 4)
        offset = abs(rem)
        if offset != 0 and length >= 4:
            blocks = length // 4
            for k in range(blocks):
                idx1 = k * 4
                idx2 = k * 4 + offset
                buf[idx1], buf[idx2] = buf[idx2], buf[idx1]

    # 2. 逆向全体字节异或常数掩码
    mask8 = loc8 & 0xFF
    for i in range(length):
        buf[i] ^= mask8

    # 3. 逆向选择性异或
    for i in range(length):
        if ((loc7 + i) & 1) == 0:
            buf[i] ^= key[i % 4]

    return buf


class DefaultMsgSeq:
    """
    消息包序号发生器。
    匹配 DefaultMsgSeq.as:
        packetNum = current * 2 + 1; (超过 2147483647 归零)
    """

    MAX_SEQ = 2147483647

    def __init__(self, initial_seq: int = -1):
        self._current_seq = initial_seq

    @property
    def current(self) -> int:
        return self._current_seq

    def next_seq(self) -> int:
        """获取下一个包序号。首包返回 0。"""
        if self._current_seq == -1:
            self._current_seq = 0
            return 0
        nxt = self._current_seq * 2 + 1
        if nxt >= self.MAX_SEQ:
            self._current_seq = 0
        else:
            self._current_seq = nxt
        return self._current_seq

    def reset(self) -> None:
        self._current_seq = -1
