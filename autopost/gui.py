"""簡易GUI（Windows / macOS / Linux 共通、tkinter 標準ライブラリのみ）。

デザインより機能優先。以下ができれば十分:
  投稿フォルダ選択 / 接続状況の確認 / 一括予約 / 検証 / 予約一覧 / ログ
"""

from __future__ import annotations

import queue as queue_module
import threading
from datetime import datetime, time as dtime
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, W, X, Y, BooleanVar, StringVar, Tk, filedialog, messagebox, ttk
import tkinter as tk

from .config import Settings
from .db import Queue, STATUS_POSTED
from .hosting import get_host
from .loader import LoaderError, load_posts
from .models import PLATFORMS
from .oauth.store import TokenStore
from .scheduler import LOCK_NAME, Runner, SingleInstance, bulk_schedule
from .version import current_version
from .validate import ERROR, summarize, validate_post

POLL_MS = 200


class AutoPostApp:
    """メインウィンドウ。"""

    def __init__(self, root: Tk) -> None:
        self.root = root
        self.settings = Settings.load()
        self.queue = Queue(self.settings.db_path)
        self.store = TokenStore(self.settings.token_dir)
        self.messages: queue_module.Queue[tuple[str, object]] = queue_module.Queue()
        self.watching = False

        root.title(f"本音心理テスト 自動投稿  v{current_version()}")
        root.geometry("980x720")

        self.folder_var = StringVar(value=str(Path("output").resolve()))
        self.start_var = StringVar(value=datetime.now().strftime("%Y-%m-%d"))
        self.time_var = StringVar(value="21:00")
        self.interval_var = StringVar(value="1")
        self.count_var = StringVar(value="100")
        self.platform_vars = {p: BooleanVar(value=True) for p in PLATFORMS}
        self.status_vars = {p: StringVar(value="確認中") for p in PLATFORMS}
        self.host_var = StringVar(value="")
        self.update_var = StringVar(value=f"v{current_version()}")
        self.pending_update = None

        self._build()
        self.refresh_status()
        self.refresh_queue()
        self.root.after(POLL_MS, self._drain)

    # ------------------------------------------------------------------
    # 画面構築
    # ------------------------------------------------------------------
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}

        version_frame = ttk.LabelFrame(self.root, text="ソフトのバージョン")
        version_frame.pack(fill=X, **pad)
        ttk.Label(version_frame, textvariable=self.update_var).pack(
            side=LEFT, padx=8, pady=8)
        self.update_button = ttk.Button(
            version_frame, text="更新を確認", command=self.check_update)
        self.update_button.pack(side=RIGHT, padx=8, pady=8)

        top = ttk.LabelFrame(self.root, text="投稿フォルダ")
        top.pack(fill=X, **pad)
        ttk.Entry(top, textvariable=self.folder_var).pack(side=LEFT, fill=X, expand=True, padx=8, pady=8)
        ttk.Button(top, text="参照", command=self.choose_folder).pack(side=LEFT, padx=8)

        conn = ttk.LabelFrame(self.root, text="接続状況")
        conn.pack(fill=X, **pad)
        for row, platform in enumerate(PLATFORMS):
            ttk.Label(conn, text=platform, width=12).grid(row=row, column=0, sticky=W, padx=8, pady=3)
            ttk.Label(conn, textvariable=self.status_vars[platform]).grid(row=row, column=1, sticky=W)
            ttk.Button(
                conn, text="接続", command=lambda p=platform: self.connect(p)
            ).grid(row=row, column=2, padx=6)
        ttk.Label(conn, text="画像ホスティング", width=14).grid(row=2, column=0, sticky=W, padx=8, pady=3)
        ttk.Label(conn, textvariable=self.host_var).grid(row=2, column=1, columnspan=2, sticky=W)
        ttk.Button(conn, text="再確認", command=self.refresh_status).grid(row=2, column=3, padx=6)

        setting = ttk.LabelFrame(self.root, text="予約設定")
        setting.pack(fill=X, **pad)
        fields = [
            ("開始日 (YYYY-MM-DD)", self.start_var, 14),
            ("投稿時刻 (HH:MM)", self.time_var, 8),
            ("投稿間隔 (日)", self.interval_var, 6),
            ("投稿数", self.count_var, 6),
        ]
        for col, (label, var, width) in enumerate(fields):
            ttk.Label(setting, text=label).grid(row=0, column=col * 2, sticky=W, padx=8, pady=6)
            ttk.Entry(setting, textvariable=var, width=width).grid(row=0, column=col * 2 + 1, sticky=W)

        for col, platform in enumerate(PLATFORMS):
            ttk.Checkbutton(
                setting, text=platform, variable=self.platform_vars[platform]
            ).grid(row=1, column=col * 2, columnspan=2, sticky=W, padx=8)

        buttons = ttk.Frame(self.root)
        buttons.pack(fill=X, **pad)
        ttk.Button(buttons, text="投稿を検証", command=self.validate).pack(side=LEFT, padx=4)
        ttk.Button(buttons, text="一括予約", command=self.schedule).pack(side=LEFT, padx=4)
        ttk.Button(buttons, text="予約一覧を更新", command=self.refresh_queue).pack(side=LEFT, padx=4)
        ttk.Button(buttons, text="今すぐ実行", command=self.run_once).pack(side=LEFT, padx=4)
        self.watch_button = ttk.Button(buttons, text="自動投稿を開始", command=self.toggle_watch)
        self.watch_button.pack(side=LEFT, padx=4)
        ttk.Button(buttons, text="成績を見る", command=self.show_report).pack(side=LEFT, padx=4)

        panes = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        panes.pack(fill=BOTH, expand=True, padx=8, pady=4)

        list_frame = ttk.LabelFrame(panes, text="予約一覧")
        columns = ("post", "schedule", "tiktok", "instagram")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", height=10)
        for name, title, width in (
            ("post", "投稿", 120),
            ("schedule", "予約時刻", 160),
            ("tiktok", "TikTok", 260),
            ("instagram", "Instagram", 260),
        ):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, anchor=W)
        scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side=LEFT, fill=BOTH, expand=True)
        scroll.pack(side=RIGHT, fill=Y)
        panes.add(list_frame, weight=3)

        log_frame = ttk.LabelFrame(panes, text="ログ")
        self.log_text = tk.Text(log_frame, height=10, wrap="word")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set, state="disabled")
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)
        log_scroll.pack(side=RIGHT, fill=Y)
        panes.add(log_frame, weight=2)

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def check_update(self) -> None:
        """「更新を確認」。通信するのでワーカースレッドで行う。"""
        from . import updater

        self.update_button.configure(state="disabled")
        self.update_var.set("確認しています…")

        def work() -> None:
            try:
                info = updater.check()
            except updater.UpdateError as exc:
                self.messages.put(("update_error", str(exc)))
                return
            self.messages.put(("update_checked", info))

        threading.Thread(target=work, daemon=True).start()

    def _offer_update(self, info) -> None:
        """確認結果をダイアログで見せ、了承されたら更新する。"""
        version = current_version()
        if not info.available:
            self.update_var.set(f"v{version}（最新バージョンです）")
            self.log(f"最新バージョンです（v{version}）")
            messagebox.showinfo("更新の確認", "最新バージョンです")
            return

        self.update_var.set(f"v{version} → v{info.latest} が利用できます")
        self.log(info.message)

        lines = [f"新しいバージョンがあります", "", f"  現在: v{info.current}",
                 f"  最新: v{info.latest}", ""]
        if info.notes:
            lines.append("変更内容:")
            lines += [f"  ・{note}" for note in info.notes]
            lines.append("")
        lines.append("更新しますか？")
        lines.append("（.env・データベース・実験データは変更されません）")

        if not messagebox.askyesno("更新の確認", "\n".join(lines)):
            self.log("更新を中止しました")
            return

        self.pending_update = info
        self.update_button.configure(state="disabled")
        self.update_var.set("更新しています…")

        def work() -> None:
            from . import migrations, updater

            try:
                backup = updater.apply_update(
                    info, log=lambda m: self.messages.put(("log", m)))
            except updater.UpdateError as exc:
                self.messages.put(("update_error", str(exc)))
                return
            try:
                migrations.migrate(
                    self.settings, log=lambda m: self.messages.put(("log", m)))
            except migrations.MigrationError as exc:
                self.messages.put((
                    "update_error",
                    f"DBの更新に失敗しました: {exc}\n"
                    f"戻すには: python autopost.py update --rollback {backup.name}",
                ))
                return
            self.messages.put(("update_done", info.latest))

        threading.Thread(target=work, daemon=True).start()

    def _finish_update(self, version: str) -> None:
        self.update_button.configure(state="normal")
        self.update_var.set(f"v{version} へ更新しました（再起動が必要）")
        self.log(f"v{version} へ更新しました")
        restart = messagebox.askyesno(
            "更新完了",
            f"v{version} へ更新しました。\n\n"
            "今すぐ再起動しますか？\n"
            "（開いているこの画面は古いコードのままです）",
        )
        if not restart:
            return
        from . import updater
        import subprocess

        try:
            subprocess.Popen(updater.restart_command(), cwd=str(updater.APP_ROOT))
        except OSError as exc:
            messagebox.showerror(
                "再起動できません",
                f"手動で起動し直してください。\n{exc}")
            return
        self.root.destroy()

    def choose_folder(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.folder_var.get() or ".")
        if selected:
            self.folder_var.set(selected)
            self.log(f"投稿フォルダ: {selected}")

    def refresh_status(self) -> None:
        for platform in PLATFORMS:
            missing = self.settings.missing(platform)
            if missing:
                self.status_vars[platform].set("未設定（.env: " + ", ".join(missing) + "）")
            else:
                self.status_vars[platform].set(self.store.status(platform))
        try:
            self.host_var.set(get_host(self.settings).describe())
        except Exception as exc:
            self.host_var.set(f"設定エラー: {exc}")

    def connect(self, platform: str) -> None:
        def work() -> None:
            try:
                if platform == "tiktok":
                    from .oauth import tiktok_oauth

                    token = tiktok_oauth.connect(self.settings, self.store)
                else:
                    from .oauth import meta_oauth

                    token = meta_oauth.connect(self.settings, self.store)
                self.messages.put(("log", f"{platform} に接続しました: {token.masked()}"))
            except Exception as exc:
                self.messages.put(("error", f"{platform} の接続に失敗: {exc}"))
            self.messages.put(("status", None))

        self.log(f"{platform} の認証をブラウザで開きます…")
        threading.Thread(target=work, daemon=True).start()

    def validate(self) -> None:
        platforms = self._selected_platforms()
        if not platforms:
            messagebox.showwarning("確認", "投稿先を1つ以上選んでください")
            return

        def work() -> None:
            try:
                bundles = load_posts(Path(self.folder_var.get()))
            except LoaderError as exc:
                self.messages.put(("error", str(exc)))
                return
            total_errors = 0
            for bundle in bundles:
                issues = validate_post(bundle, self.settings, platforms, queue=self.queue)
                errors, warns = summarize(issues)
                total_errors += errors
                mark = "OK" if errors == 0 else "NG"
                self.messages.put((
                    "log",
                    f"[{mark}] {bundle.post_id} 画像{bundle.image_count}枚 エラー{errors} 注意{warns}",
                ))
                for issue in issues:
                    if issue.level == ERROR or warns:
                        self.messages.put(("log", "      " + str(issue)))
            self.messages.put((
                "log", f"検証完了: {len(bundles)}投稿 / エラーのある投稿 {total_errors}件"
            ))

        self.log("検証を開始します…")
        threading.Thread(target=work, daemon=True).start()

    def schedule(self) -> None:
        platforms = self._selected_platforms()
        if not platforms:
            messagebox.showwarning("確認", "投稿先を1つ以上選んでください")
            return
        try:
            start = datetime.strptime(self.start_var.get(), "%Y-%m-%d")
            hour, minute = (int(x) for x in self.time_var.get().split(":"))
            interval = float(self.interval_var.get())
            count = int(self.count_var.get())
        except ValueError:
            messagebox.showerror("入力エラー", "開始日・時刻・間隔・投稿数を確認してください")
            return

        def work() -> None:
            try:
                results = bulk_schedule(
                    self.queue, self.settings, Path(self.folder_var.get()),
                    start, dtime(hour, minute), interval, count, platforms,
                )
            except LoaderError as exc:
                self.messages.put(("error", str(exc)))
                return
            created = sum(1 for _, _, a in results if a == "created")
            kept = sum(1 for _, _, a in results if a == "kept")
            self.messages.put(("log", f"予約しました: 新規 {created} / 変更なし {kept}"))
            self.messages.put(("queue", None))

        threading.Thread(target=work, daemon=True).start()

    def run_once(self) -> None:
        def work() -> None:
            try:
                with SingleInstance(self.settings.cache_dir / LOCK_NAME):
                    runner = Runner(self.settings, self.queue, log=lambda m: self.messages.put(("log", m)))
                    report = runner.run_due()
                self.messages.put(("log", report.summary()))
            except RuntimeError as exc:
                self.messages.put(("error", str(exc)))
            self.messages.put(("queue", None))

        self.log("予約時刻を過ぎた投稿を処理します…")
        threading.Thread(target=work, daemon=True).start()

    def toggle_watch(self) -> None:
        if self.watching:
            self.watching = False
            self.watch_button.config(text="自動投稿を開始")
            self.log("自動投稿を停止しました")
            return
        self.watching = True
        self.watch_button.config(text="自動投稿を停止")
        self.log("自動投稿を開始しました（1分ごとに確認）")

        def work() -> None:
            import time as time_module

            try:
                with SingleInstance(self.settings.cache_dir / LOCK_NAME):
                    runner = Runner(
                        self.settings, self.queue, log=lambda m: self.messages.put(("log", m))
                    )
                    while self.watching:
                        report = runner.run_due()
                        if report.executed or report.failed or report.rescheduled:
                            self.messages.put(("log", report.summary()))
                            self.messages.put(("queue", None))
                        for _ in range(60):
                            if not self.watching:
                                break
                            time_module.sleep(1)
            except RuntimeError as exc:
                self.messages.put(("error", str(exc)))
                self.watching = False

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------
    def show_report(self) -> None:
        """カテゴリ別の成績と、生成割合が変わった理由を別窓で表示する。"""
        try:
            from . import report as report_module

            text = report_module.build(self.settings)
        except Exception as exc:                       # DBが無い場合など
            self.log(f"成績を読めませんでした: {exc}")
            return

        window = tk.Toplevel(self.root)
        window.title("カテゴリ別の成績")
        window.geometry("720x560")
        box = tk.Text(window, wrap="none", font=("Consolas", 10))
        scroll_y = ttk.Scrollbar(window, orient="vertical", command=box.yview)
        scroll_x = ttk.Scrollbar(window, orient="horizontal", command=box.xview)
        box.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        scroll_y.pack(side=RIGHT, fill=Y)
        scroll_x.pack(side=tk.BOTTOM, fill=X)
        box.pack(side=LEFT, fill=BOTH, expand=True)
        box.insert("1.0", text)
        box.configure(state="disabled")

    def refresh_queue(self) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)
        grouped: dict[str, dict] = {}
        for job in self.queue.list_jobs():
            entry = grouped.setdefault(
                job.post_id, {"when": job.scheduled_at, "platforms": {}}
            )
            entry["platforms"][job.platform] = job
            entry["when"] = min(entry["when"], job.scheduled_at)
        for post_id, entry in grouped.items():
            cells = []
            for platform in PLATFORMS:
                job = entry["platforms"].get(platform)
                if job is None:
                    cells.append("-")
                elif job.status == STATUS_POSTED:
                    cells.append(f"posted  {job.platform_post_id or ''}")
                elif job.last_error:
                    cells.append(f"{job.status}  {job.last_error[:40]}")
                else:
                    cells.append(job.status)
            self.tree.insert(
                "", END,
                values=(post_id, entry["when"].strftime("%Y-%m-%d %H:%M"), cells[0], cells[1]),
            )

    def log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(END, f"[{datetime.now():%H:%M:%S}] {message}\n")
        self.log_text.see(END)
        self.log_text.configure(state="disabled")

    def _drain(self) -> None:
        """ワーカースレッドからのメッセージを画面へ反映する。"""
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue_module.Empty:
                break
            if kind == "log":
                self.log(str(payload))
            elif kind == "error":
                self.log(f"エラー: {payload}")
                messagebox.showerror("エラー", str(payload))
            elif kind == "status":
                self.refresh_status()
            elif kind == "queue":
                self.refresh_queue()
            elif kind == "update_checked":
                self._offer_update(payload)
            elif kind == "update_done":
                self._finish_update(str(payload))
            elif kind == "update_error":
                self.update_button.configure(state="normal")
                self.update_var.set(f"v{current_version()}")
                self.log(f"更新エラー: {payload}")
                messagebox.showerror("更新エラー", str(payload))
        self.root.after(POLL_MS, self._drain)

    def _selected_platforms(self) -> tuple[str, ...]:
        return tuple(p for p in PLATFORMS if self.platform_vars[p].get())


def main() -> int:
    root = Tk()
    AutoPostApp(root)
    root.mainloop()
    return 0
