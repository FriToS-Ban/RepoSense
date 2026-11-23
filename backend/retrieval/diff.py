"""Unified-diff parsing (design §5.1).

Replaces v1's ``extract_diff_chunks``, which treated each file header as a query and
capped retrieval at 8 chunks.  Output is structural: files -> hunks -> numbered lines.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: list[tuple[str, int | None, int | None, str]] = field(default_factory=list)  # (tag, old_no, new_no, text)

    @property
    def added(self) -> list[tuple[int, str]]:
        return [(n, t) for tag, _, n, t in self.lines if tag == "+"]

    @property
    def removed(self) -> list[tuple[int, str]]:
        return [(o, t) for tag, o, _, t in self.lines if tag == "-"]


@dataclass
class FileDiff:
    old_path: str | None
    new_path: str | None
    status: str = "modified"  # added | deleted | renamed | modified
    binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def added_lines(self) -> set[int]:
        return {n for h in self.hunks for n, _ in h.added}

    @property
    def removed_lines(self) -> set[int]:
        return {o for h in self.hunks for o, _ in h.removed}

    def changed_text(self) -> list[str]:
        return [t for h in self.hunks for tag, _, _, t in h.lines if tag in "+-"]


def _strip(p: str) -> str | None:
    p = p.strip().split("\t")[0]
    if p == "/dev/null":
        return None
    return p[2:] if p[:2] in ("a/", "b/") else p


def parse_unified_diff(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    hunk: Hunk | None = None
    old_no = new_no = 0
    for raw in text.split("\n"):
        line = raw.rstrip("\r")
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            cur = FileDiff(m.group(1) if m else None, m.group(2) if m else None)
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            if line.startswith("--- "):  # diff without a `diff --git` header
                cur = FileDiff(_strip(line[4:]), None)
                files.append(cur)
            continue
        if hunk is not None and line == "" and old_no - hunk.old_start < hunk.old_len:
            line = " "  # some tools strip the single space of empty context lines
        if hunk is None or not line[:1] in (" ", "+", "-", "\\"):
            if line.startswith("new file mode"):
                cur.status = "added"
            elif line.startswith("deleted file mode"):
                cur.status = "deleted"
            elif line.startswith("rename from "):
                cur.old_path, cur.status = line[len("rename from "):], "renamed"
            elif line.startswith("rename to "):
                cur.new_path, cur.status = line[len("rename to "):], "renamed"
            elif line.startswith("Binary files") or line.startswith("GIT binary patch"):
                cur.binary = True
            elif line.startswith("--- ") and hunk is None:
                cur.old_path = _strip(line[4:])
                if cur.old_path is None:
                    cur.status = "added"
            elif line.startswith("+++ ") and hunk is None:
                cur.new_path = _strip(line[4:])
                if cur.new_path is None:
                    cur.status = "deleted"
            m = HUNK_RE.match(line)
            if m:
                hunk = Hunk(int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1))
                cur.hunks.append(hunk)
                old_no, new_no = hunk.old_start, hunk.new_start
            continue
        tag, body = line[:1], line[1:]
        if tag == "\\":  # "\ No newline at end of file"
            continue
        if tag == " ":
            hunk.lines.append((" ", old_no, new_no, body))
            old_no += 1
            new_no += 1
        elif tag == "+":
            hunk.lines.append(("+", None, new_no, body))
            new_no += 1
        elif tag == "-":
            hunk.lines.append(("-", old_no, None, body))
            old_no += 1
    return files
