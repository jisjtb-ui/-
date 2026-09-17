"""1投稿分（画像10枚 + caption.txt + meta.json）の書き出し。"""

from __future__ import annotations

import json
import random
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .captions import build_caption
from .config import Layout
from .renderer import Renderer, build_contact_sheet

PREVIEW_NAME = "preview.jpg"


@dataclass
class PostResult:
    post_id: int
    folder: Path
    images: list[Path]
    caption: str


def post_folder_name(post_id: int) -> str:
    return f"post_{post_id:03d}"


def build_post(
    post_id: int,
    tests: list[dict],
    category: str,
    output_dir: Path,
    renderer: Renderer,
    caption_data: dict,
    rng: random.Random,
    layout: Layout,
    make_preview: bool = True,
    overwrite: bool = False,
) -> PostResult:
    """画像10枚と caption.txt / meta.json を1フォルダに書き出す。

    ファイル名は 01_question.png / 02_answer.png ... と連番になるため、
    TikTokで10枚まとめて選ぶだけで「問題→答え」の順番が崩れない。
    """
    folder = output_dir / post_folder_name(post_id)
    if folder.exists():
        if not overwrite:
            raise FileExistsError(f"すでに存在します: {folder}")
        shutil.rmtree(folder)
    folder.mkdir(parents=True)

    images: list[Path] = []
    for index, test in enumerate(tests):
        number = index + 1
        q_path = folder / f"{number * 2 - 1:02d}_question.png"
        a_path = folder / f"{number * 2:02d}_answer.png"
        renderer.render_question(test, number).save(q_path, "PNG", optimize=True)
        renderer.render_answer(test, number).save(a_path, "PNG", optimize=True)
        images.extend([q_path, a_path])

    caption = build_caption(caption_data, rng, category, tests)
    (folder / "caption.txt").write_text(caption, encoding="utf-8")

    meta = {
        "post_id": post_id,
        "folder": folder.name,
        "category": category,
        "header": tests[0].get("header", "") if tests else "",
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "image_size": {"width": layout.width, "height": layout.height},
        "image_count": len(images),
        "caption": caption,
        "tests": [
            {
                "number": t["number"],
                "id": t["id"],
                "category": t["category"],
                "form": t["form"],
                "form_label": t["form_label"],
                "motif": t["motif"],
                "title": t["title"],
                "question": t["question"],
                "choices": t["choices"],
                "answers": t["answers"],
                "closing": t["closing"],
                "variant": t["variant"],
                "files": {
                    "question": f"{t['number'] * 2 - 1:02d}_question.png",
                    "answer": f"{t['number'] * 2:02d}_answer.png",
                },
            }
            for t in tests
        ],
    }
    (folder / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if make_preview:
        sheet = build_contact_sheet(images)
        sheet.save(folder / PREVIEW_NAME, "JPEG", quality=88, optimize=True)

    return PostResult(post_id=post_id, folder=folder, images=images, caption=caption)
