#!/usr/bin/env python3
"""
build-archetypes.py — Convert olano-agents .md personas into Olano specialist archetype JSON5 files.

For each <category>/<name>.md (with YAML frontmatter), emits a sibling
<category>/<name>.json5 that Olano's archetype loader picks up automatically.

Usage:
    python scripts/build-archetypes.py [--dry-run] [--force] [--audit] [<path>...]

Options:
    --dry-run   Print what would be written without writing anything.
    --force     Overwrite existing .json5 files (default: skip if present).
    --audit     Print per-archetype tool inference lines to stdout.
    <path>...   Specific .md files or directories to convert (default: all agent dirs).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# ── Dependencies ──────────────────────────────────────────────────────────────
try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install pyyaml")

REPO_ROOT = Path(__file__).parent.parent.resolve()

AGENT_DIRS = [
    "academic",
    "design",
    "engineering",
    "finance",
    "game-development",
    "marketing",
    "paid-media",
    "product",
    "project-management",
    "sales",
    "spatial-computing",
    "specialized",
    "strategy",
    "support",
    "testing",
]

# ── Color map ─────────────────────────────────────────────────────────────────
# CSS color names that appear in frontmatter → hex. Copied from convert.sh,
# extended. Hex colors pass through unchanged.
COLOR_MAP: dict[str, str] = {
    "cyan":          "#06B6D4",
    "blue":          "#3B82F6",
    "green":         "#22C55E",
    "red":           "#EF4444",
    "purple":        "#A855F7",
    "orange":        "#F97316",
    "teal":          "#14B8A6",
    "indigo":        "#6366F1",
    "pink":          "#EC4899",
    "gold":          "#EAB308",
    "amber":         "#F59E0B",
    "yellow":        "#EAB308",
    "violet":        "#8B5CF6",
    "rose":          "#F43F5E",
    "lime":          "#84CC16",
    "gray":          "#6B7280",
    "grey":          "#6B7280",
    "fuchsia":       "#D946EF",
    "magenta":       "#D946EF",
    "neon-green":    "#10B981",
    "neon-cyan":     "#06B6D4",
    "metallic-blue": "#3B82F6",
    "dark-blue":     "#1D4ED8",
    "light-blue":    "#60A5FA",
    "emerald":       "#10B981",
    "sky":           "#0EA5E9",
    "slate":         "#64748B",
    "zinc":          "#71717A",
    "stone":         "#78716C",
    "black":         "#1F2937",
    "white":         "#F9FAFB",
    "brown":         "#92400E",
}

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{3,8}$")


def resolve_color(raw: str | None) -> str:
    if not raw:
        return "#6B7280"
    raw = raw.strip()
    if raw.startswith('"') or raw.startswith("'"):
        raw = raw.strip('"').strip("'")
    if _HEX_RE.match(raw):
        # Normalise to uppercase #RRGGBB
        h = raw.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return f"#{h.upper()[:6]}"
    return COLOR_MAP.get(raw.lower(), "#6B7280")


# ── Platform-tool → Olano-tool mapping ────────────────────────────────────
# Other converters (gemini, opencode, qwen) use generic names; map them to
# Olano's tool registry.
PLATFORM_TOOL_MAP: dict[str, str | None] = {
    # generic → olano
    "webfetch":    "http_request",
    "websearch":   "web_search",
    "web_search":  "web_search",
    "web_fetch":   "http_request",
    "http_request": "http_request",
    "bash":        "safe_shell",
    "shell":       "safe_shell",
    "safe_shell":  "safe_shell",
    "read":        None,    # file access via backend, no explicit tool needed
    "write":       None,
    "edit":        None,
    "multiedit":   None,
    "glob":        None,
    "grep":        None,
    "ls":          None,
    "todowrite":   None,
    # olano platform-specific kept as-is
    "ask_user":    "ask_user",
    "talk_to_agent": "talk_to_agent",
    "github_list_repos": "github_list_repos",
    "github_create_issue": "github_create_issue",
    "github_list_issues": "github_list_issues",
    "github_create_pr":   "github_create_pr",
    "github_get_file":    "github_get_file",
    "github_list_prs":    "github_list_prs",
    "github_search_code": "github_search_code",
}

KNOWN_OLANO_TOOLS = {
    "safe_shell", "web_search", "http_request",
    "ask_user", "talk_to_agent",
    "github_list_repos", "github_create_issue", "github_list_issues",
    "github_create_pr", "github_get_file", "github_list_prs", "github_search_code",
    "alpaca_get_account", "alpaca_list_positions", "alpaca_place_order",
    "alpaca_get_portfolio_history", "alpaca_list_orders",
}


def map_tools_from_frontmatter(raw_tools: str) -> list[str]:
    """Convert a comma-separated tools string from frontmatter to Olano tools."""
    result: list[str] = []
    seen: set[str] = set()
    for t in raw_tools.split(","):
        t = t.strip()
        if not t:
            continue
        mapped = PLATFORM_TOOL_MAP.get(t.lower())
        if mapped is None:
            # Unmapped platform-specific tools: silently drop non-Olano ones
            if t.lower() in KNOWN_OLANO_TOOLS:
                if t not in seen:
                    result.append(t)
                    seen.add(t)
        elif mapped and mapped not in seen:
            result.append(mapped)
            seen.add(mapped)
    return result


# ── Tool inference ─────────────────────────────────────────────────────────────
# Each entry: (score, lionclaw_tool, [keywords])
# Text is checked case-insensitively. Highest score wins; threshold = 2.
_INFERENCE_RULES: list[tuple[int, str, list[str]]] = [
    (3, "safe_shell", [
        "deploy", "ci/cd", "pipeline", "devops", "infrastructure", "build system",
        "command line", "shell script", "bash script", "run command", "terminal",
        "docker", "kubernetes", "k8s", "helm", "terraform", "ansible",
        "package manager", "npm", "brew", "pip install", "git commit",
        "ssh", "scp", "rsync", "cron job", "systemd", "nginx", "apache",
        "server config", "cloud provider", "aws cli", "gcloud", "azure cli",
    ]),
    (2, "safe_shell", [
        "code review", "test suite", "unit test", "integration test",
        "linting", "formatter", "static analysis", "benchmark",
        "firmware", "embedded", "microcontroller",
    ]),
    (3, "web_search", [
        "market research", "competitive analysis", "seo", "keyword research",
        "trend analysis", "competitor", "industry report", "benchmarking",
        "news monitoring", "citation", "literature review", "academic search",
        "research", "data gathering", "information retrieval",
    ]),
    (2, "web_search", [
        "growth", "marketing", "content strategy", "social media",
        "audience", "lead generation", "brand awareness",
        "analytics", "metrics", "kpi",
    ]),
    (3, "http_request", [
        "api integration", "rest api", "graphql", "webhook",
        "http request", "api client", "third-party api",
        "stripe", "twilio", "sendgrid", "hubspot", "salesforce",
        "crm integration", "payment gateway", "auth0", "oauth",
    ]),
    (2, "http_request", [
        "integration", "data pipeline", "etl", "data ingestion",
        "notification", "alert", "reporting", "dashboard data",
    ]),
]


def infer_tools(description: str, vibe: str, body: str) -> tuple[list[str], str]:
    """
    Return (tools_list, audit_note) where audit_note describes what was inferred.
    """
    combined = " ".join([description, vibe, body]).lower()
    scores: dict[str, int] = {}
    for score, tool, keywords in _INFERENCE_RULES:
        for kw in keywords:
            if kw in combined:
                scores[tool] = scores.get(tool, 0) + score

    threshold = 2
    tools = [t for t, s in scores.items() if s >= threshold]

    # Always include web_search for research/strategy roles without a shell signal
    if not tools:
        tools = ["web_search"]
        note = "inferred: no strong signal → [web_search] (default)"
    else:
        note = f"inferred from content: {tools!r} (scores: {dict(sorted(scores.items(), key=lambda x: -x[1]))})"

    return tools, note


# ── Frontmatter parser ────────────────────────────────────────────────────────

def parse_md(path: Path) -> tuple[dict, str]:
    """Return (frontmatter_dict, body_text). Body does not include frontmatter."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}, text

    # Find closing ---
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text

    fm_text = text[3:end].strip()
    body = text[end + 4:].lstrip("\n")

    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError:
        fm = {}

    return fm, body


