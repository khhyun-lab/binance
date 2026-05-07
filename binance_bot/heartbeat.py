from __future__ import annotations

import json
import os
import time
from pathlib import Path


class HeartbeatWriter:
    def __init__(self, path: Path) -> None:
        self.path = path

    def touch(self, status: str = "running") -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "pid": os.getpid(),
            "status": status,
            "timestamp": time.time(),
        }
        tmp_path = self.path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp_path.replace(self.path)