"""TikTok / Instagram への予約投稿システム。

通常運用ではLLM APIを一切呼ばない。生成済みの画像・caption・meta.json を
機械的に読み取り、公式APIへ投稿するだけの決定論的な処理で完結する。
"""

__version__ = "1.0.0"
