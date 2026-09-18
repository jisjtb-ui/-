"""ソフト本体の更新。

配布方法は「公開GitHubリポジトリのブランチ」。ビルド成果物やEXEは無く、
Pythonのソースがそのまま動くため、**ファイルを置き換えるだけ**で更新できる。

安全側の作り:

1. 更新情報（update_manifest.json）を取得してバージョンを比べる
2. ソース一式をZIPで取得し、一時領域へ展開する
3. manifest に書かれた1ファイルずつ SHA256 を照合する（整合性確認）
4. 一時領域のまま起動テストを行う（壊れたコードを反映させない）
5. 現在のファイルをバックアップしてから置き換える
6. 途中で失敗したらバックアップから復元する

**ユーザーのデータには触らない。**
置き換える対象は manifest に載っているファイルだけで、manifest は
Gitで管理されているファイルから作られる。.env / *.db / .tokens /
output などは Git管理外なので、構造上まず対象にならない。
念のため PROTECTED による二重の防御も入れている。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable

import requests

from .version import APP_ROOT, MANIFEST_FILE, current_version, is_newer

LogFn = Callable[[str], None]

DEFAULT_REPO = "jisjtb-ui/-"
DEFAULT_BRANCH = "main"
MANIFEST_NAME = "update_manifest.json"

# 見本なので配布する（PROTECTED より優先する）。
# .env.example は新しい設定項目を伝える手段なので、更新から外してはいけない。
SHIPPABLE = (".env.example", ".env.sample", ".env.template")

# 更新で絶対に触らないもの（manifest に混ざっていても無視する）
PROTECTED = (
    ".env",
    ".env.*",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "*.log",
    "*.sqlite",
    "*.sqlite3",
    "history.json",
    "診断結果.txt",
    ".tokens/*",
    "output/*",
    "ready/*",
    "posted/*",
    "pages_media/*",
    "backups/*",
    ".autopost_cache/*",
)

# 上流から消えたファイルを削除してよいディレクトリ（アプリのコードだけ）
MANAGED_DIRS = ("autopost/", "night_test/", "tools/", "scripts/")

BACKUP_DIR = APP_ROOT / "backups"
TIMEOUT = 60


class UpdateError(RuntimeError):
    """更新を中止した。現在のアプリは壊れていない。"""


def is_protected(relative: str) -> bool:
    path = relative.replace("\\", "/")
    name = path.rsplit("/", 1)[-1]
    if name in SHIPPABLE:
        return False
    return any(fnmatch(path, pattern) or fnmatch(name, pattern) for pattern in PROTECTED)


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ----------------------------------------------------------------------
@dataclass
class Manifest:
    """配布物の目録。tools/release.py が生成する。"""

    version: str
    released_at: str = ""
    channel: str = DEFAULT_BRANCH
    notes: list[str] = field(default_factory=list)
    files: dict[str, dict] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        version = str(data.get("version") or "").strip()
        if not version:
            raise UpdateError("更新情報にバージョンが入っていません")
        files = data.get("files") or {}
        if not isinstance(files, dict) or not files:
            raise UpdateError("更新情報にファイル一覧が入っていません")
        return cls(
            version=version,
            released_at=str(data.get("released_at") or ""),
            channel=str(data.get("channel") or DEFAULT_BRANCH),
            notes=[str(n) for n in (data.get("notes") or [])],
            files=files,
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "released_at": self.released_at,
            "channel": self.channel,
            "notes": self.notes,
            "files": self.files,
        }

    def updatable(self) -> dict[str, dict]:
        """実際に置き換えてよいファイルだけ。"""
        return {
            path: meta for path, meta in self.files.items() if not is_protected(path)
        }


@dataclass
class UpdateInfo:
    available: bool
    current: str
    latest: str
    notes: list[str] = field(default_factory=list)
    manifest: Manifest | None = None
    message: str = ""


# ----------------------------------------------------------------------
class UpdateSource:
    """更新の取得元。テスト時は環境変数で差し替えられる。"""

    def __init__(self, repo: str = "", branch: str = "") -> None:
        self.repo = repo or os.environ.get("UPDATE_REPO") or DEFAULT_REPO
        self.branch = branch or os.environ.get("UPDATE_BRANCH") or DEFAULT_BRANCH

    @property
    def manifest_url(self) -> str:
        override = os.environ.get("UPDATE_MANIFEST_URL")
        if override:
            return override
        return (
            f"https://raw.githubusercontent.com/{self.repo}/{self.branch}/{MANIFEST_NAME}"
        )

    @property
    def package_url(self) -> str:
        override = os.environ.get("UPDATE_PACKAGE_URL")
        if override:
            return override
        return f"https://codeload.github.com/{self.repo}/zip/refs/heads/{self.branch}"

    def describe(self) -> str:
        if os.environ.get("UPDATE_MANIFEST_URL"):
            return self.manifest_url
        return f"{self.repo} / {self.branch}"


def fetch_manifest(source: UpdateSource) -> Manifest:
    url = source.manifest_url
    try:
        response = requests.get(url, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise UpdateError(
            f"更新情報を取得できませんでした（{type(exc).__name__}）。"
            "インターネット接続を確認してください"
        ) from exc

    if response.status_code == 404:
        raise UpdateError(
            f"更新情報が見つかりません（{url}）。"
            "配布元にまだ更新情報が公開されていません"
        )
    if response.status_code != 200:
        raise UpdateError(f"更新情報を取得できませんでした（HTTP {response.status_code}）")

    try:
        return Manifest.from_dict(response.json())
    except ValueError as exc:
        raise UpdateError("更新情報の形式が壊れています") from exc


def check(source: UpdateSource | None = None) -> UpdateInfo:
    """更新の有無を調べる。ここではまだ何も書き換えない。"""
    source = source or UpdateSource()
    installed = current_version()
    manifest = fetch_manifest(source)

    if not is_newer(manifest.version, installed):
        return UpdateInfo(
            available=False,
            current=installed,
            latest=manifest.version,
            manifest=manifest,
            message="最新バージョンです",
        )
    return UpdateInfo(
        available=True,
        current=installed,
        latest=manifest.version,
        notes=manifest.notes,
        manifest=manifest,
        message=f"新しいバージョンがあります（v{installed} → v{manifest.version}）",
    )


# ----------------------------------------------------------------------
def _download(source: UpdateSource, destination: Path, log: LogFn) -> Path:
    log("ダウンロードしています…")
    try:
        response = requests.get(source.package_url, timeout=TIMEOUT, stream=True)
    except requests.RequestException as exc:
        raise UpdateError(f"ダウンロードできませんでした（{type(exc).__name__}）") from exc
    if response.status_code != 200:
        raise UpdateError(f"ダウンロードできませんでした（HTTP {response.status_code}）")

    archive = destination / "package.zip"
    size = 0
    with archive.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=256 * 1024):
            handle.write(chunk)
            size += len(chunk)
    if size == 0:
        raise UpdateError("ダウンロードした内容が空でした")
    log(f"ダウンロード完了（{size / 1024 / 1024:.1f} MB）")
    return archive


def _extract(archive: Path, destination: Path, log: LogFn) -> Path:
    log("展開しています…")
    try:
        with zipfile.ZipFile(archive) as zf:
            if zf.testzip() is not None:
                raise UpdateError("ダウンロードしたファイルが壊れています")
            for member in zf.namelist():
                # ZIP内の絶対パスや .. による外部書き込みを拒否する
                if member.startswith("/") or ".." in Path(member).parts:
                    raise UpdateError(f"不正なパスが含まれています: {member}")
            zf.extractall(destination)
    except zipfile.BadZipFile as exc:
        raise UpdateError("ダウンロードしたファイルがZIPとして読めません") from exc

    entries = [p for p in destination.iterdir() if p.is_dir()]
    if len(entries) != 1:
        raise UpdateError("展開後の構成が想定と違います")
    return entries[0]


def _verify(root: Path, manifest: Manifest, log: LogFn) -> None:
    """manifest の SHA256 と1ファイルずつ照合する。"""
    log("整合性を確認しています…")
    missing: list[str] = []
    broken: list[str] = []
    for relative, meta in manifest.updatable().items():
        target = root / relative
        if not target.is_file():
            missing.append(relative)
            continue
        expected = str(meta.get("sha256") or "")
        if expected and sha256_of(target) != expected:
            broken.append(relative)

    if missing or broken:
        detail = []
        if missing:
            detail.append(f"不足 {len(missing)}件（例: {missing[0]}）")
        if broken:
            detail.append(f"内容が一致しない {len(broken)}件（例: {broken[0]}）")
        raise UpdateError("整合性の確認に失敗しました: " + " / ".join(detail))
    log(f"整合性OK（{len(manifest.updatable())}ファイル）")


def _smoke_test(root: Path, log: LogFn) -> None:
    """展開したコードが実際に起動するかを、反映前に確かめる。"""
    log("起動テストをしています…")
    checks = (
        ["-c", "import autopost, autopost.cli, autopost.updater, night_test"],
        ["autopost.py", "version"],
    )
    for args in checks:
        result = subprocess.run(
            [sys.executable, *args],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            tail = detail[-1] if detail else "詳細なし"
            raise UpdateError(f"新しいバージョンの起動テストに失敗しました: {tail}")
    log("起動テストOK")


def _preflight(root: Path, targets: dict[str, dict], log: LogFn) -> None:
    """置き換えを始める前に、置き換えられない場所が無いか確かめる。

    置き換え先が「ディレクトリ」だと shutil.copy2 はその中へコピーしてしまい、
    失敗もせずに更新漏れが起きる。始める前に見つけて中止する。
    """
    conflicts = [
        relative for relative in targets
        if (root / relative).exists() and not (root / relative).is_file()
    ]
    if conflicts:
        raise UpdateError(
            "置き換えられない場所があります（同名のフォルダが存在します）: "
            + ", ".join(conflicts[:5])
            + "。手動で削除してから、もう一度お試しください"
        )
    log("置き換え先を確認しました")


def _removed_files(old: Manifest | None, new: Manifest) -> list[str]:
    """上流から削除されたファイル（アプリのコードに限る）。"""
    if old is None:
        return []
    gone = set(old.files) - set(new.files)
    return sorted(
        path
        for path in gone
        if not is_protected(path)
        and path.replace("\\", "/").startswith(MANAGED_DIRS)
    )


def local_manifest() -> Manifest | None:
    try:
        return Manifest.from_dict(json.loads(MANIFEST_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError, UpdateError):
        return None


def _clear_pycache(root: Path) -> None:
    """古いバイトコードが残ると更新後に食い違うため消す。"""
    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def apply_update(
    info: UpdateInfo,
    source: UpdateSource | None = None,
    log: LogFn = print,
    app_root: Path | None = None,
) -> Path:
    """更新を適用し、バックアップの場所を返す。失敗時は元に戻す。"""
    source = source or UpdateSource()
    root = app_root or APP_ROOT
    manifest = info.manifest
    if manifest is None:
        raise UpdateError("先に更新の確認を行ってください")

    with tempfile.TemporaryDirectory(prefix="autopost_update_") as tmp:
        workspace = Path(tmp)
        archive = _download(source, workspace, log)
        extracted = _extract(archive, workspace / "unpacked", log)
        _verify(extracted, manifest, log)
        _smoke_test(extracted, log)

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = BACKUP_DIR / f"v{info.current}_{stamp}"
        backup.mkdir(parents=True, exist_ok=True)

        targets = manifest.updatable()
        _preflight(root, targets, log)
        removed = _removed_files(local_manifest(), manifest)
        copied: list[str] = []
        log(f"置き換えます（{len(targets)}ファイル）…")
        try:
            for relative in list(targets) + removed:
                existing = root / relative
                if existing.is_file():
                    saved = backup / relative
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(existing, saved)

            for relative in targets:
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                # 直接上書きせず、隣に書いてから差し替える。
                # 途中で電源が切れても中途半端なファイルが残らない。
                staged = destination.with_name(destination.name + ".new")
                shutil.copy2(extracted / relative, staged)
                os.replace(staged, destination)
                copied.append(relative)

            for relative in removed:
                victim = root / relative
                if victim.is_file():
                    victim.unlink()
                    log(f"削除: {relative}")
        except Exception as exc:  # 置き換え中の失敗 → 元に戻す
            log(f"失敗しました。元のバージョンへ戻します: {exc}")
            _restore(backup, root, log)
            raise UpdateError(
                f"更新に失敗したため元のバージョンへ戻しました: {exc}"
            ) from exc

        _clear_pycache(root)
        log(f"置き換え完了（バックアップ: {backup}）")
        return backup


def _restore(backup: Path, root: Path, log: LogFn) -> None:
    """バックアップから復元する。"""
    restored = 0
    for saved in backup.rglob("*"):
        if not saved.is_file():
            continue
        relative = saved.relative_to(backup)
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(saved, destination)
        restored += 1
    _clear_pycache(root)
    log(f"{restored}ファイルを復元しました")


def rollback(backup: Path, log: LogFn = print, app_root: Path | None = None) -> None:
    """手動で1つ前へ戻す。"""
    root = app_root or APP_ROOT
    if not backup.is_dir():
        raise UpdateError(f"バックアップが見つかりません: {backup}")
    _restore(backup, root, log)


def backups() -> list[Path]:
    """アプリ本体のバックアップ（新しい順）。DBのバックアップは含めない。"""
    if not BACKUP_DIR.is_dir():
        return []
    return sorted(
        (p for p in BACKUP_DIR.iterdir() if p.is_dir() and p.name.startswith("v")),
        reverse=True,
    )


def restart_command() -> list[str]:
    """更新後にアプリを起動し直すコマンド。"""
    return [sys.executable, str(APP_ROOT / "autopost_gui.py")]
