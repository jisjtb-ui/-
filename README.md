# 本音心理テスト｜TikTok / Instagram 投稿セット自動生成ツール

白背景に黒文字だけ。極端にミニマルな見た目なのに、質問だけ少し際どい——
**「恋人と答えを見せ合うと気まずい本音テスト」**の画像セットをローカルで量産するツールです。

- **1投稿 = 心理テスト5問 = 画像10枚**（問題5枚 + 答え5枚）
- ファイル名は `01_question.png` → `02_answer.png` … の連番なので、
  **TikTokで10枚まとめて選ぶだけで「問題→答え」の順番が崩れません**
- `caption.txt` をコピペするだけで投稿できます
- 生成履歴（`history.json`）で、似た問題の連発を防ぎます

```bash
python generate.py --posts 20      # 20投稿 = 画像200枚を一気に生成
```

---

## 1. コンテンツの方向性

**「恋人と答えを見せ合うと、ちょっと気まずい本音テスト」** です。
露骨な性的描写は扱わず、「大人なら意味が分かる」ラインに抑えつつ、
無難な質問（理想のデートは？ 連絡頻度は？ だけで終わる質問）には逃げません。

### 質問の刺激度（LEVEL）

ネタ1件ごとに `level` を持ち、**1投稿の中で後半ほど踏み込む**よう自動で並べます。

| LEVEL | 内容 | ネタ数 |
| --- | --- | --- |
| 1 | 答えやすい（好きになる理由・呼ばれ方・連絡テンポ） | 25 |
| 2 | 本音（元恋人・裏アカ・マチアプ・外見・お金・浮気の境界線） | 103 |
| 3 | ちょっと際どい（ホテル・泊まり・キス・そういう雰囲気・主導権・一人の時間） | 60 |

出題順は `Q1=L1 / Q2=L1〜2 / Q3=L2 / Q4=L2〜3 / Q5=L3`。
「次はもっと本音が出そう」と思わせ、最後までスワイプさせる構成です。

### カテゴリ（`--category` で指定）

| カテゴリ | 内容 | ネタ数 |
| --- | --- | --- |
| `alone` | 一人の時間 | 8 |
| `app` | マッチングアプリ | 8 |
| `before` | 付き合う前の境界線 | 8 |
| `breakup` | 別れ・復縁 | 8 |
| `cheating` | 浮気の境界線 | 8 |
| `closeness` | 恋人との距離感 | 8 |
| `desire` | 衝動と理性 | 10 |
| `dilemma` | 究極の選択 | 10 |
| `distance` | 距離感 | 10 |
| `ex` | 元恋人・過去 | 8 |
| `honne` | 言えていない本音 | 10 |
| `hotel` | ホテル・泊まり | 8 |
| `lead` | 主導権・S/M | 10 |
| `looks` | 外見の本音 | 8 |
| `marriage` | 恋愛と結婚 | 8 |
| `money` | お金・スペック | 8 |
| `mood` | そういう雰囲気 | 8 |
| `opener` | 導入（答えやすい） | 14 |
| `possessive` | 嫉妬・束縛 | 10 |
| `secret` | 裏アカ・SNS | 8 |
| `skinship` | スキンシップ | 10 |
| `random` | 上記を混ぜて出題（既定） | 188 |

1投稿の5問は**テーマが重複しない**ように選ばれ、直近の投稿で使ったテーマも避けます
（ホテルばかり・マチアプばかりになりません）。

画像上部の見出しは固定ではなく、「ちょっと大人の心理テスト」「人には言いにくい心理テスト」
「意見が割れる恋愛心理テスト」など `data/headers.json` のパターンから投稿ごとに選ばれます。

答えは断定せず、`〜しやすいタイプです` `〜傾向があります` のような表現に統一しています
（「浮気性です」「依存症です」のような診断・断定はしません）。
選択肢はA〜Dすべて実際に選ばれうる内容にし、「A=正解 / D=悪い人」構造は作りません。

