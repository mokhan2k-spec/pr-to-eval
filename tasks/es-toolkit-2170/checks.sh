#!/usr/bin/env bash
# Usage: bash checks.sh <workspace> <pristine-upstream-dir>
# One line per check: "PASS <name>" or "FAIL <name>: <reason>". Exit 1 on any FAIL. No network.
WS="$(cd "$1" && pwd)"
PRISTINE="$(cd "$2" && pwd)"
fail=0

# --- scope: only files under src/promise/ may differ from pristine
changed=$(diff -rq "$PRISTINE" "$WS" \
  -x hidden_tests -x node_modules -x 'junit*' -x '*.xml' -x .git 2>&1 \
  | sed -E 's/^Files .* and (.*) differ$/\1/; s/^Only in ([^:]*): (.*)$/\1\/\2/' )
bad=""
while IFS= read -r p; do
  [ -z "$p" ] && continue
  rel="${p#"$WS"/}"
  rel="${rel#"$PRISTINE"/}"
  case "$rel" in
    src/promise/*) ;;
    *) bad="$bad $rel" ;;
  esac
done <<< "$changed"
if [ -z "$bad" ]; then echo "PASS scope"; else echo "FAIL scope: changed outside src/promise/:$bad"; fail=1; fi

# --- typecheck
root="$WS"
while [ "$root" != "/" ] && [ ! -d "$root/node_modules/@types/node" ]; do root="$(dirname "$root")"; done
if [ ! -d "$root/node_modules/@types/node" ]; then
  echo "FAIL typecheck: no node_modules found above workspace"; fail=1
else
  tmp="$(mktemp -d)"
  # tsc may be a native Windows node: give it mixed-style paths when cygpath exists
  wp() { cygpath -m "$1" 2>/dev/null || echo "$1"; }
  wws="$(wp "$WS")"; wroot="$(wp "$root")"; wtmp="$(wp "$tmp")"
  cat > "$tmp/tsconfig.json" <<JSON
{
  "compilerOptions": {
    "strict": true,
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "allowImportingTsExtensions": true,
    "noEmit": true,
    "skipLibCheck": true,
    "types": ["node"],
    "typeRoots": ["$wroot/node_modules/@types"]
  },
  "include": ["$wws/src/**/*.ts"],
  "exclude": ["$wws/src/**/*.spec.ts"]
}
JSON
  if out=$(cd "$root" && npx tsc --noEmit -p "$wtmp/tsconfig.json" 2>&1); then
    echo "PASS typecheck"
  else
    echo "FAIL typecheck: $(echo "$out" | head -3 | tr '\n' ' ')"; fail=1
  fi
  rm -rf "$tmp"
fi

# --- signature: exported withTimeout declaration is unchanged (whitespace-insensitive)
sig() { tr -s ' \t\r\n' ' ' < "$1" | grep -oE 'export (async )?function withTimeout *<[^>]*> *\([^)]*(\([^)]*\)[^)]*)*\) *: *[^{]*' | tr -d ' '; }
a="$(sig "$PRISTINE/src/promise/withTimeout.ts")"
b="$(sig "$WS/src/promise/withTimeout.ts")"
if [ -n "$a" ] && [ "$a" = "$b" ]; then echo "PASS signature"; else echo "FAIL signature: withTimeout declaration changed or missing"; fail=1; fi

exit $fail
