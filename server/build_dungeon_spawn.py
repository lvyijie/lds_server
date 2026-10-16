# -*- coding: utf-8 -*-
"""生成副本刷怪配置表 server/data/dungeon_spawn.json（build_dungeon_spawn.py）。

## 数据来源（全部是工作区内的官方客户端资源，非推测）

    09-副本刷怪还原/fbsource_raw/scripts/mmo/fb/model/FbConfig.as
        ├─ fbDefines      → 副本表：fbId / 名称 / 场景列表 / 准入等级
        └─ fbSceneDefines → 场景表：<场景>_<难度> → 类型 / 通关条件 / 需击杀怪
    09-副本刷怪还原/SceneNames.as
        └─ SceneNames.X 常量 → 场景名字面量
    server/data/dungeon_monsters.json
        └─ 41 个 aiscript SWF 内嵌 XML 导出的怪物数值（等级/血量/攻防/AI）
    server/data/dungeon_spawn_reference.json
        └─ 抓包实测的每幕刷怪构成与坐标（fb10 永望角 / fb14 诺兰密林）

## 怪物 ID 结构（实测确认，全 41 副本一致）

    mId = <难度前缀> * 100 + <类型序号>
        例 fb14 诺兰密林：前缀 373/374/375/427，类型序号 01..05
            37301 = 难度1 蓝蓝毛贼   37401 = 难度2 蓝蓝毛贼   42701 = 难度4 蓝蓝毛贼

    前缀按等级升序 = 难度档位 1/2/3/4，与 15_0 的 `fb.lv` 一一对应：
        抓包(老手/战士诺兰.pcapng) fb14 lv=2 → 实际刷的是 374xx（=第 2 个前缀）
        FbConfig fb1 场景定义 "_2" 需击杀 3107（第 2 个前缀）、"_3" 需击杀 3207（第 3 个）

## 每幕刷怪构成

    有抓包的副本直接用实测构成（`dungeon_spawn_reference.json`）：
        fb10 永望角 yongwanggethree : 类型2×4 + 类型6×1（类型6 = 波利王 rk2 头目）
        fb14 诺兰密林 三幕：
            一幕 fubennbuolanone  : 类型1×2 + 类型2×3
            二幕 fubennbuolantwo  : 类型3×2 + 类型4×3
            三幕 fubennbuolanthree: 类型4×2 + 类型5×1（类型5 = 蓝蓝大头目 rk2）
    其余副本按同一规律重建：非头目类型平均分给前面各幕，最后一幕收尾
    （倒数第二个类型 + 全部 rk>=2 头目）。JSON 里以 `posSource` 标注来源。

用法
----
    python server/build_dungeon_spawn.py
    python server/build_dungeon_spawn.py --outdir server/data
"""
from __future__ import annotations

import argparse
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SERVER_DIR = Path(__file__).resolve().parent
WS = SERVER_DIR.parent

FBCONFIG = (WS / "09-副本刷怪还原" / "fbsource_raw" / "scripts" / "mmo" / "fb"
            / "model" / "FbConfig.as")
SCENENAMES = WS / "09-副本刷怪还原" / "SceneNames.as"
MONSTER_TABLE = SERVER_DIR / "data" / "dungeon_monsters.json"
REFERENCE = SERVER_DIR / "data" / "dungeon_spawn_reference.json"
#: 场景入口点（场景类 `super(x,y)`，见 server/build_scene_entrances.py）
ENTRANCES = SERVER_DIR / "data" / "scene_entrances.json"
#: 场景像素尺寸表（取列数/行数时除以 GRID_SIZE）
SCENE_DATA_XML = WS / "04-data-json" / "game-data" / "06-scenes" / "SceneData_dataClass.xml"
GRID_SIZE = 8
DEFAULT_OUTDIR = SERVER_DIR / "data"