## 1-2. CTA（保存・共有・コメント導線）

広告臭くならないよう、CTAは**最初と最後の2枚だけ**に入れます。

| 画像 | CTA |
| --- | --- |
| `01_question.png` | 上部に小さく「恋人とやってみて🌸」 |
| `02`〜`09` | CTAなし（テスト体験に集中させる） |
| `10_answer.png` | 回答の下に「保存してあとで二人でやってみて🌸」→「恋人にもやらせてみて」→「コメントで結果を教えて」 |

- 文言は `data/cta.json` で管理し、コードにはハードコードしていません
- セットを切り替えるだけでA/Bテストできます（`default` / `crush` / `couple` / `friends` / `none`）
- 使用したCTAは `meta.json` と `history.json` に記録され、後から効果を集計できます
- 🌸 はカラー絵文字フォントを合成して描画します。環境に絵文字フォントがない場合は
  豆腐（□）を出さず、自動的に絵文字を外したテキストになります

```bash
python generate.py --posts 5 --cta-set couple   # CTAセットを切り替え
python generate.py --posts 5 --no-cta           # CTAなしで生成
python generate.py --list-cta                   # セット一覧を表示
```

## 2. セットアップ

```bash
pip install -r requirements.txt     # Pillow のみ
python generate.py --validate       # ネタ元とフォントの確認
```

必要なのは **Python 3.10以上** と **Pillow**、そして**日本語フォント**だけです。

フォントは自動検出します（ヒラギノ / 游ゴシック / メイリオ / Noto Sans JP / IPAゴシック）。
見つからない場合や変更したい場合は次のいずれかで指定します。

```bash
python generate.py --posts 5 --font "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc"
export NIGHT_TEST_FONT=/path/to/font.otf       # 環境変数でもOK
```

リポジトリ直下に `fonts/` を作って `.ttf` / `.otf` を置いても自動で使われます。

---

## 3. 使い方

### 生成する

```bash
python generate.py --posts 10                   # 10投稿（画像100枚）
python generate.py --posts 10 --category distance   # カテゴリ指定
python generate.py --posts 5 --category hotel
python generate.py --posts 3 --seed 42          # シード固定で再現生成
python generate.py --posts 1 --size 1350x1800   # 大きめサイズ
python generate.py --posts 3 --dry-run          # 画像を作らず構成だけ確認
```

| オプション | 説明 |
| --- | --- |
| `--posts N` | 生成する投稿数（既定 1） |
| `--category NAME` | 上の表のカテゴリ名、または `random`（既定）|
| `--width` / `--height` | 画像サイズ（既定 `1080 x 1440`） |
| `--size` | `1080x1440` / `1200x1600` / `1350x1800` のプリセット |
| `--seed N` | 乱数シード（同じ値なら同じ組み合わせ） |
| `--font PATH` | 日本語フォントを明示指定 |
| `--header TEXT` | 見出し文言を固定する |
| `--cta-set NAME` | CTAセットを切り替える（`data/cta.json`） |
| `--no-cta` | CTAを表示しない |
| `--list-cta` | CTAセット一覧を表示 |
| `--tests-per-post N` | 1投稿の問題数（既定 5 = 画像10枚） |
| `--start-index N` | 投稿番号の開始値（既定は履歴の続き） |
| `--output DIR` | 出力先（既定 `output/`） |
| `--history PATH` | 履歴ファイルの場所 |
| `--paper` | 白い紙のようなごく薄い質感を加える |
| `--no-preview` | `preview.jpg`（10枚一覧）を作らない |
| `--overwrite` | 既存の投稿フォルダを上書きする |
| `--dry-run` | 画像を書き出さず構成だけ表示（履歴も残らない） |
| `--validate` | ネタ元の文字数チェック |
| `--list-categories` | カテゴリ一覧を表示 |

### 投稿を管理する

