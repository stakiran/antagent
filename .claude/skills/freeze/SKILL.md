---
name: freeze
description: antagent の今のトレイル一式（.antagent/ の problem.md・answer.md・trail.db）を .antagent_archive/yymmdd_<キャプション>/ に移して保存し、次の問題を始められるようにする。ユーザーが /freeze と打ったときだけ使う。
argument-hint: "[キャプション]"
disable-model-invocation: true
---

# freeze

カレントディレクトリの `.antagent/` を、丸ごと `.antagent_archive/yymmdd_<キャプション>/` に移します。

1. キャプションを決める
   - 引数があればそれを使う: `$ARGUMENTS`
   - 無ければ `.antagent/problem.md` だけを読み、問題を一言で表すキャプションを決める（例: `しぐれうい`）。名詞1つ程度、10文字以内を目安にする
2. 実行する

```
python "${CLAUDE_SKILL_DIR}/scripts/freeze.py" <キャプション>
```

- 作業中のアリ（claimed のタスク）がいるときは止まります。結果をユーザーに伝え、それでも凍結するか聞いてください。凍結するなら `--force` を付けて実行し直します
- 保存先が既にあるときも止まります。別のキャプションをユーザーに聞いてください

実行後は、スクリプトの出力（保存先と移したファイル）をそのままユーザーに伝えて終わります。problem.md 以外は読まず、中身を要約したりもしません。
