# -*- coding: utf-8 -*-
"""抽取每个场景的**入口点**（玩家进入该场景时站的位置）。

## 为什么需要它

`dungeon_spawn.json` 里曾有 **142/146 幕**的刷怪坐标是 `reconstructed`，
而 `build_dungeon_spawn.slots_to_positions()` 只是从一份**硬编码的 13 个诺兰密林坐标**
里循环取（实测 1694/1694 = 100%），**完全没看目标地图**。后果：

* 怪被刷在**别的地图**的坐标上 ⇒ 落到墙里/背景装饰上
* 客户端 `PathFinder.searchnearpoint(pos, 5)` 只能把起点吸附到 **5 格内**的可走格
  （`PathFinder.as:107`），超出这个半径的怪**真的走不出来**

入口点则一定是**这块地图上可走的地面**（玩家就站在那里），所以它可以当
「刷怪位锚点」和「掩码正确性校验」的权威真值。

## 两个数据源（实测**零重叠、完全互补**，必须都用）

1. **配置 `initPoint`**：`SceneData_dataClass.xml` 的 `@initPoint="x_y"`（像素）。
   客户端 `SceneData.as:72` 读的就是它，`ConfigReader.sceneMaps[s].initPoint`。
   实测 833 个场景有，**主要是野外场景**。
2. **场景类构造器 `super(x, y)`**：副本场景不读配置，而是在自己的类里写死
   （`FBSceneBase(param1=-1, param2=-1)`：传了正数就用传的，否则回落到
   `ConfigReader...initPoint`，见 `FBSceneBase.as:14-32`）。
   实测 **74 个副本幕走这条**。

并集对 146 个副本场景覆盖 **145 个（99%）**（唯一缺的 `yongwanggethree`
是 capture 幕，本来就有实测坐标）。⚠ 单位是**像素**，落格要除以 `gridSize`（=8）。

## 输出

`server/data/scene_entrances.json`：

```json
{"gridSize": 8, "_source": "...", "scenes": {
   "fubennbuolanone": {"src": "class", "cls": "FubenNuoLanOne", "px": 300, "py": 460,
                       "gx": 37.5, "gy": 57.5},
   "chiseyunhai3":    {"src": "config", "px": 150, "py": 500, "gx": 18.75, "gy": 62.5},
   ...}}
```

用法：
    python -m server.build_scene_entrances
    python -m server.build_scene_entrances --no-class   # 只用配置源（快）
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Optional, Tuple

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
OUT_PATH = Path(__file__).resolve().parent / "data" / "scene_entrances.json"
GRID_SIZE = 8

#: 场景类 = `extends FBSceneBase`（副本/大场景）。
#: ⚠ 必须容忍 `dynamic`：实测 23 个场景类写作 `public dynamic class X extends FBSceneBase`，
#:   早期正则漏了 `dynamic` 导致这些场景取不到入口点。
CLASS_RE = re.compile(r"class\s+(?:dynamic\s+)?(\w+)\s+extends\s+(FBSceneBase|BigSceneBase|SceneBase)\b")
SUPER_RE = re.compile(r"\bsuper\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)")


def find_java() -> str:
    for cand in JAVA_CANDIDATES:
        if cand.is_file():
            return str(cand)
    found = shutil.which("java")
    if found:
        return found
    raise SystemExit("[X] 找不到 java，无法调用 ffdec。")


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


# ── 源 1：配置 initPoint ────────────────────────────────────────────────────
def load_config_points() -> Dict[str, Tuple[float, float]]:
    """`SceneData_dataClass.xml` 的 `@initPoint="x_y"`（像素）。"""
    out: Dict[str, Tuple[float, float]] = {}
    if not SCENE_XML.is_file():
        return out
    try:
        root = ET.parse(SCENE_XML).getroot()
    except ET.ParseError:
        return out
    for node in root.iter():
        nm, ip = node.get("nm"), node.get("initPoint")
        if not nm or not ip:
            continue
        try:
            x, y = (float(v) for v in ip.split("_"))
        except ValueError:
            continue
        out[str(nm)] = (x, y)
    return out


# ── 源 2：场景类 super(x, y) ────────────────────────────────────────────────
def export_scripts(java: str, ffdec: str, swf: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [java, "-jar", ffdec, "-export", "script", str(out_dir), str(swf)],
            capture_output=True, timeout=180, check=False,
        )
    except Exception:                                          # noqa: BLE001
        pass


def parse_entrance(script_dir: Path) -> Optional[Tuple[str, int, int]]:
    """在导出的脚本里找「场景类 + 它的 super(x, y)」。

    ⚠ 不能全局搜第一个 `super(d,d)`：包里还有 `ColorMatrix.super(5,5)` 这类无关类。
     必须先定位 `extends FBSceneBase` 的类，再在其构造器之后取 super。
    ⚠ `super()`（无参）是合法的：那表示复用 `FBSceneBase` 的默认参数 ⇒ 回落到配置
       `initPoint`（`FBSceneBase.as:20-31`）。所以取不到数字时返回 None，由调用方回落。
    """
    for f in sorted(script_dir.rglob("*.as")):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = CLASS_RE.search(text)
        if not m:
            continue
        cls = m.group(1)
        ctor = re.search(r"function\s+%s\s*\(\s*\)\s*:?\s*\w*\s*\{" % re.escape(cls), text)
        body = text[ctor.end():] if ctor else text
        sm = SUPER_RE.search(body)
        if sm:
            return cls, int(sm.group(1)), int(sm.group(2))
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-class", action="store_true", help="只用配置 initPoint（快）")
    ap.add_argument("--only", default="")
    args = ap.parse_args(argv)

    out: Dict[str, Dict] = {}
    cfg = load_config_points()
    for sn, (x, y) in cfg.items():
        out[sn] = {"src": "config", "px": x, "py": y,
                   "gx": round(x / GRID_SIZE, 3), "gy": round(y / GRID_SIZE, 3)}
    print("[i] 配置 initPoint: %d 个场景" % len(out))

    if not args.no_class:
        java, ffdec = find_java(), find_ffdec()
        # 只需处理还没有入口点的场景（配置源已覆盖的不用再反编译）
        targets = ([args.only] if args.only
                   else [sn for sn in load_scene_names() if sn not in out])
        print("[i] 待反编译补入口点的场景 %d 个（src=class）" % len(targets))
        tmp_root = ROOT / "scratch" / "_sceneentr"
        tmp_root.mkdir(parents=True, exist_ok=True)
        got = 0
        for idx, sn in enumerate(targets, 1):
            swf = find_scene_swf(sn)
            if swf is None:
                continue
            d = tmp_root / sn
            if not d.is_dir():
                export_scripts(java, ffdec, swf, d)
            parsed = parse_entrance(d)
            if not parsed:
                continue
            cls, px, py = parsed
            out[sn] = {"src": "class", "cls": cls, "px": px, "py": py,
                       "gx": round(px / GRID_SIZE, 3), "gy": round(py / GRID_SIZE, 3)}
            got += 1
        print("[i] 场景类 super(x,y) 补到 %d 个" % got)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps({
        "_source": "① SceneData_dataClass.xml 的 @initPoint（ConfigReader.sceneMaps[].initPoint）；"
                   "② 场景 SWF 里场景类构造器的 super(x,y)。单位均为像素，"
                   "两者零重叠互补 —— 副本场景走②，野外场景走①。"
                   "该点 = 玩家进入该场景的落点 ⇒ 必然可走，用作刷怪锚点与掩码校验真值。",
        "gridSize": GRID_SIZE,
        "sceneCount": len(out),
        "scenes": out,
    }, ensure_ascii=False), encoding="utf-8")
    print("\n[OK] %d 个场景入口点 -> %s" % (len(out), OUT_PATH))
    for sn in ("fubennbuolanone", "wutuobanglevelone", "chiseyunhai3", "doublemoonsquare"):
        r = out.get(sn)
        print("     %-22s %s super(%s,%s) -> 格 (%.1f, %.1f)"
              % (sn, r["src"], r["px"], r["py"], r["gx"], r["gy"]) if r else "     %s 无" % sn)
    return 0


def load_scene_names() -> list:
    if not SCENE_XML.is_file():
        return []
    try:
        root = ET.parse(SCENE_XML).getroot()
    except ET.ParseError:
        return []
    names = []
    for node in root.iter():
        nm = node.get("nm")
        if nm and (node.get("swf") or "").startswith("scene/"):
            names.append(nm)
    return sorted(set(names))


if __name__ == "__main__":
    sys.exit(main())