```bash
python manage.py list                     # output / ready / posted の状況
python manage.py ready post_001           # output → ready（投稿準備OK）
python manage.py posted post_001          # ready（なければ output）→ posted
python manage.py posted post_001 post_002 # まとめて指定
python manage.py ready --all              # output のすべてを ready へ
python manage.py back post_001            # ひとつ前の段階へ戻す
```

---

## 4. 出力されるもの

```
output/
└─ post_001/
   ├─ 01_question.png   問題1
   ├─ 02_answer.png     答え1
   ├─ 03_question.png   問題2
   ├─ 04_answer.png     答え2
   ├─ 05_question.png ─ 10_answer.png
   ├─ caption.txt       TikTok用キャプション（コピペするだけ）
   ├─ meta.json         5問分の元データ（再生成・確認用）
   └─ preview.jpg       10枚を一覧できるコンタクトシート
```

- **画像**: 1080x1440（3:4）／白背景・黒文字のみ／枠線・色・イラスト・絵文字なし
- **caption.txt**: 導入文・誘導文・締め・ハッシュタグをパターンから合成するので毎回変わります
- **meta.json**: タイトル / 質問 / 選択肢 / 答え / 締め文 / 使用ネタID / LEVEL / テーマ / CTA をすべて保存

### 運用フロー

```
1. python generate.py --posts 20
2. output/post_001 を開く
3. preview.jpg でざっと確認
4. 10枚を順番どおりTikTokへ（ファイル名順に選ぶだけ）
5. caption.txt をコピペ
6. python manage.py posted post_001
```

---

## 5. レイアウト仕様

「スマホ画面いっぱいのコンテンツ」ではなく、
**白い余白の中に静かに置かれた1枚の紙面**として見えるよう設計しています。

- 文字を置くのは画像中央の安全エリアのみ（左右 15%〜85% / 上下 18%〜82%）
- 要素は中央のコンテンツボックスに縦に積み、**安全エリア内で垂直中央**に配置
- 一番上・一番下・右端に重要情報を置かない
- 文字量が多い場合はフォント倍率を自動で下げてレイアウト崩れを防ぐ
- 日本語の禁則処理つき自動改行（行頭に `、。」` を置かない／中央揃えは行長を揃える）

| 要素 | 内容 |
| --- | --- |
| 問題画像 | 見出し → 問題番号 → タイトル → 細い区切り線 → 質問文 → 選択肢A〜D → 「答えは次へ」 |
| 答え画像 | 「答え」 → 問題番号 → タイトル → 細い区切り線 → A〜Dの結果 → 締めの一文 |

問題画像に答えは表示されません。

---

## 6. ネタを追加して増やす

`data/tests/*.json` にネタを足すだけで、半永久的に増やせます。
ファイルを新規追加すれば、そのファイル名（`category`）がそのまま新カテゴリになります。

```json
{
  "category": "distance",
  "label": "距離感",
  "items": [
    {
      "id": "dist_11",
      "form": "scene",
      "level": 3,
      "theme": "mood",
      "motif": "傘の中",
      "titles": ["ひとつの傘", "相合傘の距離"],
      "questions": ["急な雨で、傘はひとつだけ。\n\nあなたはどうする？"],
      "choices": { "A": "…", "B": "…", "C": "…", "D": "…" },
      "answers": { "A": "…", "B": "…", "C": "…", "D": "…" },
      "closings": ["…", "…"]
    }
  ]
}
```

| キー | 説明 |
| --- | --- |
| `id` | 全ファイルを通して一意（履歴管理に使用） |
| `form` | 出題形式: `scene`(情景) / `action`(行動) / `object`(物) / `priority`(優先順位) / `reaction`(反応) / `intuition`(直感) |
| `level` | 刺激度 `1`（答えやすい）/ `2`（本音）/ `3`（ちょっと際どい）。出題順の決定に使う |
| `theme` | テーマ（`hotel` `app` `secret` `looks` `money` `lead` など）。1投稿で重複させない |
| `motif` | ネタの題材。同じ題材が短期間に繰り返されないよう使われます |
| `titles` / `questions` / `closings` | **複数の言い回しを書けます**（組み合わせで自動的にバリエーションが増えます） |
| `choices` / `answers` | A〜D（3個でも可）。キーは対応させること |

