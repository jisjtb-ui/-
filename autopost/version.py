"""バージョン情報の単一の出所（Single Source of Truth）。

バージョン番号は **リポジトリ直下の VERSION ファイルだけ** に書く。
コードやドキュメントに同じ番号を二重に書かない。

  VERSION            ← ここだけを更新する（tools/release.py が行う）
  autopost/version.py← 読み取るだけ
"""

from __future__ import annotations

import re
from pathlib import Path

# autopost/version.py → リポジトリ直下
APP_ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = APP_ROOT / "VERSION"
CHANGELOG_FILE = APP_ROOT / "CHANGELOG.md"
MANIFEST_FILE = APP_ROOT / "update_manifest.json"

FALLBACK_VERSION = "0.0.0"


def current_version() -> str:
    """インストールされているバージョン。"""
    try:
        text = VERSION_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return FALLBACK_VERSION
    return text or FALLBACK_VERSION


def parse(version: str) -> tuple[int, int, int]:
    """'v1.2.3' / '1.2.3' → (1, 2, 3)。解釈できない部分は0にする。"""
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(g) for g in match.groups())  # type: ignore[return-value]


def is_newer(candidate: str, than: str) -> bool:
    """candidate が than より新しいか。"""
    return parse(candidate) > parse(than)


def bump(version: str, level: str) -> str:
    """major / minor / patch を1つ上げた番号を返す。"""
    major, minor, patch = parse(version)
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError(f"不明な更新種別: {level}")


def write_version(version: str) -> None:
    """VERSION ファイルを書き換える（リリース作業専用）。"""
    if parse(version) == (0, 0, 0):
        raise ValueError(f"バージョン番号として解釈できません: {version}")
    VERSION_FILE.write_text(f"{version}\n", encoding="utf-8")


def changelog_for(version: str, path: Path | None = None) -> list[str]:
    """CHANGELOG.md から、そのバージョンの変更内容だけを抜き出す。"""
    target = path or CHANGELOG_FILE
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    wanted = parse(version)
    collecting = False
    notes: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if collecting:
                break
            collecting = parse(line) == wanted
            continue
        if collecting:
            text = line.strip()
            if text:
                notes.append(text.lstrip("-・* ").strip())
    return notes
