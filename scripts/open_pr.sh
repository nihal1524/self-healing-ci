#!/usr/bin/env bash
# Commit the fix heal.py made to self-healing/<branch>, push it, and open (or update) a PR into <branch>.
#
# Env:  HEAL_BRANCH       branch whose tests failed (the PR base)
#       HEAL_RESULT_FILE  JSON written by `heal.py --result-file`
#       GH_TOKEN          token for gh (contents: write, pull-requests: write)
# Test hooks: GH_BIN (default gh), HEAL_REMOTE (default origin)
set -euo pipefail

BRANCH="${HEAL_BRANCH:?HEAL_BRANCH is required}"
RESULT="${HEAL_RESULT_FILE:?HEAL_RESULT_FILE is required}"
GH="${GH_BIN:-gh}"
REMOTE="${HEAL_REMOTE:-origin}"
FIX_BRANCH="self-healing/${BRANCH}"

git config user.name  "${HEAL_GIT_NAME:-self-healing-ci[bot]}"
git config user.email "${HEAL_GIT_EMAIL:-self-healing-ci@users.noreply.github.com}"

# Only tracked files the model edited. heal.py's attemptN_M.patch files are untracked, so they stay out.
git add -u
if git diff --cached --quiet; then
  echo "No source changes to commit."
  exit 0
fi

BODY="$(mktemp)"
python3 - "$RESULT" "$BRANCH" > "$BODY" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
print(f"Automated fix proposed by self-healing-ci for failing tests on `{sys.argv[2]}`.\n")
print(f"**Root cause:** {r.get('root_cause') or 'n/a'}\n")
print(f"**Fix attempts used:** {r.get('fixes_used')}  |  **Edits that failed to apply:** {r.get('apply_failures')}")
for note in r.get("attempt_notes") or []:
    print(f"- {note}")
print(f"\nModel: `{r.get('model')}`. Review the diff before merging; the tests passed in a container after this change.")
PY
ROOT_CAUSE="$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('root_cause') or '')" "$RESULT")"

git checkout -q -B "$FIX_BRANCH"
git commit -q -m "self-healing: fix failing tests on ${BRANCH}" -m "${ROOT_CAUSE}"
git push --force "$REMOTE" "$FIX_BRANCH"   # bot-owned branch, safe to overwrite when a later run retries

EXISTING="$("$GH" pr list --head "$FIX_BRANCH" --state open --json number --jq '.[0].number // empty')"
if [ -n "$EXISTING" ]; then
  "$GH" pr edit "$EXISTING" --body-file "$BODY"
else
  "$GH" pr create --base "$BRANCH" --head "$FIX_BRANCH" \
    --title "self-healing: fix failing tests on ${BRANCH}" --body-file "$BODY"
fi

URL="$("$GH" pr view "$FIX_BRANCH" --json url --jq .url)"
echo "Pull request: ${URL}"
echo "url=${URL}" >> "${GITHUB_OUTPUT:-/dev/null}"