追加したら文字数をチェックします。

```bash
python generate.py --validate
```

**文字量の目安**（超えると自動縮小がかかります）

| 項目 | 目安 |
| --- | --- |
| タイトル | 14文字以内 |
| 質問文 | 70文字以内（2〜5秒で読める量） |
| 選択肢 | 14文字以内 |
| 答え | 52文字以内（3〜7秒で読める量） |
| 締めの一文 | 34文字以内 |

**書き方の指針**

- 無難な質問に逃げない。「答えるのが少し恥ずかしい」「A〜Dで意見が割れる」ものを優先する
- 直接聞きすぎず、情景や選択肢から想像させる（露骨な性的描写は書かない／成人同士が前提）
- 選択肢は4つとも実際に選ばれうる内容にし、「A=正解 / D=悪い人」構造にしない
- 答えは「優しい人です」で終わらせず、少しだけ本音に踏み込む
- 断定・診断はしない（`〜しやすいタイプです` / `〜傾向があります`）
- カテゴリを掛け合わせると似た問題が減る（マチアプ×浮気 / 裏アカ×秘密 / ホテル×付き合う前 など）

---

## 7. 重複防止のしくみ

`history.json` に、生成した投稿・問題・言い回しをすべて記録しています。

- **同じネタ**を直近12投稿では再利用しない
- **同じ題材（motif）**を直近25問では繰り返さない
- 文字bigramの類似度（Dice係数）が高い問題を弾く
- 質問の**書き出しが8文字以上一致**する問題（構図が同じ問題）を弾く
- 1投稿内では**同じテーマを使わない**（直近14問のテーマも避ける）
- 1投稿内では、出題形式・カテゴリが偏らないよう散らす
- ネタを再利用する時は、**まだ使っていない言い回し**を優先する

条件を満たせない場合は段階的にルールを緩め、最後は使用回数の少ないネタから選びます。
履歴をリセットしたい場合は `history.json` を削除してください。

---

## 8. ファイル構成

```
.
├─ generate.py             生成CLI
├─ manage.py               投稿ステータス管理CLI
├─ night_test/
│  ├─ config.py            画像サイズ・安全エリア・文字サイズ
│  ├─ fonts.py             日本語フォントの自動検出
│  ├─ textlayout.py        禁則処理つき自動改行・ブロック積み上げ
│  ├─ renderer.py          Pillow描画（問題/答え/コンタクトシート）
│  ├─ content.py           ネタ読み込みと5問の構成
│  ├─ history.py           履歴と類似度判定
│  ├─ captions.py          caption.txt と見出し文言の生成
│  ├─ cta.py               CTA文言の管理（A/Bテスト用のセット切替）
│  ├─ emoji.py             絵文字(🌸)の合成描画
│  └─ builder.py           1投稿分の書き出し
├─ data/
│  ├─ tests/*.json         ネタ元（ここを増やす）
│  ├─ captions.json        キャプションの断片
│  ├─ headers.json         見出し文言のパターン
│  └─ cta.json             CTA文言（ここだけ直せば全投稿に反映）
├─ autopost.py             自動投稿CLI
├─ autopost_gui.py         自動投稿GUI
├─ autopost/               自動投稿システム（詳細は9章）
├─ SETUP_AUTOPOST.md       APIキー取得と初期設定の手順
├─ .env.example            自動投稿の設定サンプル
├─ sample_output/          サンプル出力（2投稿分）
├─ output/ ready/ posted/  運用フォルダ
└─ history.json            生成履歴（自動生成）
```

`output/` `ready/` `posted/` `history.json` は `.gitignore` 済みです。

---

## 9. 自動投稿（TikTok / Instagram）

生成した投稿フォルダを、そのまま TikTok と Instagram へ予約投稿できます。
**投稿処理でLLM APIは一切呼びません**（保存済みの caption.txt / meta.json を読むだけ）。

