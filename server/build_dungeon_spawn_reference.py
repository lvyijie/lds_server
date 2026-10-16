# -*- coding: utf-8 -*-
"""从官方抓包重放生成 `server/data/dungeon_spawn_reference.json`（**可复现**）。

## 为什么需要这个脚本

`dungeon_spawn_reference.json` 是 `dungeon_spawn.json` 里 `posSource: "capture"` 的
唯一来源（也是全工程**仅有的 4 幕真值坐标**：fb10 永望角 1 幕 + fb14 诺兰密林 3 幕），
但**仓库里原本没有生成它的脚本** —— 那个文件是不可复现的手工产物。
本脚本从 pcapng 里把 2302 刷怪帧的 `x/y` 解出来重建它。

## 抓包里的真值从哪来

* `2302` = cmdAddMonsterWithInfo：每只怪一帧，带 `bi`（战场 id）/`cti`/`x`/`y`/`d`
* `15_0`  = 建本响应：`fb.id` → 副本 id，`fb.scenes[]` = `{id: bi, rn: 房间名}`
  ⇒ 由它把 `bi` 映射回 `(fbId, 房间名)`，才能把坐标归到某一幕

## 输出格式（与既有文件一致）

```json
{"_source": "...", "dungeons": {"14": {"scenes": {
    "fubennbuolanone": [[1, 93.0, 30.0, 0.0], ...],   // [类型序号, x, y, d]
}}}}
```

`类型序号 = cti % 100`（`mId = 难度前缀*100 + 类型序号`，全 41 副本一致）。
同一幕在多个抓包里都有时，**取怪物数最多的那一份**（更完整），
并在 `_sources` 里记下实际采用哪个包。

用法：
    python -m server.build_dungeon_spawn_reference                # 默认扫 netbag/*.pcapng
    python -m server.build_dungeon_spawn_reference --check        # 只比对不写文件
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "09-副本刷怪还原"))

OUT_PATH = Path(__file__).resolve().parent / "data" / "dungeon_spawn_reference.json"
DEFAULT_PCAPS = [
    ROOT / "netbag" / "老手.pcapng",
    ROOT / "netbag" / "战士诺兰.pcapng",
    ROOT / "netbag" / "战士死亡进入副本.pcapng",
    ROOT / "netbag" / "战士被怪物攻击.pcapng",
    ROOT / "08-工具与代码" / "新手战士.pcapng",
]


def parse_pcap(path: Path) -> Tuple[Dict[int, str], Dict[int, List[Tuple]]]:
    """返回 `(bi -> 房间名, bi -> [(cti, x, y, d), ...])`。"""
    from extract_official_spawns import (collect_tcp, iter_eth_frames,     # noqa: E402
                                         reassemble, scan_frames)
    streams = collect_tcp(iter_eth_frames(path.read_bytes()))
    best = None
    for key, chunks in streams.items():
        sp, dp = key
        if sp in (80, 443, 8443) or dp in (80, 443, 8443):
            continue
        if len(chunks) < 20:
            continue
        s2c = reassemble(chunks)
        objs = scan_frames(s2c)
        if objs and (best is None or len(objs) > len(best[1])):
            best = (key, objs)
    if best is None:
        return {}, {}
    bi_to_room: Dict[int, str] = {}
    spawns: Dict[int, List[Tuple]] = {}
    for obj in best[1]:
        cmd = str(obj.get("_cmd"))
        if cmd == "15_0":
            for sc in ((obj.get("fb") or {}).get("scenes") or []):
                if isinstance(sc, dict) and sc.get("rn") and sc.get("id") is not None:
                    bi_to_room[int(sc["id"])] = str(sc["rn"])
        elif cmd == "2302":
            bi, cti = obj.get("bi"), obj.get("cti")
            if bi is None or cti is None:
                continue
            try:
                x = float(obj.get("x") or 0.0)
                y = float(obj.get("y") or 0.0)
                d = float(obj.get("d") or 0.0)
            except (TypeError, ValueError):
                continue
            spawns.setdefault(int(bi), []).append((int(cti), x, y, d))
    return bi_to_room, spawns


def _scene_name_from_room(rn: str) -> str:
    """把服务端的房间实例名还原成场景名。

    抓包里的 `rn` 形如 `fubennbuolanone_u88343134_0_fb`
    （`<场景名>_u<角色id>_<第几次>_fb`），必须剥掉后缀才能查 FbConfig 的场景表。
    """
    i = rn.find("_u")
    return rn[:i] if i > 0 else rn


def load_fb_id_by_room() -> Dict[str, int]:
    """由 `FbConfig.as` 给出的副本场景列表 → {房间名: fbId}。"""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from build_dungeon_spawn import (load_scene_names, parse_fb_defines,   # noqa: E402
                                         parse_fb_scenes)
        text = (ROOT / "09-副本刷怪还原" / "fbsource_raw" / "scripts" / "mmo" / "fb"
                / "model" / "FbConfig.as").read_text(encoding="utf-8", errors="replace")
        dungeons = parse_fb_defines(text, load_scene_names())
        out: Dict[str, int] = {}
        for fb_id, d in dungeons.items():
            for sn in d["scenes"]:
                out[str(sn)] = int(fb_id)
        return out
    except Exception:                                          # noqa: BLE001
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比对，不写文件")
    ap.add_argument("pcaps", nargs="*")
    args = ap.parse_args(argv)

    pcaps = [Path(p) for p in args.pcaps] if args.pcaps else DEFAULT_PCAPS
    room_to_fb = load_fb_id_by_room()
    print("[i] FbConfig 房间表 %d 条" % len(room_to_fb))

    #: {(fbId, 房间名): (怪物数, 来源包, layout)}
    best: Dict[Tuple[int, str], Tuple[int, str, List[List]]] = {}
    per_pcap: Dict[str, int] = {}
    for p in pcaps:
        if not p.is_file():
            continue
        bi_to_room, spawns = parse_pcap(p)
        n_frames = sum(len(v) for v in spawns.values())
        if n_frames == 0:
            continue
        per_pcap[p.name] = n_frames
        for bi, lst in spawns.items():
            rn = bi_to_room.get(bi)
            if not rn:
                continue
            sn = _scene_name_from_room(rn)
            fb_id = room_to_fb.get(sn)
            if fb_id is None:
                continue
            layout = [[t % 100, x, y, d] for (t, x, y, d) in lst]
            key = (fb_id, sn)
            cur = best.get(key)
            # 同一幕取怪物数最多的那份抓包（更完整）
            if cur is None or len(layout) > cur[0]:
                best[key] = (len(layout), p.name, layout)
        print("  [解析] %-28s 2302 帧 %3d，已映射 %d 幕"
              % (p.name, n_frames, len(bi_to_room)))

    dungeons: Dict[str, Dict] = {}
    sources: Dict[str, str] = {}
    for (fb_id, rn), (cnt, src, layout) in sorted(best.items()):
        dungeons.setdefault(str(fb_id), {"scenes": {}})["scenes"][rn] = layout
        sources["%d/%s" % (fb_id, rn)] = src

    doc = {
        "_source": "官方抓包 2302 刷怪帧实测坐标。由 server/build_dungeon_spawn_reference.py "
                   "从 pcapng 重放生成（可复现）。每幕一行 [类型序号, x, y, d]，"
                   "mId = 难度前缀 * 100 + 类型序号。",
        "_note": "同一幕在多个抓包里都有时取怪物数最多的那一份；"
                 "实测坐标只覆盖极少数副本（其余幕必须用场景入口点生成，见 "
                 "server/build_scene_entrances.py）。",
        "_sources": sources,
        "dungeons": dungeons,
    }

    if args.check:
        old = {}
        if OUT_PATH.is_file():
            old = json.loads(OUT_PATH.read_text(encoding="utf-8")).get("dungeons") or {}
        same = diff = 0
        for fid, d in doc["dungeons"].items():
            for sn, layout in d["scenes"].items():
                cur = ((old.get(fid) or {}).get("scenes") or {}).get(sn)
                if cur == layout:
                    same += 1
                else:
                    diff += 1
                    print("  [差异] fb%s %s\n     新: %s\n     旧: %s" % (fid, sn, layout, cur))
        for fid, d in old.items():
            for sn in d.get("scenes", {}):
                if sn not in ((doc["dungeons"].get(fid) or {}).get("scenes") or {}):
                    diff += 1
                    print("  [缺失] fb%s %s 旧文件里有、抓包里没解出" % (fid, sn))
        print("\n[cmp] 一致 %d 幕，有差异 %d 幕" % (same, diff))
        return 1 if diff else 0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n[OK] %d 个副本 / %d 幕 -> %s"
          % (len(dungeons), sum(len(d["scenes"]) for d in dungeons.values()), OUT_PATH))
    for fid, d in sorted(dungeons.items()):
        for sn, layout in sorted(d["scenes"].items()):
            print("     fb%-3s %-24s %d 只  (来源 %s)"
                  % (fid, sn, len(layout), sources.get("%s/%s" % (fid, sn))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
