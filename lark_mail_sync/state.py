# -*- coding: utf-8 -*-
"""本地同步状态（JSON，原子写）。

多维表记录本身保存 消息IDs 与 附件 file_token，是可恢复的最终事实；
state.json 只是加速增量扫描与附件去重的游标，丢了可 rebuild-state 重建。
"""
from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, List, Optional, Set


class SubjectEntry(dict):
    """单个归一化主题的同步状态。"""

    @property
    def record_id(self) -> str:
        return self.get("record_id", "")

    @property
    def message_ids(self) -> List[str]:
        return self.get("message_ids", [])

    @property
    def attachment_keys(self) -> List[str]:
        return self.get("attachment_keys", [])

    @property
    def attachment_tokens(self) -> List[str]:
        return self.get("attachment_tokens", [])


class SyncState:
    def __init__(self, path: str):
        self.path = path
        self.version = 1
        self.processed: Set[str] = set()
        self.subjects: Dict[str, SubjectEntry] = {}
        self.last_sync_at: float = 0.0
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.version = d.get("version", 1)
            self.processed = set(d.get("processed", []))
            self.subjects = {k: SubjectEntry(v) for k, v in d.get("subjects", {}).items()}
            self.last_sync_at = float(d.get("last_sync_at", 0.0))
        except (json.JSONDecodeError, ValueError, OSError):
            # 状态损坏不应阻塞同步：可从多维表 rebuild 重建
            self.processed, self.subjects, self.last_sync_at = set(), {}, 0.0

    def save(self) -> None:
        d = {
            "version": self.version,
            "processed": sorted(self.processed),
            "subjects": {k: dict(v) for k, v in sorted(self.subjects.items())},
            "last_sync_at": self.last_sync_at,
        }
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ---------- 访问器 ----------

    def is_processed(self, message_id: str) -> bool:
        return message_id in self.processed

    def mark_processed(self, message_id: str) -> None:
        self.processed.add(message_id)

    def entry(self, normalized_subject: str) -> SubjectEntry:
        if normalized_subject not in self.subjects:
            self.subjects[normalized_subject] = SubjectEntry(
                record_id="", message_ids=[], attachment_keys=[], attachment_tokens=[])
        return self.subjects[normalized_subject]

    def record_id_of(self, normalized_subject: str) -> Optional[str]:
        e = self.subjects.get(normalized_subject)
        return e.record_id if e and e.record_id else None

    @staticmethod
    def attachment_key(message_id: str, attachment_id: str) -> str:
        return f"{message_id}:{attachment_id}"
