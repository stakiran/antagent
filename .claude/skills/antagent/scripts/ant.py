#!/usr/bin/env python3
"""antagent のトレイル（決定的な部分）。標準ライブラリのみ。

LLM（アリ・女王）は判断と作業だけを行い、それ以外はすべてここで決める:
タスクの選択（フェロモン）、出力の検証、木の更新、検証タスクの自動生成、
失敗・死亡の判定、停滞の検知、女王を起こすタイミング、予算。

コマンド:
  init  (PROBLEM | -f FILE) [--ants N --steps N --depth N]   コロニーを作る
  next  [--done TASK:CLAIM ...]   終わったアリを回収し、次にすべきことを JSON で返す
  sense TASK CLAIM                アリ（または女王: TASK=queen）が自分の環世界を見る
  report TASK CLAIM < JSON        アリ（または女王）が結果を書き戻す
  show                            トレイル全体を表示する
  extend [--steps N --queen-wakes N]   予算を増やす（halt から再開するとき）
"""

import argparse
import hashlib
import json
import os
import random
import re
import secrets
import sqlite3
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().as_posix()

DEFAULTS = {
    "ants": 4,          # 同時に歩くアリの数
    "steps": 60,        # 働きアリの呼び出し回数の上限
    "depth": 3,         # 分割の深さの上限
    "children": 5,      # 1回の分割で産める子の数の上限
    "fails": 3,         # この回数失敗したら死ぬ
    "stall": 12,        # この歩数 done が出なければ停滞とみなす
    "queen_wakes": 3,   # 女王を起こせる回数（創設を除く）
    "lease_sec": 1800,  # 報告がないまま、この秒数を過ぎた作業は失敗扱い
}
HALF_LIFE = 20     # フェロモンの半減期（歩数）
EPS = 0.05         # 薄い道にもたまにアリが行くための下駄
DEPOSIT = 1.0      # 子が done したとき親に足すフェロモン
VERIFY_PHER = 2.0  # 検証タスクは詰まりを解消するので濃いめ
CUT = 400          # 兄弟の結果を見せるときの文字数上限