# ── JSON5 serialiser ──────────────────────────────────────────────────────────

def _to_json5(obj: object, indent: int = 2) -> str:
    """Produce a pretty-printed JSON string (valid JSON5 subset, no comments)."""
    return json.dumps(obj, indent=indent, ensure_ascii=False)


# ── Main conversion ───────────────────────────────────────────────────────────

def convert_file(
    md_path: Path,
    *,
    dry_run: bool = False,
    force: bool = False,
    audit: bool = False,
) -> str | None:
    """
    Convert one .md file to a sibling .json5.
    Returns the output path (str) on success, None if skipped.
    """
    out_path = md_path.with_suffix(".json5")
    if out_path.exists() and not force:
        return None  # already converted

    fm, body = parse_md(md_path)
    if not fm or not fm.get("name"):
        return None  # not an agent file

    # ── Identity fields ────────────────────────────────────────────────────
    stem = md_path.stem  # file stem = stable ID (already kebab-case)

    # Category: walk up from file to find which AGENT_DIRS entry is an ancestor
    category = md_path.parent.name  # default to immediate parent
    for part in md_path.parts:
        if part in AGENT_DIRS:
            category = part
            # Check if there's a meaningful subcategory below this top-level dir
            rel = md_path.relative_to(REPO_ROOT / part)
            if len(rel.parts) > 1:  # file is in a subdirectory
                category = part  # keep top-level as category
            break

    label: str = str(fm.get("name", stem))
    # The position the archetype holds ("Recruitment Specialist"). An
    # installed agent keeps it as its title even when a person names the
    # agent something else — frontmatter `title:` when authored, else the
    # human name, which is the job the persona was written for.
    title: str = str(fm.get("title") or label)
    description: str = str(fm.get("description", ""))
    emoji: str = str(fm.get("emoji", "🤖"))
    color: str = resolve_color(fm.get("color"))

    # ── Tool resolution (priority order) ──────────────────────────────────
    tool_note: str
    tools: list[str]

    if fm.get("lionclaw_tools"):
        # Explicit Olano tool list in frontmatter (highest priority)
        raw = str(fm["lionclaw_tools"])
        tools = [t.strip() for t in raw.split(",") if t.strip()]
        tool_note = f"explicit lionclaw_tools: {tools!r}"

    elif fm.get("tools"):
        # Platform tools field (some files have it from other converters)
        raw = str(fm["tools"])
        tools = map_tools_from_frontmatter(raw)
        tool_note = f"mapped from frontmatter tools '{raw}': {tools!r}"

    else:
        # Infer from content
        vibe = str(fm.get("vibe", ""))
        tools, tool_note = infer_tools(description, vibe, body)

    if audit:
        print(f"  {stem}: {tool_note}")

    # ── Assemble archetype dict ────────────────────────────────────────────
    archetype: dict = {
        "name":        stem,
        "label":       label,
        "title":       title,
        "description": description,
        "emoji":       emoji,
        "color":       color,
        "category":    category,

        "system_prompt": body.rstrip("\n"),

        # Deployment alias, not a concrete provider:model — the pack standard
        # since e92f514, and what lint-archetypes.sh enforces. A literal here
        # would fail the linter on regeneration and pin every install to one
        # provider regardless of what the deployment is configured for.
        "model":   "default:turbo",
        "tools":   tools,
        "subagents": [],
        "builtin_subagents": [],

        "memory": {
            "markdown":      True,
            "rag":           False,
            "self_learning": False,
        },
        "backend": {
            "type":         "filesystem",
            "root_dir":     "workspace",
            "virtual_mode": False,
        },
        "security": {
            "profile":     "safe",
            "trust_level": 1,
        },
        "pulse": {
            "enabled": False,
        },
    }

    json5_text = _to_json5(archetype)

    if not dry_run:
        out_path.write_text(json5_text + "\n", encoding="utf-8")

    return str(out_path)


