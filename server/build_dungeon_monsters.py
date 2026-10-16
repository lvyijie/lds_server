# -*- coding: utf-8 -*-
"""把副本刷怪脚本 SWF 汇总成服务端可用的小怪数据表（build_dungeon_monsters.py）。

管线
----
07-swf/downloaded_core/play/aiscript/fb<fbId>~1000.swf
   │  ffdec -export binaryData
   ▼
   内嵌 XML ×3（ClientMonsterInfoConfig / ClientMonsterAIConfig / ClientMonsterSkillExtConfig）
   │  容错解析（源 XML 属性间缺空格，且表头带 SELECT 注释）
   ▼
server/data/
   ├── dungeon_monsters.json    结构化：副本 → 小怪[]（含 AI 明细）
   ├── dungeon_monsters.csv     扁平表：一行一只怪
   └── dungeon_monster_ai.csv   一行一只怪的 AI 参数

与 server/official_monsters.json 的关系
----
`official_monsters.json` 只有 id→名字（2418 条，含全部活动怪）；
本表是**能进副本的**怪物的完整数值（等级/血量/攻防/暴击/移速/AI），
是 `dungeon_spawn.py` 构造 2302 刷怪包时的真值来源。

用法
----
    python server/build_dungeon_monsters.py
    python server/build_dungeon_monsters.py --swf-dir <dir> --keep-tmp
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
WS = SERVER_DIR.parent
JAVA = WS / "08-工具与代码/OpenJDK8U-jdk_x64_windows_hotspot_8u504b01/jdk8u504-b01/bin/java.exe"
FFDEC = WS / "08-工具与代码/ffdec_26.3.0/ffdec-cli.jar"
DEFAULT_SWF_DIR = WS / "07-swf" / "downloaded_core" / "play" / "aiscript"
DEFAULT_OUTDIR = SERVER_DIR / "data"

ATTR = re.compile(r'([A-Za-z_]\w*)\s*=\s*"([^"]*)"')
COMMENT = re.compile(r"<!--.*?-->", re.S)
INT_RE = re.compile(r"^-?\d+$")


def _int(v, default: int = 0) -> int:
    v = (v or "").strip()
    return int(v) if INT_RE.match(v) else default


def read_text(p: Path) -> str:
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def attrs(tag: str) -> dict:
    return dict(ATTR.findall(tag))


def strip_comments(text: str) -> str:
    """源 XML 表头有 `SELECT CONCAT('<ClientMonsterInfo mId="' ...` 注释，会污染正则。"""
    return COMMENT.sub("", text)


def parse_info(text: str) -> list:
    out = []
    for m in re.finditer(r"<ClientMonsterInfo\b([^>]*?)/?>", strip_comments(text)):
        a = attrs(m.group(1))
        if not a.get("mId") or not INT_RE.match(a["mId"].strip()):
            continue
        out.append({
            "mId": int(a["mId"]), "name": a.get("name", ""),
            "lv": _int(a.get("lv")), "mhp": _int(a.get("mhp")),
            "attack": _int(a.get("attack")), "defence": _int(a.get("defence")),
            "critical": _int(a.get("critical")), "hit": _int(a.get("hit")),
            "evasion": _int(a.get("evasion")), "rk": _int(a.get("rk"), 1),
            "normalSpeed": _int(a.get("normalSpeed")), "madSpeed": _int(a.get("madSpeed")),
            "defaultAI": _int(a.get("defaultAI")),
        })
    return out


def parse_ai(text: str) -> list:
    text = strip_comments(text)
    out = []
    for m in re.finditer(r"<AIInfo\b([^>]*)>(.*?)</AIInfo>", text, re.S):
        a = attrs(m.group(1))
        if not a.get("aiId"):
            continue
        skills = []
        for sm in re.finditer(r"<AISkillInfo\b([^>]*?)/?>", m.group(2)):
            sa = attrs(sm.group(1))
            skills.append({
                "aKey": sa.get("aKey", ""),
                "attInterval": _int(sa.get("attInterval")),
                "attBoundXMax": _int(sa.get("attBoundXMax")),
                "attBoundXMin": _int(sa.get("attBoundXMin")),
                "attBoundYMax": _int(sa.get("attBoundYMax")),
                "attBoundYMin": _int(sa.get("attBoundMin")),
                "randomPercent": _int(sa.get("randomPercent")),
                "priority": _int(sa.get("priority")),
            })
        out.append({
            "aiId": _int(a["aiId"]), "mId": _int(a.get("mId")),
            "aiType": _int(a.get("aiType")), "charType": _int(a.get("charType")),
            "chaseR": _int(a.get("chaseR")), "sightR": _int(a.get("sightR")),
            "attackPercent": _int(a.get("attackPercent")),
            "escapePercent": _int(a.get("escapePercent")),
            "idlePercent": _int(a.get("idlePercent")),
            "roamPercent": _int(a.get("roamPercent")),
            "chasePercent": _int(a.get("chasePercent")),
            "superArmerCD": _int(a.get("superArmerCD")),
            "hrmTime": _int(a.get("hrmTime")), "samMaxTime": _int(a.get("samMaxTime")),
            "caromPercent": _int(a.get("caromPercent")),
            "skills": skills,
        })
    return out


def parse_skill_ext(text: str) -> list:
    out = []
    for m in re.finditer(r"<SkillExtInfo\b([^>]*?)/?>", strip_comments(text)):
        a = attrs(m.group(1))
        if a.get("skillId"):
            out.append({"skillId": int(a["skillId"]), "frames": _int(a.get("frames"))})
    return out


def find_bin(d: Path, kind: str):
    return next(iter(d.glob(f"*_{kind}.bin")), None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--swf-dir", default=str(DEFAULT_SWF_DIR))
    ap.add_argument("--outdir", default=str(DEFAULT_OUTDIR))
    ap.add_argument("--keep-tmp", action="store_true")
    args = ap.parse_args()

    swf_dir, outdir = Path(args.swf_dir), Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    swfs = sorted((p for p in swf_dir.glob("fb*~*.swf")),
                  key=lambda p: int(re.match(r"fb(\d+)", p.name).group(1)))
    if not swfs:
        print(f"未找到 SWF: {swf_dir}（先跑 server/fetch_aiscript_swf.py）")
        return 1

    tmp = Path(tempfile.mkdtemp(prefix="aiscript_bin_"))
    dungeons, all_mon, all_ai, all_skill = [], {}, {}, {}
    for swf in swfs:
        fb = int(re.match(r"fb(\d+)", swf.name).group(1))
        d = tmp / f"fb{fb}"
        try:
            subprocess.run([str(JAVA), "-jar", str(FFDEC), "-export", "binaryData",
                            str(d), str(swf)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            print(f"  [跳过] {swf.name}: ffdec 失败 {e}")
            continue
        ip_, ap_, sp_ = find_bin(d, "infoClass"), find_bin(d, "aiClass"), find_bin(d, "skillClass")
        mons = parse_info(read_text(ip_)) if ip_ else []
        ais = parse_ai(read_text(ap_)) if ap_ else []
        sks = parse_skill_ext(read_text(sp_)) if sp_ else []
        ai_by_id = {a["aiId"]: a for a in ais}
        for m in mons:
            m["fbId"] = fb
            m["ai"] = ai_by_id.get(m["defaultAI"])
            all_mon.setdefault(m["mId"], m)
        for a in ais:
            a["fbId"] = fb
            all_ai.setdefault(a["aiId"], a)
        for s in sks:
            all_skill.setdefault(s["skillId"], s)
        dungeons.append({
            "fbId": fb, "swf": swf.name, "swfBytes": swf.stat().st_size,
            "monsterCount": len(mons), "aiCount": len(ais), "monsters": mons,
        })

    doc = {
        "source": "play/aiscript/fb<fbId>~1000.swf 内嵌 XML "
                  "（ClientMonsterInfoConfig / ClientMonsterAIConfig / ClientMonsterSkillExtConfig）",
        "cdn": "http://lds.100bt.com/play/aiscript/fb<fbId>~1000.swf",
        "dungeonCount": len(dungeons),
        "monsterTotal": sum(x["monsterCount"] for x in dungeons),
        "monsterUnique": len(all_mon), "aiUnique": len(all_ai),
        "skillExtUnique": len(all_skill),
        "dungeons": dungeons,
    }
    (outdir / "dungeon_monsters.json").write_text(
        json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")

    with (outdir / "dungeon_monsters.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fbId", "mId", "名称", "定位", "等级", "血量", "攻击", "防御",
                    "暴击", "命中", "闪避", "移速", "跑速", "默认AI"])
        for x in dungeons:
            for m in x["monsters"]:
                w.writerow([x["fbId"], m["mId"], m["name"],
                            "头目/精英" if m["rk"] >= 2 else "小怪", m["lv"], m["mhp"],
                            m["attack"], m["defence"], m["critical"], m["hit"],
                            m["evasion"], m["normalSpeed"], m["madSpeed"], m["defaultAI"]])

    with (outdir / "dungeon_monster_ai.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["fbId", "aiId", "mId", "aiType", "追击半径", "视野半径", "攻击倾向%",
                    "逃跑%", "待机%", "游荡%", "追击%", "霸体CD", "迂回%",
                    "攻击键", "攻击框X", "攻击框Y"])
        for x in dungeons:
            for m in x["monsters"]:
                a = m.get("ai")
                if not a:
                    continue
                s = (a.get("skills") or [{}])[0]
                w.writerow([x["fbId"], a["aiId"], a["mId"], a["aiType"], a["chaseR"],
                            a["sightR"], a["attackPercent"], a["escapePercent"],
                            a["idlePercent"], a["roamPercent"], a["chasePercent"],
                            a["superArmerCD"], a["caromPercent"], s.get("aKey", ""),
                            f'{s.get("attBoundXMin", 0)}~{s.get("attBoundXMax", 0)}',
                            f'0~{s.get("attBoundYMax", 0)}'])

    if args.keep_tmp:
        print(f"中间产物保留在: {tmp}")
    else:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"副本 {len(dungeons)}｜怪物条目 {doc['monsterTotal']}｜去重 {doc['monsterUnique']}"
          f"｜AI {doc['aiUnique']}｜技能帧 {doc['skillExtUnique']}")
    for x in dungeons:
        head = "、".join(f"{m['name']}({m['mId']})" for m in x["monsters"][:4])
        print(f"  fb{x['fbId']:<3} {x['monsterCount']:>3} 只  {head}")
    print("输出:", outdir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
