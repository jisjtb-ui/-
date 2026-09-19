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

## B1. Threads / Instagram を一括でセットアップする（かんたん版）

`.env` に認証情報を入れたら、あとはボタン1つで最後まで進められます。

```
6_SNSセットアップ.bat をダブルクリック
```

ウィザードが次を順に実行します（途中で止めても、再実行すれば続きから進みます）。

```
[1/6] .env の確認
[2/6] Threads / Instagram へ接続（ブラウザで許可）
[3/6] コンテンツと画像を生成（件数を指定）
[4/6] 実験として登録 → Cloudflare Pages 用に書き出し
[5/6] 予約の割り当て（開始日・時刻・1日の件数）
[6/6] 毎日の自動配信を登録
```

| ボタン | 用途 |
| --- | --- |
| **6_SNSセットアップ.bat** | 上のウィザード（最初に1回） |
| **7_SNS予約を実行.bat** | 予約時刻を過ぎた分を今すぐ配信 |
| **8_SNS状況を確認.bat** | 待ち件数・次の予約・接続状況 |
| **5_反応データを集める.bat** | 1h/6h/24h/72h の反応データを取得 |

### 在庫が尽きたらどうなるか

毎日の自動実行には**自動補充**が含まれています。配信待ちが減ると、
新しいコンテンツを作って予約まで自動で行うため、実験ループは止まりません。

```
毎日のタスク:
  1. sns topup    … 配信待ちが少なければ生成して予約
  2. sns run      … 予約時刻を過ぎた分を配信
  3. collect --due … 1h/6h/24h/72h の反応データを取得
```

```
AUTO_TOPUP=true
AUTO_TOPUP_MIN=10      # 待ちがこれを下回ったら補充
AUTO_TOPUP_COUNT=30    # 1回に作る件数
PAGES_DEPLOY_COMMAND=  # 設定すると画像の公開まで自動化
```

> `PAGES_DEPLOY_COMMAND` が空だと、補充で作った画像は未公開のままです。
> 完全自動にするには次を設定してください。
> `npx wrangler pages deploy pages_media --project-name honeshinri-media`

配信ペースは `.env` で調整できます（既定は安全側）。

```
THREADS_DAILY_LIMIT=10     # API上限は250
INSTAGRAM_DAILY_LIMIT=5    # API上限は100
```

コマンドで操作する場合:

```bash
python autopost.py sns enqueue --folder output --base-url https://honeshinri-media.pages.dev
python autopost.py sns schedule --start 2026-09-20 --times 09:00,21:00
python autopost.py sns run        # 予約時刻を過ぎた分を配信
python autopost.py sns queue      # 予約状況
```

---

## B1-2. Threads（本命1・完全自動投稿）

TikTokと違い、**APIから直接公開できて、反応データも取得できます**。
実験ループの主軸になるチャネルです。

| 項目 | 仕様 |
| --- | --- |
| 投稿 | コンテナ作成 → 公開 の2段階 |
| 本文 | 500文字まで |
| 画像 | JPEG / PNG・8MB以下・幅320〜1440px |
| カルーセル | 2〜20枚 |
| 1日の上限 | **250投稿**（TikTokの5件とは桁が違う） |
| Insights | views / likes / replies / reposts / quotes |

### 手順

1. https://developers.facebook.com/apps/ を開く
2. **アプリを作成** → ユースケースで **Threads API** を選択
3. 「Threads」の設定画面で **App ID** と **App secret** を控える
4. **有効なOAuthリダイレクトURI** に次を登録（**HTTPS必須**。ループバック不可）
   ```
   https://honeshinri-media.pages.dev/
   ```
5. 権限（スコープ）に次を追加
   ```
   threads_basic, threads_content_publish, threads_manage_insights
   ```
6. **Threadsテスターを追加して、Threads側で承認する**（ここでつまずきやすい）

   a. 開発者ダッシュボード → **アプリの役割（App roles）** → **役割（Roles）**
      → **人を追加（Add People）** → **Threads Tester** を選び、対象アカウントを招待
   b. **Threads側で承認する**（この操作を忘れると投稿時に
      `error_code=1349245 The user has not accepted the invite` になります）
      ```
      Threadsアプリ（または https://www.threads.net/）
        → 設定
        → アカウント
        → ウェブサイトの許可（Website permissions）
        → 招待（Invites）
        → 該当アプリの招待を「承認」
      ```
   自分のアカウントへ投稿するだけならアプリ審査は不要です。