def collect_md_files(paths: list[Path]) -> list[Path]:
    """Collect .md files from given paths (files or dirs). If empty, use AGENT_DIRS."""
    if not paths:
        result: list[Path] = []
        for d in AGENT_DIRS:
            dir_path = REPO_ROOT / d
            if dir_path.is_dir():
                result.extend(sorted(dir_path.rglob("*.md")))
        return result

    result = []
    for p in paths:
        if p.is_file() and p.suffix == ".md":
            result.append(p)
        elif p.is_dir():
            result.extend(sorted(p.rglob("*.md")))
    return result


def should_skip(path: Path) -> bool:
    """Return True for non-agent docs."""
    name = path.name.lower()
    if name in {"readme.md", "contributing.md", "contributing_zh-cn.md", "security.md"}:
        return True
    # Files without frontmatter (quick check: first char != '-')
    try:
        first = path.read_text(encoding="utf-8", errors="replace")[:3]
        return first != "---"
    except OSError:
        return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert olano-agents .md personas to Olano archetype JSON5.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("paths", nargs="*", type=Path, help="Specific files or dirs to convert")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without writing")
    parser.add_argument("--force", action="store_true", help="Overwrite existing .json5 files")
    parser.add_argument("--audit", action="store_true", help="Print tool inference audit lines")
    args = parser.parse_args()

    md_files = [f for f in collect_md_files(args.paths) if not should_skip(f)]

    converted = 0
    skipped = 0
    errors: list[tuple[Path, Exception]] = []

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Converting {len(md_files)} agent files...")
    if args.audit:
        print()

    for md in md_files:
        if args.audit:
            rel = md.relative_to(REPO_ROOT)
            print(f"{rel}:")
        try:
            out = convert_file(md, dry_run=args.dry_run, force=args.force, audit=args.audit)
        except Exception as exc:
            errors.append((md, exc))
            print(f"  ERROR {md.relative_to(REPO_ROOT)}: {exc}", file=sys.stderr)
            continue

        if out:
            converted += 1
            if args.dry_run or args.audit:
                rel_out = Path(out).relative_to(REPO_ROOT)
                print(f"  → {rel_out}")
        else:
            skipped += 1

    print()
    print(f"Done. {converted} written, {skipped} skipped (already exist; use --force to overwrite), {len(errors)} errors.")
    if errors:
        sys.exit(1)


if __name__ == "__main__":
    main()
