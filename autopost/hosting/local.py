"""ローカルの公開ディレクトリへコピーするホスト。

すでに自分でWebサーバ（Nginx / Cloudflare Tunnel / レンタルサーバ等）を
持っている場合に使う。追加の課金は発生しない。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..config import Settings
from . import HostingError, ImageHost


class LocalDirHost(ImageHost):
    name = "local"

    def __init__(self, settings: Settings) -> None:
        missing = settings.missing("hosting")
        if missing:
            raise HostingError(".env の設定が不足しています: " + ", ".join(missing))
        self.directory = Path(settings.local_host_dir)
        self.base_url = settings.local_host_base_url.rstrip("/")

    def upload(self, post_id: str, files: list[Path], content_hash: str) -> list[str]:
        target_dir = self.directory / post_id / content_hash
        target_dir.mkdir(parents=True, exist_ok=True)
        urls: list[str] = []
        for path in files:
            destination = target_dir / path.name
            if not destination.is_file() or destination.stat().st_size != path.stat().st_size:
                shutil.copy2(path, destination)
            urls.append(f"{self.base_url}/{post_id}/{content_hash}/{path.name}")
        return urls

    def describe(self) -> str:
        return f"ローカル公開ディレクトリ（{self.directory} → {self.base_url}）"

    def public_prefix(self) -> str:
        return self.base_url
