"""生成履歴（history.json）と、ネタの重複・類似判定。

完全一致だけでなく、「構図がほぼ同じ問題」が短期間に繰り返されるのを防ぐため、
文字bigramによる簡易類似度（Dice係数）で判定する。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# 直近この件数の問題と比較して類似判定を行う
RECENT_ITEM_WINDOW = 60
# 同じネタIDを再利用しない直近の投稿数
RECENT_POST_WINDOW = 12
# これを超えると「ほぼ同じ問題」とみなす
SIMILARITY_THRESHOLD = 0.55
# 質問の書き出しがこの文字数以上一致したら、構図が同じとみなす
COMMON_PREFIX_LIMIT = 8
# 同じモチーフ（夜道・既読など）を避ける直近の問題数
MOTIF_WINDOW = 25

_STRIP = re.compile(r"[\s、。，．・？！?!「」『』（）()\[\]【】〜ー…\.,:：;；]")


def normalize(text: str) -> str:
    """比較用にテキストを正規化する（記号・空白を落とす）。"""
    return _STRIP.sub("", text or "")


def bigrams(text: str) -> set[str]:
    text = normalize(text)
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def similarity(a: str, b: str) -> float:
    """文字bigramのDice係数（0.0〜1.0）。"""
    ga, gb = bigrams(a), bigrams(b)
    if not ga or not gb:
        return 0.0
    overlap = len(ga & gb)
    return 2 * overlap / (len(ga) + len(gb))


def common_prefix_len(a: str, b: str) -> int:
    """正規化済みテキスト同士の先頭一致文字数。"""
    limit = min(len(a), len(b))
    i = 0
    while i < limit and a[i] == b[i]:
        i += 1
    return i


def fingerprint(test: dict) -> str:
    """1問を代表する比較用テキストを作る。"""
    parts = [test.get("title", ""), test.get("question", "")]
    parts.extend(test.get("choices", {}).values())
    return "".join(parts)


@dataclass
class History:
    """history.json の読み書きと参照。"""

    path: Path
    data: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> "History":
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                backup = path.with_suffix(".broken.json")
                path.replace(backup)
                print(f"[警告] history.json が壊れていたため {backup.name} に退避しました")
                data = {}
        else:
            data = {}
        data.setdefault("version", 1)
        data.setdefault("last_post_id", 0)
        data.setdefault("posts", [])
        data.setdefault("usage", {})
        return cls(path=path, data=data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    # ------------------------------------------------------------------
    @property
    def posts(self) -> list[dict]:
        return self.data["posts"]

    @property
    def usage(self) -> dict:
        return self.data["usage"]

    @property
    def last_post_id(self) -> int:
        return int(self.data.get("last_post_id", 0))

    def next_post_id(self) -> int:
        return self.last_post_id + 1

    def recent_items(self, window: int = RECENT_ITEM_WINDOW) -> list[dict]:
        """直近に生成した問題を新しい順に返す。"""
        items: list[dict] = []
        for post in reversed(self.posts):
            for test in reversed(post.get("tests", [])):
                items.append(test)
                if len(items) >= window:
                    return items
        return items

    def recent_item_ids(self, posts_window: int = RECENT_POST_WINDOW) -> set[str]:
        ids: set[str] = set()
        for post in self.posts[-posts_window:]:
            for test in post.get("tests", []):
                ids.add(test.get("id", ""))
        return ids

    def recent_motifs(self, window: int = MOTIF_WINDOW) -> set[str]:
        return {t.get("motif", "") for t in self.recent_items(window) if t.get("motif")}

    def use_count(self, item_id: str) -> int:
        return int(self.usage.get(item_id, {}).get("count", 0))

    def last_used_post(self, item_id: str) -> int:
        return int(self.usage.get(item_id, {}).get("last_post_id", 0))

    def used_variants(self, item_id: str) -> set[str]:
        return set(self.usage.get(item_id, {}).get("variants", []))

    def conflict_score(
        self,
        test: dict,
        window: int = RECENT_ITEM_WINDOW,
        threshold: float = SIMILARITY_THRESHOLD,
        use_prefix_rule: bool = True,
    ) -> float:
        """直近の問題との「近さ」を返す。1.0 は実質同一とみなす。"""
        target = fingerprint(test)
        target_q = normalize(test.get("question", ""))
        best = 0.0
        for past in self.recent_items(window):
            score = similarity(target, past.get("fingerprint", ""))
            # 書き出しが長く一致する＝構図が同じ問題とみなす
            if use_prefix_rule and common_prefix_len(
                target_q, normalize(past.get("question", ""))
            ) >= COMMON_PREFIX_LIMIT:
                score = max(score, threshold + 0.01)
            if score > best:
                best = score
        return best

    # ------------------------------------------------------------------
    def record_post(self, post_id: int, category: str, folder: str, tests: list[dict]) -> None:
        """1投稿分の生成結果を履歴に追加する。"""
        entry = {
            "post_id": post_id,
            "folder": folder,
            "category": category,
            "header": tests[0].get("header", "") if tests else "",
            "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "tests": [
                {
                    "id": t["id"],
                    "category": t["category"],
                    "form": t["form"],
                    "motif": t["motif"],
                    "title": t["title"],
                    "question": t["question"],
                    "choices": list(t["choices"].values()),
                    "answer_theme": t.get("answer_theme", ""),
                    "variant": t["variant"],
                    "fingerprint": fingerprint(t),
                }
                for t in tests
            ],
        }
        self.posts.append(entry)
        self.data["last_post_id"] = max(self.last_post_id, post_id)
        for test in tests:
            rec = self.usage.setdefault(test["id"], {"count": 0, "last_post_id": 0, "variants": []})
            rec["count"] = int(rec.get("count", 0)) + 1
            rec["last_post_id"] = post_id
            key = test["variant"]
            if key not in rec["variants"]:
                rec["variants"].append(key)
