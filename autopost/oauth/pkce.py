"""PKCE（Proof Key for Code Exchange）。

TikTok Login Kit の Desktop フローでは PKCE が必須。
注意: TikTok の code_challenge は **SHA256 の16進(hex)エンコード** で、
RFC 7636 の base64url ではない（公式ドキュメント記載の仕様）。

  code_verifier   : 43〜128文字、使用可能文字は [A-Z] [a-z] [0-9] "-" "." "_" "~"
  code_challenge  : hex(SHA256(code_verifier))
  code_challenge_method : "S256" 固定
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

# RFC 7636 の unreserved 文字（TikTokドキュメントと同じ集合）
UNRESERVED = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789-._~"
)
MIN_LENGTH = 43
MAX_LENGTH = 128
METHOD = "S256"


@dataclass(frozen=True)
class PkcePair:
    """1回の認可リクエストで使う verifier と challenge の組。"""

    verifier: str
    challenge: str
    method: str = METHOD


def new_code_verifier(length: int = 64) -> str:
    """毎回新しく生成する（使い回さない）。"""
    if not MIN_LENGTH <= length <= MAX_LENGTH:
        raise ValueError(f"code_verifier の長さは {MIN_LENGTH}〜{MAX_LENGTH} 文字です")
    return "".join(secrets.choice(UNRESERVED) for _ in range(length))


def code_challenge(verifier: str) -> str:
    """TikTok仕様: SHA256 を hex でエンコードしたもの（64文字）。"""
    return hashlib.sha256(verifier.encode("ascii")).hexdigest()


def new_pair(length: int = 64) -> PkcePair:
    verifier = new_code_verifier(length)
    return PkcePair(verifier=verifier, challenge=code_challenge(verifier))
