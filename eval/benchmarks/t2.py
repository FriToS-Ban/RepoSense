"""T2: history-based benchmark (real commits; ground truth not derived from static analysis).

For every pinned repository (eval/manifest/t2_repos.json):

  * candidate commits: non-merge commits changing 2-8 code .py files (docs/examples/scripts
    excluded), <= 600 changed lines, subject not a release/format/lint/merge/revert chore;
  * query   = the commit's diff of its *anchor* file (the non-test, modified file with the most
              changed lines) + the commit message as PR text; base snapshot = the parent commit;
  * judged  = symbols (classes/functions/methods; not modules) the same commit changed in its
              OTHER modified .py files that exist in the parent, tagged test / code;
  * split   = the most recent QUERIES_PER_REPO candidates (temporal test split).  Everything older
              is available as training history (co-change, future learning-to-rank) and nothing
              newer than the query is ever used for it.

Known biases (dataset card, design §6.2): co-change ground truth omits context that was only
*read* (e.g. a callee the author consulted), so it under-rewards dependency retrieval; tangled
commits add noise (bounded by the file and size filters).
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1]
MANIFEST = EVAL_DIR / "manifest" / "t2_repos.json"
REPO_CACHE = EVAL_DIR / ".cache" / "t2" / "repos"
DATA_DIR = EVAL_DIR / "data" / "t2"
T2_VERSION = "1.0"
QUERIES_PER_REPO = 50
MIN_FILES, MAX_FILES, MAX_LINES = 2, 8, 600
NON_CODE_TOP = {"docs", "doc", "examples", "example", "scripts", "benchmarks", "bench", ".github"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "site-packages"}
TEST_RE = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$")
CHORE_RE = re.compile(r"^\s*(bump|release|version|prepare|format|reformat|lint|style|typo|merge|revert|"
                      r"apply (black|ruff|isort)|run (black|ruff|isort)|pre-commit|update (copyright|changelog))",
                      re.I)


@dataclass
class Judgment:
    key: str  # path::qualname
    test: bool


@dataclass
class Query:
    qid: str
    repo: str
    sha: str
    parent: str
    time: int
    anchor: str
    message: str
    files_changed: list[str]
    judgments: list[Judgment] = field(default_factory=list)

    def grades(self, view: str) -> dict[str, int]:
        if view == "sym_all":
            return {j.key: 1 for j in self.judgments}
        if view == "sym_code":
            return {j.key: 1 for j in self.judgments if not j.test}
        if view == "sym_tests":
            return {j.key: 1 for j in self.judgments if j.test}
        if view == "file":
            return {j.key.split("::")[0]: 1 for j in self.judgments}
        raise ValueError(view)


VIEWS = ("sym_all", "sym_code", "sym_tests", "file")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True).stdout.decode("utf-8", "replace")


def git_bytes(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True, input=input_bytes).stdout


def is_code(path: str) -> bool:
    parts = path.split("/")
    return path.endswith(".py") and parts[0] not in NON_CODE_TOP and not any(p in SKIP_DIRS for p in parts[:-1])


def manifest() -> list[dict]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["repos"]


def repo_path(name: str) -> Path:
    return REPO_CACHE / name


def ensure_clone(spec: dict) -> Path:
    path = repo_path(spec["name"])
    if not path.exists():
        REPO_CACHE.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "--quiet", "--no-tags", spec["url"], str(path)], check=True)
    git(path, "cat-file", "-e", f"{spec['pin']}^{{commit}}")  # fails loudly if the pin is missing
    return path


def log_commits(repo: Path, pin: str) -> list[dict]:
    """All non-merge commits reachable from ``pin``, oldest first, with their changed files."""
    out = git(repo, "log", "--no-merges", "-M", "--name-status", "--format=%x1e%H%x1f%P%x1f%ct%x1f%s", pin)
    commits = []
    for rec in out.split("\x1e")[1:]:
        head, _, body = rec.partition("\n")
        sha, parents, ts, subject = head.split("\x1f")
        files = []
        for line in body.strip().split("\n"):
            if not line.strip():
                continue
            parts = line.split("\t")
            st = parts[0][0]
            path = parts[-1] if st == "R" else parts[1]
            old = parts[1] if st == "R" else None
            files.append((st, path, old))
        commits.append({"sha": sha, "parent": parents.split(" ")[0] if parents else "", "time": int(ts),
                        "subject": subject, "files": files})
    commits.reverse()
    return commits


def code_files(commit: dict) -> list[tuple[str, str, str | None]]:
    return [f for f in commit["files"] if is_code(f[1])]


def _symbols_touched(repo: Path, parent: str, sha: str, path: str) -> set[str]:
    """Qualnames (existing in the parent) whose span contains a changed line of ``path``."""
    from backend.kg.extract_python import extract
    from backend.retrieval.diff import parse_unified_diff

    base_text = git(repo, "show", f"{parent}:{path}")
    head_text = git(repo, "show", f"{sha}:{path}")
    base, head = extract(path, base_text), extract(path, head_text)
    base_q = {s.qualname for s in base.symbols if s.qualname}

    def innermost(ff, line):
        best, size = None, None
        for s in ff.symbols:
            if not s.qualname:
                continue
            for a, b in s.spans:
                if a <= line <= b and (best is None or b - a < size):
                    best, size = s.qualname, b - a
        return best

    out = set()
    for fd in parse_unified_diff(git(repo, "diff", "-U0", parent, sha, "--", path)):
        for o in fd.removed_lines:
            q = innermost(base, o)
            if q:
                out.add(q)
        for n in fd.added_lines:
            q = innermost(head, n)
            if q and q in base_q:
                out.add(q)
    return out


def mine(spec: dict, per_repo: int = QUERIES_PER_REPO) -> list[Query]:
    repo = ensure_clone(spec)
    commits = log_commits(repo, spec["pin"])
    queries: list[Query] = []
    for c in reversed(commits):  # newest first: temporal test split
        if len(queries) >= per_repo:
            break
        files = code_files(c)
        if not c["parent"] or not MIN_FILES <= len(files) <= MAX_FILES or CHORE_RE.match(c["subject"]):
            continue
        numstat = git(repo, "diff", "--numstat", c["parent"], c["sha"], "--", *[f[1] for f in files])
        changed = {}
        for line in numstat.strip().split("\n"):
            parts = line.split("\t")
            if len(parts) == 3 and parts[0] != "-":
                changed[parts[2]] = int(parts[0]) + int(parts[1])
        if sum(changed.values()) > MAX_LINES:
            continue
        modified = [p for st, p, _ in files if st == "M"]
        anchors = [p for p in modified if not TEST_RE.search(p) and p in changed]
        if not anchors:
            continue
        anchor = max(anchors, key=lambda p: (changed[p], p))
        js = []
        for p in sorted(modified):
            if p == anchor:
                continue
            for q in sorted(_symbols_touched(repo, c["parent"], c["sha"], p)):
                js.append(Judgment(f"{p}::{q}", bool(TEST_RE.search(p))))
        if not js:
            continue
        message = git(repo, "log", "-1", "--format=%B", c["sha"]).strip()[:1500]
        queries.append(Query(f"t2-{spec['name']}-{len(queries):03d}", spec["name"], c["sha"], c["parent"], c["time"],
                             anchor, message, sorted(p for _, p, _ in files), js))
    queries.sort(key=lambda q: q.time)
    return queries


def save(queries: list[Query], meta: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "queries.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for q in queries:
            fh.write(json.dumps(asdict(q), sort_keys=True) + "\n")
    (DATA_DIR / "meta.json").write_text(json.dumps({"t2_version": T2_VERSION, **meta}, indent=1, sort_keys=True) + "\n",
                                        encoding="utf-8")
    write_qrels(queries)


def write_qrels(queries: list[Query]) -> None:
    """TREC qrels (``qid 0 doc grade``) for the sym_all and file views, for cross-checking with trec_eval."""
    for view in ("sym_all", "file"):
        lines = [f"{q.qid} 0 {doc} {g}" for q in queries for doc, g in sorted(q.grades(view).items())]
        (DATA_DIR / f"qrels.{view}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def load() -> tuple[list[Query], dict]:
    meta = json.loads((DATA_DIR / "meta.json").read_text(encoding="utf-8"))
    qs = []
    with open(DATA_DIR / "queries.jsonl", encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            d["judgments"] = [Judgment(**j) for j in d["judgments"]]
            qs.append(Query(**d))
    return qs, meta


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(EVAL_DIR.parent))
    all_q, counts = [], {}
    for spec in manifest():
        qs = mine(spec)
        counts[spec["name"]] = {"pin": spec["pin"], "queries": len(qs),
                                "judged_symbols": sum(len(q.judgments) for q in qs),
                                "span_days": round((qs[-1].time - qs[0].time) / 86400) if qs else 0}
        all_q += qs
        print(spec["name"], counts[spec["name"]], flush=True)
    save(all_q, {"repos": counts, "queries_per_repo": QUERIES_PER_REPO})
    print("total", len(all_q))
