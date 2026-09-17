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

## B0. Pinterest（最初の完全自動チャネル）

Pinterestは審査を通さなくても自分のアカウントへPinを作成でき、
公開HTTPS URLの画像をそのまま投稿できるため、最初の実験チャネルに向いています。

1. https://developers.pinterest.com/apps/ を開く（Pinterestアカウントでログイン）
2. **Create app** でアプリを作成
   - アプリ名・説明は自由（例: honeshinri-lab）
   - 用途を聞かれたら「自分のコンテンツを投稿する」旨を記載
3. アプリの画面で **App ID** と **App secret key** を控える
4. **Redirect URIs** に次を登録
   ```
   http://localhost:8730/callback/
   ```
   > ループバックURIが登録できない場合は、自分が所有するHTTPSのURL
   > （例: `https://honeshinri-media.pages.dev/`）を登録し、
   > あとで `--manual` を付けて認証します。
5. スコープに次を含める
   ```
   user_accounts:read, boards:read, boards:write, pins:read, pins:write
   ```
6. `.env` に記入

```
PINTEREST_APP_ID=（App ID）
PINTEREST_APP_SECRET=（App secret key）
PINTEREST_REDIRECT_URI=http://localhost:8730/callback/
```

7. 接続する

```bash
python autopost.py connect pinterest
# HTTPSのリダイレクトURIしか登録できなかった場合:
python autopost.py connect pinterest --manual
```

8. 投稿先ボードのIDを調べて `.env` に入れる

```bash
python autopost.py pinterest boards
```

```
PINTEREST_BOARD_ID=（表示されたID）
```

9. 公開済み画像1枚で疎通確認（実際にPinが作成されます）

```bash
python autopost.py pinterest test-pin \
  --image-url https://honeshinri-media.pages.dev/test/01.jpg \
  --title "彼氏の笑い方キモすぎるのに" \
  --text "最近これ聞かないと逆に落ち着かない" \
  --hypothesis "恋人の少しキモい行動に愛着を感じる話は共感される" \
  --category "恋愛/共感"
```

成功すると実験IDとPin IDが表示され、`experiments.db` に記録されます。

```bash
python autopost.py experiment show EXP-20260917-0001   # 経過ログと状態
python autopost.py experiment collect                  # 反応データを取得
```

### Pinterest APIでできること・できないこと

| 項目 | 可否 | 備考 |
| --- | --- | --- |
| 画像URLからのPin作成 | ○ | `media_source.source_type=image_url` |
| タイトル / 説明 / リンク | ○ | 100 / 800 / 2048文字 |
| 複数画像のカルーセル | △ | `multiple_image_urls` は2〜5枚（10枚は不可） |
| 予約投稿 | × | ローカルのキューで管理 |
| Pin単位の分析 | ○ | インプレッション・保存・クリック等 |
| サンドボックス | ○ | `PINTEREST_SANDBOX=true`（公開されない検証用） |

---

## B. TikTok

1. https://developers.tiktok.com/ でアカウント作成 → **Manage apps** → アプリ作成
2. アプリに **Login Kit** と **Content Posting API** を追加
   - Login Kit のプラットフォームは **Desktop** を選択
3. Content Posting API の設定で **Direct Post** を有効化
4. スコープを申請・有効化する
   - `user.info.basic` … アカウント表示
   - `video.publish` … Direct Post（APIから直接公開）
   - `video.upload` … **下書き転送**（TikTokアプリのインボックスへ送る。公開は本人が行う）
   > `video.upload` が無いと下書き転送のフォールバックが使えません。両方追加してください。
5. **Redirect URI** に次を**そのまま**登録（末尾のスラッシュまで一致させること）
   ```
   http://127.0.0.1:3455/callback/
   ```
   このツールはローカルHTTPサーバ（127.0.0.1:3455）を一時的に起動し、
   **OAuth 2.0 + PKCE（S256）** で認可コードを受け取ってトークンへ交換します。
   client_secret をブラウザへ渡さないため、デスクトップ環境でも安全です。
6. **URLの所有権を検証**（重要）
   - 開発者ポータルの *URL properties* で、Aで用意したドメイン
     （例 `media.example.com`）またはURLプレフィックスを追加
   - 表示された署名文字列をDNSのTXTレコードに登録して検証
   - これをしないと投稿時に `url_ownership_unverified` で必ず失敗します
7. `.env` に記入

