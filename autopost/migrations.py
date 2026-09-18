"""DBの更新（スキーマ変更）を、データを壊さずに適用する。

スキーマの追加自体は各ストアが起動時に行う（experiments.py の _migrate、
db.py の CREATE TABLE IF NOT EXISTS）。このモジュールの役目は、
**その前にバックアップを取り、後で壊れていないか確かめること**。

  1. DBファイルをコピーして backups/db_<日時>/ に退避
  2. ストアを開いてスキーマ変更を適用
  3. PRAGMA integrity_check で健全性を確認
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable

from .config import Settings
from .version import APP_ROOT

LogFn = Callable[[str], None]
BACKUP_ROOT = APP_ROOT / "backups"


class MigrationError(RuntimeError):
    """スキーマ変更に失敗した。バックアップから戻せる。"""


def _db_paths(settings: Settings) -> list[Path]:
    return [Path(settings.db_path), Path(settings.experiments_db_path)]


def backup_databases(settings: Settings, log: LogFn = print) -> Path | None:
    """既存のDBを退避する。WALも一緒に持っていく。"""
    existing = [p for p in _db_paths(settings) if p.is_file()]
    if not existing:
        log("DBはまだありません（バックアップ不要）")
        return None

    destination = BACKUP_ROOT / f"db_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    destination.mkdir(parents=True, exist_ok=True)
    for path in existing:
        for suffix in ("", "-wal", "-shm"):
            source = Path(str(path) + suffix)
            if source.is_file():
                shutil.copy2(source, destination / source.name)
    log(f"DBをバックアップしました: {destination}")
    return destination


def _integrity_ok(path: Path) -> bool:
    try:
        with sqlite3.connect(path, timeout=30) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error:
        return False
    return bool(result) and result[0] == "ok"


def migrate(settings: Settings, log: LogFn = print, backup: bool = True) -> bool:
    """スキーマ変更を適用する。戻り値は成功したか。"""
    saved = backup_databases(settings, log) if backup else None

    try:
        # ストアを開くだけでスキーマ変更が走る（既存データは消さない）
        from .db import Queue
        from .experiments import ExperimentStore

        Queue(settings.db_path)
        ExperimentStore(settings.experiments_db_path)
    except Exception as exc:
        log(f"スキーマ変更に失敗しました: {exc}")
        if saved:
            log(f"バックアップから戻せます: {saved}")
        raise MigrationError(str(exc)) from exc

    for path in _db_paths(settings):
        if path.is_file() and not _integrity_ok(path):
            log(f"DBの健全性チェックに失敗しました: {path}")
            if saved:
                log(f"バックアップから戻せます: {saved}")
            raise MigrationError(f"{path} が壊れています")

    log("DBは最新の形式です")
    return True
