"""Interactive shell and result formatting."""
import sys
import time

from .engine import Database, SQLError


def fmt(v):
    return "NULL" if v is None else repr(v) if isinstance(v, float) else str(v)


def format_table(columns, rows):
    cells = [[fmt(v) for v in row] for row in rows]
    widths = [max([len(str(c))] + [len(r[i]) for r in cells]) for i, c in enumerate(columns)]
    numeric = [all(isinstance(r[i], (int, float)) or r[i] is None for r in rows) and rows != [] for i in range(len(columns))]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"

    def row_text(vals):
        return "| " + " | ".join(v.rjust(w) if num else v.ljust(w) for v, w, num in zip(vals, widths, numeric)) + " |"

    out = [line, "| " + " | ".join(str(c).ljust(w) for c, w in zip(columns, widths)) + " |", line]
    out += [row_text(r) for r in cells]
    out.append(line)
    return "\n".join(out)


def show(result, out, elapsed=None):
    if result.columns:
        print(format_table(result.columns, result.rows), file=out)
        n = len(result.rows)
        tail = f"({n} row{'s' if n != 1 else ''}"
    else:
        tail = f"({result.message or 'ok'}"
    if elapsed is not None:
        tail += f", {elapsed * 1000:.2f} ms"
    print(tail + ")", file=out)


HELP = """\
.tables            list tables            .schema            show CREATE statements
.timer on|off      show query timings     .quit              leave
SQL statements end with ';' and may span several lines."""


def run_text(db, text, out=sys.stdout, timer=False):
    for res in db.run_all(text):
        show(res, out)


def dot_command(db, line, state, out):
    cmd, *args = line.split()
    if cmd in (".quit", ".exit", ".q"):
        return False
    if cmd == ".tables":
        print("  ".join(sorted(db.tables)) or "(no tables)", file=out)
    elif cmd == ".schema":
        print("\n".join(s + ";" for s in db.schema()) or "(empty)", file=out)
    elif cmd == ".timer":
        state["timer"] = bool(args) and args[0].lower() == "on"
        print(f"timer {'on' if state['timer'] else 'off'}", file=out)
    elif cmd == ".help":
        print(HELP, file=out)
    else:
        print(f"unknown command {cmd} (try .help)", file=out)
    return True


def balanced(text):
    """True when every quote is closed (so a trailing ';' really ends the statement)."""
    return text.count("'") % 2 == 0 and text.count('"') % 2 == 0


def repl(db, inp=sys.stdin, out=sys.stdout):
    interactive = inp.isatty()
    state = {"timer": False}
    if interactive:
        print("Tessera 1.0 - type .help for help, .quit to leave", file=out)
    buf = ""
    while True:
        if interactive:
            out.write("tessera> " if not buf else "     ...> ")
            out.flush()
        line = inp.readline()
        if not line:
            break
        if not buf and line.strip().startswith("."):
            if not dot_command(db, line.strip(), state, out):
                break
            continue
        buf += line
        if not buf.strip():
            buf = ""
            continue
        if buf.rstrip().endswith(";") and balanced(buf):
            t0 = time.perf_counter()
            try:
                for res in db.run_all(buf):
                    show(res, out, (time.perf_counter() - t0) if state["timer"] else None)
            except SQLError as e:
                print(f"Error: {e}", file=out)
            buf = ""
    if buf.strip():
        print("Error: incomplete statement (missing ';')", file=out)
