"""Build the official **task reward** table from the client's `TaskBonusReader`.

## 数据来源（官方原表，不是我们猜的）

客户端 `mmo.config.task.TaskBonusReader` 把一个嵌在 config SWF 里的 XML
（`TaskBonusDefine.xml`）解析成 `taskId -> TaskBonus`，供任务面板显示奖励。
反编译产物就落在：

    02-反编译源码AS3/反编译副本/config/binaryData/6_mmo.config.task.TaskBonusReader__defineClass.bin

XML 结构：`<config><tk id="514" bs="100:2:10000#102:1:10000#..."/></config>`

## 词义（来自 `TaskBonus.parseData`，官方实现）

```as3
for each(bonus in bonus.split("#")) {
   mul = bonus.split(":");
   if(mul.length == 3) {
      if(mul[0] == 100)          _moneyNum  += parseInt(mul[2]);   // 金币
      else if(mul[0] == 102) {
         if(mul[1] == 10)        _bottleEx  += parseInt(mul[2]);   // 宠物经验
         else                    _humanEx   += parseInt(mul[2]);   // 人物经验
      }
      else                       _extBonusArr.push(bonus);          // 额外奖励(道具/衣服/技能点…)
   }
}
```

⇒ 输出：

```json
{"514": {"money": 10000, "human_exp": 10000, "bottle_exp": 0,
         "ext": [[2, 2027, 1], [105, 0, 5], [110, 9011, 1], [109, 1, 1], [50, 132012, 4]]}}
```

用法：

    python -m server.build_task_bonus            # 生成
    python -m server.build_task_bonus --check    # 只校验是否与现状一致
"""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (
    ROOT / "02-反编译源码AS3" / "反编译副本" / "config" / "binaryData"
    / "6_mmo.config.task.TaskBonusReader__defineClass.bin"
)
OUTPUT = Path(__file__).resolve().parent / "data" / "task_bonus.json"


def parse_bonus(bs: str) -> Dict[str, Any]:
    """按官方 `TaskBonus.parseData` 的语义拆 `bs` 串。"""
    money = 0
    human_exp = 0
    bottle_exp = 0
    ext: List[List[int]] = []
    for chunk in str(bs or "").split("#"):
        parts = chunk.split(":")
        if len(parts) != 3:
            continue
        try:
            kind, sub, value = int(parts[0]), int(parts[1]), int(parts[2])
        except (TypeError, ValueError):
            continue
        if kind == 100:
            money += value
        elif kind == 102:
            if sub == 10:
                bottle_exp += value
            else:
                human_exp += value
        else:
            ext.append([kind, sub, value])
    return {"money": money, "human_exp": human_exp, "bottle_exp": bottle_exp, "ext": ext}


def build() -> Dict[str, Dict[str, Any]]:
    root = ET.fromstring(SOURCE.read_bytes())
    table: Dict[str, Dict[str, Any]] = {}
    for node in root.iter("tk"):
        tid = node.get("id")
        if not tid or not str(tid).isdigit():
            continue
        table[str(int(tid))] = parse_bonus(node.get("bs"))
    return dict(sorted(table.items(), key=lambda kv: int(kv[0])))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="生成官方任务奖励表")
    ap.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = ap.parse_args(argv)

    table = build()
    payload = json.dumps(table, ensure_ascii=False, indent=1, sort_keys=False)
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current.strip() == payload.strip():
            print("task_bonus.json 与官方表一致（%d 条）" % len(table))
            return 0
        print("⚠ task_bonus.json 与官方表**不一致**，需要重新生成")
        return 1
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(payload, encoding="utf-8")
    with_money = sum(1 for v in table.values() if v["money"])
    with_exp = sum(1 for v in table.values() if v["human_exp"])
    with_ext = sum(1 for v in table.values() if v["ext"])
    print("Wrote %d task bonuses to %s" % (len(table), OUTPUT))
    print("  有金币 %d 条 / 有人物经验 %d 条 / 有额外奖励 %d 条" % (with_money, with_exp, with_ext))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