#: 官方抓包实测坐标池（老手.pcapng / 战士诺兰.pcapng，诺兰密林三幕）
#: 用于没有抓包的副本按同一布局风格铺位，保证落在地面可点范围内。
OFFICIAL_POSITIONS: List[Tuple[float, float, float]] = [
    (93.0, 30.0, 0.0), (153.0, 48.0, 0.0), (49.0, 31.0, 0.0),
    (90.0, 49.0, 0.0), (141.0, 33.0, 0.0), (60.0, 31.0, 0.0),
    (71.0, 53.0, 0.0), (101.0, 38.0, 0.0), (136.0, 29.0, 0.0),
    (155.0, 52.0, 0.0), (98.0, 43.0, 0.0), (120.0, 56.0, 0.0),
    (136.0, 46.0, 0.0),
]

#: 每个非头目类型刷几只 / 头目刷几只（对齐抓包：每幕 3~5 只）
COUNT_NORMAL = 2
COUNT_BOSS = 1

#: 调试残留怪物（"电饭煲1/2/3号"、"小银角"），出现在旧脚本里，不是副本正表
DEBUG_NAME_RE = re.compile(r"电饭煲|小银角")

SCENE_TYPE_SHORT = {
    "NORMAL_FB_SCENE": "NORMAL",
    "OPTIONAL_FB_SCENE": "OPTIONAL",
    "FINISH_FB_SCENE": "FINISH",
}


# ── FbConfig.as 解析 ────────────────────────────────────────────────────────
def _brace_block(text: str, anchor: str) -> str:
    """取 anchor 之后第一个 `{ ... }` 的块体（花括号配平）。"""
    start = text.index("{", text.index(anchor))
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
    raise ValueError(f"未找到配平的花括号块: {anchor}")


def _split_args(args: str) -> List[str]:
    """按顶层逗号切分实参。"""
    parts, depth, cur = [], 0, []
    for ch in args:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur).strip())
    return parts


def load_scene_names() -> Dict[str, str]:
    text = SCENENAMES.read_text(encoding="utf-8", errors="replace")
    out = {}
    for m in re.finditer(
        r'public\s+static\s+const\s+([A-Za-z_]\w*)\s*:\s*[A-Za-z*]+\s*=\s*"([^"]*)"', text
    ):
        out[m.group(1)] = m.group(2)
    return out


def resolve_scene(token: str, names: Dict[str, str]) -> str:
    token = token.strip()
    if token.startswith("SceneNames."):
        return names.get(token[len("SceneNames."):], token[len("SceneNames."):])
    return token.strip('"')


def parse_fb_defines(text: str, names: Dict[str, str]) -> Dict[int, Dict[str, Any]]:
    body = _brace_block(text, "fbDefines")
    out: Dict[int, Dict[str, Any]] = {}
    for m in re.finditer(r'"(\d+)"\s*:\s*new\s+Fb\(', body):
        fb_id = int(m.group(1))
        i = m.end()
        depth = 1
        while i < len(body) and depth > 0:
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
            i += 1
        parts = _split_args(body[m.end():i - 1])
        if len(parts) < 3:
            continue
        name = parts[1].strip().strip('"')
        toks = re.findall(r'"(?:[^"\\]|\\.)*"|SceneNames\.\w+', parts[2])
        scenes = [resolve_scene(t, names) for t in toks]
        access = {}
        for km in re.finditer(r"(\d+)\s*:\s*(\d+)", parts[3] if len(parts) > 3 else ""):
            access[int(km.group(1))] = int(km.group(2))
        out[fb_id] = {
            "fbId": fb_id, "name": name, "scenes": scenes,
            "accessLevel": access,
            "minLevel": min(access.values()) if access else 1,
        }
    return out


