"""Mutation testing of the hidden tests against the reference solution.

Question: do the hidden tests catch small mistakes in the code they protect?
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import tokenize
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import readme
from .grade import grade
from .tasks import (TASKS_DIR, Task, TaskError, is_production_dir, load_corpus, load_tasks,
                    utf8_stdout)
from .workspace import ApplyError, make_workspace, remove_tree

MIN_MUTANTS_PER_TASK = 5
MIN_JUSTIFICATION_CHARS = 20


class MutationError(RuntimeError):
    """The mutation setup is unsound (bad config, no mutants, unhealthy baseline)."""


class MutationInfraError(MutationError):
    """A mutant run ended in ERROR: infrastructure, so it says nothing about the tests."""

HEADER = "| Task | Mutants | Killed | Equivalent (justified) | Unjustified survivors | Score |"
SEP = "|" + "---|" * 6

PY_KEYWORDS = {"return", "if", "elif", "else", "for", "while", "def", "class", "try",
               "except", "finally", "with", "import", "from", "raise", "assert", "yield",
               "del", "pass", "break", "continue", "global", "nonlocal", "async", "await",
               "lambda", "match", "case"}
TS_KEYWORDS = {"return", "if", "else", "for", "while", "do", "switch", "case", "throw",
               "function", "class", "import", "export", "const", "let", "var", "try",
               "catch", "finally", "break", "continue", "interface", "type", "enum",
               "default", "new", "delete", "typeof"}


# --------------------------------------------------------------------------
# Masks: which characters are inside a string literal or a comment
# --------------------------------------------------------------------------

def _py_mask(lines: list[str], text: str) -> list[list[bool]]:
    mask = [[False] * len(line) for line in lines]

    def mark(start, end):
        for row in range(start[0], end[0] + 1):
            if row > len(lines):
                break
            c0 = start[1] if row == start[0] else 0
            c1 = end[1] if row == end[0] else len(lines[row - 1])
            for c in range(c0, min(c1, len(mask[row - 1]))):
                mask[row - 1][c] = True

    fstart = getattr(tokenize, "FSTRING_START", -1)
    fend = getattr(tokenize, "FSTRING_END", -1)
    depth, begin = 0, None
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                mark(tok.start, tok.end)
            elif tok.type == fstart:
                if depth == 0:
                    begin = tok.start
                depth += 1
            elif tok.type == fend:
                depth -= 1
                if depth == 0 and begin is not None:
                    mark(begin, tok.end)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return mask


def _ts_mask(lines: list[str], text: str) -> list[list[bool]]:
    n = len(text)
    flat = [False] * n

    def mark(a, b):
        for k in range(a, min(b, n)):
            flat[k] = True

    i = 0
    while i < n:
        c = text[i]
        if c == "/" and text[i + 1:i + 2] == "/":
            j = text.find("\n", i)
            j = n if j == -1 else j
            mark(i, j)
            i = j
        elif c == "/" and text[i + 1:i + 2] == "*":
            j = text.find("*/", i + 2)
            j = n if j == -1 else j + 2
            mark(i, j)
            i = j
        elif c in "'\"`":
            j = i + 1
            while j < n:
                if text[j] == "\\":
                    j += 2
                    continue
                if text[j] == c or (c != "`" and text[j] == "\n"):
                    break
                j += 1
            end = min(j + 1, n)
            mark(i, end)
            i = end
        else:
            i += 1
    mask, pos = [], 0
    for line in lines:
        mask.append(flat[pos:pos + len(line)])
        pos += len(line) + 1
    return mask


def code_mask(text: str, lang: str) -> list[list[bool]]:
    lines = text.split("\n")
    return _py_mask(lines, text) if lang == "python" else _ts_mask(lines, text)


def depth_at_line_starts(lines, mask, lang) -> list[int]:
    opens = "([{" if lang == "python" else "(["
    closes = ")]}" if lang == "python" else ")]"
    depth, out = 0, []
    for line, m in zip(lines, mask):
        out.append(depth)
        for ch, masked in zip(line, m):
            if masked:
                continue
            if ch in opens:
                depth += 1
            elif ch in closes:
                depth -= 1
    return out


def _balanced(line: str, m: list[bool]) -> bool:
    d = 0
    for ch, masked in zip(line, m):
        if masked:
            continue
        if ch in "([{":
            d += 1
        elif ch in ")]}":
            d -= 1
            if d < 0:
                return False
    return d == 0


# --------------------------------------------------------------------------
# Operators
# --------------------------------------------------------------------------

def _op(name, pattern, repl, langs=("python", "typescript")):
    return (name, re.compile(pattern), repl, langs)


OPERATORS = [
    _op("eq-ne", r"(?<![=!<>])(===?)(?!=)", lambda m: "!" + m.group(1)[1:]),
    _op("ne-eq", r"(!==?)", lambda m: "=" + m.group(1)[1:]),
    _op("lt-le", r"(?<= )<(?= )", lambda m: "<="),
    _op("le-lt", r"(?<= )<=(?= )", lambda m: "<"),
    _op("gt-ge", r"(?<= )>(?= )", lambda m: ">="),
    _op("ge-gt", r"(?<= )>=(?= )", lambda m: ">"),
    _op("and-or", r"(?<=\s)and(?=\s)", lambda m: "or", ("python",)),
    _op("or-and", r"(?<=\s)or(?=\s)", lambda m: "and", ("python",)),
    _op("andand-oror", r"&&", lambda m: "||", ("typescript",)),
    _op("oror-andand", r"\|\|", lambda m: "&&", ("typescript",)),
    _op("not-remove", r"\bnot\s+", lambda m: "", ("python",)),
    _op("true-false", r"\bTrue\b", lambda m: "False", ("python",)),
    _op("false-true", r"\bFalse\b", lambda m: "True", ("python",)),
    _op("true-false", r"\btrue\b", lambda m: "false", ("typescript",)),
    _op("false-true", r"\bfalse\b", lambda m: "true", ("typescript",)),
    _op("plus1-minus1", r"(?<=\s)\+ 1\b(?![\d.])", lambda m: "- 1"),
    _op("minus1-plus1", r"(?<=\s)- 1\b(?![\d.])", lambda m: "+ 1"),
]

_RETURN = re.compile(r"^(\s*)return\s+(.+?)\s*;?\s*$")
_PY_ASSIGN = re.compile(r"^[A-Za-z_][\w.\[\]'\", ]*?\s*(?:[-+*/%|&^]|//|\*\*|<<|>>)?=(?!=)\s*\S")
_PY_CALL = re.compile(r"^[A-Za-z_][\w.]*\(.*\)$")
_TS_ASSIGN = re.compile(r"^[A-Za-z_$][\w$.\[\]'\"]*\s*[-+*/|&]?=(?!=)\s*\S")
_TS_CALL = re.compile(r"^(?:await\s+)?[A-Za-z_$][\w$.]*\(.*\);$")
# single-line conditions only: `if x:` / `} else if (x) {`
_PY_COND = re.compile(r"^(\s*)(if|elif|while) (.+?):\s*$")
_TS_COND = re.compile(r"^(\s*(?:\}\s*else\s+)?)(if|while) \((.+)\) \{\s*$")


def _first_word(s: str) -> str:
    m = re.match(r"[A-Za-z_$]+", s)
    return m.group(0) if m else ""


def _is_deletable(stripped: str, lang: str) -> bool:
    if lang == "python":
        if _first_word(stripped) in PY_KEYWORDS or stripped.startswith("@"):
            return False
        if stripped.endswith((":", ",", "\\", "(", "[", "{")):
            return False
        return bool(_PY_ASSIGN.match(stripped) or _PY_CALL.match(stripped))
    if _first_word(stripped) in TS_KEYWORDS or not stripped.endswith(";"):
        return False
    return bool(_TS_ASSIGN.match(stripped) or _TS_CALL.match(stripped))


def line_mutants(line: str, mask: list[bool], depth: int, lang: str):
    """Yield (operator, occurrence, mutated_line) for one source line."""
    stripped = line.strip()
    if not stripped:
        return
    first = len(line) - len(line.lstrip())
    if first < len(mask) and mask[first]:
        return  # comment or string-only line (docstring)
    for name, pattern, repl, langs in OPERATORS:
        if lang not in langs:
            continue
        occ = 0
        for m in pattern.finditer(line):
            if any(mask[m.start():m.end()]):
                continue
            occ += 1
            yield name, occ, line[:m.start()] + repl(m) + line[m.end():]
    if depth != 0:
        return
    m = _RETURN.match(line)
    if m and _balanced(line, mask) and m.group(2) not in ("None", "undefined") \
            and not stripped.endswith(("\\", ",")):
        if lang == "python":
            yield "return-none", 1, m.group(1) + "return None"
        else:
            semi = ";" if stripped.endswith(";") else ""
            yield "return-none", 1, m.group(1) + "return undefined" + semi
    m = (_PY_COND if lang == "python" else _TS_COND).match(line)
    if m and _balanced(m.group(3), mask[m.start(3):m.end(3)]) \
            and not any(mask[m.start(2):m.end(2)]):
        if lang == "python":
            yield "negate-cond", 1, f"{m.group(1)}{m.group(2)} not ({m.group(3)}):"
        else:
            yield "negate-cond", 1, f"{m.group(1)}{m.group(2)} (!({m.group(3)})) {{"
    if _balanced(line, mask) and _is_deletable(stripped, lang):
        indent = line[:first]
        yield "delete-stmt", 1, (indent + "pass") if lang == "python" else ""


def check_targets(task: Task, base_ws: Path) -> None:
    """Every target range must satisfy 1 <= start <= end <= line count of the
    reference-patched file."""
    targets = task.mutation.get("targets")
    if not targets:
        raise MutationError(f"{task.id}: no mutation targets configured")
    for target in targets:
        rel = target["file"]
        path = Path(base_ws) / rel
        if not path.is_file():
            raise MutationError(f"{task.id}: mutation target file {rel} not found")
        n = len(path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n"))
        if not target.get("lines"):
            raise MutationError(f"{task.id}: {rel}: no line ranges")
        for rng in target["lines"]:
            lo, hi = rng
            if not (isinstance(lo, int) and isinstance(hi, int) and 1 <= lo <= hi <= n):
                raise MutationError(f"{task.id}: {rel}: line range {list(rng)} is outside "
                                    f"1..{n} (the reference-patched file) or reversed")


def generate_mutants(task: Task, base_ws: Path) -> list[dict]:
    """First-order mutants for the task's target line ranges, in a stable order.
    Raises MutationError for a bad range, a range with no mutant, or fewer than
    MIN_MUTANTS_PER_TASK mutants in total."""
    check_targets(task, base_ws)
    seen: dict[str, dict] = {}
    for target in task.mutation["targets"]:
        rel = target["file"]
        text = (Path(base_ws) / rel).read_text(encoding="utf-8").replace("\r\n", "\n")
        lines = text.split("\n")
        mask = code_mask(text, task.language)
        depths = depth_at_line_starts(lines, mask, task.language)
        for lo, hi in target["lines"]:
            before = len(seen)
            for ln in range(lo, hi + 1):
                for op, occ, mutated in line_mutants(lines[ln - 1], mask[ln - 1],
                                                     depths[ln - 1], task.language):
                    if mutated == lines[ln - 1]:
                        continue
                    mid = f"{rel}:{ln}:{op}:{occ}"
                    seen.setdefault(mid, {"id": mid, "file": rel, "line": ln,
                                          "operator": op, "occurrence": occ,
                                          "original": lines[ln - 1], "mutated": mutated})
            if len(seen) == before:
                raise MutationError(f"{task.id}: {rel} lines {lo}-{hi} generate no mutants")
    if len(seen) < MIN_MUTANTS_PER_TASK:
        raise MutationError(f"{task.id}: only {len(seen)} mutants generated, "
                            f"need at least {MIN_MUTANTS_PER_TASK}")
    return list(seen.values())


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

def reference_patch(task: Task) -> Path:
    return task.patch_path("reference")


def run_mutant(task: Task, mutant: dict, keep: bool = False) -> dict:
    """Run the hidden tests (not the shell checks) on one mutant. Returns the grade
    dict; only verdict FAIL means the tests killed the mutant."""
    ws = make_workspace(task, reference_patch(task))
    try:
        path = ws / mutant["file"]
        lines = path.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
        lines[mutant["line"] - 1] = mutant["mutated"]
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(lines))
        return grade(task, ws, with_checks=False)
    finally:
        if not keep:
            remove_tree(ws)


def baseline_passes(task: Task) -> bool:
    ws = make_workspace(task, reference_patch(task))
    try:
        return grade(task, ws, with_checks=False)["verdict"] == "PASS"
    finally:
        remove_tree(ws)


def list_mutants(task: Task) -> list[dict]:
    ws = make_workspace(task, reference_patch(task))
    try:
        return generate_mutants(task, ws)
    finally:
        remove_tree(ws)


def justified(value) -> bool:
    """An equivalent-mutant entry needs a real reason: a string of >= 20 non-blank chars."""
    return isinstance(value, str) and len("".join(value.split())) >= MIN_JUSTIFICATION_CHARS


def evaluate(task: Task, mutants: list[dict], killed: dict[str, bool]) -> dict:
    all_eq = task.equivalent_mutants()
    eq = {k: v for k, v in all_eq.items() if justified(v)}
    unjust_entries = sorted(k for k, v in all_eq.items() if not justified(v))
    ids = {m["id"] for m in mutants}
    survivors = [m for m in mutants if not killed[m["id"]]]
    surv_ids = {m["id"] for m in survivors}
    unjustified = [m for m in survivors if m["id"] not in eq]
    stale = sorted(i for i in eq if i not in surv_ids)  # killed, or no such mutant
    n_killed = len(mutants) - len(survivors)
    n_equiv = len(survivors) - len(unjustified)
    denom = len(mutants) - n_equiv
    score = f"{100 * n_killed / denom:.1f}%" if denom else "n/a"
    return {"task": task.id, "mutants": len(mutants), "killed": n_killed,
            "equivalent": n_equiv, "unjustified": unjustified, "stale": stale,
            "score": score, "unknown": sorted(i for i in eq if i not in ids),
            "unjustified_entries": unjust_entries}


def render_table(summaries: list[dict]) -> str:
    lines = [HEADER, SEP]
    for s in summaries:
        lines.append(f"| {s['task']} | {s['mutants']} | {s['killed']} | {s['equivalent']} "
                     f"| {len(s['unjustified'])} | {s['score']} |")
    clean = sum(1 for s in summaries if not s["unjustified"] and not s["stale"]
                and not s["unjustified_entries"])
    lines += ["", f"**{clean}/{len(summaries)} tasks have no unjustified survivors "
                  "and no stale equivalent-mutant entries.**"]
    return "\n".join(lines)


def mutate_task(task: Task, workers: int = 4, keep: bool = False) -> dict:
    if not baseline_passes(task):
        raise MutationError(f"{task.id}: hidden tests do not PASS on the unmutated "
                            "reference solution")
    mutants = list_mutants(task)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda m: run_mutant(task, m, keep), mutants))
    errors = [(m["id"], r) for m, r in zip(mutants, results) if r["verdict"] == "ERROR"]
    if errors:
        detail = "; ".join(f"{mid} (runner exit {r['runner']['exit']}, junit_ok="
                           f"{r['runner']['junit_ok']}): {r['runner']['diag_tail'][-300:]!r}"
                           for mid, r in errors[:3])
        raise MutationInfraError(f"{task.id}: {len(errors)} mutant run(s) ended in ERROR "
                                 f"(infrastructure, not counted as killed): {detail}")
    killed = {m["id"]: r["verdict"] == "FAIL" for m, r in zip(mutants, results)}
    return evaluate(task, mutants, killed)


def select_tasks(tasks_dir: Path, only: str | None) -> list[Task]:
    """With the production tasks/ dir every corpus task needs a mutation config. Other
    dirs (test fixtures) just skip tasks that have none."""
    tasks = load_tasks(tasks_dir, only)
    if is_production_dir(tasks_dir):
        wanted = {t["id"] for t in load_corpus().get("tasks", [])}
        if only is None:
            absent = sorted(wanted - {t.id for t in tasks})
            if absent:
                raise MutationError(f"corpus tasks missing from tasks/: {absent}")
        for t in tasks:
            if not t.mutation.get("targets"):
                raise MutationError(f"{t.id}: no mutation config (task.json 'mutation')")
        return tasks
    return [t for t in tasks if t.mutation]


def main(argv=None) -> int:
    utf8_stdout()
    ap = argparse.ArgumentParser(prog="harness.mutate")
    ap.add_argument("--task")
    ap.add_argument("--tasks-dir", default=str(TASKS_DIR))
    ap.add_argument("--check-readme", action="store_true")
    ap.add_argument("--write-readme", action="store_true")
    ap.add_argument("--readme", default=str(readme.README))
    ap.add_argument("--list", action="store_true", help="print mutants, run nothing")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv)
    if (args.check_readme or args.write_readme) and args.task:
        print("--task cannot be combined with README flags", file=sys.stderr)
        return 2
    try:
        tasks = select_tasks(Path(args.tasks_dir), args.task)
    except (TaskError, MutationError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if args.list:
        for t in tasks:
            try:
                mutants = list_mutants(t)
            except MutationError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
            for m in mutants:
                print(f"{t.id}  {m['id']}  ->  {m['mutated'].strip() or '(line deleted)'}")
        return 0
    status, summaries = 0, []
    for t in tasks:
        try:
            s = mutate_task(t, keep=args.keep)
        except (MutationError, ApplyError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        summaries.append(s)
        for m in s["unjustified"]:
            print(f"SURVIVOR {m['id']}: {m['mutated'].strip() or '(line deleted)'}",
                  file=sys.stderr)
            status = 1
        for i in s["unjustified_entries"]:
            print(f"UNJUSTIFIED equivalent_mutants entry {t.id}: {i} (needs a string of at "
                  f"least {MIN_JUSTIFICATION_CHARS} non-blank characters)", file=sys.stderr)
            status = 1
        for i in s["stale"]:
            print(f"STALE equivalent_mutants entry {t.id}: {i} "
                  "(mutant is killed or no longer generated)", file=sys.stderr)
            status = 1
    table = render_table(summaries)
    print(table)
    if args.write_readme:
        readme.write_block(readme.MUTATION, table, Path(args.readme))
    elif args.check_readme:
        if not readme.check_block(readme.MUTATION, table, Path(args.readme)):
            print("README mutation table is out of date; run `make readme`", file=sys.stderr)
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
