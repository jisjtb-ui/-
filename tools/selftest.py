"""リリース前の自己テスト。外部通信もAPIキーも使わない。

  python tools/selftest.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "OK  " if condition else "NG  "
    print(f"  {mark}{name}" + (f" … {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(f"{name}: {detail}" if detail else name)


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * 52)


def main() -> int:
    section("モジュールの読み込み")
    try:
        import autopost
        import autopost.cli
        import autopost.collector
        import autopost.doctor
        import autopost.engine
        import autopost.migrations
        import autopost.queueing
        import autopost.updater
        import night_test.builder

        check("import", True)
    except Exception as exc:
        check("import", False, str(exc))
        print("\n読み込みに失敗したため以降を中止します")
        return 1

    section("バージョン")
    from autopost.version import bump, changelog_for, current_version, is_newer, parse

    version = current_version()
    check("VERSION が読める", parse(version) != (0, 0, 0), version)
    check("__version__ と一致", autopost.__version__ == version)
    check("比較が正しい", is_newer("1.2.0", "1.1.9") and not is_newer("1.1.0", "1.1.0"))
    check("bump", bump("1.1.0", "minor") == "1.2.0")
    check("CHANGELOGに今のバージョンの記載がある", bool(changelog_for(version)))

    section("更新機能の安全装置")
    from autopost.updater import Manifest, is_protected

    for path in (".env", "autopost.db", ".tokens/threads.json", "history.json",
                 "output/post_001/01_question.png", "autopost.log"):
        check(f"守られる: {path}", is_protected(path))
    for path in ("autopost/cli.py", "data/cta.json", "VERSION", "README.md"):
        check(f"更新対象: {path}", not is_protected(path))

    manifest = Manifest(version="9.9.9", files={"autopost/cli.py": {"sha256": "x"},
                                                ".env": {"sha256": "y"}})
    check("manifestに.envが混ざっても除外する", set(manifest.updatable()) == {"autopost/cli.py"})

    section("CLI")
    for args in (["version"], ["doctor", "--offline", "--out", tempfile.mkstemp(suffix=".txt")[1]],
                 ["update", "--help"]):
        result = subprocess.run([sys.executable, "autopost.py", *args],
                                cwd=ROOT, capture_output=True, text=True, timeout=180)
        check(f"autopost.py {args[0]}", result.returncode == 0,
              (result.stderr or result.stdout).strip()[-200:])

    section("画像生成")
    result = subprocess.run([sys.executable, "generate.py", "--validate"],
                            cwd=ROOT, capture_output=True, text=True, timeout=300)
    check("コンテンツ定義の検証", result.returncode == 0,
          (result.stderr or result.stdout).strip()[-300:])

    section("DBスキーマ")
    from autopost.config import Settings
    from autopost import migrations

    with tempfile.TemporaryDirectory() as tmp:
        settings = Settings.load()
        settings.db_path = Path(tmp) / "a.db"
        settings.experiments_db_path = Path(tmp) / "b.db"
        try:
            migrations.migrate(settings, log=lambda m: None, backup=False)
            migrations.migrate(settings, log=lambda m: None, backup=False)
            check("2回流しても壊れない", True)
        except Exception as exc:
            check("2回流しても壊れない", False, str(exc))

    print("\n" + "=" * 52)
    if FAILURES:
        print(f"失敗 {len(FAILURES)}件")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("すべて通りました")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
