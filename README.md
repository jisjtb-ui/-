# ちょっと大人の心理テスト｜TikTok投稿セット自動生成ツール

白い紙のような背景に黒文字だけ。極端にミニマルな
**「ちょっと大人の恋愛心理テスト」画像セット**をローカルで無限に量産するツールです。

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

「少しだけ踏み込んだ恋愛心理テスト」です。露骨な性的表現は扱わず、
**想像するとドキッとする / 恋人や友達とやると盛り上がる**ラインに抑えています。

| カテゴリ | 内容 | ネタ数 |
| --- | --- | --- |
| `distance` | 距離感（ソファ・エレベーター・並び方・終電後） | 10 |
| `possessive` | 独占欲・嫉妬・束縛・順位 | 10 |
| `skinship` | スキンシップ・触れ方・人前での距離 | 10 |
| `tempo` | 攻めるか待つか・主導権・駆け引き | 10 |
| `honne` | 本音・秘密・人には言いにくいこと | 10 |
| `desire` | 衝動と理性・一線の基準・深夜の誘い | 10 |
| `dependence` | 甘え方・依存・求められたさ | 10 |
| `random` | 上記を混ぜて出題（既定） | 70 |

画像上部の見出しは毎回固定ではなく、
「ちょっと大人の心理テスト」「恋愛の本性が出る心理テスト」「人には言いにくい心理テスト」など
`data/headers.json` のパターンから投稿ごとに選ばれます（1投稿10枚は同じ見出しで統一）。

答えは断定せず、`〜かもしれません` `〜しやすい傾向があります` のような表現に統一しています
（医療・診断的な断定は行いません）。

---

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
python generate.py --posts 5 --category skinship
python generate.py --posts 3 --seed 42          # シード固定で再現生成
python generate.py --posts 1 --size 1350x1800   # 大きめサイズ
python generate.py --posts 3 --dry-run          # 画像を作らず構成だけ確認
```

| オプション | 説明 |
| --- | --- |
| `--posts N` | 生成する投稿数（既定 1） |
| `--category NAME` | `distance` / `possessive` / `skinship` / `tempo` / `honne` / `desire` / `dependence` / `random` |
| `--width` / `--height` | 画像サイズ（既定 `1080 x 1440`） |
| `--size` | `1080x1440` / `1200x1600` / `1350x1800` のプリセット |
| `--seed N` | 乱数シード（同じ値なら同じ組み合わせ） |
| `--font PATH` | 日本語フォントを明示指定 |
| `--header TEXT` | 見出し文言を固定する |
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
- **meta.json**: タイトル / 質問 / 選択肢 / 答え / 締め文 / 使用ネタID をすべて保存

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

- 直接聞きすぎず、情景や選択肢から想像させる
- 答えは「優しい人です」で終わらせず、少しだけ本音に踏み込む
- 断定しない（`〜かもしれません` / `〜しやすい傾向があります`）
- 露骨な性的描写・下品な表現・雑な決めつけは書かない

---

## 7. 重複防止のしくみ

`history.json` に、生成した投稿・問題・言い回しをすべて記録しています。

- **同じネタ**を直近12投稿では再利用しない
- **同じ題材（motif）**を直近25問では繰り返さない
- 文字bigramの類似度（Dice係数）が高い問題を弾く
- 質問の**書き出しが8文字以上一致**する問題（構図が同じ問題）を弾く
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
│  └─ builder.py           1投稿分の書き出し
├─ data/
│  ├─ tests/*.json         ネタ元（ここを増やす）
│  ├─ captions.json        キャプションの断片
│  └─ headers.json         見出し文言のパターン
├─ sample_output/          サンプル出力（2投稿分）
├─ output/ ready/ posted/  運用フォルダ
└─ history.json            生成履歴（自動生成）
```

`output/` `ready/` `posted/` `history.json` は `.gitignore` 済みです。