AGENT = {"explore": "ant-scout", "build": "ant-porter", "verify": "ant-soldier"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id         INTEGER PRIMARY KEY,
  parent_id  INTEGER,
  depth      INTEGER NOT NULL,
  kind       TEXT    NOT NULL,                -- explore | build | verify
  goal       TEXT    NOT NULL,
  goal_hash  TEXT    NOT NULL UNIQUE,         -- 同じ道を二度作らない（死の渦よけ）
  status     TEXT    NOT NULL DEFAULT 'open', -- open | claimed | waiting | done | dead
  result     TEXT    NOT NULL DEFAULT '',
  note       TEXT    NOT NULL DEFAULT '',     -- 失敗理由や検証の指摘
  pher       REAL    NOT NULL,
  pher_at    INTEGER NOT NULL,
  fails      INTEGER NOT NULL DEFAULT 0,
  claim      TEXT,
  claimed_at REAL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Reject(Exception):
    """アリの報告を受け付けない。メッセージはそのままアリに返り、直して出し直させる。"""


def _hash(goal):
    return hashlib.sha1(re.sub(r"\s+", " ", goal.strip().lower()).encode()).hexdigest()


def _one_line(s, n):
    return s.replace("\n", " ")[:n]


class Trail:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=60, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        # 並行して報告してくるアリと衝突しないよう、コマンド全体を1トランザクションにする
        self.db.execute("BEGIN IMMEDIATE")
        self.events = []

    def commit(self):
        self.db.execute("COMMIT")

    # --- meta ---

    def meta(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))

    def cfg(self, key):
        return self.meta("cfg", DEFAULTS)[key]

    @property
    def now(self):
        return self.meta("now", 0)

    def tick(self):
        self.set_meta("now", self.now + 1)
        self.set_meta("idle", self.meta("idle", 0) + 1)

    # --- 読む ---

    def get(self, tid):
        return self.db.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()

    def root(self):
        return self.db.execute("SELECT * FROM tasks WHERE parent_id IS NULL ORDER BY id LIMIT 1").fetchone()

    def children(self, tid, include_verify=False):
        sql = "SELECT * FROM tasks WHERE parent_id=?" + ("" if include_verify else " AND kind!='verify'")
        return self.db.execute(sql + " ORDER BY id", (tid,)).fetchall()

    def claimed(self):
        return self.db.execute("SELECT * FROM tasks WHERE status='claimed'").fetchall()

    def has_open(self):
        return self.db.execute("SELECT 1 FROM tasks WHERE status='open' LIMIT 1").fetchone() is not None

    def smell(self, row):
        """蒸発込みの現在のフェロモン濃度。読むときに計算するので蒸発処理は不要。"""
        return row["pher"] * 0.5 ** ((self.now - row["pher_at"]) / HALF_LIFE)

    def pick(self, rng):
        """open のタスクを「自分の匂い × 親の匂い」に比例した確率で1つ選ぶ。"""
        rows = self.db.execute("SELECT * FROM tasks WHERE status='open' ORDER BY id").fetchall()
        if not rows:
            return None
        weights = []
        for r in rows:
            parent = self.get(r["parent_id"]) if r["parent_id"] else None
            weights.append(self.smell(r) * (self.smell(parent) if parent else 1.0) + EPS)
        return rng.choices(rows, weights=weights)[0]

    def tree_text(self, tid=None, indent=0):
        row = self.get(tid) if tid else self.root()
        out = row["result"] or row["note"]
        line = (f"{'  ' * indent}#{row['id']} [{row['kind']}/{row['status']} pher={self.smell(row):.2f} "
                f"fails={row['fails']}] {_one_line(row['goal'], 120)}")
        if out:
            line += f"  => {_one_line(out, 160)}"
        lines = [line]
        for k in self.children(row["id"], include_verify=True):
            lines.append(self.tree_text(k["id"], indent + 1))
        return "\n".join(lines)

    # --- 書く ---

    def add(self, parent_id, kind, goal, pher):
        """タスクを産む。同じ goal が既にあれば None。"""
        parent = self.get(parent_id) if parent_id else None
        depth = 0 if parent is None else parent["depth"] + (0 if kind == "verify" else 1)
        cur = self.db.execute(
            "INSERT OR IGNORE INTO tasks (parent_id, depth, kind, goal, goal_hash, pher, pher_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (parent_id, depth, kind, goal, _hash(goal), pher, self.now))
        return cur.lastrowid if cur.rowcount else None

    def set(self, tid, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self.db.execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), tid))

    def deposit(self, tid, delta):
        self.set(tid, pher=self.smell(self.get(tid)) + delta, pher_at=self.now)

    def scale(self, tid, factor):
        self.set(tid, pher=self.smell(self.get(tid)) * factor, pher_at=self.now)

    def log(self, task, event):
        self.events.append(f"#{task['id']} {task['kind']} {event}: {_one_line(task['goal'], 60)}")

    # --- 木の規則 ---

    def release(self, task, **fields):
        self.set(task["id"], claim=None, claimed_at=None, **fields)

    def progress(self, task):
        self.set_meta("idle", 0)
        if task["parent_id"]:
            self.deposit(task["parent_id"], DEPOSIT)
            self.check_parent(task["parent_id"])

    def fail(self, task, reason):
        fails = task["fails"] + 1
        if fails >= self.cfg("fails"):
            self.release(task, status="dead", fails=fails, note=reason)
            self.log(task, f"dead ({_one_line(reason, 80)})")
            if task["kind"] == "verify":
                target = self.get(task["parent_id"])
                if target["status"] == "waiting":
                    self.fail(target, "verification kept failing")
            else:
                self.check_parent(task["parent_id"])
        else:
            self.release(task, status="open", fails=fails, note=reason)
            self.scale(task["id"], 0.5)
            self.log(task, f"fail ({_one_line(reason, 80)})")

    def check_parent(self, pid):
        """子がすべて片付いた親に、検証タスクを産む。"""
        if pid is None:
            return
        p = self.get(pid)
        if p["status"] != "waiting":
            return
        kids = self.children(pid, include_verify=True)
        if any(k["status"] in ("open", "claimed", "waiting") for k in kids):
            return
        if p["result"] or any(k["status"] == "done" and k["kind"] != "verify" for k in kids):
            self.spawn_verify(pid)
        else:
            self.fail(p, "all subtasks died")

    def spawn_verify(self, pid):
        n = sum(1 for k in self.children(pid, include_verify=True) if k["kind"] == "verify") + 1
        self.add(pid, "verify", f"verify #{pid} (attempt {n})", VERIFY_PHER)

    def kill_subtree(self, tid, note):
        row = self.get(tid)
        if row["status"] != "done":
            self.release(row, status="dead", note=note)
        for k in self.children(tid, include_verify=True):
            self.kill_subtree(k["id"], note)


