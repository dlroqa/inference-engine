#!/usr/bin/env bash
# Regenerates the UI -> subsystem -> endpoint table in docs/dashboard.md,
# captures the complete result, restores the tracked file, and reports drift.
# Runs in GitHub Actions only (the generator executes product code).
#
#   scripts/ci_wiring_docs.sh <repo-root> <out-dir>
#
# Writes <out-dir>/dashboard.md (the complete generated document) and
# <out-dir>/wiring-docs.diff (the complete diff). The step summary shows the
# diff bounded to WIRING_DOCS_LIMIT bytes (default 60000) with an explicit
# truncation notice.
#
# Exit status is non-zero if any of these failed, each reported by name:
# generation, capturing the generated document, the diff, restoring the
# tracked file, or if the document drifted. A successful restore never hides
# an earlier failure, and a failed restore fails the step even when everything
# else passed. Only docs/dashboard.md is restored; nothing else is reset. An
# EXIT trap restores it on unexpected exits too (not on a forced runner kill).
#
# WIRING_DOCS_GEN overrides the generator command (run in <repo-root>/dashboard);
# the failure-path harness uses it with a throwaway repository.
set -u

root=${1:?repo root}
out=${2:?output dir}
doc="docs/dashboard.md"
limit=${WIRING_DOCS_LIMIT:-60000}
summary=${GITHUB_STEP_SUMMARY:-/dev/null}
gen_cmd=${WIRING_DOCS_GEN:-"npm run docs:wiring"}

say() { printf '%s\n' "$*" | tee -a "$summary"; }

restored=0
restore() {
  [ "$restored" = 1 ] && return "${restore_status:-0}"
  restored=1
  git -C "$root" checkout -- "$doc"
  restore_status=$?
  return "$restore_status"
}
on_exit() {
  local status=$?
  trap - EXIT
  if ! restore; then
    echo "::error::Restoring $doc failed."
    [ "$status" -eq 0 ] && status=1
  fi
  exit "$status"
}

say "### Wiring docs (generated)"
if ! mkdir -p "$out"; then
  say "FAILED: could not create the output directory; nothing was generated."
  exit 1
fi
if [ -z "${WIRING_DOCS_GEN:-}" ] && [ ! -d "$root/dashboard/node_modules" ]; then
  echo "::error::Wiring docs not generated: dashboard dependencies are not installed (an earlier step failed)."
  say "FAILED: not generated, because the dashboard dependencies are not installed."
  exit 1
fi

# From here on the tracked document may be modified: restore it on any exit.
trap on_exit EXIT

(cd "$root/dashboard" && eval "$gen_cmd")
gen=$?
cp "$root/$doc" "$out/dashboard.md"
copy=$?
git -C "$root" diff --no-color --exit-code -- "$doc" > "$out/wiring-docs.diff"
diff_status=$? # 0: no drift, 1: drift, other: git failed
restore
rs=$?

failed=()
[ "$gen" -ne 0 ] && failed+=("generation (exit $gen)")
if [ "$copy" -ne 0 ]; then
  failed+=("capturing the generated document")
  rm -f "$out/dashboard.md" # never publish an incomplete document as complete
fi
[ "$diff_status" -gt 1 ] && failed+=("git diff (exit $diff_status)")
[ "$rs" -ne 0 ] && failed+=("restoring $doc (exit $rs)")

size=$(wc -c < "$out/wiring-docs.diff" 2>/dev/null || echo 0)
if [ "$diff_status" -eq 1 ]; then
  say "Drift: $size bytes of diff. The complete generated document is in the \`wiring-docs\` artifact (dashboard.md)."
  if [ "$size" -gt "$limit" ]; then
    say "**Truncated:** showing the first $limit of $size bytes; the complete diff is in the artifact."
  fi
  {
    echo '```diff'
    head -c "$limit" "$out/wiring-docs.diff"
    echo
    echo '```'
  } | tee -a "$summary"
fi

if [ "${#failed[@]}" -gt 0 ]; then
  for f in "${failed[@]}"; do
    echo "::error::Wiring docs: $f failed."
    say "FAILED: $f."
  done
  exit 1
fi
if [ "$diff_status" -eq 1 ]; then
  exit 1
fi
say "No drift: docs/dashboard.md matches the registry (generated, compared and restored)."
exit 0
