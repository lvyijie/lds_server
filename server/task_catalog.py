"""Runtime access to task definitions extracted from the original TaskConfig SWF."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)
CATALOG_PATH = Path(__file__).resolve().parent / "data" / "task_catalog.json"

try:
    TASKS: Dict[str, Dict[str, Any]] = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    logger.exception("无法加载官方任务定义 %s", CATALOG_PATH)
    TASKS = {}


def get_task(task_id: Any) -> Optional[Dict[str, Any]]:
    return TASKS.get(str(task_id))


def _prerequisite_ids(raw: Any) -> list[str]:
    # TaskConfig stores requirements as strings such as "1048_0" or
    # comma-separated groups. job#... clauses are class gates, not task IDs.
    return [m.group(1) for m in re.finditer(r"(?:^|[,;])\s*(\d+)_\d+", str(raw or ""))]


def can_start_task(task_id: Any, player: Dict[str, Any]) -> tuple[bool, str]:
    definition = get_task(task_id)
    if not definition:
        return False, "unknown"
    basic = player.get("basic", {})
    try:
        level = int(basic.get("level", 1) or 1)
        job = int(basic.get("job", 1) or 1)
        required_level = int(str(definition.get("lvl", 0)).split("-", 1)[0] or 0)
        required_job = int(definition.get("job", 0) or 0)
    except (TypeError, ValueError):
        return False, "invalid_definition"
    if required_level > level or (required_job and required_job != job):
        return False, "requirements"
    done = player.get("tasks", {})
    for prerequisite in _prerequisite_ids(definition.get("preTasks")):
        record = done.get(prerequisite, {})
        if int(record.get("st", 0) or 0) != 1:
            return False, "prerequisite"
    return True, ""
