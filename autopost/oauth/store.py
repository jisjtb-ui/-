"""アクセストークンの保存。

- 保存先は .tokens/（.gitignore 済み）
- 可能な環境ではファイル権限を 0600 にする
- トークンの値そのものはログに出さない（表示は必ずマスクする）
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# 期限のこの秒数前から「切れている」とみなして更新する
EXPIRY_MARGIN_SECONDS = 300


@dataclass
class Token:
    """1プラットフォーム分の認証情報。"""

    platform: str
    access_token: str = ""
    refresh_token: str = ""
    expires_at: str = ""            # ISO8601
    refresh_expires_at: str = ""
    scope: str = ""
    account_id: str = ""            # TikTok: open_id / Instagram: user id
    account_name: str = ""
    extra: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    @staticmethod
    def expiry_from_seconds(seconds: int | float | None) -> str:
        if not seconds:
            return ""
        return (datetime.now(timezone.utc) + timedelta(seconds=int(seconds))).isoformat()

    def is_expired(self, margin: int = EXPIRY_MARGIN_SECONDS) -> bool:
        if not self.access_token:
            return True
        if not self.expires_at:
            return False
        try:
            expires = datetime.fromisoformat(self.expires_at)
        except ValueError:
            return False
        return datetime.now(timezone.utc) >= expires - timedelta(seconds=margin)

    def can_refresh(self) -> bool:
        if not self.refresh_token:
            return False
        if not self.refresh_expires_at:
            return True
        try:
            expires = datetime.fromisoformat(self.refresh_expires_at)
        except ValueError:
            return True
        return datetime.now(timezone.utc) < expires

    def masked(self) -> str:
        """画面表示用。トークン本体は絶対に出さない。"""
        if not self.access_token:
            return "未接続"
        tail = self.access_token[-4:]
        expiry = self.expires_at[:16].replace("T", " ") if self.expires_at else "不明"
        return f"接続済み（…{tail} / 期限 {expiry}）"


class TokenStore:
    """トークンファイルの読み書き。"""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)

    def path_for(self, platform: str) -> Path:
        return self.directory / f"{platform}.json"

    def load(self, platform: str) -> Token | None:
        path = self.path_for(platform)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        data.setdefault("platform", platform)
        known = {f for f in Token.__dataclass_fields__}
        return Token(**{k: v for k, v in data.items() if k in known})

    def save(self, token: Token) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(token.platform)
        path.write_text(
            json.dumps(asdict(token), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        try:
            os.chmod(path, 0o600)
        except OSError:      # Windows など権限設定できない環境
            pass
        return path

    def delete(self, platform: str) -> None:
        self.path_for(platform).unlink(missing_ok=True)

    def status(self, platform: str) -> str:
        token = self.load(platform)
        if token is None:
            return "未接続"
        if token.is_expired():
            return "期限切れ（再認証または自動更新が必要）"
        return token.masked()