# --- 報告の検証と適用（すべて決定的） ---

def _parse(raw):
    raw = raw.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, re.S)
    if m:
        raw = m.group(1)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise Reject(f"JSON として読めません: {e}")
    if not isinstance(data, dict):
        raise Reject("JSON はオブジェクト {...} にしてください")
    return data


def _fields(data, spec):
    extra = set(data) - set(spec)
    missing = set(spec) - set(data)
    if extra or missing:
        raise Reject(f"キーは {sorted(spec)} ちょうどにしてください（余分: {sorted(extra)}, 不足: {sorted(missing)}）")
    for k, typ in spec.items():
        if not isinstance(data[k], typ) or (typ is int and isinstance(data[k], bool)):
            raise Reject(f"'{k}' の型が違います（期待: {typ.__name__}）")


def apply_worker(t, task, data):
    _fields(data, {"outcome": str, "result": str, "children": list})
    outcome, result, children = data["outcome"], data["result"].strip(), data["children"]
    if outcome not in ("done", "split", "fail"):
        raise Reject("outcome は done / split / fail のどれかです")
    if outcome != "split" and children:
        raise Reject("children は split のときだけ使います。それ以外は [] にしてください")

    if outcome == "done":
        if not result:
            raise Reject("done なのに result が空です。このタスクの成果物を result に書いてください")
        if task["depth"] == 0:
            # 根の答えは必ず兵隊が検証する
            t.release(task, status="waiting", result=result, note="")
            t.spawn_verify(task["id"])
            t.log(task, "draft")
        else:
            t.release(task, status="done", result=result, note="")
            t.log(task, "done")
            t.progress(task)
        return "accepted: done"

    if outcome == "split":
        if task["depth"] >= t.cfg("depth"):
            raise Reject("最大深さに達しているので分割できません。このタスクを直接やり遂げて done にしてください")
        if not 2 <= len(children) <= t.cfg("children"):
            raise Reject(f"split の子は 2〜{t.cfg('children')} 個にしてください")
        for c in children:
            if not isinstance(c, dict):
                raise Reject("children の各要素は {\"kind\": ..., \"goal\": ...} です")
            _fields(c, {"kind": str, "goal": str})
            if c["kind"] not in ("explore", "build") or not c["goal"].strip():
                raise Reject("子の kind は explore / build、goal は空でない文字列です")
        pher = t.smell(task)
        added, dup = [], []
        for c in children:
            tid = t.add(task["id"], c["kind"], c["goal"].strip(), pher)
            (added if tid else dup).append(tid or c["goal"])
        if not added:
            raise Reject("子がすべて既存のタスクと重複しています。直接やり遂げるか、違う分け方にしてください")
        t.release(task, status="waiting", result="", note="")
        t.log(task, "split -> " + ", ".join(f"#{a}" for a in added))
        return "accepted: split" + (f" (重複のため捨てた子: {len(dup)})" if dup else "")

    if not result:
        raise Reject("fail のときは理由を result に書いてください")
    t.fail(task, result)
    return "accepted: fail"


def apply_verify(t, v, data):
    _fields(data, {"passed": bool, "result": str, "feedback": str})
    if data["passed"] and not data["result"].strip():
        raise Reject("passed=true のときは統合した成果物を result に書いてください")
    if not data["passed"] and not data["feedback"].strip():
        raise Reject("passed=false のときは足りない点を feedback に具体的に書いてください")
    target = t.get(v["parent_id"])
    t.release(v, status="done", result="passed" if data["passed"] else data["feedback"])
    if target["status"] != "waiting":
        return "accepted (対象はもう不要でした)"
    if data["passed"]:
        t.set(target["id"], status="done", result=data["result"].strip(), note="")
        t.log(target, "verified")
        t.progress(target)
    else:
        t.log(target, "rejected")
        t.fail(target, "verifier: " + data["feedback"].strip())
    return "accepted"


