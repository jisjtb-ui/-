#!/usr/bin/env python3
"""自動投稿システムのエントリポイント。

  python autopost.py status
  python autopost.py schedule --folder output --start 2026-09-20 --time 21:00 --count 100
  python autopost.py run
"""

from autopost.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
