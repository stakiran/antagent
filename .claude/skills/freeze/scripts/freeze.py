#!/usr/bin/env python3
"""antagent のトレイル一式（.antagent/）を .antagent_archive/yymmdd_<キャプション>/ に移して凍結する。"""
import argparse
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SRC = Path(".antagent")
DST = Path(".antagent_archive")


def slug(text, limit=24):
    # Windows で使えない文字と空白を除き、先頭の1行だけを名前に使う
    text = text.strip()
    s = re.sub(r'[\\/:*?"<>|\s]+', "_", text.splitlines()[0] if text else "")
    return s.strip("._")[:limit]


def running(db):
    con = sqlite3.connect(db)
    try:
        return [r[0] for r in con.execute("SELECT id FROM tasks WHERE status='claimed' ORDER BY id")]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def main():
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    ap =argparse.ArgumentParser(description=__doc__)
    ap.add_argument("caption", nargs="?", help="一言のキャプション。保存先は yymmdd_<キャプション>（省略時は yymmdd）")
    ap.add_argument("--force", action="store_true", help="作業中のアリがいても凍結する")
    args = ap.parse_args()

    if not SRC.is_dir() or not any(SRC.iterdir()):
        sys.exit(f"凍結するものがありません: {SRC.as_posix()}/")

    db = SRC / "trail.db"
    if db.exists() and not args.force:
        ids = running(db)
        if ids:
            sys.exit("作業中のアリがいます: " + ", ".join(f"#{i}" for i in ids)
                     + "（終わるのを待つか、--force を付けてください）")

    caption = slug(args.caption or "")
    name = datetime.now().strftime("%y%m%d") + (f"_{caption}" if caption else "")

    DST.mkdir(exist_ok=True)
    dest = DST / name
    if dest.exists():
        sys.exit(f"保存先が既にあります: {dest.as_posix()}/（別のキャプションを指定してください）")

    # trail.db の -wal / -shm も含め、フォルダごと移す
    SRC.rename(dest)
    files = sorted(p.name for p in dest.iterdir())
    print(f"凍結しました: {dest.as_posix()}/ ({', '.join(files)})")


if __name__ == "__main__":
    main()