def apply_queen(t, data):
    _fields(data, {"kill": list, "brood": list})
    root = t.root()
    for tid in data["kill"]:
        if not isinstance(tid, int) or isinstance(tid, bool) or not t.get(tid):
            raise Reject(f"kill に存在しない id があります: {tid!r}")
        if tid == root["id"]:
            raise Reject("根（root）は殺せません")
    for b in data["brood"]:
        if not isinstance(b, dict):
            raise Reject("brood の各要素は {\"parent_id\", \"kind\", \"goal\"} です")
        _fields(b, {"parent_id": int, "kind": str, "goal": str})
        parent = t.get(b["parent_id"])
        if not parent or parent["kind"] == "verify":
            raise Reject(f"brood の parent_id が不正です: {b['parent_id']}")
        if b["kind"] not in ("explore", "build") or not b["goal"].strip():
            raise Reject("brood の kind は explore / build、goal は空でない文字列です")
        if parent["depth"] >= t.cfg("depth"):
            raise Reject(f"#{parent['id']} は最大深さなので、その下には産めません")

    touched = set()
    for tid in data["kill"]:
        row = t.get(tid)
        if row["status"] != "done":
            t.kill_subtree(tid, "killed by queen")
            touched.add(row["parent_id"])
            t.events.append(f"queen kill #{tid}")
    laid = 0
    for b in data["brood"]:
        parent = t.get(b["parent_id"])
        if parent["status"] not in ("open", "waiting"):
            continue
        tid = t.add(parent["id"], b["kind"], b["goal"].strip(), 1.0)
        if tid:
            laid += 1
            t.set(parent["id"], status="waiting")
            t.events.append(f"queen lay #{tid} under #{parent['id']}: {_one_line(b['goal'], 60)}")
    for pid in touched:
        t.check_parent(pid)
    return f"accepted: kill {len(data['kill'])}, laid {laid}"


# --- 環世界（アリに見える世界） ---

def _report_howto(t, task_ref, claim, example):
    return (f"\n## 報告のしかた\n作業が終わったら、次のコマンドで結果を1回だけ報告してください。\n\n"
            f"python \"{SCRIPT}\" --db \"{t.path.as_posix()}\" report {task_ref} {claim} <<'JSON'\n{example}\nJSON\n\n"
            f"- `rejected:` が返ったら、理由に従って直し、もう一度報告してください。\n"
            f"- `stale:` が返ったら、この仕事はもう無効です。何もせず終了してください。\n"
            f"- `accepted` が返ったら終了です。最後の返答は1行だけにしてください。")


def umwelt(t, task, claim):
    colony_goal = t.root()["goal"]
    if task["kind"] == "verify":
        target = t.get(task["parent_id"])
        lines = [f"# 検証タスク #{task['id']}", "", f"## コロニーの目的\n{colony_goal}", "",
                 f"## 検証する対象（#{target['id']}）\n{target['goal']}"]
        parts = [k for k in t.children(target["id"]) if k["status"] == "done"]
        if parts:
            lines += ["", "## 完了した部品"] + [f"### {k['goal']}\n{k['result']}" for k in parts]
        if target["result"]:
            lines += ["", "## 下書き", target["result"]]
        example = '{"passed": true, "result": "統合した成果物", "feedback": ""}'
        return "\n".join(lines) + "\n" + _report_howto(t, task["id"], claim, example)

    lines = [f"# タスク #{task['id']}（{task['kind']}, 深さ {task['depth']}/{t.cfg('depth')}）", ""]
    if task["parent_id"]:
        parent = t.get(task["parent_id"])
        lines += [f"## コロニーの目的\n{colony_goal}", "", f"## 親タスク\n{parent['goal']}", ""]
        sibs = [s for s in t.children(parent["id"]) if s["status"] == "done" and s["id"] != task["id"]]
        if sibs:
            lines += ["## 完了済みの兄弟タスク"] + [f"- {s['goal']}\n  => {s['result'][:CUT]}" for s in sibs] + [""]
    lines.append(f"## あなたのタスク\n{task['goal']}")
    if task["note"]:
        lines += ["", f"## 前回の試みへの指摘\n{task['note']}"]
    kids = [k for k in t.children(task["id"]) if k["status"] == "done"]
    if kids:
        lines += ["", "## 以前に分割した子タスクの結果"] + [f"- {k['goal']}\n  => {k['result']}" for k in kids]
    can_split = task["depth"] < t.cfg("depth")
    lines += ["", "## 分割", f"可（2〜{t.cfg('children')} 個）" if can_split else "不可（最大深さ）。直接やり遂げてください"]
    example = ('{"outcome": "done | split | fail", "result": "...", '
               '"children": [{"kind": "explore | build", "goal": "..."}]}')
    return "\n".join(lines) + "\n" + _report_howto(t, task["id"], claim, example)