7. `.env` に記入

```
THREADS_APP_ID=（App ID）
THREADS_APP_SECRET=（App secret）
THREADS_REDIRECT_URI=https://honeshinri-media.pages.dev/
```

> `THREADS_USER_ID` は空のままで構いません。接続時に自動取得します。
> 接続後は `python autopost.py status` で確認できます
> （`@ユーザー名` ではなく `17841405793187218` のような数値です）。

8. 接続する（HTTPSのリダイレクトURIなので手動モードを使います）

```bash
python autopost.py connect threads --manual
```

表示されたURLをブラウザで開いて許可 → 戻ってきたURL全体をコピーして貼り付けます。

9. テスト投稿（既存の公開画像1枚で実際に公開されます）

```bash
python autopost.py experiment new \
  --hypothesis "恋人の少しキモい行動に愛着を感じる話は共感される" \
  --category "恋愛/共感" \
  --hook "彼氏の笑い方キモすぎるのに" \
  --text "最近これ聞かないと逆に落ち着かない。これ私だけ？" \
  --image-url https://honeshinri-media.pages.dev/（画像のパス） \
  --platforms threads

python autopost.py experiment run --platform threads
python autopost.py experiment show EXP-20260917-0001
```

10. 反応データを集める（投稿から1h / 6h / 24h / 72h の時点で自動判定）

```bash
python autopost.py experiment collect --due
```

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

### 手順（かんたん版・Windows）

フォルダを開くと、一番上に4つのボタン（.bat）が並んでいます。順番に押すだけです。

| ボタン | いつ押すか | 何が起きるか |
| --- | --- | --- |
| **1_セットアップ.bat** | 最初に1回 | 100投稿の生成 → 実験登録 → 画像の書き出し（約3分） |
| **2_下書きを送る.bat** | 今すぐ送りたいとき | その日の残り枠（最大5件）を下書きへ転送 |
| **3_毎日自動で送る.bat** | 最初に1回 | 毎日決まった時刻の自動送信を登録（以降は放置） |
| **4_状況を確認する.bat** | いつでも | 残り件数・本日の送信数・接続状況を表示 |
| **5_反応データを集める.bat** | いつでも | 1h/6h/24h/72h の反応データを取得 |

> 未接続のまま押しても、キューは消費されません（接続方法が表示されます）。

```
1. git pull
2. 「1_セットアップ.bat」をダブルクリック
3. 表示されたコマンドで画像をPagesへデプロイ
     npx wrangler pages deploy pages_media --project-name honeshinri-media
4. python autopost.py connect tiktok            ← 初回のみ（ブラウザで許可）
5. 「3_毎日自動で送る.bat」をダブルクリック
```

登録後は放置で構いません。1日5件ずつ下書きが入り、20日で100件に到達します。
今すぐ1回だけ送りたいときは `scripts\tiktok_drafts.bat` をダブルクリックします。

### 手順（コマンド版）

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

## C. Instagram（本命2・完全自動投稿）

**前提**：投稿先アカウントが **プロアカウント（ビジネス or クリエイター）** であること。
個人アカウントではAPI投稿できません（Instagramアプリの設定から切り替えられます）。

| 項目 | 仕様 |
| --- | --- |
| 投稿 | コンテナ作成 → 公開 の2段階 |
| 画像 | **JPEG のみ**・8MB以下・アスペクト比 4:5〜1.91:1・幅320〜1440px |
| カルーセル | 2〜10枚 |
| キャプション | 2200文字・ハッシュタグ30個まで |
| 1日の上限 | 100投稿 / コンテナ作成400件 |
| Insights | views / reach / likes / comments / saves / shares |

### 手順

1. https://developers.facebook.com/apps/ を開く
2. **アプリを作成** → ユースケースで **Instagram** を選択
3. 左メニューの **Instagram → API setup with Instagram login** を開く
4. **Instagram app ID** と **Instagram app secret** を控える
   > FacebookのApp IDとは別物です。必ずInstagram側の値を使ってください。
