# 自動投稿セットアップ手順

画像生成はこの手順なしで動きます。**投稿を実際に行う場合だけ**必要な準備です。

作業は3つ。すべて `.env` に値を入れるところまでがゴールです。

```
A. 画像の置き場所（公開HTTPS URL）を用意する
B. TikTokの開発者アプリを作る
C. Metaの開発者アプリを作り、Instagramを繋ぐ
```

準備が終わったら次で確認できます。

```bash
python autopost.py status
```

---

## A. 画像ホスティング（最初に必要）

**なぜ必要か**：TikTokの写真投稿APIは「公開HTTPS URLからの取得（PULL_FROM_URL）」しか
受け付けません。Instagramも公開URLが必要です。ローカルのPNGを直接送ることはできません。

このツールは画像を**1回だけアップロードし、TikTokとInstagramで同じURLを使い回します**。
1投稿あたり約850KB（10枚）なので、100投稿でも約85MBです。

### 選択肢1: Cloudflare R2（推奨・無料枠内に収まりやすい）

1. Cloudflareアカウントを作成 → R2 を有効化（**クレジットカード登録が必要。無料枠あり**）
2. バケットを作成（例: `honne-media`）
3. バケットに**カスタムドメイン**を接続（例: `media.example.com`）
   - TikTokのドメイン検証にドメインが必要なため、R2の `*.r2.dev` ではなく自分のドメインを使う
4. R2 API トークンを作成（Object Read & Write）
5. `.env` に記入

```
IMAGE_HOST=r2
R2_ACCOUNT_ID=（CloudflareダッシュボードのアカウントID）
R2_ACCESS_KEY_ID=
R2_SECRET_ACCESS_KEY=
R2_BUCKET=honne-media
R2_PUBLIC_BASE_URL=https://media.example.com
```

6. `pip install boto3`

### 選択肢2: すでに持っているWebサーバ

レンタルサーバやVPSの公開ディレクトリへコピーする方式です。追加費用は発生しません。

```
IMAGE_HOST=local
LOCAL_HOST_DIR=C:\path\to\public_html\honne
LOCAL_HOST_BASE_URL=https://media.example.com/honne
```

> どちらの場合も、URLは **HTTPS必須・リダイレクト禁止（TikTokは3xxを無効とみなす）** です。

---

## B. TikTok

1. https://developers.tiktok.com/ でアカウント作成 → **Manage apps** → アプリ作成
2. アプリに **Login Kit** と **Content Posting API** を追加
3. Content Posting API の設定で **Direct Post** を有効化
4. スコープ `user.info.basic` と `video.publish` を申請・有効化
5. **Redirect URI** に次を登録（このツールがローカルで認証を受け取ります）
   ```
   http://127.0.0.1:8720/callback/tiktok
   ```
6. **URLの所有権を検証**（重要）
   - 開発者ポータルの *URL properties* で、Aで用意したドメイン
     （例 `media.example.com`）またはURLプレフィックスを追加
   - 表示された署名文字列をDNSのTXTレコードに登録して検証
   - これをしないと投稿時に `url_ownership_unverified` で必ず失敗します
7. `.env` に記入

```
TIKTOK_CLIENT_KEY=
TIKTOK_CLIENT_SECRET=
TIKTOK_REDIRECT_URI=http://127.0.0.1:8720/callback/tiktok
```

8. 接続（ブラウザが開きます）

```bash
python autopost.py connect tiktok
```

### 審査について

審査前（unaudited）のアプリが投稿したコンテンツは **非公開扱い** になります。
そのため既定値は `TIKTOK_PRIVACY_LEVEL=SELF_ONLY` です。
自分のアカウントで動作確認したうえで、公開投稿したい場合は開発者ポータルから
**audit（審査）** を申請し、通過後に `.env` を `PUBLIC_TO_EVERYONE` へ変更してください。

---

## C. Instagram

**前提**：投稿先アカウントが **プロアカウント（ビジネス or クリエイター）** であること。
個人アカウントではAPI投稿できません（Instagramアプリの設定から切り替え可能）。

1. https://developers.facebook.com/ でアプリを作成
2. **Instagram** プロダクトを追加
3. ログイン方式を選ぶ（`.env` の `META_LOGIN_MODE`）
   - `instagram`（推奨）… Instagram Login。長期トークン(60日)を**自動更新**できる
   - `facebook` … Facebook Login for Business。Facebookページ連携が必要。60日ごとに再認証
4. **有効なOAuthリダイレクトURI** に登録
   ```
   http://127.0.0.1:8720/callback/meta
   ```
5. 権限（審査対象）
   - Instagram Login: `instagram_business_basic`, `instagram_business_content_publish`
   - Facebook Login: `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`
   - 自分のアカウントでテストする間は開発モードのままで動きます。
     他人のアカウントでも使う場合はアプリ審査が必要です。
6. **Instagram アカウントID** を取得して `.env` へ
   - Instagram Login の場合：接続後に `python autopost.py status` で確認できるユーザーID
   - Facebook Login の場合：`/me/accounts` → ページの `instagram_business_account.id`
7. `.env` に記入

```
META_APP_ID=
META_APP_SECRET=
META_REDIRECT_URI=http://127.0.0.1:8720/callback/meta
META_LOGIN_MODE=instagram
INSTAGRAM_ACCOUNT_ID=
```

8. 接続

```bash
python autopost.py connect instagram
```

---

## 確認

```bash
python autopost.py status          # 接続状況
python autopost.py validate --folder output   # 投稿前の機械チェック
```

すべて緑になったら、1投稿だけでテストしてください。

```bash
python autopost.py schedule --folder output --start 2026-09-20 --time 21:00 --count 1
python autopost.py run
```

---

## APIでできないこと（仕様上の制限）

| やりたいこと | 可否 | 対応 |
| --- | --- | --- |
| TikTokの写真カルーセル投稿 | ○ | 最大35枚。このツールは10枚を投稿 |
| TikTokの音楽自動付与 | ○ | `auto_add_music` を送信（`.env` で切替） |
| TikTok側での予約投稿 | × | ローカルのキューで予約を管理 |
| Instagramのカルーセル投稿 | ○ | 最大10枚（この構成とちょうど同じ） |
| Instagramの音楽設定 | × | APIに機能がない。`not_supported` として記録 |
| Instagram側での予約投稿 | × | ローカルのキューで予約を管理 |
| ローカルPNGの直接アップロード | × | 公開HTTPS URLが必須（Aのホスティング） |
| 個人アカウントへの投稿 | × | プロアカウントが必要 |
