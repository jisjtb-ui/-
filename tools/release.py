"""リリース作業をひとまとめにする（Claude Code が実行する）。

  python tools/release.py --minor --notes "Instagram Reelに対応" --notes "Analytics修正"
  python tools/release.py --patch            # CHANGELOGを自分で書いた場合
  python tools/release.py --set 2.0.0 --push

流れ:
  1. 自己テスト（tools/selftest.py）
  2. バージョンを上げる（VERSION が唯一の出所）
  3. CHANGELOG.md に変更内容を追記
  4. update_manifest.json を生成（Git管理下のファイルのSHA256一覧）
  5. コミット
  6. --push を付けたときだけ push する

push と公開は既定では行わない。壊れたものを配布しないため、
明示的に指定したときだけ外部へ出す。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autopost.updater import MANIFEST_NAME, Manifest, is_protected, sha256_of  # noqa: E402
from autopost.version import (  # noqa: E402
    CHANGELOG_FILE,
    MANIFEST_FILE,
    bump,
    changelog_for,
    current_version,
    parse,
    write_version,
)

BRANCH_DEFAULT = "main"


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=ROOT, text=True, **kwargs)


def tracked_files() -> list[str]:
    """Gitが管理しているファイル = アプリ本体。

    .env / *.db / .tokens / output などは .gitignore 済みなので
    ここに現れない。つまりユーザーデータは構造的に配布物へ入らない。
    """
    result = run(["git", "ls-files", "-z"], capture_output=True, check=True)
    paths = [p for p in result.stdout.split("\0") if p]
    return sorted(
        p for p in paths if p != MANIFEST_NAME and not is_protected(p)
    )


def build_manifest(version: str, notes: list[str], branch: str) -> Manifest:
    files: dict[str, dict] = {}
    for relative in tracked_files():
        path = ROOT / relative
        if not path.is_file():
            continue
        files[relative] = {"sha256": sha256_of(path), "size": path.stat().st_size}
    if not files:
        raise SystemExit("[エラー] 配布対象のファイルが見つかりません")
    return Manifest(
        version=version,
        released_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        channel=branch,
        notes=notes,
        files=files,
    )


def prepend_changelog(version: str, notes: list[str]) -> None:
    text = CHANGELOG_FILE.read_text(encoding="utf-8") if CHANGELOG_FILE.is_file() else "# 変更履歴\n"
    lines = text.splitlines()
    entry = [f"## v{version}", ""] + [f"- {n}" for n in notes] + [""]

    # 最初の "## " の直前に差し込む
    for index, line in enumerate(lines):
        if line.startswith("## "):
            lines[index:index] = entry
            break
    else:
        lines += [""] + entry
    CHANGELOG_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="リリース作業")
    level = parser.add_mutually_exclusive_group(required=True)
    level.add_argument("--major", action="store_true")
    level.add_argument("--minor", action="store_true")
    level.add_argument("--patch", action="store_true")
    level.add_argument("--set", dest="exact", help="バージョンを直接指定")
    parser.add_argument("--notes", action="append", default=[],
                        help="変更内容（複数指定可）。省略時はCHANGELOGの記載を使う")
    parser.add_argument("--branch", default=BRANCH_DEFAULT, help="配布に使うブランチ")
    parser.add_argument("--skip-tests", action="store_true", help="テストを飛ばす（非推奨）")
    parser.add_argument("--push", action="store_true", help="コミット後にpushする")
    parser.add_argument("--dry-run", action="store_true", help="書き換えずに内容だけ見る")
    args = parser.parse_args()

    installed = current_version()
    if args.exact:
        version = args.exact.lstrip("v")
        if parse(version) == (0, 0, 0):
            raise SystemExit(f"[エラー] バージョン番号として不正です: {args.exact}")
    else:
        level_name = "major" if args.major else "minor" if args.minor else "patch"
        version = bump(installed, level_name)

    print(f"現在: v{installed}  →  新: v{version}")

    # 1. テスト
    if args.skip_tests:
        print("\n[1/5] テスト … 飛ばしました")
    else:
        print("\n[1/5] テスト")
        result = run([sys.executable, "tools/selftest.py"])
        if result.returncode != 0:
            raise SystemExit("[中止] テストが通らないためリリースしません")

    notes = args.notes or changelog_for(version) or changelog_for(installed)
    if not args.notes and not changelog_for(version):
        print("\n[注意] --notes が無く、CHANGELOGにも新バージョンの記載がありません。")
        print("       変更内容なしでリリースします。--notes で指定できます。")
        notes = []

    if args.dry_run:
        manifest = build_manifest(version, notes, args.branch)
        print(f"\n[dry-run] ファイル{len(manifest.files)}件 / 変更内容{len(notes)}件")
        for note in notes:
            print(f"  - {note}")
        return 0

    # 2. バージョン
    print("\n[2/5] バージョンを更新")
    write_version(version)
    print(f"  VERSION → {version}")

    # 3. CHANGELOG
    print("\n[3/5] CHANGELOG")
    if args.notes:
        prepend_changelog(version, notes)
        print(f"  v{version} の項目を追記しました")
    else:
        print("  既存の記載を使います")

    # 4. manifest
    print("\n[4/5] 更新情報を生成")
    manifest = build_manifest(version, notes, args.branch)
    MANIFEST_FILE.write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    total = sum(meta["size"] for meta in manifest.files.values())
    print(f"  {MANIFEST_NAME} … {len(manifest.files)}ファイル / {total / 1024 / 1024:.1f} MB")

    # 5. コミット
    print("\n[5/5] コミット")
    run(["git", "add", "-A"], check=True)
    message = f"Release v{version}\n\n" + "\n".join(f"- {n}" for n in notes)
    commit = run(["git", "commit", "-m", message], capture_output=True)
    if commit.returncode != 0:
        print(f"  コミットなし: {(commit.stdout or commit.stderr).strip()[:200]}")
    else:
        print(f"  v{version} をコミットしました")

    if args.push:
        branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                     capture_output=True, check=True).stdout.strip()
        print(f"\npush: {branch}")
        if run(["git", "push", "-u", "origin", branch]).returncode != 0:
            raise SystemExit("[エラー] pushに失敗しました")
    else:
        print("\npushはしていません。配布するには push してください。")
        print(f"  git push -u origin <branch>   → その後 {args.branch} へ反映")

    print(f"\n完了: v{version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