5. 「ビジネスログインの設定」で **リダイレクトURI** に次を登録（**HTTPS必須**）
   ```
   https://honeshinri-media.pages.dev/
   ```
6. 権限（スコープ）に次を追加
   ```
   instagram_business_basic
   instagram_business_content_publish
   instagram_business_manage_insights
   ```
   > `instagram_business_manage_insights` が無いと反応データを取得できません。
7. **Instagramテスターを追加して承認する**（Threadsと同じ手順です）

   a. 開発者ダッシュボード → **アプリの役割** → **役割** → **人を追加**
      → **Instagramテスター** で対象アカウントを招待
   b. Instagramアプリ側で承認
      ```
      Instagramアプリ → 設定 → アカウントセンター
        → ウェブサイトの許可（Website permissions）
        → テスター招待 → 承認
      ```
8. `.env` に記入

```
META_APP_ID=（Instagram app ID）
META_APP_SECRET=（Instagram app secret）
META_REDIRECT_URI=https://honeshinri-media.pages.dev/
META_LOGIN_MODE=instagram
```

> `INSTAGRAM_ACCOUNT_ID` は空のままで構いません。接続時に自動取得します。

9. 接続する（HTTPSのリダイレクトURIなので手動モードを使います）

```bash
python autopost.py connect instagram --manual
```

10. テスト投稿（同じ experiment_id で Threads と並べて比較できます）

```bash
python autopost.py experiment new \
  --hypothesis "恋人の少しキモい行動に愛着を感じる話は共感される" \
  --category "恋愛/共感" \
  --hook "彼氏の笑い方キモすぎるのに" \
  --text "最近これ聞かないと逆に落ち着かない。これ私だけ？" \
  --image-url https://honeshinri-media.pages.dev/（画像のパス） \
  --platforms threads,instagram

python autopost.py experiment run
python autopost.py experiment collect --due
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

---

## 画像を公開する（Cloudflare Pages）

`12_画像を公開する.bat` をダブルクリックするだけです。コマンドを打つ必要はありません。

```
画像の書き出し → ページの作成 → Cloudflareへ公開 → 公開できたか確認
```

初回だけ Cloudflare のログイン画面がブラウザで開きます。
プロジェクト名も初回だけ聞かれます（Cloudflareの画面 → Workers & Pages に出ている名前）。

> **Node.js が必要です。** 入っていない場合はその旨を表示します。
> https://nodejs.org/ から LTS版を入れて、PCを再起動してください。

`.env` に次を入れておくと、次回から自動で公開されます。

```
PAGES_DEPLOY_COMMAND=npx wrangler pages deploy pages_media --project-name （プロジェクト名）
```

**画像が公開されていないと、Threads も Instagram も投稿できません。**
APIが公開URLから画像を取りに行くためです。

## スマホから投稿する

PCで作ったものを、ケーブルでつながずにスマホから投稿できます。
画像はすでにCloudflare Pagesへ公開されているので、そこに一覧ページを1枚置くだけです。

```
11_スマホ用ページを作る.bat
  ↓ 表示されたURLをスマホで開く