def parse_fb_scenes(text: str) -> Dict[str, Dict[str, Any]]:
    """返回 {场景名: {types: {难度: 类型}, conditions: {难度: [..]}, bossKills: {难度: [..]}}}。"""
    body = _brace_block(text, "fbSceneDefines")
    by_scene: Dict[str, Dict[str, Any]] = {}
    for m in re.finditer(
        r'"([A-Za-z0-9_]+)_(\d+)"\s*:\s*new\s+FbSceneDefine\(((?:[^()]|\([^()]*\))*)\)', body
    ):
        sn, tier, inner = m.group(1), int(m.group(2)), m.group(3)
        typ = re.search(r"FbSceneTypes\.(\w+)", inner)
        rec = by_scene.setdefault(sn, {"types": {}, "conditions": {}, "bossKills": {}})
        rec["types"][tier] = typ.group(1) if typ else "NORMAL_FB_SCENE"
        rec["conditions"][tier] = re.findall(r"FbConditionType\.(\w+)", inner)
        rec["bossKills"][tier] = [
            {"mId": int(a), "count": int(b)}
            for a, b in re.findall(r"FbBossKillDefine\((\d+)\s*,\s*(\d+)\)", inner)
        ]
    return by_scene


# ── 怪物数值表 ──────────────────────────────────────────────────────────────
KEEP = ("name", "lv", "mhp", "attack", "defence", "critical", "hit",
        "evasion", "rk", "normalSpeed", "madSpeed", "defaultAI")


def build_monster_index() -> Tuple[Dict[int, Dict[int, Dict[str, Any]]], Dict[int, str]]:
    """返回 ({fbId: {mId: 数值}}, {fbId: 名称})，已剔除调试残留怪。"""
    doc = json.loads(MONSTER_TABLE.read_text(encoding="utf-8"))
    per_fb: Dict[int, Dict[int, Dict[str, Any]]] = {}
    for d in doc["dungeons"]:
        fb = d["fbId"]
        pool: Dict[int, Dict[str, Any]] = {}
        for m in d["monsters"]:
            if DEBUG_NAME_RE.search(m.get("name") or ""):
                continue
            pool[m["mId"]] = {k: m.get(k) for k in KEEP}
        per_fb[fb] = pool
    return per_fb, {d["fbId"]: None for d in doc["dungeons"]}


