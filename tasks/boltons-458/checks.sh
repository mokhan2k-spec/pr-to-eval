#!/usr/bin/env bash
# Usage: bash checks.sh <workspace> <pristine-upstream-dir>
# One line per check: "PASS <name>" or "FAIL <name>: <reason>". Exit 1 on any FAIL.
# No network needed.
ws="${1:?usage: checks.sh <workspace> <pristine>}"
pristine="${2:?usage: checks.sh <workspace> <pristine>}"
ws="$(cd "$ws" && pwd)"
pristine="$(cd "$pristine" && pwd)"
rc=0

PY=python
command -v "$PY" >/dev/null 2>&1 || PY=python3

# --- changed files (relative paths), ignoring hidden tests, caches, junit ---
changed_files() {
    {
        (cd "$ws" && find . -type f \
            -not -path './hidden_tests/*' -not -path './.git/*' \
            -not -path '*/__pycache__/*' -not -path '*/.pytest_cache/*' \
            -not -name '*.pyc' -not -name 'junit*.xml' -not -name 'TASK.md' | sed 's|^\./||')
        (cd "$pristine" && find . -type f -not -path '*/__pycache__/*' -not -name '*.pyc' | sed 's|^\./||')
    } | sort -u | while IFS= read -r f; do
        if [ ! -f "$ws/$f" ] || [ ! -f "$pristine/$f" ] || ! cmp -s "$ws/$f" "$pristine/$f"; then
            echo "$f"
        fi
    done
}

changed="$(changed_files)"

# --- scope ---
bad=""
for f in $changed; do
    case "$f" in
        boltons/*|tests/*) ;;
        *) bad="$bad $f" ;;
    esac
done
if [ -z "$bad" ]; then
    echo "PASS scope"
else
    echo "FAIL scope: changes outside boltons/ and tests/:$bad"
    rc=1
fi

# --- import ---
if out="$(cd "$ws" && PYTHONPATH="$ws" PYTHONDONTWRITEBYTECODE=1 "$PY" -c 'import boltons.dictutils, boltons.urlutils' 2>&1)"; then
    echo "PASS import"
else
    echo "FAIL import: $(echo "$out" | tail -n 1)"
    rc=1
fi

# --- no-new-deps ---
# Every changed or added .py file under boltons/ or tests/ (what TASK.md allows) is scanned.
# A newly added import is fine if it is stdlib or resolves to a module in the workspace
# (relative imports are resolved against the package, so a new helper file is internal).
pyfiles=""
for f in $changed; do
    case "$f" in
        boltons/*.py|tests/*.py) [ -f "$ws/$f" ] && pyfiles="$pyfiles $f" ;;
    esac
done
res="$("$PY" - "$ws" "$pristine" $pyfiles <<'PYEOF'
import ast, os, sys
ws, pristine, *files = sys.argv[1:]
stdlib = set(sys.stdlib_module_names)
TEST_OK = {'pytest', 'tests', 'boltons'}


def imports(path):
    """set of (level, module, name) for every import statement in a file."""
    try:
        tree = ast.parse(open(path, encoding='utf-8').read())
    except (OSError, SyntaxError):
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {(0, a.name, None) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            out |= {(node.level, node.module or '', a.name) for a in node.names}
    return out


def exists(base, parts):
    p = os.path.join(base, *parts)
    return os.path.isfile(p + '.py') or os.path.isdir(p)


bad = []
for f in files:
    old = imports(os.path.join(pristine, f))
    for level, mod, name in sorted(imports(os.path.join(ws, f)) - old,
                                   key=lambda t: (t[0], t[1], t[2] or '')):
        if level:
            base = os.path.dirname(f)
            for _ in range(level - 1):
                base = os.path.dirname(base)
            parts = mod.split('.') if mod else []
            root = os.path.join(ws, base)
            ok = exists(root, parts) if parts else os.path.isdir(root)
            if not ok:
                bad.append(f"{f}: unresolved relative import {'.' * level}{mod}")
            continue
        top = mod.split('.')[0]
        if top in stdlib or top == 'boltons':
            continue
        if f.startswith('tests/') and top in TEST_OK:
            continue
        if exists(ws, [top]):
            continue
        bad.append(f'{f}: {top}')
print('; '.join(bad))
PYEOF
)"
if [ -z "$res" ]; then
    echo "PASS no-new-deps"
else
    echo "FAIL no-new-deps: new non-stdlib imports: $res"
    rc=1
fi

exit $rc