キャプションをコピー → 画像10枚をまとめて保存 → アプリで投稿
```

Reelの動画がある投稿では「Reel動画を保存」も出ます。保存してから
Instagramアプリでリールとして開き、音源を付けて投稿してください。

### 一括保存について

「画像10枚をまとめて保存」は、端末の共有シートを開きます。
そこで**「画像を保存」**を選ぶと10枚まとめて写真に入ります。

対応していない端末では、その旨を表示します。その場合は一覧の画像を
**長押しして1枚ずつ保存**してください（ページ内にも同じ案内を出しています）。

### URLの扱い

このページは**推測されにくい名前のフォルダ**に置き、検索避け（noindex と robots.txt）も
入れていますが、**URLを知られると誰でも見られます**。予定している投稿が全部見えるので、
人に教えないでください。

フォルダ名は `.mobile_slug` に保存され、毎回同じURLになります（Gitには入りません）。
変えたいときはこのファイルを削除して作り直してください。

毎日の自動実行にも組み込んであるので、放っておいても内容は最新になります。

## スマホからワンタップで投稿する（任意）

PCを起動していなくても、スマホの一覧ページから Threads と Instagram へ
投稿できるようにできます。**設定しなくても、PCの自動投稿はこれまでどおり動きます。**

### できること・できないこと

| | ワンタップ投稿 |
| --- | --- |
| Threads | できる |
| Instagram（カルーセル） | できる |
| Instagram Reel | **できない**（音源をご自分で付けるため手動のまま） |
| TikTok | **できない**（下書きまで。APIに下書きを公開する機能がない） |

### なぜWorkerを挟むのか

ページは公開URLなので、**投稿トークンをページやスマホに置くと危険**です。
トークンはCloudflareのSecretとして保管し、スマホからは**合言葉だけ**を送ります。

```
スマホ ──合言葉──> Worker ──トークン──> Threads / Instagram
```

### 設定手順（1回だけ）

`worker/wrangler.jsonc` の `ALLOWED_IMAGE_PREFIX` と `ALLOWED_ORIGIN` を
ご自分のPagesのURLに書き換えてから、`worker` フォルダで次を実行します。

```
cd worker

npx wrangler kv namespace create PUBLISHED
  → 表示された id を wrangler.jsonc の kv_namespaces に貼る

npx wrangler secret put PUBLISH_PASSPHRASE     ← 好きな合言葉を決めて入力
npx wrangler secret put THREADS_ACCESS_TOKEN   ← .tokens/threads.json の access_token
npx wrangler secret put THREADS_USER_ID
npx wrangler secret put INSTAGRAM_ACCESS_TOKEN ← .tokens/instagram.json の access_token
npx wrangler secret put INSTAGRAM_ACCOUNT_ID

npx wrangler deploy
```

最後に表示されたURLと合言葉を、PCの `.env` に書きます。

```
PUBLISH_WORKER_URL=https://honeshinri-publish.<あなた>.workers.dev
PUBLISH_PASSPHRASE=決めた合言葉
```

`11_スマホ用ページを作る.bat` を押し直すと、カードに **「今すぐ投稿」** が出ます。
スマホで初回だけ合言葉を聞かれ、以降はその端末に保存されます。

### 二重投稿について

スマホから投稿したものは Worker に記録され、PCが取りに来て
「投稿済み」にします。そのためPCの予約投稿が同じものをもう一度出すことはありません。
毎日の自動実行にも組み込んであります。

```
python autopost.py mobile --sync    # 手動で取り込む場合
```

トークンを入れ直したとき（60日ごと）は、Workerのsecretも更新してください。

## Instagram Reel（縦動画）

同じ心理テストを、カルーセルとは別に**縦動画のReel**としても出せます。
既存の10枚をそのまま9:16でつなぎ、約32秒の動画にします。デザインは変わりません。

見る人の**認知と操作に余白**を取っています。スクロール直後は内容を読んでいないため
冒頭を長めにし、最終ページは4つのアクションを読んで実際に押すまで止まるよう、
はっきり長く表示します。

### なぜ自動投稿しないのか

APIから投稿したReelには、**Instagramの音楽ライブラリを使えません**。
動画ファイルに入っている音しか鳴らないため、自動投稿すると必ず無音になります。
Reelは音が効果を大きく左右するので、次の形にしています。

```
予約時刻になる → 動画を書き出す → 手渡し待ちになる
  ↓
自分でスマホから、音源を付けて投稿する
  ↓
