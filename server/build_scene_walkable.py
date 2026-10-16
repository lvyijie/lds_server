# -*- coding: utf-8 -*-
"""从客户端场景 SWF 生成「可行走掩码」表（副本刷怪坐标吸附用）。

## 为什么需要它

副本怪的刷怪坐标来自官方抓包（`dungeon_spawn.json` 的 `posSource: "capture"`），
**但官方是服务端驱动怪物的**（2451 直接让怪走过去，完全无视可行走网格），
所以我们照抄坐标没问题、官方也从不觉得有问题。

而本工程的一条链路改成了**客户端自管怪**（见
`06-文档/方案-怪物AI归属与掉血链路.md`）：怪的移动靠客户端
`CMAIAChaseState.chasingByAStar()` → `PathFinder`。
**起点落在不可走格 ⇒ 寻不出路径 ⇒ 这只怪永远卡住**：
玩家走不到它、宠物也够不到它、本幕永远清不空 ⇒ 传送门不开。

实测：`fubennbuolanone`（fb14 幕1）5 个刷怪点里 `(49,31)` 落在障碍区（周围 7×7 全阻），
而该坐标在 4 个难度里都出现。

## 可行走规则（**逐字复现客户端**，不要凭感觉改）

`AbstractScene.setMapArray()`：

```as3
_loc1_ = sceneWidth / gridSize;          // 列数
_loc2_ = sceneHeight / gridSize;         // 行数
_loc3_ = gridSize;                       // 8
_loc4_ = gridSize * 0.5;                 // 4  ← 取样点 = 格中心
_loc7_.draw(this.obStruction);           // 把 obstruction 层画进 BitmapData
// 逐格：
_loc10_ = col * gridSize + _loc4_;
_loc11_ = row * gridSize + _loc4_;
if((pixel32 & 0xFF0000) > 8323072)       // R > 0x7F  → 可走（抬升层）
      _raiseArray=1; _obArray=0;
else if(pixel32 != 0)                    // 非透明其它 → 障碍
      _obArray=1;
else                                     // 全透明 → 空（也不算障碍）
      _obArray=0;
```

⇒ 可走判定 = `alpha != 0 且 (R << 16) > 0x7F0000`，即 **R > 127**。
`gridSize` 恒为 8（`AbstractSceneHelper.getGridSize()`）；
场景宽高是**每场景配置**（`BigAbstractSceneHelper` → `ConfigReader`，
见 `04-data-json/game-data/06-scenes/SceneData_dataClass.xml` 的 `width/height`）。

## 输出

`server/data/scene_walkable.json`：

```json
{"gridSize": 8, "_source": "...", "scenes": {
   "fubennbuolanone": {"w": 200, "h": 84, "mask": "<base64 位图，行优先，1=可走>"},
   ...}}
```

用法：
    python -m server.build_scene_walkable                 # 全量（约 146 个场景）
    python -m server.build_scene_walkable --limit 5       # 先跑 5 个试试
    python -m server.build_scene_walkable --only fubennbuolanone
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
SCENE_XML = ROOT / "04-data-json" / "game-data" / "06-scenes" / "SceneData_dataClass.xml"
SCENE_SWF_DIRS = [
    ROOT / "07-swf" / "downloaded_core" / "play" / "scene",
    ROOT / "07-原始SWF" / "核心downloaded_core" / "play" / "scene",
]
FFDEC = ROOT / "08-工具与代码" / "ffdec_26.3.0" / "ffdec.jar"
JAVA_CANDIDATES = [
    ROOT / "08-工具与代码" / "OpenJDK8U-jdk_x64_windows_hotspot_8u504b01"
         / "jdk8u504-b01" / "bin" / "java.exe",
]
OUT_PATH = Path(__file__).resolve().parent / "data" / "scene_walkable.json"
GRID_SIZE = 8
#: 客户端判可走的阈值（`(pixel & 0xFF0000) > 8323072` ⇒ R > 127）
WALK_RED_THRESHOLD = 8323072


# ── 工具定位 ────────────────────────────────────────────────────────────────
def find_java() -> str:
    for cand in JAVA_CANDIDATES:
        if cand.is_file():
            return str(cand)
    found = shutil.which("java")
    if found:
        return found
    raise SystemExit("[X] 找不到 java，无法调用 ffdec。请用 --java 指定。")


def find_ffdec() -> str:
    if FFDEC.is_file():
        return str(FFDEC)
    for p in (ROOT / "08-工具与代码").rglob("ffdec.jar"):
        return str(p)
    raise SystemExit("[X] 找不到 ffdec.jar")


def find_scene_swf(scene_name: str) -> Optional[Path]:
    for d in SCENE_SWF_DIRS:
        if not d.is_dir():
            continue
        for name in ("%s~1000.swf" % scene_name, "%s.swf" % scene_name):
            p = d / name
            if p.is_file():
                return p
    return None


# ── 场景表 ──────────────────────────────────────────────────────────────────
def load_scene_sizes() -> Dict[str, Tuple[int, int]]:
    """`SceneData_dataClass.xml` → {场景名: (width, height)}（像素）。"""
    sizes: Dict[str, Tuple[int, int]] = {}
    if not SCENE_XML.is_file():
        return sizes
    root = ET.parse(SCENE_XML).getroot()
    for node in root.iter():
        nm = node.get("nm")
        if not nm:
            continue
        try:
            w = int(node.get("width") or 0)
            h = int(node.get("height") or 0)
        except ValueError:
            continue
        if w > 0 and h > 0:
            sizes[nm] = (w, h)
    return sizes


# ── 单场景：导出图 → 算掩码 ─────────────────────────────────────────────────
def export_images(java: str, ffdec: str, swf: Path, out_dir: Path) -> List[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [java, "-jar", ffdec, "-export", "image", str(out_dir), str(swf)],
            capture_output=True, timeout=180, check=False,
        )
    except Exception:
        return []
    return sorted(out_dir.rglob("*.png"))


def _png_size(path: Path) -> Optional[Tuple[int, int]]:
    try:
        with open(path, "rb") as fp:
            head = fp.read(24)
        if head[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        import struct
        w, h = struct.unpack(">II", head[16:24])
        return int(w), int(h)
    except OSError:
        return None


def _png_profile(path: Path, sample: int = 3) -> Optional[Tuple[int, int, float, float]]:
    """返回 `(w, h, 透明比例, 红比例)`；不是 PNG 或读不了时返回 None。"""
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("[X] 需要 Pillow：请用自带 Python 运行（含 Pillow）")
    try:
        im = Image.open(path).convert("RGBA")
    except Exception:                                          # noqa: BLE001
        return None
    W, H = im.size
    if W < 2 or H < 2:
        return None
    px = im.load()
    n = trans = red = 0
    for y in range(0, H, sample):
        for x in range(0, W, sample):
            A = px[x, y][3]
            n += 1
            if A == 0:
                trans += 1
            elif (px[x, y][0] << 16) > WALK_RED_THRESHOLD:
                red += 1
    if n == 0:
        return None
    return W, H, trans / float(n), red / float(n)


#: 挑图时的透明比例上限。⚠ 早期设成 2% 是**错的**：真 obstruction 层允许有透明
#: （客户端规则里**透明 = 空 = 可走**），实测 `castlegarden` **正确**的那张是
#: 1441×564 / 15.4% 透明，被 2% 门禁误杀，结果选中一张高度只有 380 的局部层
#: （覆盖不到入口那几行）⇒ 掩码全错。
MAX_TRANSPARENT_RATIO = 0.45

#: 可走比例的合理区间（超出即判为"选错图层"，宁可不输出）
MIN_WALK_RATIO, MAX_WALK_RATIO = 0.05, 0.95

#: **自校验门禁**：**只对 `posSource == "capture"` 的幕生效**。
#:
#: ⚠ 这里踩过一个坑，务必看清坐标的真实来源：
#:   `dungeon_spawn.json` 里 **只有 4 幕**（fb10 永望角 1 幕 + fb14 诺兰密林 3 幕）
#:   的坐标是抓包实测的（`posSource: "capture"`）；**其余 142 幕是 `reconstructed`**，
#:   而 `build_dungeon_spawn.slots_to_positions()` 是从一份**硬编码的 13 个诺兰密林
#:   坐标**里循环取（实测 1694/1694 = 100%），**完全没看目标地图**。
#:   ⇒ 对 reconstructed 幕，"官方坐标应当可走"这个前提**根本不成立**，
#:     拿它做校验会把**正确**的掩码全部拒掉（覆盖率曾因此从 53 个掉到 23 个）。
#:   ⇒ 只对 capture 幕做自校验。
MIN_SPAWN_WALKABLE_RATIO = 0.80
#: 刷怪点太少时比例噪声大，不足以自校验
MIN_SPAWN_POINTS_FOR_VALIDATION = 3


def _sample_walkable(path: Path, gx: int, gy: int) -> Optional[bool]:
    """按客户端的格中心取样规则判断该图在 (gx,gy) 是否可走；图不覆盖该点返回 None。"""
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("[X] 需要 Pillow：请用自带 Python 运行（含 Pillow）")
    try:
        with Image.open(path) as im:
            rgba = im.convert("RGBA")
    except Exception:                                          # noqa: BLE001
        return None
    W, H = rgba.size
    x0 = gx * GRID_SIZE + GRID_SIZE // 2
    y0 = gy * GRID_SIZE + GRID_SIZE // 2
    if x0 >= W or y0 >= H or x0 < 0 or y0 < 0:
        return None
    R, _G, _B, A = rgba.load()[x0, y0]
    return A != 0 and (R << 16) > WALK_RED_THRESHOLD


def pick_obstruction(pngs: List[Path], expect: Tuple[int, int],
                     entrance: Optional[Tuple[float, float]] = None) -> Optional[Path]:
    """挑出 `obstruction` 层。

    ## 判据（按优先级）

    1. **图必须覆盖入口点，且在入口点可走**（该场景有入口点时）。
       入口点 = 场景类 `super(x,y)`（`server/build_scene_entrances.py`），
       即**玩家进入该场景站的位置** ⇒ 必然是这块地图上可走的地面。
       这是**每场景独有的权威真值**，比任何"图像特征"可靠：
       实测只靠图像特征挑时，121 个有入口的掩码里 **57 个（47%）入口点不可走** ⇒ 掩码全错。
    2. 尺寸最接近该场景配置宽高。
    3. 没有入口点时，退回"透明比例不太高"。

    ⚠ **不能**用"透明≈0"当硬判据，见 `MAX_TRANSPARENT_RATIO`。
    """
    scored: List[Tuple[Tuple[int, float], Path]] = []
    fallback: List[Tuple[float, Path]] = []
    for p in pngs:
        size = _png_size(p)
        if not size or size[0] < 400:
            continue
        w, h = size
        dw = abs(w - expect[0]) / max(1.0, expect[0])
        dh = abs(h - expect[1]) / max(1.0, expect[1])
        score = dw + dh
        if entrance is not None:
            gx, gy = int(round(entrance[0])), int(round(entrance[1]))
            if _sample_walkable(p, gx, gy) is True:
                scored.append(((0, score), p))
            continue
        prof = _png_profile(p)
        trans = prof[2] if prof else 1.0
        if trans <= MAX_TRANSPARENT_RATIO:
            fallback.append((score, p))
    if scored:
        return min(scored, key=lambda kv: kv[0])[1]
    if fallback:
        fallback.sort(key=lambda kv: kv[0])
        return fallback[0][1]
    return None


def build_mask(png: Path, expect: Tuple[int, int]) -> Optional[Tuple[int, int, bytes]]:
    """按客户端规则算可行走位图。返回 `(cols, rows, packed_bits)`，位=1 表示可走。"""
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("[X] 需要 Pillow：请用自带 Python 运行（含 Pillow）")
    im = Image.open(png).convert("RGBA")
    W, H = im.size
    px = im.load()
    cols = min(W // GRID_SIZE, max(1, expect[0] // GRID_SIZE))
    rows = min(H // GRID_SIZE, max(1, expect[1] // GRID_SIZE))
    half = GRID_SIZE // 2
    bits = bytearray((cols * rows + 7) // 8)
    for c in range(cols):
        for r in range(rows):
            x = c * GRID_SIZE + half
            y = r * GRID_SIZE + half
            if x >= W or y >= H:
                continue
            R = px[x, y][0]
            A = px[x, y][3]
            if A != 0 and (R << 16) > WALK_RED_THRESHOLD:
                i = c * rows + r
                bits[i >> 3] |= 1 << (i & 7)
    return cols, rows, bytes(bits)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--keep-tmp", action="store_true")
    args = ap.parse_args(argv)

    java, ffdec = find_java(), find_ffdec()
    sizes = load_scene_sizes()
    print("[i] 场景表 %d 条；java=%s" % (len(sizes), java))

    # 刷怪表里实际用到的场景优先（其余也一起做，便于以后用）
    spawn_doc = json.loads((Path(__file__).resolve().parent / "data" / "dungeon_spawn.json")
                           .read_text(encoding="utf-8"))
    used = {}
    capture_scenes = set()
    for d in spawn_doc.get("dungeons") or []:
        for sc in d.get("scenes") or []:
            used.setdefault(sc["sn"], []).extend(
                (int(s["x"]), int(s["y"])) for w in (sc.get("waves") or {}).values() for s in w)
            # ⚠ 只有 posSource=="capture" 的幕坐标才真属于本图（详见常量注释）。
            #   其余 142 幕是从诺兰密林抄的 13 个坐标，不能当校验依据。
            if (sc.get("posSource") or "") == "capture":
                capture_scenes.add(str(sc["sn"]))
    print("[i] 抓到实测坐标的幕（可做自校验）: %s" % (sorted(capture_scenes) or "无"))

    #: 场景入口点（场景类 `super(x,y)`）—— 挑图与校验的**权威真值**：
    #: 玩家进入该场景站的位置，必然可走。
    entrances: Dict[str, Tuple[float, float]] = {}
    ents_path = Path(__file__).resolve().parent / "data" / "scene_entrances.json"
    if ents_path.is_file():
        try:
            for sn_, rec_ in (json.loads(ents_path.read_text(encoding="utf-8"))
                              .get("scenes") or {}).items():
                entrances[str(sn_)] = (float(rec_["gx"]), float(rec_["gy"]))
        except (ValueError, KeyError, TypeError):
            entrances = {}
    print("[i] 场景入口点 %d 个（用于挑图 + 硬校验）" % len(entrances))

    names = [args.only] if args.only else sorted(sizes.keys())
    if args.limit:
        names = names[:args.limit]

    out: Dict[str, Dict] = {}
    # ⚠ 不用系统临时目录：DSH 沙箱不允许写 `%TEMP%`。放在工程内。
    tmp_root = ROOT / "scratch" / "_scenemask"
    tmp_root.mkdir(parents=True, exist_ok=True)
    bad_total, snapped_total, checked_total = 0, 0, 0
    try:
        for idx, sn in enumerate(names, 1):
            swf = find_scene_swf(sn)
            if swf is None:
                print("  [%3d/%d] %-28s 无场景 SWF，跳过" % (idx, len(names), sn))
                continue
            expect = sizes.get(sn) or (960, 560)
            ent = entrances.get(str(sn))
            pngs = export_images(java, ffdec, swf, tmp_root / sn)
            png = pick_obstruction(pngs, expect, ent)
            if png is None:
                print("  [%3d/%d] %-28s 未找到 obstruction 图（导出 %d 张）"
                      % (idx, len(names), sn, len(pngs)))
                continue
            res = build_mask(png, expect)
            if res is None:
                continue
            cols, rows, bits = res
            # **硬门禁 0（最强）：入口点必须可走**。
            #   入口点 = 场景类 super(x,y) = 玩家进入该场景站的位置 ⇒ 必然可走。
            #   实测只靠图像特征挑图时，121 个有入口的掩码里 57 个（47%）入口点不可走，
            #   那些掩码全是错的（用它们吸附会把怪丢到随机位置）。
            if ent is not None:
                gx, gy = int(round(ent[0])), int(round(ent[1]))
                inside = 0 <= gx < cols and 0 <= gy < rows
                ent_ok = bool(inside and (bits[(gx * rows + gy) >> 3] >> ((gx * rows + gy) & 7)) & 1)
                if not ent_ok:
                    print("  [%3d/%d] %-28s 入口点(%d,%d)不可走 -> 掩码判为错位，拒绝输出"
                          % (idx, len(names), sn, gx, gy))
                    continue
                ent_flag = " | 入口点(%d,%d)可走 [OK]" % (gx, gy)
            else:
                ent_flag = " | 无入口点数据"
            # 兜底门禁 1：可走比例不合理 ⇒ 判为选错图层，不输出（宁缺勿滥）
            n_cells = cols * rows
            walk_cells = sum(bin(b).count("1") for b in bits)
            ratio = walk_cells / float(n_cells) if n_cells else 0.0
            if not (MIN_WALK_RATIO <= ratio <= MAX_WALK_RATIO):
                print("  [%3d/%d] %-28s 可走率 %.1f%% 不合理，拒绝输出该掩码"
                      % (idx, len(names), sn, ratio * 100))
                continue

            # 兜底门禁 2（**自校验，仅 capture 幕**）：坐标真属于本图时，
            # 正确掩码应当接受其中绝大多数。reconstructed 幕的坐标是从别的图抄来的，
            # 不能用来判断掩码对错（详见常量注释）。
            pts = sorted(set(used.get(sn) or []))
            is_capture = sn in capture_scenes
            if is_capture and len(pts) >= MIN_SPAWN_POINTS_FOR_VALIDATION:
                good = 0
                for (px_, py_) in pts:
                    i = px_ * rows + py_
                    if 0 <= px_ < cols and 0 <= py_ < rows and (bits[i >> 3] >> (i & 7)) & 1:
                        good += 1
                spawn_ok = good / float(len(pts))
                if spawn_ok < MIN_SPAWN_WALKABLE_RATIO:
                    print("  [%3d/%d] %-28s 自校验失败：实测刷怪点可走率 %.0f%%（%d/%d），"
                          "判为掩码错位，拒绝输出"
                          % (idx, len(names), sn, spawn_ok * 100, good, len(pts)))
                    continue
                flagged = " | capture 幕自校验通过：刷怪点可走 %d/%d" % (good, len(pts))
            elif is_capture:
                flagged = " | capture 幕但刷怪点不足 %d 个，无法自校验" % MIN_SPAWN_POINTS_FOR_VALIDATION
            else:
                flagged = " | 重建幕(坐标抄自他图) -> 不做坐标自校验"
            out[sn] = {"w": cols, "h": rows, "mask": base64.b64encode(bits).decode("ascii")}

            # 统计（用于最后的汇总）：该场景有多少刷怪点仍需吸附
            for (x, y) in set(used.get(sn) or []):
                checked_total += 1
                i = x * rows + y
                ok = 0 <= x < cols and 0 <= y < rows and (bits[i >> 3] >> (i & 7)) & 1
                if not ok:
                    bad_total += 1
            print("  [%3d/%d] %-28s %dx%d 图 %dx%d%s%s"
                  % (idx, len(names), sn, cols, rows,
                     _png_size(png)[0], _png_size(png)[1], ent_flag, flagged))
    finally:
        if not args.keep_tmp:
            shutil.rmtree(tmp_root, ignore_errors=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "_source": "07-swf/downloaded_core/play/scene/*.swf 的 obstruction 层，"
                   "按 AbstractScene.setMapArray 的规则（R>127 且不透明 = 可走）",
        "gridSize": GRID_SIZE,
        "sceneCount": len(out),
        "scenes": out,
    }, ensure_ascii=False), encoding="utf-8")
    print("\n[OK] %d 个场景掩码 → %s" % (len(out), OUT_PATH))
    print("     校验刷怪点 %d 个，其中落在不可走格 %d 个（这些会被运行期吸附）"
          % (checked_total, bad_total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