```
TIKTOK_CLIENT_KEY=
TIKTOK_CLIENT_SECRET=
TIKTOK_REDIRECT_URI=http://127.0.0.1:3455/callback/
```

8. 接続（ブラウザが開きます）

```bash
python autopost.py connect tiktok
```

ブラウザでTikTokの認可画面が開き、許可すると
`http://127.0.0.1:3455/callback/` へ戻ってきて「認証に成功しました」と表示されます。
ポート3455が他のアプリで使用中の場合はエラーになるので、そのアプリを終了してください。

### 投稿方式のフォールバック

`.env` の `TIKTOK_MODE` で挙動を選べます。規約に反する方法（ブラウザ自動操作・
非公式API・Cookie利用）は実装していません。

| 値 | 挙動 |
| --- | --- |
| `direct_post`（既定） | Direct Postを試し、審査等で使えない場合は自動で下書き転送へ切り替え |
| `upload` | 最初からTikTokアプリの下書き（インボックス）へ転送。公開は手動 |
| `queue_only` | 送信せずQueueに保持。あとで手動投稿 |

**下書き転送（`upload`）でどうなるか**

1. APIが画像をTikTokへ送る（`post_mode=MEDIA_UPLOAD`）
2. TikTokアプリの**インボックスに通知**が届く
3. 通知をタップすると編集画面が開く（タイトル・説明はAPIで送った内容が入る）
4. 本人が確認して投稿する

つまり「APIで下書きまで作り、公開は人がワンタップ」という運用になります。
自動公開ではありませんが、審査が通っていなくても実運用でき、規約にも沿います。

### 審査について

審査前（unaudited）のアプリが投稿したコンテンツは **非公開扱い** になります。
そのため既定値は `TIKTOK_PRIVACY_LEVEL=SELF_ONLY` です。
自分のアカウントで動作確認したうえで、公開投稿したい場合は開発者ポータルから
**audit（審査）** を申請し、通過後に `.env` を `PUBLIC_TO_EVERYONE` へ変更してください。


---

## D. TikTok下書きを100件まとめて仕込む

**仕様上の制約（公式ドキュメントで確認）**

| 項目 | 実際 |
| --- | --- |
| 下書き転送（MEDIA_UPLOAD）で送れる項目 | **title と description のみ** |
| 音楽 | **APIでは付けられない**（`auto_add_music` は Direct Post 専用）。編集画面で選ぶ |
| ハッシュタグ | description に含めて送る（編集画面に入った状態で開く） |
| 1日の上限 | **保留中の共有は24時間あたり5件**（`spam_risk_too_many_pending_share`） |
| リクエスト制限 | アクセストークンあたり6リクエスト/分 |

つまり100件を一度には送れません。**1日5件 × 20日**で自動的に消化します。

### 手順

```bash
# 0) 最新のコードを取得
git pull

# 1) 100投稿を生成（画像1000枚 / 約2分）
python generate.py --posts 100 --seed 20260917

# 2) 実験として登録し、公開URLを紐付ける
python autopost.py tiktok enqueue --folder output \
       --base-url https://honeshinri-media.pages.dev

# 3) Cloudflare Pages へデプロイする形で画像を書き出す（約87MB）
python autopost.py tiktok export-media --dest pages_media
npx wrangler pages deploy pages_media --project-name honeshinri-media

# 4) .env を下書きモードにする
#    TIKTOK_MODE=upload
#    TIKTOK_DAILY_DRAFT_LIMIT=5

# 5) TikTokに接続（video.upload スコープが必要）
python autopost.py connect tiktok

# 6) 今日の分（5件）を下書きへ送る
python autopost.py tiktok drafts

# 残りと見込みの確認
python autopost.py tiktok queue
```

### 毎日自動で送る（Windows）

タスクスケジューラで次を1日1回実行するだけです。中断しても続きから再開します。

```
プログラム : C:\path\to\python.exe
引数       : autopost.py tiktok drafts
開始場所   : C:\path\to\honne-test
```

### 送られる内容

```
title       : 見出し（90文字まで）
description : 本文 + ハッシュタグ
画像        : 10枚（4:5 JPEGに変換済み・Cloudflare Pagesの公開URL）
音楽        : 未設定（TikTokの編集画面で選ぶ）
```

TikTokアプリのインボックスに通知が届き、タップすると文言が入った編集画面が開きます。
音楽を選んで投稿ボタンを押せば公開されます。

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