「投稿した」と記録する → 以降は反応データを自動で集める
```

### 使い方

```
7_SNS予約を実行.bat      … 予約時刻が来ると自動で書き出される
10_Reelを書き出す.bat    … 書き出しと、投稿済みの記録
```

`10_Reelを書き出す.bat` を押すと、まだ作っていない分を作り、置き場所を教えます。
投稿し終えたら、同じボタンから実験IDを入力して記録してください。
**メディアIDも入れると、そのReelの反応データも自動で集まります。**

手で操作する場合:

```
python autopost.py sns enqueue --folder output --platforms instagram_reel
python autopost.py sns schedule --platforms instagram_reel --times 21:00
python autopost.py reel list
python autopost.py reel posted EXP-20260918-0002 --url <URL> --media-id <ID>
```

### 設定（.env）

```
REEL_SECONDS_QUESTION=3.0      # 問題を映す秒数
REEL_SECONDS_ANSWER=2.5        # 答えを映す秒数
REEL_LEAD_IN_SECONDS=1.2       # 冒頭：何の動画か認知するための余白
REEL_TAIL_SECONDS=6.0          # 末尾：CTAを読んで押すための余白
INSTAGRAM_REEL_DAILY_LIMIT=3   # 1日に書き出す上限
REEL_OUTPUT_DIR=reels_ready    # 置き場所
```

ffmpeg は `imageio-ffmpeg` に同梱されているものを使うため、**別途インストールは不要**です。
`1_セットアップ.bat` か `pip install -r requirements.txt` で入ります。

## ソフトを最新版にする

`0_更新を確認.bat` をダブルクリック、または画面右上の **「更新を確認」** を押します。

```
更新を確認
  ↓
最新なら         → 「最新バージョンです」
新しい版があれば → 変更内容が出る → 「はい」で更新 → 再起動
```

現在のバージョンは画面のタイトルと「ソフトのバージョン」欄に出ます。

### 更新で消えないもの

次のものは更新しても**一切変更されません**。

| 消えないもの | 中身 |
| --- | --- |
| `.env` | APIキー・シークレット |
| `.tokens/` | アクセストークン |
| `autopost.db` / `experiments.db` | 予約・実験・Analyticsのデータ |
| `output/` `ready/` `posted/` `pages_media/` | 生成した画像と投稿済みの記録 |
| `history.json` | 重複防止の履歴 |

更新の対象は、配布物の目録（`update_manifest.json`）に載っているファイルだけです。
この目録はGitで管理されているファイルから作られ、上のものはすべてGit管理外なので、
**仕組みとして対象になりません**。

### 途中で失敗したら

更新は次の順で進み、どこで失敗しても**今使っているアプリは壊れません**。

```
ダウンロード → 整合性確認(SHA256) → 起動テスト → バックアップ → 置き換え → DB更新
```

置き換えの前に問題が見つかった場合は、何も書き換えずに中止します。
置き換え中に失敗した場合は、自動でバックアップから元に戻します。

手動で1つ前に戻すこともできます。

```
python autopost.py update --rollback list     # 戻せる一覧
python autopost.py update --rollback v1.1.0_20260918_020420
```

バックアップは `backups/` に残ります。不要になったら削除して構いません。

### DBの形式が変わるとき

新機能で保存する項目が増えることがあります。更新の最後に自動で適用され、
その前に `backups/db_<日時>/` へDBをコピーします。既存のデータは消しません。

## うまく動かないとき

`9_不具合を調べる.bat` をダブルクリックしてください。次を自動で調べます。

- `.env` のどの項目が埋まっていて、どれが空か（**値そのものは出しません**）
- トークンの有効期限・残り日数・スコープ
- 各APIへ実際に繋いでみた結果（エラーコードとメッセージをそのまま転記）
- 画像の公開URLが本当に画像を返しているか
- 配信待ちの件数、本日の配信数、直近の失敗

結果は `診断結果.txt` にも保存されます。**このファイルには秘密情報が入りません**
（キー・シークレット・トークンは一切出力しない設計です）ので、そのまま貼り付けて
相談できます。

> **キーやトークンそのものを人に送らないでください。** 送ってしまった場合は、
> そのアプリの管理画面で必ず再発行してください。詳しくは末尾の
> 「秘密情報を漏らしてしまったら」を参照してください。

## リダイレクトURIの決め方

認証が終わったあとに戻ってくる場所です。**受け取り口のページを用意してあります。**

```
https://（あなたのドメイン）/callback/
```

これを `.env` と Meta の管理画面の**両方に、同じ文字列で**入れてください。
Threads と Instagram で同じ値を使って構いません。

```
THREADS_REDIRECT_URI=https://（あなたのドメイン）/callback/
META_REDIRECT_URI=https://（あなたのドメイン）/callback/
```

### 守ること

| | |
| --- | --- |
| **https://** で始める | Meta は `http://` を受け付けません |
| **末尾のスラッシュまで一致** | `/callback` と `/callback/` は別物です |
| **手で打ち直さない** | コピーして貼る。タイプミスが 1349168 の最多原因です |

