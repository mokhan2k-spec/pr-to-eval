#!/usr/bin/env bash
# usage: checks.sh <workspace> <pristine-upstream-dir>
ws="$1"; pristine="$2"
fail=0
pass() { echo "PASS $1"; }
bad() { echo "FAIL $1: $2"; fail=1; }

# scope: only files under more_itertools/ or tests/ may differ from pristine
changed=$(diff -rq \
  -x hidden_tests -x __pycache__ -x .pytest_cache -x '*.pyc' -x 'junit*.xml' \
  "$pristine" "$ws" 2>&1)
offending=""
while IFS= read -r line; do
  [ -z "$line" ] && continue
  case "$line" in
    "Files "*" and "*" differ")
      f=${line#Files }; f=${f%% and *}; rel=${f#"$pristine"/} ;;
    "Only in "*)
      d=${line#Only in }; d=${d%%: *}; n=${line##*: }
      rel="${d#"$pristine"}"; rel="${rel#"$ws"}"; rel="${rel#/}"
      rel="${rel:+$rel/}$n" ;;
    *) rel="$line" ;;
  esac
  case "$rel" in
    more_itertools/*|tests/*) ;;
    *) offending="$offending $rel" ;;
  esac
done <<< "$changed"
if [ -z "$offending" ]; then pass scope; else bad scope "unexpected changes:$offending"; fi

# len-api: len() of a huge range still raises OverflowError, normal len unchanged
if (cd "$ws" && PYTHONPATH="$ws" python -c "
from more_itertools import numeric_range
try:
    len(numeric_range(0, 10**30))
except OverflowError:
    pass
else:
    raise SystemExit('len did not raise OverflowError')
assert len(numeric_range(0, 10)) == 10
" >/dev/null 2>&1); then pass len-api; else bad len-api "len() contract changed"; fi

# import: the package imports
if (cd "$ws" && PYTHONPATH="$ws" python -c "import more_itertools" >/dev/null 2>&1); then pass import; else bad import "import more_itertools failed"; fi

exit $fail
