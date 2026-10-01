# antagent

グンタイアリの生態をまねた、汎用的な問題解決のための Claude Code スキルです。

女王（Fable）が卵（タスク）を産みます。Sonnet の働きアリ（偵察・運搬・兵隊）は、トレイルを介して並列に分割・作業・検証を進めます。中央の司令塔はいません。連携はトレイルへの読み書きだけで行い、それ以外の規則はすべて決定的なスクリプトが受け持ちます。

構想と設計の背景は [CONTEXT.md](CONTEXT.md) にあります。

## 必要なもの

- Claude Code（サブエージェントで `model: fable` と `effort` が使えるもの）
- Python 3.8 以上（標準ライブラリのみ。追加のインストールは不要）

## 導入

このリポジトリの `.claude/` を、使いたいプロジェクトにコピーします。

```
.claude/
├── skills/antagent/
│   ├── SKILL.md          # メインセッション（森）が従う手順
│   └── scripts/ant.py    # トレイル（決定的な規則）
└── agents/
    ├── ant-queen.md      # 女王    Fable  / high
    ├── ant-scout.md      # 偵察    Sonnet / low
    ├── ant-porter.md     # 運搬    Sonnet / medium
    └── ant-soldier.md    # 兵隊    Sonnet / high
```

サブエージェントの定義はスキルの中に同梱できない仕様なので、`.claude/agents/` に置いてください。

アリは毎回 `python …/ant.py` を実行します。許可の確認で止まらないよう、`Bash(python *ant.py*)` を許可リストに入れておくと進みがよくなります。

## 使い方

```
/antagent <解かせたい問題>
```

メインセッションはコロニーを作り（`ant.py init`）、`ant.py next` の指示に従ってアリを放ち続けます。根のタスクが検証を通ると、`.antagent/answer.md` に最終回答が書き出されます。

規模を変えたいときは、起動時に伝えてください（例：「アリ6匹、ステップ100で」）。

| オプション | 既定 | 意味 |
|---|---|---|
| `--ants` | 4 | 同時に歩くアリの数 |
| `--steps` | 60 | 働きアリの呼び出し回数の上限 |
| `--depth` | 3 | タスク分割の深さの上限 |

### 途中経過を見る・再開する

```
python .claude/skills/antagent/scripts/ant.py show            # トレイル全体
python .claude/skills/antagent/scripts/ant.py next --recover  # 中断したセッションから再開
python .claude/skills/antagent/scripts/ant.py extend --steps 30  # 予算切れ（halt）から延長
```

状態はすべて `.antagent/trail.db`（SQLite）にあります。新しい問題を始めるときは `.antagent/` を消してください。

## ant.py のコマンド

| コマンド | 使う人 | 内容 |
|---|---|---|
| `init` | 森 | コロニーを作る |
| `next [--done TASK:CLAIM ...]` | 森 | 帰ってきたアリを回収し、次の行動（spawn / queen / wait / finish / halt）を JSON で返す |
| `sense TASK CLAIM` | アリ・女王 | 自分の環世界（タスクとそのまわり）を見る |
| `report TASK CLAIM < JSON` | アリ・女王 | 結果を書き戻す。不正なら `rejected:`、無効な仕事なら `stale:` |
| `show` | 人 | トレイル全体を表示する |
| `extend` | 森・人 | 歩数や女王の起床回数の予算を増やす |

## ライセンス
MIT