`9_不具合を調べる.bat` に、アプリが実際に送っている値が出ます。管理画面の値と見比べてください。

### 受け取り口のページ

`/callback/` を開くと、こう表示します。

```
認証コードを受け取りました
この画面のURL全体を、アプリに貼り付けてください。

https://.../callback/?code=AQB...

［ URLをコピー ］
```

コピーして、コマンド画面の「リダイレクト先URL:」に貼れば接続完了です。
認証に失敗した場合は、その理由もこのページに出ます。

## 認証がどうしても通らないとき

Threads と Instagram は、**Meta App ID とは別のIDを使います**。ここを取り違えると、
認可画面が「不明なエラー（error_code=1）」などで止まります。

| | .env に入れる値 | 取得場所 |
| --- | --- | --- |
| Threads | `THREADS_APP_ID` = **Threads App ID** | App Dashboard → App settings → Basic → Threads App ID |
| Instagram | `INSTAGRAM_APP_ID` = **Instagram App ID** | App Dashboard → Instagram → API setup with Instagram login → 3. Set up Instagram business login → Business login settings |

シークレットも同じ画面にあるものを使ってください（Meta App Secret ではありません）。

`9_不具合を調べる.bat` に、実際に使っている値が出ます。

```
[instagram]
  client_id     : 1234567890（Instagram App ID）
  リダイレクトURI: https://.../
```

ここがMetaの管理画面の値と一致していない限り、何度試しても通りません。

### スコープについて

Instagram で使えるスコープは次の4つだけです。存在しない値を混ぜると認可画面がエラーになります。

```
instagram_business_basic
instagram_business_content_publish
instagram_business_manage_messages
instagram_business_manage_comments
```

反応データ専用のスコープはありません（basic の範囲で取得します）。

## よくあるエラー

| エラー | 原因と対処 |
| --- | --- |
| `1349245 The user has not accepted the invite` | Threads側で招待未承認。Threads → 設定 → アカウント → ウェブサイトの許可 → 招待 で承認 |
| `1349125` 画像URLにアクセスできない | Cloudflare Pages へのデプロイが未完了、またはURLの綴り違い |
| `1349138` 画像の要件エラー | JPEG/PNG・8MB以下・幅320〜1440px に収める |
| Instagram: テスター招待エラー | Instagram → 設定 → アカウントセンター → ウェブサイトの許可 → テスター招待 で承認 |
| Instagram: 画像が拒否される | **JPEGのみ**。アスペクト比4:5〜1.91:1（1080x1440は要件外。本ツールは1152x1440へ自動変換） |
| Instagram: Insightsが空 | `instagram_business_manage_insights` を追加して再認証 |
| TikTok `scope_not_authorized` | 開発者ポータルで `video.upload` を追加して再認証 |
| TikTok `spam_risk_too_many_pending_share` | 下書きの24時間あたり5件の上限。翌日自動で再試行される |
| TikTok `url_ownership_unverified` | 画像URLのドメインがTikTokで未検証 |
| Pinterest `PINTEREST_BOARD_ID` 未設定 | `python autopost.py pinterest boards` でIDを確認 |

---

## 秘密情報を漏らしてしまったら

client secret / app secret / アクセストークンを、チャットや画面共有、
スクリーンショットなどで外部に出してしまった場合は、**必ず再発行してください**。
漏れた秘密情報でアカウントを操作される可能性があります。

| プラットフォーム | 再発行する場所 |
| --- | --- |
| TikTok | developers.tiktok.com → Manage apps → 対象アプリ → Client secret の **Reset** |
| Meta（Threads / Instagram） | developers.facebook.com → アプリ → 設定 → ベーシック → app secret の **リセット** |
| Pinterest | developers.pinterest.com/apps → 対象アプリ → App secret の再生成 |

再発行したら `.env` の値だけ差し替えて、`connect` をやり直してください。
`.env` と `.tokens/` は `.gitignore` 済みなので、Gitには入りません。
