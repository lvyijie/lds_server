# -*- coding: utf-8 -*-
"""
从 新手战士.pcapng 提取官方高真实度响应包数据 (build_captured_data.py)
重构并输出 server/data/action_presets.pkl
包含:
1. triggers: Dict[str, List[List[bytes]]] - 按上行指令触发的完整下行帧批次列表
2. single_frames: Dict[str, bytes] - 单个下行帧按内部 _cmd 建立的兜底查找索引
"""

import os
import sys
import struct
import pickle
import collections
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(WORKSPACE_ROOT))

from server.protocol.cipher import undo_bytes
from server.protocol.amf3 import decode_amf3

PCAP_PATH = WORKSPACE_ROOT / "08-工具与代码" / "新手战士.pcapng"
OUT_PATH = WORKSPACE_ROOT / "server" / "data" / "action_presets.pkl"

print(f"Reading pcapng from {PCAP_PATH}...")
raw = PCAP_PATH.read_bytes()
size = len(raw)
offset = 0
all_chunks = []

while offset + 12 <= size:
    bt = raw[offset:offset+4]
    tot = struct.unpack("<I", raw[offset+4:offset+8])[0]
    if tot < 12 or offset + tot > size:
        break
    if bt == b"\x06\x00\x00\x00":
        body = raw[offset+8:offset+tot-4]
        frame = body[20:]
        if len(frame) >= 34 and frame[12:14] == b"\x08\x00":
            ihl = (frame[14] & 0x0f) * 4
            proto = frame[23]
            if proto == 6:
                tcp = frame[14+ihl:]
                if len(tcp) >= 20:
                    sp, dp, seq = struct.unpack(">HHI", tcp[:8])
                    doff = (tcp[12] >> 4) * 4
                    pl = tcp[doff:]
                    if pl and (sp == 8990 and dp == 21778 or sp == 21778 and dp == 8990) and len(pl) > 2 and set(pl) != {0}:
                        ts_h, ts_l = struct.unpack("<II", body[4:12])
                        ts = ((ts_h << 32) | ts_l) / 1000000.0
                        direction = "S2C" if sp == 8990 else "C2S"
                        all_chunks.append((ts, direction, seq, pl))
    offset += tot

all_chunks.sort(key=lambda x: x[0])

# 1. 重组 C2S 上行 TCP 流
c2s_chunks = [c for c in all_chunks if c[1] == "C2S"]
c2s_chunks.sort(key=lambda x: x[2])
expected_seq = c2s_chunks[0][2]
c2s_stream = bytearray()
chunk_times_c2s = []
for ts, dirn, seq, pl in c2s_chunks:
    if seq < expected_seq:
        c2s_stream = c2s_stream[:-(expected_seq - seq)]
    elif seq > expected_seq:
        c2s_stream.extend(b"\x00" * (seq - expected_seq))
    chunk_times_c2s.append((len(c2s_stream), ts))
    c2s_stream.extend(pl)
    expected_seq = seq + len(pl)

# 2. 重组 S2C 下行 TCP 流
s2c_chunks = [c for c in all_chunks if c[1] == "S2C"]
s2c_chunks.sort(key=lambda x: x[2])
expected_seq = s2c_chunks[0][2]
s2c_stream = bytearray()
chunk_times_s2c = []
for ts, dirn, seq, pl in s2c_chunks:
    if seq < expected_seq:
        s2c_stream = s2c_stream[:-(expected_seq - seq)]
    elif seq > expected_seq:
        s2c_stream.extend(b"\x00" * (seq - expected_seq))
    chunk_times_s2c.append((len(s2c_stream), ts))
    s2c_stream.extend(pl)
    expected_seq = seq + len(pl)

print(f"C2S Stream: {len(c2s_stream)} bytes, S2C Stream: {len(s2c_stream)} bytes")

