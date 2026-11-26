"""Private operator markings, scoped to a project and immutable PDF bytes."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import re

from . import research_workspace as rw
from .domain_queries import DomainQueryError
from .persistence import atomic_write_json, file_lock


def _read(path):
    if not path.exists():
        return []
    value = json.loads(rw.safe(path, file=True).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != 1 or not isinstance(value.get("items"), list):
        raise DomainQueryError("invalid_state", "标注文件状态异常，未覆盖原记录。")
    return value["items"]


def mark_path(data, stage, content_hash):
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", content_hash):
        raise DomainQueryError("invalid_state", "无法确认 PDF 版本。")
    return rw.safe(data / "_reader" / "desk-marks" / sha256(stage.encode()).hexdigest(), exists=False) / (content_hash[7:] + ".json")


def list_marks(data, stage, content_hash):
    return {"items": _read(mark_path(data, stage, content_hash))}


def add_mark(data, stage, content_hash, page_count, payload):
    page, quote, note, rects, mark_id = (payload.get(k) for k in ("page", "quote", "note", "rects", "mark_id"))
    if (set(payload) != {"stage_id", "page", "quote", "note", "rects", "mark_id"}
            or type(page) is not int or not 1 <= page <= page_count
            or not isinstance(quote, str) or not quote.strip() or len(quote) > 1500
            or not isinstance(note, str) or len(note) > 2000
            or not isinstance(mark_id, str) or not re.fullmatch(r"mark-[a-f0-9]{32}", mark_id)
            or not isinstance(rects, list) or not 1 <= len(rects) <= 80):
        raise DomainQueryError("invalid_argument", "请选中一段正文，确认页码，并缩短标注。")
    for rect in rects:
        if (not isinstance(rect, dict) or set(rect) != {"x", "y", "width", "height"}
                or any(type(v) not in (float, int) or not 0 <= v <= 1 or not math.isfinite(v) for v in rect.values())
                or rect["width"] <= 0 or rect["height"] <= 0
                or rect["x"] + rect["width"] > 1.002 or rect["y"] + rect["height"] > 1.002):
            raise DomainQueryError("invalid_argument", "高亮位置不在 PDF 页内。")
    mark = {"mark_id": mark_id, "page": page, "quote": quote.strip(), "note": note.strip(), "rects": rects}
    path = mark_path(data, stage, content_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path):
        items = _read(path)
        previous = next((item for item in items if item["mark_id"] == mark_id), None)
        if previous:
            if any(previous.get(k) != value for k, value in mark.items()):
                raise DomainQueryError("invalid_argument", "这次保存标识已用于不同标注。")
            return previous
        if len(items) >= 500:
            raise DomainQueryError("resource_limit", "本项目这篇 PDF 的标注已达 500 条。")
        mark["created_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(path, {"version": 1, "items": [*items, mark]})
        return mark
