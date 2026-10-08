"""python -m tessera [database-file] [-c SQL] [-f script.sql]"""
import argparse
import sys

from .engine import Database, SQLError
from .repl import repl, run_text


def main(argv=None):
    ap = argparse.ArgumentParser(prog="tessera", description="Tessera SQL database shell")
    ap.add_argument("database", nargs="?", help="file for a durable database (omit for in-memory)")
    ap.add_argument("-c", "--command", help="run SQL and exit")
    ap.add_argument("-f", "--file", help="run a SQL script file and exit")
    args = ap.parse_args(argv)
    try:
        db = Database(args.database)
    except SQLError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        if args.command or args.file:
            text = args.command if args.command else open(args.file, encoding="utf-8").read()
            run_text(db, text)
        else:
            repl(db)
    except SQLError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
