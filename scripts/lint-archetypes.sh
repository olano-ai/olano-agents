#!/usr/bin/env bash
#
# lint-archetypes.sh — Validate generated Olano archetype JSON5 files.
#
# Checks every .json5 file in agent category dirs and teams/:
#   1. Valid JSON (parseable)
#   2. Required fields present: name, label, title, description, system_prompt, tools, model
#   3. from_archetype references in team files resolve against the specialist catalog
#
# Usage:
#   ./scripts/lint-archetypes.sh [file ...]
#
# Exit code: 0 = PASSED, 1 = errors found.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

AGENT_DIRS=(
  academic design engineering finance game-development marketing paid-media product
  project-management sales spatial-computing specialized strategy support testing
)

REQUIRED_FIELDS=("name" "label" "title" "description" "system_prompt" "tools" "model")

errors=0
warnings=0
checked=0

# ── JSON5 parse + field check via Python ──────────────────────────────────────

check_json5() {
  local file="$1"
  local result
  result=$(python3 - "$file" <<'PYEOF'
import sys, json, pathlib

f = sys.argv[1]
text = pathlib.Path(f).read_text(encoding="utf-8")

# Minimal comment stripper (same logic as installer.py fallback)
import re
out = []
i, n = 0, len(text)
while i < n:
    c = text[i]
    if c in ('"', "'"):
        quote = c
        out.append(c); i += 1
        while i < n:
            c = text[i]; out.append(c)
            if c == '\\':
                i += 1
                if i < n: out.append(text[i])
            elif c == quote:
                break
            i += 1
    elif c == '/' and i+1 < n and text[i+1] == '/':
        while i < n and text[i] != '\n': i += 1
        continue
    elif c == '/' and i+1 < n and text[i+1] == '*':
        i += 2
        while i < n-1 and not (text[i] == '*' and text[i+1] == '/'): i += 1
        i += 2; continue
    else:
        out.append(c)
    i += 1

stripped = ''.join(out)
stripped = re.sub(r',\s*([}\]])', r'\1', stripped)

try:
    d = json.loads(stripped)
except json.JSONDecodeError as e:
    print(f"PARSE_ERROR:{e}")
    sys.exit(0)

required = ["name", "label", "title", "description", "system_prompt", "tools", "model"]
missing = [k for k in required if k not in d]
if missing:
    print(f"MISSING_FIELDS:{','.join(missing)}")
    sys.exit(0)

# The model must be a deployment default alias (the pack standard: experts on
# default:turbo, team supervisors on default:pro) or a concrete provider:model.
# The aliases resolve per deployment at build time, which is what lets one
# config.yaml edit retarget every installed archetype — a bare model name or a
# typo'd alias here would fail on every box that installs the file.
def _valid_model(value):
    if value in ("default:turbo", "default:pro", "default:max"):
        return True
    if value.startswith("default:"):
        return False  # unknown alias tier — fails at build on every install
    import re as _re
    return bool(_re.match(r"^[a-z0-9_-]+:[A-Za-z0-9 ._/:-]+$", value))

model_value = d.get("model")
if not isinstance(model_value, str) or not _valid_model(model_value):
    print(f"BAD_MODEL:{model_value}")
    sys.exit(0)

# Per-member overrides may pin models too — hold them to the same contract.
for sub in d.get("subagents", []):
    if isinstance(sub, dict) and isinstance(sub.get("model"), str) and not _valid_model(sub["model"]):
        print(f"BAD_MODEL:{sub['model']}")
        sys.exit(0)

# Collect from_archetype references
refs = []
for sub in d.get("subagents", []):
    if isinstance(sub, dict) and sub.get("from_archetype"):
        refs.append(sub["from_archetype"])

print(f"OK:{d['name']}:{','.join(refs)}")
PYEOF
  )
  echo "$result"
}

# Build a catalog of all known specialist names (json5 stems in category dirs)
# Store as newline-separated list (bash 3.2 compatible — no associative arrays)
SPECIALIST_CATALOG_FILE="$(mktemp)"
trap 'rm -f "$SPECIALIST_CATALOG_FILE"' EXIT

for dir in "${AGENT_DIRS[@]}"; do
  dirpath="$REPO_ROOT/$dir"
  [[ -d "$dirpath" ]] || continue
  find "$dirpath" -name "*.json5" -type f | while IFS= read -r f; do
    basename "$f" .json5
  done
done >> "$SPECIALIST_CATALOG_FILE"

specialist_exists() {
  grep -qFx "$1" "$SPECIALIST_CATALOG_FILE"
}

# ── Lint a single .json5 file ─────────────────────────────────────────────────

lint_json5() {
  local file="$1"
  local rel="${file#$REPO_ROOT/}"
  local result

  result="$(check_json5 "$file")"

  if [[ "$result" == PARSE_ERROR:* ]]; then
    echo "ERROR $rel: ${result#PARSE_ERROR:}"
    (( errors++ )) || true
    return
  fi

  if [[ "$result" == MISSING_FIELDS:* ]]; then
    echo "ERROR $rel: missing required fields: ${result#MISSING_FIELDS:}"
    (( errors++ )) || true
    return
  fi

  if [[ "$result" == BAD_MODEL:* ]]; then
    echo "ERROR $rel: invalid model '${result#BAD_MODEL:}' — use default:turbo|pro|max or provider:model"
    (( errors++ )) || true
    return
  fi

  if [[ "$result" == OK:* ]]; then
    local remainder="${result#OK:}"
    local name="${remainder%%:*}"
    local refs_str="${remainder#*:}"

    # Check that file stem matches the name field
    local stem
    stem="$(basename "$file" .json5)"
    if [[ "$stem" != "$name" ]]; then
      echo "WARN  $rel: file stem '$stem' does not match name field '$name'"
      (( warnings++ )) || true
    fi

    # Validate from_archetype references
    if [[ -n "$refs_str" ]]; then
      IFS=',' read -ra refs <<< "$refs_str"
      for ref in "${refs[@]}"; do
        [[ -z "$ref" ]] && continue
        if ! specialist_exists "$ref"; then
          echo "ERROR $rel: from_archetype '$ref' not found in specialist catalog"
          (( errors++ )) || true
        fi
      done
    fi

    (( checked++ )) || true
    return
  fi

  echo "ERROR $rel: unexpected lint output: $result"
  (( errors++ )) || true
}

# ── Collect files ─────────────────────────────────────────────────────────────

files=()
if [[ $# -gt 0 ]]; then
  files=("$@")
else
  for dir in "${AGENT_DIRS[@]}"; do
    dirpath="$REPO_ROOT/$dir"
    [[ -d "$dirpath" ]] || continue
    while IFS= read -r f; do
      files+=("$f")
    done < <(find "$dirpath" -name "*.json5" -type f | sort)
  done
  teams_dir="$REPO_ROOT/teams"
  if [[ -d "$teams_dir" ]]; then
    while IFS= read -r f; do
      files+=("$f")
    done < <(find "$teams_dir" -name "*.json5" -type f | sort)
  fi
fi

if [[ ${#files[@]} -eq 0 ]]; then
  echo "No .json5 archetype files found."
  exit 1
fi

echo "Linting ${#files[@]} archetype JSON5 files..."
echo ""

for f in "${files[@]}"; do
  lint_json5 "$f"
done

echo ""
echo "Results: $errors error(s), $warnings warning(s) in ${#files[@]} files (${checked} OK)."

if [[ $errors -gt 0 ]]; then
  echo "FAILED."
  exit 1
else
  echo "PASSED."
  exit 0
fi
