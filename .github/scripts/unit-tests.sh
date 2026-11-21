#!/usr/bin/env bash
# The unit tests CI runs (docker.yml, sync-upstream.yml): CONTRIBUTING.md's list, so tests upstream adds there come along,
# plus this fork's own tests and a few upstream ones that are not on that list, when they exist.
set -euo pipefail
cd "$(dirname "$0")/../.."
mods=$(grep -o -m1 'python3 -B -m unittest [^`]*' CONTRIBUTING.md | sed 's/^python3 -B -m unittest //')
if [ -z "$mods" ]; then
  echo "No 'python3 -B -m unittest ...' line found in CONTRIBUTING.md" >&2
  exit 1
fi
for m in app.tests.test_web_service app.tests.test_map_checklist app.tests.test_encyclopedias app.tests.test_wiki_routes; do
  if [ -f "${m//.//}.py" ]; then
    case " $mods " in *" $m "*) ;; *) mods="$mods $m" ;; esac
  fi
done
echo "Running: $mods"
# shellcheck disable=SC2086  # one argument per module
exec python3 -B -m unittest $mods