def group_tiers(pool: Dict[int, Dict[str, Any]]) -> List[Tuple[int, List[int]]]:
    """把怪物按「难度前缀」分组，返回按难度升序的 [(前缀, [类型序号...])]。

    mId = 前缀*100 + 类型序号；排序键先看该前缀的平均等级，再看前缀值，
    这样「难度档位 1..N」与 15_0 的 fb.lv 对齐。
    """
    by_prefix: Dict[int, List[int]] = {}
    for mid in pool:
        by_prefix.setdefault(mid // 100, []).append(mid % 100)
    rows = []
    for p, types in by_prefix.items():
        if not types:
            continue
        avg_lv = sum(pool[p * 100 + t]["lv"] or 0 for t in types) / len(types)
        rows.append((avg_lv, p, sorted(types)))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [(p, types) for _lv, p, types in rows]


# ── 每幕构成 ────────────────────────────────────────────────────────────────
def compose_scenes(types: List[int], rk_of: Dict[int, int], n_scenes: int,
                   seeds: Optional[Dict[int, List[int]]] = None) -> Dict[int, List[int]]:
    """把类型序号分配到各幕。

    - `seeds`（场景定义里的「必须击杀」怪）是权威落点，优先固定；
    - 无权威落点时：头目（rk>=2）落在最后一幕，其余类型平均分给前面各幕，
      并把最后一个（精英）同时放进最后一幕 —— 该规则经抓包实测验证成立
      （fb14 三幕 = {1,2} / {3,4} / {4,5}）。
    - 有权威落点的副本：剩余类型在各幕间平均分摊。
    """
    seeds = seeds or {}
    result: Dict[int, List[int]] = {i: [] for i in range(n_scenes)}
    for i, tlist in seeds.items():
        if 0 <= i < n_scenes:
            result[i].extend(t for t in tlist if t not in result[i])
    seeded = {t for tl in result.values() for t in tl}
    remaining = [t for t in types if t not in seeded]

    if n_scenes <= 1:
        result[0] = list(types)
        return result

    if seeded:
        # 已由权威数据定过位：剩下的均匀铺到各幕
        for i in range(n_scenes):
            lo = round(i * len(remaining) / n_scenes)
            hi = round((i + 1) * len(remaining) / n_scenes)
            result[i].extend(remaining[lo:hi])
        return result

    boss_types = [t for t in remaining if rk_of.get(t, 1) >= 2]
    normal_types = [t for t in remaining if rk_of.get(t, 1) < 2]
    head = normal_types[: max(0, len(normal_types) - 1)]  # 留最后一个给关底
    n_head = n_scenes - 1
    for i in range(n_head):
        lo = round(i * len(head) / n_head)
        hi = round((i + 1) * len(head) / n_head)
        result[i].extend(head[lo:hi])
    final = list(boss_types)
    if normal_types:
        final = normal_types[-1:] + final  # 关底带上精英
    result[n_scenes - 1].extend(t for t in final if t not in result[n_scenes - 1])
    return result


def slots_to_positions(count: int, scene_idx: int) -> List[Tuple[float, float, float]]:
    """从官方坐标池里取 count 个位置；按幕序错开，避免每幕完全重合。

    ⚠ **这只是最后的兜底**。这个池子是**诺兰密林实测的 13 个坐标**，
    对本图来说基本是随机位置（落到墙里/背景上）。只有在拿不到该场景入口点时
    才用它，并且 `posSource` 会标成 `reconstructed`（明确"不可信"）。
    """
    pool = OFFICIAL_POSITIONS
    off = (scene_idx * 5) % len(pool)
    return [pool[(off + i) % len(pool)] for i in range(count)]


#: 围绕**场景入口点**铺开刷怪位的偏移（格）。入口点 = 场景类 `super(x,y)`，
#: 即玩家进入该场景时站的位置，**必然是这块地图上可走的地面**（见
#: `server/build_scene_entrances.py`）。
#:
#: 设计约束：
#:   · |dx| 可以大一些（地面带在 x 方向通常很宽，实测诺兰密林 y≈57 那行有 191 个可走格）
#:   · |dy| 保持很小（y 是纵深，稍偏就可能掉进背景/墙）
#:   · 半径控制在 ~5 格内优先，因为客户端 `PathFinder.searchnearpoint(pos, 5)`
#:     只能把起点吸附到 5 格内的可走格（超出就真的走不出来）
ENTRANCE_OFFSETS: List[Tuple[int, int]] = [
    (0, -3), (-5, -2), (5, -2), (-9, -3), (9, -3),
    (-13, -2), (13, -2), (-17, -3), (17, -3), (0, -7),
]


def entrance_layout(count: int, gx: float, gy: float,
                    cols: Optional[int] = None,
                    rows: Optional[int] = None) -> List[Tuple[float, float, float]]:
    """围绕场景入口点铺开 `count` 个刷怪位（格）。越界的做夹取。"""
    out: List[Tuple[float, float, float]] = []
    for i in range(count):
        dx, dy = ENTRANCE_OFFSETS[i % len(ENTRANCE_OFFSETS)]
        x, y = gx + dx, gy + dy
        if cols and rows:
            x = min(max(x, 1.0), float(cols - 2))
            y = min(max(y, 1.0), float(rows - 2))
        out.append((float(x), float(y), 0.0))
    return out


def load_scene_grid_sizes() -> Dict[str, Tuple[int, int]]:
    """`SceneData_dataClass.xml` → {场景名: (列数, 行数)}（像素 / gridSize）。"""
    sizes: Dict[str, Tuple[int, int]] = {}
    if not SCENE_DATA_XML.is_file():
        return sizes
    try:
        root = ET.parse(SCENE_DATA_XML).getroot()
    except ET.ParseError:
        return sizes
    for node in root.iter():
        nm = node.get("nm")
        if not nm:
            continue
        try:
            w = int(node.get("width") or 0) // GRID_SIZE
            h = int(node.get("height") or 0) // GRID_SIZE
        except ValueError:
            continue
        if w > 0 and h > 0:
            sizes[nm] = (w, h)
    return sizes



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=str(DEFAULT_OUTDIR))
    args = ap.parse_args()

    text = FBCONFIG.read_text(encoding="utf-8", errors="replace")
    names = load_scene_names()
    dungeons = parse_fb_defines(text, names)
    scene_defs = parse_fb_scenes(text)
    pool_by_fb, _ = build_monster_index()
    reference = json.loads(REFERENCE.read_text(encoding="utf-8")) if REFERENCE.is_file() else {}
    entrances: Dict[str, Any] = {}
    if ENTRANCES.is_file():
        try:
            entrances = json.loads(ENTRANCES.read_text(encoding="utf-8")).get("scenes") or {}
        except ValueError:
            entrances = {}
    scene_grid_sizes = load_scene_grid_sizes()
    print("[i] 场景入口点 %d 个；场景尺寸 %d 条" % (len(entrances), len(scene_grid_sizes)))

    out_dungeons: List[Dict[str, Any]] = []
    stat_scenes = stat_monsters = 0

    for fb_id in sorted(dungeons):
        dun = dungeons[fb_id]
        pool = pool_by_fb.get(fb_id, {})
        if not pool:
            continue
        prefixes = [p for p, _types in group_tiers(pool)]
        rk_of = {mid % 100: (pool[mid].get("rk") or 1) for mid in pool}
        n_scenes = len(dun["scenes"])

        # 难度档位（sceneDefines 的 `<场景>_<难度>` 后缀）按序对齐到怪物前缀。
        # 抓包实测 fb14 lv=2 → 374（第 2 个前缀）；fb36 档位 {2,4} → 前缀 {509,510}。
        diff_keys = sorted({k for sn in dun["scenes"]
                            for k in scene_defs.get(sn, {}).get("types", {})})
        if not diff_keys:
            diff_keys = list(range(1, len(prefixes) + 1))
        diff_map = {k: prefixes[i] for i, k in enumerate(diff_keys) if i < len(prefixes)}
        default_diff = 2 if 2 in diff_map else (diff_keys[0] if diff_keys else 1)

        scenes_out: List[Dict[str, Any]] = []
        ref_scenes = (reference.get("dungeons", {}).get(str(fb_id), {}) or {}).get("scenes", {})

        # 每个难度各算一份分配：场景定义里的「必须击杀」怪是按难度分别写的
        rule_maps: Dict[int, Dict[int, List[int]]] = {}
        for dk in diff_map:
            seeds: Dict[int, List[int]] = {}
            for i, sn in enumerate(dun["scenes"]):
                for bk in scene_defs.get(sn, {}).get("bossKills", {}).get(dk, []):
                    seeds.setdefault(i, []).append(bk["mId"] % 100)
            rule_maps[dk] = compose_scenes(sorted(rk_of), rk_of, n_scenes, seeds)

        for idx, sn in enumerate(dun["scenes"]):
            sd = scene_defs.get(sn, {})
            key = default_diff if default_diff in sd.get("types", {}) else (
                min(sd["types"]) if sd.get("types") else default_diff)
            s_type = sd["types"].get(key, "NORMAL_FB_SCENE")
            s_conds = sd["conditions"].get(key, ["MONSTERS_CLEAR"])
            s_kills = sd["bossKills"].get(key, [])

            ref = ref_scenes.get(sn)

            waves: Dict[str, List[Dict[str, Any]]] = {}
            pos_source = "capture" if ref else "reconstructed"
            ent = entrances.get(sn)
            grid_dims = scene_grid_sizes.get(sn)
            for diff_key, prefix in sorted(diff_map.items()):
                if ref:
                    # 抓包实测构成：每项 [类型序号, x, y, d]
                    layout = [(t, x, y, d) for t, x, y, d in ref]
                else:
                    rule_map = rule_maps.get(diff_key, {})
                    layout = []
                    for t in rule_map.get(idx, []):
                        cnt = COUNT_BOSS if rk_of.get(t, 1) >= 2 else COUNT_NORMAL
                        layout.extend([(t, 0.0, 0.0, 0.0)] * cnt)
                    if ent:
                        # ★ 用**本场景自己的入口点**铺位：入口点必然是这块地图上可走的地面
                        #   （玩家进入时站的地方），比抄诺兰密林的坐标可靠得多。
                        cols, rows = (grid_dims or (None, None))
                        positions = entrance_layout(len(layout), ent["gx"], ent["gy"],
                                                    cols, rows)
                        pos_source = "entrance"
                    else:
                        # 兜底：本图入口点不可得，只能用诺兰密林的坐标池（明确标为不可信）
                        positions = slots_to_positions(len(layout), idx)
                    layout = [(t, p[0], p[1], p[2])
                              for (t, _x, _y, _d), p in zip(layout, positions)]
                spawns = []
                for t, x, y, d in layout:
                    mid = prefix * 100 + t
                    if mid not in pool:
                        continue
                    spawns.append({"mId": mid, "x": x, "y": y, "d": d})
                if spawns:
                    waves[str(diff_key)] = spawns

            scenes_out.append({
                "idx": idx,
                "sn": sn,
                "type": s_type,
                "typeShort": SCENE_TYPE_SHORT.get(s_type, s_type),
                "optional": s_type == "OPTIONAL_FB_SCENE",
                "conditions": s_conds,
                "bossKills": s_kills,
                "posSource": pos_source,
                "waves": waves,
            })
            stat_scenes += 1
            stat_monsters += sum(len(v) for v in waves.values())

        out_dungeons.append({
            "fbId": fb_id,
            "name": dun["name"],
            "minLevel": dun["minLevel"],
            "accessLevel": {str(k): v for k, v in sorted(dun["accessLevel"].items())},
            "defaultDifficulty": default_diff,
            "difficultyMap": {str(k): v for k, v in sorted(diff_map.items())},
            "tiers": [{"difficulty": k, "prefix": diff_map[k]} for k in sorted(diff_map)],
            "scenes": scenes_out,
            "monsters": {str(k): v for k, v in sorted(pool.items())},
        })

    doc = {
        "_source": [
            "09-副本刷怪还原/fbsource_raw/scripts/mmo/fb/model/FbConfig.as"
            "（fbDefines 副本表 / fbSceneDefines 场景表）",
            "09-副本刷怪还原/SceneNames.as（场景名常量）",
            "server/data/dungeon_monsters.json（aiscript SWF 内嵌怪物数值）",
            "server/data/dungeon_spawn_reference.json（抓包实测每幕构成与坐标）",
        ],
        "_monsterIdRule": "mId = 难度前缀*100 + 类型序号；前缀按等级升序 = 难度档位 1..N，"
                          "与 15_0 的 fb.lv 对齐",
        "_posSource": "capture = 抓包实测坐标；reconstructed = 按官方坐标池重建",
        "dungeonCount": len(out_dungeons),
        "sceneCount": stat_scenes,
        "spawnCount": stat_monsters,
        "dungeons": out_dungeons,
    }
    out = Path(args.outdir) / "dungeon_spawn.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"副本 {doc['dungeonCount']}｜幕 {doc['sceneCount']}｜刷怪条目 {doc['spawnCount']}")
    for d in out_dungeons:
        cap = sum(1 for s in d["scenes"] if s["posSource"] == "capture")
        diff = ",".join(f"{t['difficulty']}→{t['prefix']}" for t in d["tiers"])
        print(f"  fb{d['fbId']:<3} {d['name']:<8} 幕={len(d['scenes'])} 难度[{diff}] "
              f"实测幕={cap} 怪={len(d['monsters'])}")
    print("输出:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
