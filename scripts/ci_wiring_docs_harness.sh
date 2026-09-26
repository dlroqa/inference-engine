#!/usr/bin/env bash
# Failure-path harness for scripts/ci_wiring_docs.sh (GitHub Actions only).
#
# Each case runs the real step script against a throwaway git repository in a
# temporary directory, with a stand-in generator, so the checkout running the
# harness is never modified. It checks that the exit status and the reported
# failure match, and that docs/dashboard.md is restored whenever restoring can
# succeed. It does not exercise the real generator (the normal CI step does).
set -u

here=$(cd "$(dirname "$0")" && pwd)
step="$here/ci_wiring_docs.sh"
work=$(mktemp -d)
trap 'chmod -R u+w "$work" 2>/dev/null; rm -rf "$work"' EXIT
fails=0

new_repo() {
  local repo="$work/$1"
  mkdir -p "$repo/docs" "$repo/dashboard"
  printf '# Doc\n\nstable\n' > "$repo/docs/dashboard.md"
  git -C "$repo" init -q
  git -C "$repo" -c user.email=h@example.invalid -c user.name=harness add docs/dashboard.md
  git -C "$repo" -c user.email=h@example.invalid -c user.name=harness commit -qm init
  echo "$repo"
}

# case <name> <expected exit: 0|nonzero> <expected summary text> <generator> [env...]
check() {
  local name=$1 want=$2 text=$3 gen=$4
  shift 4
  local repo out sum status
  repo=$(new_repo "$name")
  out="$work/$name-out"
  sum="$work/$name-summary.md"
  : > "$sum"
  env "$@" WIRING_DOCS_GEN="$gen" GITHUB_STEP_SUMMARY="$sum" bash "$step" "$repo" "$out" > "$work/$name.log" 2>&1
  status=$?
  local ok=1
  if [ "$want" = 0 ] && [ "$status" -ne 0 ]; then ok=0; fi
  if [ "$want" != 0 ] && [ "$status" -eq 0 ]; then ok=0; fi
  grep -qF -- "$text" "$sum" || ok=0
  if [ "${RESTORABLE:-1}" = 1 ]; then
    chmod -R u+w "$repo" 2>/dev/null
    git -C "$repo" diff --quiet -- docs/dashboard.md || { echo "  $name: document not restored"; ok=0; }
  fi
  if [ "$ok" = 1 ]; then
    echo "PASS  $name (exit $status)"
  else
    echo "FAIL  $name (exit $status, wanted $want)"
    sed 's/^/  | /' "$sum" "$work/$name.log"
    fails=$((fails + 1))
  fi
}

# A git that fails "checkout" only, to simulate a failed restore.
mkdir -p "$work/badgit"
real_git=$(command -v git)
cat > "$work/badgit/git" <<EOF
#!/usr/bin/env bash
for a in "\$@"; do [ "\$a" = checkout ] && { echo "simulated checkout failure" >&2; exit 7; }; done
exec "$real_git" "\$@"
EOF
chmod +x "$work/badgit/git"

check no-drift 0 "No drift" "true"
check drift nonzero "Drift:" "echo changed >> ../docs/dashboard.md"
check generator-fails-after-mutation nonzero "FAILED: generation (exit 3)" \
  "echo changed >> ../docs/dashboard.md; exit 3"
RESTORABLE=0 check restore-fails-without-drift nonzero "FAILED: restoring docs/dashboard.md" \
  "true" PATH="$work/badgit:$PATH"
RESTORABLE=0 check restore-fails-after-drift nonzero "FAILED: restoring docs/dashboard.md" \
  "echo changed >> ../docs/dashboard.md" PATH="$work/badgit:$PATH"
check truncated-display nonzero "**Truncated:**" \
  "head -c 5000 /dev/zero | tr '\\0' 'x' >> ../docs/dashboard.md" WIRING_DOCS_LIMIT=100

# The complete generated document is captured before the restore.
grep -q changed "$work/drift-out/dashboard.md" || { echo "FAIL  drift: artifact lacks the generated change"; fails=$((fails + 1)); }

# Missing dependencies are reported, not treated as "no drift".
repo=$(new_repo missing-deps)
if GITHUB_STEP_SUMMARY="$work/md.md" bash "$step" "$repo" "$work/md-out" > /dev/null 2>&1; then
  echo "FAIL  missing-deps: exited 0"
  fails=$((fails + 1))
elif grep -qF "dependencies are not installed" "$work/md.md"; then
  echo "PASS  missing-deps"
else
  echo "FAIL  missing-deps: no explanation"
  fails=$((fails + 1))
fi

if [ "$fails" -gt 0 ]; then
  echo "::error::$fails wiring-docs failure-path case(s) failed"
  exit 1
fi
echo "All wiring-docs failure-path cases passed."