```bash
python autopost.py status                                   # 接続状況の確認
python autopost.py validate --folder output                 # 投稿前チェック
python autopost.py schedule --folder output \
       --start 2026-09-20 --time 21:00 --interval 1 --count 100   # 100投稿を一括予約
python autopost.py run                                      # 予約時刻を過ぎた分を投稿
python autopost.py watch                                    # 常駐して自動投稿
python autopost_gui.py                                      # 簡易GUI
```

セットアップ（APIキーの取得・ドメイン検証・画像ホスティング）は
**[SETUP_AUTOPOST.md](SETUP_AUTOPOST.md)** を参照してください。

### 仕組み

```
output/post_001/  ─┐
  01〜10.png       │ 画像を 4:5 JPEG へ変換（白背景に余白を足すだけ。内容は変えない）
  caption.txt      │        ↓
  meta.json        │ 1回だけアップロード → 公開HTTPS URL
                   │        ↓
                   ├→ TikTok    : creator_info → content/init(PHOTO) → status/fetch
                   └→ Instagram : 画像コンテナ×10 → CAROUSEL → media_publish
                            ↓
                   SQLite（autopost.db）に結果を記録
```

- **画像は共通**：同じURLを両プラットフォームで使うため、二重アップロードしません
- **1080x1440（3:4）は Instagram の要件外**（4:5〜1.91:1）のため、左右に白を足して
  1152x1440（4:5）のJPEGへ自動変換します。白背景なので見た目は変わりません
- **状態はSQLiteが正本**：ファイルは移動しないので、投稿中に参照が壊れません
  （`MOVE_AFTER_PUBLISH=true` にすると、両方成功した投稿だけ `posted/` へ移動します）
- **二重投稿しない**：`post_id × platform` はUNIQUE。`posted` になった組み合わせは
  通常実行では二度と投稿されません
- **片方が失敗しても巻き込まない**：TikTok成功・Instagram失敗なら、Instagramだけ再試行します
- **PCが止まっていた場合**：既定では期限切れのうち1投稿だけ投稿し、残りは翌日以降へ
  自動で再スケジュールします（`CATCHUP_POLICY` で変更可能）

### プラットフォーム別の文言

共通の caption を使いますが、必要なら post フォルダに `publish.json` を置くか、
`meta.json` に `"publish"` セクションを足すと個別に上書きできます（後方互換）。

```json
{
  "title": "恋人とやってみて",
  "caption_tiktok": "TikTok用の本文",
  "caption_instagram": "Instagram用の本文",
  "hashtags_instagram": ["#心理テスト", "#カップル"],
  "music": { "mode": "auto" },
  "schedule": { "scheduled_at": "2026-09-20T21:00:00+09:00" },
  "platforms": { "tiktok": { "enabled": true }, "instagram": { "enabled": true } }
}
```

解決順は **上書き設定 → caption.txt → meta.json → 固定テンプレート** の決定論的フォールバックです。

### 主なファイル

```
autopost.py / autopost_gui.py   CLI / GUI の入口
autopost/
├─ config.py        .env 読み込み（秘密情報はここだけ）
├─ models.py        共通Postモデル（プラットフォーム差分の解決）
├─ loader.py        投稿フォルダ・meta.json・caption.txt の読み込み
├─ validate.py      投稿前の機械的検証
├─ imageprep.py     4:5 JPEG への変換（キャッシュ付き）
├─ db.py            SQLiteキュー（状態・ログ・アップロード済みURL）
├─ scheduler.py     予約実行・catch-up・リトライ・二重投稿防止
├─ hosting/         画像ホスティング（Cloudflare R2 / ローカル公開ディレクトリ）
├─ publishers/      TikTokPublisher / InstagramPublisher（共通IFで追加可能）
├─ oauth/           TikTok・Meta の認証とトークン保存
├─ cli.py / gui.py  操作画面
```

