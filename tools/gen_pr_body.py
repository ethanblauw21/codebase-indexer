"""
Generate a pre-filled PR body from the current branch and its linked ADR.

Usage:
    python tools/gen_pr_body.py

Prints the filled PR template to stdout. Pipe to pbcopy / clip / gh pr create --body.
"""

import glob
import re
import subprocess
import sys
from pathlib import Path


# ── git helpers ──────────────────────────────────────────────────────────────

def git(*args) -> str:
    result = subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def current_branch() -> str:
    return git("symbolic-ref", "--short", "HEAD")


def diff_stat(base: str = "origin/master") -> str:
    return git("diff", "--stat", f"{base}...HEAD")


def changed_files(base: str = "origin/master") -> list[str]:
    out = git("diff", "--name-only", f"{base}...HEAD")
    return [f for f in out.splitlines() if f]


# ── ADR parsing ───────────────────────────────────────────────────────────────

def find_adr_file(adr_num: int) -> Path | None:
    pattern = f"docs/adr/ADR-{adr_num:03d}-*.md"
    matches = glob.glob(pattern)
    return Path(matches[0]) if matches else None


def parse_adr(path: Path) -> dict:
    """Extract Context first sentence, Decision body, and Implementation Log items."""
    text = path.read_text(encoding="utf-8", errors="replace")

    # Split into top-level sections (## headings)
    sections: dict[str, str] = {}
    current_key = "__preamble__"
    for line in text.splitlines():
        if re.match(r"^## ", line):
            current_key = line[3:].strip()
            sections[current_key] = ""
        else:
            sections[current_key] = sections.get(current_key, "") + line + "\n"

    # Context: first non-empty sentence
    context_body = sections.get("Context", "")
    context_sentence = ""
    for line in context_body.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            context_sentence = line.rstrip()
            # Trim at first sentence boundary
            m = re.match(r"([^.!?]+[.!?])", context_sentence)
            if m:
                context_sentence = m.group(1)
            break

    # Decision: full body, strip leading blank lines
    decision_body = sections.get("Decision", "").strip()

    # Implementation Log: parse checkboxes
    log_body = sections.get("Implementation Log", "")
    log_items = []
    for line in log_body.splitlines():
        m = re.match(r"^\s*-\s+\[([ xX])\]\s+(.+)$", line)
        if m:
            checked = m.group(1).strip().lower() in ("x",)
            log_items.append({"checked": checked, "text": m.group(2).strip()})

    return {
        "context_sentence": context_sentence,
        "decision_body": decision_body,
        "log_items": log_items,
    }


# ── heuristic: match log items to changed files ───────────────────────────────

def likely_closed(item_text: str, src_files: list[str]) -> bool:
    """Return True if any src/ basename appears in the log item text."""
    for f in src_files:
        basename = Path(f).name
        stem = Path(f).stem
        if basename in item_text or stem in item_text:
            return True
    return False


# ── template rendering ────────────────────────────────────────────────────────

def render_adr_pr(branch: str, adr_num: int, adr: dict, stat: str, files: list[str]) -> str:
    src_files = [f for f in files if f.startswith("src/")]
    is_major = bool(src_files)

    minor_check = "[ ]"
    major_check = "[x]" if is_major else "[ ]"

    adr_ref = f"docs/adr/ADR-{adr_num:03d}-*.md"
    adr_files = glob.glob(adr_ref)
    adr_link = adr_files[0] if adr_files else f"docs/adr/ADR-{adr_num:03d}-..."

    what_changed = adr["context_sentence"] or "<!-- describe what changed -->"

    decision_notes = adr["decision_body"] or "<!-- implementation notes -->"

    # Build implementation log delta
    log_lines = []
    for item in adr["log_items"]:
        if item["checked"]:
            mark = "- [x]"
        elif likely_closed(item["text"], src_files):
            mark = "- [~]"  # likely closed by this diff
        else:
            mark = "- [ ]"
        log_lines.append(f"{mark} {item['text']}")
    log_section = "\n".join(log_lines) if log_lines else "_No implementation log items found._"

    stat_block = f"\n```\n{stat}\n```" if stat else ""

    return f"""\
## What changed

{what_changed}
{stat_block}

## Type of change

- {minor_check} Minor — docs/comments/README only, no `src/` changes (no ADR needed)
- {major_check} Major — any `src/` change: new or modified MCP tool, retrieval logic, indexing pipeline, graph schema, TUI (ADR required)
- [ ] Bug fix — minor (isolated, no shared code, no issue needed)
- [ ] Bug fix — non-trivial (touches shared indexing/retrieval code, GitHub issue required)

## Links

| Type | Link |
|------|------|
| ADR | `{adr_link}` |
| Issue | N/A |
| Related PR | N/A |

## Implementation notes

{decision_notes}

## Checklist

- [ ] MCP server starts cleanly (`python src/MCPServer.py`)
- [ ] `reindex` completes without error on a test target
- [ ] Golden path manually tested for any changed tool or pipeline stage
- [ ] ADR Implementation Log updated (Major changes only)
- [ ] No leftover `print()` / debug output
- [ ] No `TODO` / `FIXME` without a linked issue

## ADR Implementation Log delta

_Items from ADR-{adr_num:03d} implementation log. `[~]` = likely closed by this diff (heuristic)._

{log_section}
"""


def render_minimal_pr(branch: str, stat: str) -> str:
    stat_block = f"\n```\n{stat}\n```" if stat else ""
    return f"""\
## What changed

<!-- One or two sentences. What does this PR do and why? -->
{stat_block}

## Type of change

- [ ] Minor — docs/comments/README only, no `src/` changes (no ADR needed)
- [ ] Major — any `src/` change: new or modified MCP tool, retrieval logic, indexing pipeline, graph schema, TUI (ADR required)
- [ ] Bug fix — minor (isolated, no shared code, no issue needed)
- [ ] Bug fix — non-trivial (touches shared indexing/retrieval code, GitHub issue required)

## Links

| Type | Link |
|------|------|
| ADR | N/A |
| Issue | N/A |
| Related PR | N/A |

## Implementation notes

<!-- Reviewer context, deviations from the ADR design, tradeoffs made. -->

## Checklist

- [ ] MCP server starts cleanly (`python src/MCPServer.py`)
- [ ] `reindex` completes without error on a test target
- [ ] Golden path manually tested for any changed tool or pipeline stage
- [ ] ADR Implementation Log updated (Major changes only)
- [ ] No leftover `print()` / debug output
- [ ] No `TODO` / `FIXME` without a linked issue
"""


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    branch = current_branch()
    if not branch:
        print("Could not determine current branch.", file=sys.stderr)
        return 1

    stat = diff_stat()
    files = changed_files()

    m = re.search(r"adr-(\d+)", branch, re.IGNORECASE)
    if not m:
        print(render_minimal_pr(branch, stat))
        return 0

    adr_num = int(m.group(1))
    adr_path = find_adr_file(adr_num)
    if not adr_path:
        print(
            f"Warning: ADR-{adr_num:03d} file not found; falling back to minimal template.",
            file=sys.stderr,
        )
        print(render_minimal_pr(branch, stat))
        return 0

    adr = parse_adr(adr_path)
    print(render_adr_pr(branch, adr_num, adr, stat, files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
