"""Category / 生成 / 成績 の画面。

この画面の決まりごと:

  - 利用者が文字を打つのは「Categoryを作る」「SubCategoryを作る」の2か所だけ
  - それ以外はすべて選択・ボタン・ON/OFF
  - 画面が内部で持ち回すのは ``category_id`` / ``sub_category_id``。名前は表示専用
  - Categoryを選ぶと、そのCategoryのSubCategoryと接続済み媒体だけが出る
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, W, X, Y, BooleanVar, StringVar, messagebox, ttk

from .catalog import Catalog, connect_platform, ensure_default, sync_accounts
from .models import ALL_PLATFORMS, MANUAL_PLATFORMS

# Categoryへ接続できる媒体（手作業のみのチャネルは除く）
CONNECTABLE = tuple(p for p in ALL_PLATFORMS if p not in MANUAL_PLATFORMS)

PAD = {"padx": 8, "pady": 4}


class Choice:
    """表示名と内部IDの対。画面には名前を、処理にはIDを渡す。"""

    def __init__(self) -> None:
        self._by_label: dict[str, int] = {}

    def set(self, pairs: list[tuple[int, str]]) -> list[str]:
        """(id, 表示名) の一覧を覚えて、表示名の一覧を返す。"""
        self._by_label = {}
        labels = []
        for identifier, label in pairs:
            unique = label
            suffix = 2
            while unique in self._by_label:      # 同名でも選び分けられるようにする
                unique = f"{label} ({suffix})"
                suffix += 1
            self._by_label[unique] = identifier
            labels.append(unique)
        return labels

    def id_of(self, label: str) -> int | None:
        return self._by_label.get(label)

    def label_of(self, identifier: int) -> str:
        for label, value in self._by_label.items():
            if value == identifier:
                return label
        return ""

    def first(self) -> str:
        return next(iter(self._by_label), "")


class CategoryTab(ttk.Frame):
    """Categoryを作り、SubCategoryを持たせ、媒体をつなぐ画面。"""

    def __init__(self, parent, app) -> None:
        super().__init__(parent)
        self.app = app
        self.catalog: Catalog = app.catalog
        self.categories = Choice()
        self.subs = Choice()
        self.account_labels: dict[str, StringVar] = {}
        self._build()
        self.reload()

    # ------------------------------------------------------------------
    def _build(self) -> None:
        left = ttk.LabelFrame(self, text="Category")
        left.pack(side=LEFT, fill=Y, **PAD)
        self.category_list = tk.Listbox(left, width=22, height=14, exportselection=False)
        self.category_list.pack(fill=BOTH, expand=True, padx=6, pady=6)
        self.category_list.bind("<<ListboxSelect>>", lambda event: self.on_select_category())

        row = ttk.Frame(left)
        row.pack(fill=X, padx=6, pady=(0, 6))
        self.new_name = StringVar()
        ttk.Entry(row, textvariable=self.new_name, width=14).pack(side=LEFT)
        ttk.Button(row, text="＋ 作成", command=self.add_category).pack(side=LEFT, padx=4)

        right = ttk.Frame(self)
        right.pack(side=LEFT, fill=BOTH, expand=True)

        sub_frame = ttk.LabelFrame(right, text="SubCategory（チェックを外すと抽選から除く）")
        sub_frame.pack(fill=BOTH, expand=True, **PAD)
        columns = ("name", "state", "weight")
        self.sub_tree = ttk.Treeview(sub_frame, columns=columns, show="headings", height=10)
        for name, title, width in (("name", "SubCategory", 220),
                                   ("state", "抽選", 70),
                                   ("weight", "生成割合", 90)):
            self.sub_tree.heading(name, text=title)
            self.sub_tree.column(name, width=width, anchor=W)
        scroll = ttk.Scrollbar(sub_frame, orient="vertical", command=self.sub_tree.yview)
        self.sub_tree.configure(yscrollcommand=scroll.set)
        self.sub_tree.pack(side=LEFT, fill=BOTH, expand=True, padx=(6, 0), pady=6)
        scroll.pack(side=RIGHT, fill=Y, pady=6)

        sub_buttons = ttk.Frame(right)
        sub_buttons.pack(fill=X, padx=8)
        self.new_sub = StringVar()
        ttk.Entry(sub_buttons, textvariable=self.new_sub, width=16).pack(side=LEFT)
        ttk.Button(sub_buttons, text="＋ SubCategory", command=self.add_sub).pack(side=LEFT, padx=4)
        ttk.Button(sub_buttons, text="抽選に入れる／外す",
                   command=self.toggle_sub).pack(side=LEFT, padx=4)

        hook_frame = ttk.LabelFrame(right, text="1枚目フック（A/B/Cテスト）")
        hook_frame.pack(fill=X, **PAD)
        ttk.Label(hook_frame, text="使うフック").grid(row=0, column=0, sticky=W,
                                                 padx=8, pady=6)
        self.hook_var = StringVar()
        self.hook_box = ttk.Combobox(hook_frame, textvariable=self.hook_var,
                                     state="readonly", width=26)
        self.hook_box.grid(row=0, column=1, sticky=W)
        self.hook_box.bind("<<ComboboxSelected>>", lambda e: self.save_hook())
        self.hook_note = StringVar(value="")
        ttk.Label(hook_frame, textvariable=self.hook_note, foreground="#555",
                  wraplength=430, justify=LEFT).grid(row=1, column=0, columnspan=3,
                                                     sticky=W, padx=8, pady=(0, 6))

        account_frame = ttk.LabelFrame(right, text="このCategoryのSNSアカウント")
        account_frame.pack(fill=X, **PAD)
        for index, platform in enumerate(CONNECTABLE):
            ttk.Label(account_frame, text=platform, width=12).grid(
                row=index, column=0, sticky=W, padx=8, pady=3)
            var = StringVar(value="未接続")
            self.account_labels[platform] = var
            ttk.Label(account_frame, textvariable=var, width=34).grid(
                row=index, column=1, sticky=W)
            ttk.Button(account_frame, text="接続",
                       command=lambda p=platform: self.connect(p)).grid(row=index, column=2, padx=6)
        ttk.Button(account_frame, text="接続状況を再確認",
                   command=self.reload_accounts).grid(row=len(CONNECTABLE), column=1,
                                                      sticky=W, pady=4)

    # ------------------------------------------------------------------
    @property
    def category_id(self) -> int | None:
        selection = self.category_list.curselection()
        if not selection:
            return None
        return self.categories.id_of(self.category_list.get(selection[0]))

    def reload(self) -> None:
        labels = self.categories.set([(c.id, c.name) for c in self.catalog.categories()])
        self.category_list.delete(0, END)
        for label in labels:
            self.category_list.insert(END, label)
        if labels:
            self.category_list.selection_set(0)
            self.on_select_category()

    def on_select_category(self) -> None:
        self.reload_subs()
        self.reload_accounts()
        self.reload_hooks()
        self.app.on_category_changed()

    def reload_subs(self) -> None:
        self.sub_tree.delete(*self.sub_tree.get_children())
        category_id = self.category_id
        if category_id is None:
            return
        weights = self.app.weight_table(category_id)
        subs = self.catalog.sub_categories(category_id, include_disabled=True)
        self.subs.set([(s.id, s.name) for s in subs])
        for sub in subs:
            share = weights.get(sub.id)
            self.sub_tree.insert(
                "", END, iid=str(sub.id),
                values=(sub.name,
                        "入れる" if sub.enabled else "外す",
                        f"{share:.1f}%" if share is not None else "—"),
            )

    def reload_accounts(self) -> None:
        category_id = self.category_id
        if category_id is None:
            return
        accounts = sync_accounts(self.app.settings, self.catalog, category_id, list(CONNECTABLE))
        for account in accounts:
            self.account_labels[account.platform].set(account.label())

    # ------------------------------------------------------------------
    def reload_hooks(self) -> None:
        """1枚目フックの選択肢を data/hooks.json から作る（手入力させない）。"""
        options = self.app.hook_choices()
        self.hooks = Choice()
        labels = self.hooks.set([(index, label) for index, (_, label)
                                 in enumerate(options)])
        self._hook_values = [value for value, _ in options]
        self.hook_box.configure(values=labels)
        current = self.app.hook_setting(self.category_id)
        if current in self._hook_values:
            self.hook_var.set(labels[self._hook_values.index(current)])
        elif labels:
            self.hook_var.set(labels[0])
        self._describe_hook()

    def _describe_hook(self) -> None:
        value = self._selected_hook()
        if value == "rotate":
            self.hook_note.set(
                "投稿ごとに A → B → C の順で切り替えます。"
                "時間帯にも偏らないようにずらすので、まず均等にデータを集められます。")
        elif value == "none":
            self.hook_note.set("フックを使いません（従来の占い2枚組のまま）。")
        else:
            lines = self.app.hook_lines(value)
            self.hook_note.set("この文言で固定します：" + " / ".join(lines))

    def _selected_hook(self) -> str:
        index = self.hooks.id_of(self.hook_var.get())
        if index is None or index >= len(self._hook_values):
            return "rotate"
        return self._hook_values[index]

    def save_hook(self) -> None:
        value = self._selected_hook()
        self.app.set_hook_setting(self.category_id, value)
        self._describe_hook()
        self.app.log(f"1枚目フックを「{self.hook_var.get()}」にしました")

    # ------------------------------------------------------------------
    def add_category(self) -> None:
        try:
            category = self.catalog.add_category(self.new_name.get())
        except ValueError as exc:
            messagebox.showerror("Category", str(exc))
            return
        self.new_name.set("")
        ensure_default(self.catalog, category.name, self.app.tests_dir, log=self.app.log)
        self.app.log(f"Category「{category.name}」を作りました")
        self.reload()
        self.app.on_category_changed()

    def add_sub(self) -> None:
        category_id = self.category_id
        if category_id is None:
            messagebox.showinfo("SubCategory", "先にCategoryを選んでください")
            return
        try:
            sub = self.catalog.add_sub_category(category_id, self.new_sub.get())
        except ValueError as exc:
            messagebox.showerror("SubCategory", str(exc))
            return
        self.new_sub.set("")
        self.app.log(f"SubCategory「{sub.name}」を追加しました")
        self.reload_subs()
        self.app.on_category_changed()

    def toggle_sub(self) -> None:
        selection = self.sub_tree.selection()
        if not selection:
            return
        sub = self.catalog.sub_category(int(selection[0]))
        if sub is None:
            return
        self.catalog.set_sub_enabled(sub.id, not sub.enabled)
        self.reload_subs()
        self.app.on_category_changed()

    def connect(self, platform: str) -> None:
        """この画面から接続する。category_id はここで確定している。"""
        category_id = self.category_id
        if category_id is None:
            messagebox.showinfo("接続", "先にCategoryを選んでください")
            return
        category = self.catalog.category(category_id)
        self.app.log(f"{platform} の接続を始めます（Category: {category.name}）")

        def work() -> None:
            try:
                account = connect_platform(
                    self.app.settings, self.catalog, category_id, platform,
                    manual=False, log=lambda m: self.app.messages.put(("log", m)),
                )
            except Exception as exc:                       # 認証の失敗は画面へ返す
                self.app.messages.put(("error", f"{platform} の接続に失敗しました: {exc}"))
                return
            self.app.messages.put((
                "log", f"{platform} を「{category.name}」へ接続しました: {account.label()}"))
            self.app.messages.put(("accounts", category_id))

        threading.Thread(target=work, daemon=True).start()


class GenerateTab(ttk.Frame):
    """Category / SubCategory を選んで作り、接続済み媒体へ回す画面。"""

    AUTO_LABEL = "おまかせ（成績に応じて自動で選ぶ）"

    def __init__(self, parent, app) -> None:
        super().__init__(parent)
        self.app = app
        self.catalog: Catalog = app.catalog
        self.categories = Choice()
        self.subs = Choice()
        self._build()
        self.reload()

    def _build(self) -> None:
        form = ttk.LabelFrame(self, text="作るもの")
        form.pack(fill=X, **PAD)

        ttk.Label(form, text="Category").grid(row=0, column=0, sticky=W, padx=8, pady=6)
        self.category_var = StringVar()
        self.category_box = ttk.Combobox(form, textvariable=self.category_var,
                                         state="readonly", width=24)
        self.category_box.grid(row=0, column=1, sticky=W)
        self.category_box.bind("<<ComboboxSelected>>", lambda e: self.reload_subs())

        ttk.Label(form, text="SubCategory").grid(row=1, column=0, sticky=W, padx=8, pady=6)
        self.sub_var = StringVar()
        self.sub_box = ttk.Combobox(form, textvariable=self.sub_var,
                                    state="readonly", width=24)
        self.sub_box.grid(row=1, column=1, sticky=W)

        ttk.Label(form, text="生成数").grid(row=2, column=0, sticky=W, padx=8, pady=6)
        self.count_var = StringVar(value="10")
        ttk.Spinbox(form, from_=1, to=200, textvariable=self.count_var,
                    width=6).grid(row=2, column=1, sticky=W)

        ttk.Label(form, text="投稿先").grid(row=3, column=0, sticky=W, padx=8, pady=6)
        self.targets_var = StringVar(value="—")
        ttk.Label(form, textvariable=self.targets_var, width=40).grid(
            row=3, column=1, sticky=W)

        buttons = ttk.Frame(self)
        buttons.pack(fill=X, **PAD)
        self.generate_button = ttk.Button(buttons, text="生成", command=self.generate)
        self.generate_button.pack(side=LEFT, padx=4)
        self.publish_button = ttk.Button(buttons, text="接続済み媒体へ予約",
                                        command=self.enqueue)
        self.publish_button.pack(side=LEFT, padx=4)
        ttk.Button(buttons, text="投稿先を再確認",
                   command=self.reload_targets).pack(side=LEFT, padx=4)

        ttk.Label(
            self,
            text="※ 名前やIDを打ち込む場面はありません。選ぶだけで、生成物と投稿先が決まります。",
            foreground="#555",
        ).pack(anchor=W, padx=12, pady=(0, 8))

    # ------------------------------------------------------------------
    @property
    def category_id(self) -> int | None:
        return self.categories.id_of(self.category_var.get())

    @property
    def sub_category_id(self) -> int | None:
        if self.sub_var.get() == self.AUTO_LABEL:
            return None
        return self.subs.id_of(self.sub_var.get())

    def reload(self) -> None:
        labels = self.categories.set([(c.id, c.name) for c in self.catalog.categories()])
        self.category_box.configure(values=labels)
        if labels and self.category_var.get() not in labels:
            self.category_var.set(labels[0])
        self.reload_subs()

    def reload_subs(self) -> None:
        category_id = self.category_id
        if category_id is None:
            self.sub_box.configure(values=[self.AUTO_LABEL])
            self.sub_var.set(self.AUTO_LABEL)
            self.targets_var.set("—")
            return
        # そのCategoryのSubCategoryだけを出す
        labels = self.subs.set([(s.id, s.name)
                                for s in self.catalog.sub_categories(category_id)])
        values = [self.AUTO_LABEL] + labels
        self.sub_box.configure(values=values)
        if self.sub_var.get() not in values:
            self.sub_var.set(self.AUTO_LABEL)
        self.reload_targets()

    def reload_targets(self) -> None:
        category_id = self.category_id
        if category_id is None:
            self.targets_var.set("—")
            return
        accounts = sync_accounts(self.app.settings, self.catalog, category_id, list(CONNECTABLE))
        connected = [a for a in accounts if a.connected]
        if connected:
            self.targets_var.set(" / ".join(
                f"{a.platform} {a.account_name}".strip() for a in connected))
        else:
            self.targets_var.set("未接続（Category画面で接続してください）")

    # ------------------------------------------------------------------
    def generate(self) -> None:
        category_id = self.category_id
        if category_id is None:
            messagebox.showinfo("生成", "先にCategoryを作ってください")
            return
        try:
            count = max(1, int(self.count_var.get()))
        except ValueError:
            messagebox.showerror("生成", "生成数は数字で入れてください")
            return

        sub_id = self.sub_category_id
        label = self.sub_var.get()
        self.generate_button.configure(state="disabled")
        self.app.log(f"{count}件を生成します（{self.category_var.get()} / {label}）")

        def work() -> None:
            try:
                result = self.app.run_generate(category_id, sub_id, count)
            except Exception as exc:
                self.app.messages.put(("error", f"生成に失敗しました: {exc}"))
            else:
                self.app.messages.put(("log", result))
            finally:
                self.app.messages.put(("generate_done", ""))

        threading.Thread(target=work, daemon=True).start()

    def enqueue(self) -> None:
        """接続済み媒体を自動で決めて予約する。アカウント名は入力させない。"""
        category_id = self.category_id
        if category_id is None:
            return
        platforms = self.catalog.connected_platforms(category_id)
        if not platforms:
            messagebox.showinfo("予約", "このCategoryに接続済みの媒体がありません")
            return
        self.publish_button.configure(state="disabled")
        self.app.log(f"予約します: {' / '.join(platforms)}")

        def work() -> None:
            try:
                summary = self.app.run_enqueue(category_id, platforms)
            except Exception as exc:
                self.app.messages.put(("error", f"予約に失敗しました: {exc}"))
            else:
                self.app.messages.put(("log", summary))
                self.app.messages.put(("queue", ""))
            finally:
                self.app.messages.put(("enqueue_done", ""))

        threading.Thread(target=work, daemon=True).start()


class AnalyticsTab(ttk.Frame):
    """成績と、生成割合が変わった理由を見る画面。"""

    def __init__(self, parent, app) -> None:
        super().__init__(parent)
        self.app = app
        self.catalog: Catalog = app.catalog
        self.categories = Choice()
        self.auto_var = BooleanVar(value=True)
        self._build()
        self.reload()

    def _build(self) -> None:
        head = ttk.Frame(self)
        head.pack(fill=X, **PAD)
        ttk.Label(head, text="Category").pack(side=LEFT, padx=(8, 4))
        self.category_var = StringVar()
        self.category_box = ttk.Combobox(head, textvariable=self.category_var,
                                         state="readonly", width=22)
        self.category_box.pack(side=LEFT)
        self.category_box.bind("<<ComboboxSelected>>", lambda e: self.reload_rows())
        ttk.Button(head, text="更新", command=self.reload_rows).pack(side=LEFT, padx=6)
        ttk.Checkbutton(head, text="自動最適化", variable=self.auto_var,
                        command=self.toggle_auto).pack(side=LEFT, padx=12)
        ttk.Button(head, text="いま集計して割合を更新",
                   command=self.update_weights).pack(side=LEFT, padx=4)

        self.summary_var = StringVar()
        ttk.Label(self, textvariable=self.summary_var, justify=LEFT,
                  foreground="#333").pack(anchor=W, padx=12)

        table = ttk.LabelFrame(self, text="SubCategory別")
        table.pack(fill=BOTH, expand=True, **PAD)
        columns = ("name", "weight", "delta", "median", "samples", "state")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", height=11)
        for name, title, width, anchor in (
            ("name", "SubCategory", 170, W),
            ("weight", "生成割合", 80, "e"),
            ("delta", "前回差", 70, "e"),
            ("median", "中央値", 90, "e"),
            ("samples", "件数", 60, "e"),
            ("state", "状態", 220, W),
        ):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, anchor=anchor)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=LEFT, fill=BOTH, expand=True, padx=(6, 0), pady=6)
        scroll.pack(side=RIGHT, fill=Y, pady=6)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_reason())

        self.reason_var = StringVar(value="行を選ぶと、割合が変わった理由が出ます")
        ttk.Label(self, textvariable=self.reason_var, wraplength=680,
                  justify=LEFT).pack(anchor=W, padx=12, pady=(0, 4))

        hook_frame = ttk.LabelFrame(self, text="1枚目フックの比較（A/B/Cテスト）")
        hook_frame.pack(fill=X, **PAD)
        hook_columns = ("variant", "posts", "views", "save", "share", "comment")
        self.hook_tree = ttk.Treeview(hook_frame, columns=hook_columns,
                                      show="headings", height=4)
        for name, title, width, anchor in (
            ("variant", "フック", 170, W),
            ("posts", "投稿", 60, "e"),
            ("views", "表示（中央値）", 110, "e"),
            ("save", "保存率", 80, "e"),
            ("share", "共有率", 80, "e"),
            ("comment", "コメント率", 90, "e"),
        ):
            self.hook_tree.heading(name, text=title)
            self.hook_tree.column(name, width=width, anchor=anchor)
        self.hook_tree.pack(fill=X, padx=6, pady=6)
        self.hook_note = StringVar()
        ttk.Label(hook_frame, textvariable=self.hook_note, wraplength=680,
                  justify=LEFT, foreground="#555").pack(anchor=W, padx=8, pady=(0, 6))

        controls = ttk.Frame(self)
        controls.pack(fill=X, padx=8, pady=(0, 6))
        ttk.Button(controls, text="選んだ行を固定／解除",
                   command=self.toggle_lock).pack(side=LEFT, padx=4)
        ttk.Button(controls, text="初期値（均等）へ戻す",
                   command=self.reset_weights).pack(side=LEFT, padx=4)
        ttk.Button(controls, text="詳しい成績を開く",
                   command=self.show_full).pack(side=LEFT, padx=4)

    # ------------------------------------------------------------------
    @property
    def category_id(self) -> int | None:
        return self.categories.id_of(self.category_var.get())

    def reload(self) -> None:
        labels = self.categories.set([(c.id, c.name) for c in self.catalog.categories()])
        self.category_box.configure(values=labels)
        if labels and self.category_var.get() not in labels:
            self.category_var.set(labels[0])
        self.reload_rows()

    def reload_rows(self) -> None:
        from . import report as report_module

        data = report_module.overview(self.app.settings, self.app.experiments,
                                      category_id=self.category_id)
        self.auto_var.set(data.auto)
        outcomes = "／".join(
            f"{report_module.OUTCOME_LABELS.get(k, k)} {v}件"
            for k, v in data.outcomes.items()
        ) or "まだ取得していません"
        stamp = (data.last_collected_at or "")[:16].replace("T", " ") or "なし"
        evaluated = (data.last_evaluated_at or "")[:16].replace("T", " ") or "なし"
        summary = (
            f"配信できた投稿 {data.total_posts}件"
            + ("（" + " / ".join(f"{k} {v}件" for k, v in sorted(data.by_platform.items())) + "）"
               if data.by_platform else "")
            + f"\n反応データ: {outcomes}　最終取得 {stamp}　最終更新 {evaluated}"
            + f"\n評価: {data.snapshot}時点の {data.metric} / 直近{data.window}件の中央値"
              f"（媒体内の相対値で比較）"
            + f"\n実効の下限・上限: {data.bounds_low:.2f}% 〜 {data.bounds_high:.1f}%"
            + (f"\n  ※ {data.bounds_reason}" if data.bounds_reason else "")
        )
        self.summary_var.set(summary)

        self.tree.delete(*self.tree.get_children())
        self._reasons = {}
        for row in data.rows:
            marks = []
            if not row.enabled:
                marks.append("抽選から除外")
            if row.locked:
                marks.append("固定中")
            if row.shortage and row.measured:
                marks.append(f"サンプル不足 {row.measured}/{data.min_samples}")
            elif not row.measured:
                marks.append("未測定")
            if row.late_skipped:
                marks.append(f"遅延{row.late_skipped}件除外")
            self._reasons[str(row.sub_category_id)] = row.reason or "まだ変更されていません"
            self.tree.insert("", END, iid=str(row.sub_category_id), values=(
                row.name,
                f"{row.weight:.1f}%",
                f"{row.delta:+.1f}" if row.delta is not None else "—",
                f"{row.raw_median:,.0f}" if row.raw_median is not None else "—",
                row.measured,
                " / ".join(marks),
            ))
        self.reload_hooks()

    def reload_hooks(self) -> None:
        from . import report as report_module

        rows, note = report_module.hook_comparison(self.app.settings, self.app.experiments)
        self.hook_tree.delete(*self.hook_tree.get_children())
        for row in rows:
            def pct(value):
                return f"{value * 100:.2f}%" if value is not None else "—"
            self.hook_tree.insert("", END, values=(
                f"{row.variant} {row.label}",
                row.posts,
                f"{row.views:,.0f}" if row.views is not None else "—",
                pct(row.save_rate), pct(row.share_rate), pct(row.comment_rate),
            ))
        if not rows:
            self.hook_note.set("まだフック付きの投稿がありません。")
        elif not note["ready"]:
            self.hook_note.set(
                f"【まだ判断しないでください】各フック{note['min_posts']}件以上そろってから比べます。"
                + ("　不足: " + " / ".join(note["missing"]) if note["missing"] else ""))
        else:
            self.hook_note.set(
                "総再生数だけで決めないでください。保存率・共有率は投稿の規模に"
                "左右されにくく、1枚目の良し悪しが出やすい指標です。")

    def show_reason(self) -> None:
        selection = self.tree.selection()
        if selection:
            self.reason_var.set(self._reasons.get(selection[0], ""))

    # ------------------------------------------------------------------
    def toggle_auto(self) -> None:
        from .weights import set_auto_enabled

        set_auto_enabled(self.app.experiments, self.auto_var.get())
        state = "有効" if self.auto_var.get() else "無効（設定した割合で抽選します）"
        self.app.log(f"自動最適化を{state}にしました")

    def toggle_lock(self) -> None:
        from .weights import SubWeightStore

        selection = self.tree.selection()
        if not selection:
            return
        sub_id = int(selection[0])
        weights = SubWeightStore(self.app.experiments)
        locked = sub_id in weights.locked()
        weights.set_locked(sub_id, not locked)
        self.app.log(f"{'固定を解除' if locked else '固定'}しました")
        self.reload_rows()

    def reset_weights(self) -> None:
        from .weights import SubWeightStore

        category_id = self.category_id
        if category_id is None:
            return
        if not messagebox.askyesno("初期値へ戻す", "生成割合を均等に戻します。よろしいですか？"):
            return
        subs = [s.id for s in self.catalog.sub_categories(category_id)]
        SubWeightStore(self.app.experiments).reset(subs)
        self.app.log("生成割合を初期値（均等）へ戻しました")
        self.reload_rows()

    def update_weights(self) -> None:
        from .weights import apply_update, SubWeightStore

        category_id = self.category_id
        if category_id is None:
            return
        subs = {s.id: s.name for s in self.catalog.sub_categories(category_id)}
        changes = apply_update(self.app.settings, self.app.experiments,
                               SubWeightStore(self.app.experiments), subs,
                               log=self.app.log)
        self.app.log(f"{len([c for c in changes if abs(c.delta) >= 0.01])}件の割合が変わりました")
        self.reload_rows()

    def show_full(self) -> None:
        from . import report as report_module

        text = report_module.build(self.app.settings, category_id=self.category_id)
        window = tk.Toplevel(self)
        window.title("成績の詳細")
        window.geometry("760x580")
        box = tk.Text(window, wrap="none", font=("Consolas", 10))
        scroll_y = ttk.Scrollbar(window, orient="vertical", command=box.yview)
        box.configure(yscrollcommand=scroll_y.set)
        scroll_y.pack(side=RIGHT, fill=Y)
        box.pack(side=LEFT, fill=BOTH, expand=True)
        box.insert("1.0", text)
        box.configure(state="disabled")
