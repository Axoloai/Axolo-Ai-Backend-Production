"""Copied-menu sites (plain HTML pages, each with its own copy of the menu): the agent edits ONE page, then our code
makes the same change in every other page, so the menu stays the same on every page.

Only two kinds of change are copied: lines added (the Team Portal item) and our own old Team Portal lines removed
(a move). A page gets the item only where the same neighbours are found exactly once; otherwise it is skipped and
reported. Shared-menu sites never use this.
"""
import difflib
import re
from pathlib import Path

from app.agent.prompt import LABEL

PAGE_EXT = {".html", ".htm"}
_SKIP = {".git", "node_modules", ".next", "dist", "build", ".vercel", ".netlify", ".cache", "out", "vendor"}
MAX_PAGES = 500
# what may differ between pages on the same menu line: the "this page" marker
_PAGE_MARK = re.compile(r"""\s+(class\s*=\s*"[^"]*"|class\s*=\s*'[^']*'|aria-current\s*=\s*"[^"]*")""")


def _loose(line: str) -> str:
    return re.sub(r"\s+", " ", _PAGE_MARK.sub("", line)).strip()


def _ours(line: str) -> bool:
    return LABEL in line or "axolo.ai" in line


def pages(repo_dir: Path) -> list[str]:
    root = Path(repo_dir)
    out = []
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        if set(rel.parts) & _SKIP or not p.is_file() or p.suffix.lower() not in PAGE_EXT:
            continue
        out.append(rel.as_posix())
        if len(out) >= MAX_PAGES:
            break
    return sorted(out)


CONTEXT = 6     # up to this many lines before and after the new item are used to find the same place


def _plan(old: list[str], new: list[str]):
    """The edit made on the first page, as (lines to remove, [(lines before, lines after, lines to insert)]) - or
    None if it is anything else than adding lines / removing our own Team Portal lines."""
    removes, inserts = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op in ("delete", "replace") and not all(_ours(x) or not x.strip() for x in old[i1:i2]):
            return None
        if op == "replace" and not all(_ours(x) or not x.strip() for x in new[j1:j2]):
            return None
        removes += [x for x in old[i1:i2] if x.strip()]
        if op in ("insert", "replace"):
            before = [_loose(x) for x in new[:j1] if x.strip() and not _ours(x)][-CONTEXT:]
            after = [_loose(x) for x in new[j2:] if x.strip() and not _ours(x)][:CONTEXT]
            if not before or not after:
                return None
            inserts.append((before, after, new[j1:j2]))
    return removes, inserts


def _spots(lines: list[str], before: list[str], after: list[str], k: int) -> list[int]:
    """Where (line index) the k lines before and k lines after match - the line the item goes in front of."""
    body = [i for i, ln in enumerate(lines) if ln.strip()]
    loose = [_loose(lines[i]) for i in body]
    b, a = before[-k:], after[:k]
    return [body[n] for n in range(len(b), len(body) - len(a) + 1)
            if loose[n - len(b):n] == b and loose[n:n + len(a)] == a]


def _apply(text: str, removes: list[str], inserts) -> str | None:
    lines = text.splitlines(keepends=True)
    nl = "\r\n" if "\r\n" in text else "\n"
    gone = {_loose(r) for r in removes}
    lines = [ln for ln in lines if not (_ours(ln) and _loose(ln) in gone)]
    for before, after, block in inserts:
        at = None
        for k in range(1, CONTEXT + 1):       # widen until it is ONE place (e.g. header vs footer both end "Contact")
            spots = _spots(lines, before, after, k)
            if not spots:
                break
            if len(spots) == 1:
                at = spots[0]
                break
        if at is None:
            return None                        # not found exactly once: don't guess
        lines[at:at] = [b.rstrip("\r\n") + nl for b in block]
    return "".join(lines)


def spread(repo_dir: Path, done_page: str, old_text: str) -> tuple[list[str], list[str]]:
    """Copy the change made in `done_page` (its old content: old_text) to every other page.
    Returns (pages updated, pages that need the agent's own Edit)."""
    root = Path(repo_dir)
    new_text = (root / done_page).read_text(encoding="utf-8", errors="replace")
    plan = _plan(old_text.splitlines(), new_text.splitlines())
    if plan is None:
        raise ValueError("Only adding the Team Portal item (or moving it) can be copied to the other pages.")
    removes, inserts = plan
    updated, skipped = [], []
    for rel in pages(root):
        if rel == done_page:
            continue
        p = root / rel
        text = p.read_text(encoding="utf-8", errors="replace")
        if not any(before[-1] in _loose(text) for before, _, _ in inserts) and not (
                removes and any(_ours(x) for x in text.splitlines())):
            continue                               # this page has no copy of that menu: leave it alone
        out = _apply(text, removes, inserts)
        if out is None:
            skipped.append(rel)
        elif out != text:
            p.write_text(out, encoding="utf-8", newline="")
            updated.append(rel)
    return updated, skipped