def queen_umwelt(t, claim):
    root = t.root()
    if not t.meta("founded", False):
        head = (f"# 創設\n根タスク（id {root['id']}）の問題に対して、最初の卵を産んでください。\n\n"
                f"## 問題\n{root['goal']}")
    else:
        why = t.meta("queen_reason", "")
        head = (f"# 再計画（{why}）\nコロニーが停滞しました。トレイルを見て、方針を立て直してください。\n\n"
                f"## トレイル\n{t.tree_text()}")
    example = '{"kill": [], "brood": [{"parent_id": %d, "kind": "explore | build", "goal": "..."}]}' % root["id"]
    return head + "\n" + _report_howto(t, "queen", claim, example)


# --- コマンド ---

def cmd_init(t, args):
    if t.root():
        raise SystemExit("トレイルは既にあります。新しく始めるなら --db を変えるか、ファイルを消してください")
    problem = Path(args.file).read_text(encoding="utf-8") if args.file else args.problem
    if not problem or not problem.strip():
        raise SystemExit("問題文が空です")
    cfg = dict(DEFAULTS)
    for k in ("ants", "steps", "depth"):
        if getattr(args, k) is not None:
            cfg[k] = getattr(args, k)
    t.set_meta("cfg", cfg)
    t.set_meta("seed", random.randrange(2**31))
    rid = t.add(None, "build", problem.strip(), 1.0)
    return {"action": "initialized", "root": rid, "db": t.path.as_posix(), "cfg": cfg}


def cmd_next(t, args):
    if args.recover:
        # 前のセッションが途中で死んだ場合: 歩いていたアリはもう帰ってこない
        for task in t.claimed():
            t.release(task, status="open")
        t.set_meta("queen_claim", None)
    # 1. 終わったと知らされたアリを回収する（報告せずに帰ってきたアリは失敗扱い）
    for item in args.done or []:
        ref, _, claim = item.partition(":")
        if ref == "queen":
            if t.meta("queen_claim") == claim:
                t.set_meta("queen_claim", None)
                t.set_meta("founded", True)
                t.events.append("queen returned without laying")
            continue
        task = t.get(int(ref))
        if task and task["status"] == "claimed" and task["claim"] == claim:
            t.tick()
            t.fail(task, "ant returned without reporting")
    # 2. リース切れ（応答のないまま時間が経った作業）も失敗扱い
    for task in t.claimed():
        if time.time() - task["claimed_at"] > t.cfg("lease_sec"):
            t.tick()
            t.fail(task, "lease expired")

    root = t.root()
    inflight = t.claimed()
    if root["status"] == "done":
        answer = t.path.parent / "answer.md"
        answer.write_text(root["result"], encoding="utf-8")
        return {"action": "finish", "answer_file": answer.as_posix(), "steps": t.now}
    if t.meta("queen_claim"):
        return {"action": "wait", "running": ["queen:" + t.meta("queen_claim")]}
    if not t.meta("founded", False):
        return _summon_queen(t, "founding")

    # 3. 移動期: 空いているアリに仕事を拾わせる。停滞中（定住期）は拾わせない
    rng = random.Random(t.meta("seed", 0) + t.now * 7919 + len(inflight))
    spawn = []
    if t.meta("idle", 0) < t.cfg("stall"):
        while len(inflight) + len(spawn) < t.cfg("ants") and t.now + len(inflight) + len(spawn) < t.cfg("steps"):
            task = t.pick(rng)
            if task is None:
                break
            claim = secrets.token_hex(3)
            t.set(task["id"], status="claimed", claim=claim, claimed_at=time.time())
            spawn.append({"subagent_type": AGENT[task["kind"]], "ref": f"{task['id']}:{claim}",
                          "prompt": _ant_prompt(t, task["id"], claim)})
    if spawn:
        return {"action": "spawn", "ants": spawn, "running": [f"{r['id']}:{r['claim']}" for r in inflight]}
    if inflight:
        return {"action": "wait", "running": [f"{r['id']}:{r['claim']}" for r in inflight]}

    # 4. 定住期: 誰も歩いていない。女王を起こすか、終わる
    if t.now >= t.cfg("steps"):
        return {"action": "halt", "reason": f"step budget exhausted ({t.now})"}
    if t.meta("wakes", 0) >= t.cfg("queen_wakes"):
        return {"action": "halt", "reason": "queen wake budget exhausted"}
    why = "no open tasks left" if not t.has_open() else f"no progress in {t.meta('idle', 0)} steps"
    if root["status"] == "dead":
        why = "root task died"
        t.set(root["id"], status="open", fails=0)
    t.set_meta("wakes", t.meta("wakes", 0) + 1)
    return _summon_queen(t, why)


