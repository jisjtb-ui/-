"""Cloudflare R2（S3互換）へアップロードするホスト。

R2 は egress 無料で、この用途（1投稿10枚・数百KB）なら無料枠に収まりやすい。
TikTok は「所有権を検証したドメイン配下のURL」しか受け付けないため、
R2_PUBLIC_BASE_URL には自分のカスタムドメインを設定し、
TikTok開発者ポータルでそのドメイン（またはURLプレフィックス）を検証しておくこと。
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from . import HostingError, ImageHost

CONTENT_TYPE = "image/jpeg"


class R2Host(ImageHost):
    name = "r2"

    def __init__(self, settings: Settings) -> None:
        missing = settings.missing("hosting")
        if missing:
            raise HostingError(".env の設定が不足しています: " + ", ".join(missing))
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - 依存が無い環境
            raise HostingError(
                "boto3 が必要です。`pip install boto3` を実行してください"
            ) from exc

        self.bucket = settings.r2_bucket
        self.base_url = settings.r2_public_base_url.rstrip("/")
        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
            config=Config(retries={"max_attempts": 3, "mode": "standard"}),
        )

    def upload(self, post_id: str, files: list[Path], content_hash: str) -> list[str]:
        urls: list[str] = []
        for path in files:
            key = f"{post_id}/{content_hash}/{path.name}"
            if not self._exists(key):
                try:
                    self.client.upload_file(
                        str(path),
                        self.bucket,
                        key,
                        ExtraArgs={"ContentType": CONTENT_TYPE, "CacheControl": "public, max-age=31536000"},
                    )
                except Exception as exc:  # boto3 の例外を包む
                    raise HostingError(f"R2へのアップロードに失敗しました: {exc}") from exc
            urls.append(f"{self.base_url}/{key}")
        return urls

    def _exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def describe(self) -> str:
        return f"Cloudflare R2（bucket={self.bucket} → {self.base_url}）"

    def public_prefix(self) -> str:
        return self.base_url