# 3. 解析所有 C2S 请求包
c2s_pkts = []
off = 0
while off + 4 <= len(c2s_stream):
    flen = struct.unpack_from(">I", c2s_stream, off)[0]
    if flen == 0 or off + 4 + flen > len(c2s_stream):
        break
    pnum = struct.unpack_from(">I", c2s_stream, off + 4)[0]
    enc = bytes(c2s_stream[off + 8 : off + 4 + flen])
    dec = enc if pnum == 0 else bytes(undo_bytes(pnum, enc, char_id=88343134))
    t = chunk_times_c2s[0][1]
    for b_off, ts in chunk_times_c2s:
        if b_off <= off:
            t = ts
        else:
            break
    mtype = dec[0] if dec else -1
    action = struct.unpack_from(">H", dec, 1)[0] if len(dec) >= 3 else -1
    cmd = None
    if mtype == 0:
        cmd = f"SYS_{action}"
    elif mtype == 1:
        if action == 10033 and len(dec) >= 11:
            u_len = struct.unpack_from(">H", dec, 9)[0]
            if 11 + u_len <= len(dec):
                cmd = dec[11:11+u_len].decode("utf-8", errors="ignore")
        elif action == 10034:
            cmd = f"XT_B_{action}"
    c2s_pkts.append({"ts": t, "pnum": pnum, "cmd": cmd, "raw": bytes(c2s_stream[off:off+4+flen])})
    off += 4 + flen

# 4. 解析所有 S2C 下行帧
idx = s2c_stream.find(b"<cross-domain-policy>")
policy_off = s2c_stream.find(b"\x00", idx) + 1 if idx != -1 else 0

s2c_frames = []
single_frames = {}
off = policy_off
while off + 4 <= len(s2c_stream):
    flen = struct.unpack_from(">I", s2c_stream, off)[0]
    if flen == 0 or off + 4 + flen > len(s2c_stream):
        break
    frame_bytes = bytes(s2c_stream[off : off + 4 + flen])
    t = chunk_times_s2c[0][1]
    for b_off, ts in chunk_times_s2c:
        if b_off <= off:
            t = ts
        else:
            break
    cat = frame_bytes[4] if len(frame_bytes) >= 5 else -1
    cmd_name = None
    if cat == 1:
        try:
            obj = decode_amf3(frame_bytes[5:])
            cmd_name = str(obj.get("_cmd"))
        except Exception:
            pass
    elif cat == 0:
        act = struct.unpack_from(">H", frame_bytes, 5)[0] if len(frame_bytes) >= 7 else -1
        cmd_name = f"SYS_{act}"
    
    if cmd_name and cmd_name not in single_frames:
        single_frames[cmd_name] = frame_bytes
        
    s2c_frames.append({"ts": t, "len": flen, "cmd": cmd_name, "raw": frame_bytes})
    off += 4 + flen

print(f"Parsed {len(c2s_pkts)} C2S packets and {len(s2c_frames)} S2C frames.")

# 5. 按照时序关联 C2S 请求与下行响应批次
triggers = collections.defaultdict(list)
for i in range(len(c2s_pkts)):
    cpkt = c2s_pkts[i]
    next_ts = c2s_pkts[i+1]["ts"] if i + 1 < len(c2s_pkts) else 9999999999999.0
    matched = [sf for sf in s2c_frames if cpkt["ts"] <= sf["ts"] < next_ts]
    cmd = cpkt["cmd"]
    if matched and cmd:
        triggers[cmd].append([sf["raw"] for sf in matched])

# 6. 保存到 pickle 数据库
payload_to_save = {
    "triggers": dict(triggers),
    "single_frames": single_frames,
}

OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
with open(OUT_PATH, "wb") as f:
    pickle.dump(payload_to_save, f)

print(f"[OK] 成功生成 {OUT_PATH}:")
print(f"   Triggers: {len(triggers)} 个动作命令")
print(f"   Single frames: {len(single_frames)} 个独立帧映射")
