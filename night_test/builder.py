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
from .actions import ActionSet, empty as empty_actions
from .cta import CtaTexts, empty as empty_cta
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
    cta: CtaTexts | None = None,
    actions: ActionSet | None = None,
    comment_prompt: str | None = None,
) -> PostResult:
    """画像10枚と caption.txt / meta.json を1フォルダに書き出す。

    ファイル名は 01_question.png / 02_answer.png ... と連番になるため、
    TikTokで10枚まとめて選ぶだけで「問題→答え」の順番が崩れない。

    CTAは広告臭くならないよう、1枚目（冒頭CTA）と最終ページ
    （保存→共有→コメント）にだけ入れる。02〜09はテスト体験に集中させる。
    """
    cta = cta or empty_cta()
    actions = actions or empty_actions()

    # アクション別CTAを使うときは、最終ページを「保存→共有→コメント」から
    # 「プロフィール／いいね／フォロー／共有 → それぞれの結果」に置き換える。
    assigned = [] if actions.is_empty() else actions.assign(rng)
    # 1枚目に入れるコメント誘導（投稿ごとに文を変える）
    if comment_prompt is None:
        comment_prompt = cta.comment_prompt(rng, len(tests))
    # 占いを使うときは、結果を最終ページに載せない。
    # 「選ぶ → めくる → 答え」の2枚組にして、押す理由とめくる理由を作る。
    final_lines = [] if assigned else cta.final_lines

    folder = output_dir / post_folder_name(post_id)
    if folder.exists():
        if not overwrite:
            raise FileExistsError(f"すでに存在します: {folder}")
        shutil.rmtree(folder)
    folder.mkdir(parents=True)

    # ページの並びを先に決める。占いは2枚1組で、指定した問題の後ろに入る。
    pages: list[tuple] = []
    insert_at = max(0, min(actions.insert_after, len(tests))) if assigned else -1
    if insert_at == 0:
        pages += [("choose", None), ("reveal", None)]
    for index, test in enumerate(tests):
        pages.append(("question", (index, test)))
        pages.append(("answer", (index, test)))
        if assigned and index + 1 == insert_at:
            pages += [("choose", None), ("reveal", None)]

    images: list[Path] = []
    slot = 0
    for kind, payload in pages:
        slot += 1
        if kind == "choose":
            path = folder / f"{slot:02d}_choose.png"
            renderer.render_message(
                actions.headline,
                [actions.action_labels.get(a, a) for a in actions.actions],
                actions.prompt_footer,
                cta_top=cta.first_page if slot == 1 else "",
                note=comment_prompt if slot == 1 else "",
            ).save(path, "PNG", optimize=True)
        elif kind == "reveal":
            path = folder / f"{slot:02d}_reveal.png"
            lines = actions.reveal_lines(assigned)
            renderer.render_message(
                lines[0] if lines else "", lines[1:], "", emphasis=False
            ).save(path, "PNG", optimize=True)
        else:
            index, test = payload
            number = index + 1
            if kind == "question":
                path = folder / f"{slot:02d}_question.png"
                renderer.render_question(
                    test, number, cta.first_page if slot == 1 else ""
                ).save(path, "PNG", optimize=True)
            else:
                path = folder / f"{slot:02d}_answer.png"
                is_last_test = index == len(tests) - 1
                renderer.render_answer(
                    test, number, final_lines if is_last_test else ()
                ).save(path, "PNG", optimize=True)
        images.append(path)

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
        "cta": cta.to_meta()
        | {
            "placement": {
                "first_page": "01_question.png" if cta.first_page else None,
                "final": f"{len(tests) * 2:02d}_answer.png" if final_lines else None,
            },
            "final_lines": list(final_lines),
            "comment_prompt": comment_prompt,
        },
        # アクション別CTAの割り当て（後からアクション別の効果を集計するため）
        "cta_actions": actions.to_meta(assigned) if assigned else None,
        "tests": [
            {
                "number": t["number"],
                "id": t["id"],
                "category": t["category"],
                "form": t["form"],
                "form_label": t["form_label"],
                "motif": t["motif"],
                "theme": t.get("theme", ""),
                "level": t.get("level", 1),
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