def cmd_extend(t, args):
    cfg = t.meta("cfg", DEFAULTS)
    if args.steps:
        cfg["steps"] = max(cfg["steps"], t.now) + args.steps
    if args.queen_wakes:
        cfg["queen_wakes"] = max(cfg["queen_wakes"], t.meta("wakes", 0)) + args.queen_wakes
    t.set_meta("cfg", cfg)
    return {"action": "extended", "cfg": cfg}


def _summon_queen(t, why):
    claim = secrets.token_hex(3)
    t.set_meta("queen_claim", claim)
    t.set_meta("queen_reason", why)
    return {"action": "queen", "reason": why, "subagent_type": "ant-queen", "ref": f"queen:{claim}",
            "prompt": _ant_prompt(t, "queen", claim)}


def _ant_prompt(t, ref, claim):
    return (f"仕事 {ref}（claim {claim}）。まず次のコマンドで自分の環世界を見て、その指示に従ってください。\n"
            f"python \"{SCRIPT}\" --db \"{t.path.as_posix()}\" sense {ref} {claim}")


def _check_claim(t, ref, claim):
    if ref == "queen":
        if t.meta("queen_claim") != claim:
            raise Reject("stale")
        return None
    task = t.get(int(ref)) if ref.isdigit() else None
    if not task or task["status"] != "claimed" or task["claim"] != claim:
        raise Reject("stale")
    return task


def cmd_sense(t, args):
    task = _check_claim(t, args.task, args.claim)
    return queen_umwelt(t, args.claim) if task is None else umwelt(t, task, args.claim)


def cmd_report(t, args):
    task = _check_claim(t, args.task, args.claim)
    data = _parse(sys.stdin.read())
    if task is None:
        msg = apply_queen(t, data)
        t.set_meta("queen_claim", None)
        t.set_meta("founded", True)
        t.set_meta("idle", 0)
        return msg
    t.tick()
    return (apply_verify if task["kind"] == "verify" else apply_worker)(t, task, data)


def main():
    for s in (sys.stdin, sys.stdout):
        s.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=os.path.join(".antagent", "trail.db"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    p.add_argument("problem", nargs="?")
    p.add_argument("-f", "--file")
    for k in ("ants", "steps", "depth"):
        p.add_argument(f"--{k}", type=int)
    p = sub.add_parser("next")
    p.add_argument("--done", nargs="*", metavar="TASK:CLAIM")
    p.add_argument("--recover", action="store_true")
    p = sub.add_parser("extend")
    p.add_argument("--steps", type=int)
    p.add_argument("--queen-wakes", type=int)
    for name in ("sense", "report"):
        p = sub.add_parser(name)
        p.add_argument("task")
        p.add_argument("claim")
    sub.add_parser("show")
    args = ap.parse_args()

    if args.cmd != "init" and not Path(args.db).exists():
        raise SystemExit(f"トレイルがありません: {args.db}（先に init してください）")
    t = Trail(args.db)
    try:
        if args.cmd == "show":
            out = t.tree_text() if t.root() else "(empty)"
            out += f"\n\nsteps {t.now}/{t.cfg('steps')}, idle {t.meta('idle', 0)}, queen wakes {t.meta('wakes', 0)}"
        else:
            out = {"init": cmd_init, "next": cmd_next, "extend": cmd_extend, "sense": cmd_sense, "report": cmd_report}[args.cmd](t, args)
    except Reject as e:
        t.db.execute("ROLLBACK")
        print(("stale: この仕事はもう無効です。終了してください" if str(e) == "stale" else f"rejected: {e}"))
        sys.exit(2)
    t.commit()
    if isinstance(out, dict):
        if t.events:
            out["events"] = t.events
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(out)
        for e in t.events:
            print(f"  event: {e}")


if __name__ == "__main__":
    main()
