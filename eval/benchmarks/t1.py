"""T1: structural benchmark (synthetic diffs, static-analysis ground truth).

Query categories
  deps    A function/method (first <= 40 lines) is rewritten.  Relevant: in-repo symbols it
          references inside the hunk (hop 1, grade 2) and their references (hop 2, grade 1).
          Only functions with >= 1 cross-file hop-1 reference are sampled.
  impact  A function's signature line changes.  Relevant: its callers (grade 2).
          Sampled among non-dunder functions with 1..30 callers, >= 1 in another file.

Ground truth comes from the FROZEN reference resolver (eval/reference).  T1 rewards
structural retrieval by construction, so it is used for regression gates and for
diagnosing pipeline loss, not for graph-vs-text claims (design doc §6.2, ADR-12).

Document keys are ``"path::name"`` (bare symbol name, which is the granularity of v1
nodes); file-level keys are ``"path"``.
"""
from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass, field
from pathlib import Path

from eval.reference import REFERENCE_VERSION, RefGraph

T1_VERSION = "1.0"
DEFAULT_SEED = 13
DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "t1"

VIEWS = {
    "deps": ("sym_xfile", "sym_all", "2hop_only", "graded_1+2hop", "file_xfile"),
    "impact": ("sym_xfile", "sym_all", "file_xfile"),
}


def sym_key(path: str, name: str) -> str:
    return f"{path}::{name}"


@dataclass
class Judgment:
    doc: str  # "path::name"
    hop: int  # 1 = direct (deps) / caller (impact); 2 = second hop (deps only)
    xfile: bool


@dataclass
class Query:
    qid: str
    corpus: str
    category: str  # deps | impact
    path: str
    qualname: str
    ws: int  # hunk start line (1-based, inclusive)
    we: int  # hunk end line
    judgments: list[Judgment] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.qualname.split(".")[-1]

    def grades(self, view: str) -> dict[str, int]:
        """Relevance judgments for one evaluation view."""
        j1 = [j for j in self.judgments if j.hop == 1]
        if view == "sym_all":
            return {j.doc: 2 for j in j1}
        if view == "sym_xfile":
            return {j.doc: 2 for j in j1 if j.xfile}
        if view == "2hop_only":
            return {j.doc: 1 for j in self.judgments if j.hop == 2}
        if view == "graded_1+2hop":
            g = {j.doc: 1 for j in self.judgments if j.hop == 2}
            g.update({j.doc: 2 for j in j1})
            return g
        if view == "file_xfile":
            return {j.doc.split("::")[0]: 1 for j in j1 if j.xfile}
        raise ValueError(view)

    def main_gt(self) -> list[str]:
        """Hop-1 symbol keys, cross-file first, each group sorted (deterministic oracle order)."""
        j1 = sorted(j.doc for j in self.judgments if j.hop == 1)
        return [d for d in j1 if not d.startswith(self.path + "::")] + [d for d in j1 if d.startswith(self.path + "::")]


def make_diff(path: str, lines: list[str], ws: int, we: int, kind: str) -> str:
    """A unified diff in GitHub's format for lines ws..we of ``path``."""
    body = lines[ws - 1 : we]
    n = len(body)
    out = [f"diff --git a/{path} b/{path}", "index 3f2a1b0..9c8d7e6 100644", f"--- a/{path}", f"+++ b/{path}"]
    out.append(f"@@ -{ws},{n} +{ws},{n} @@")
    if kind == "deps":
        out += [(" " if i < 2 else "+") + line for i, line in enumerate(body)]
    else:
        out += ["-" + body[0], "+" + body[0].rstrip() + "  # signature changed"] + [" " + line for line in body[1:]]
    return "\n".join(out) + "\n"


def _key(rg: RefGraph, sid) -> tuple[str, str]:
    s = rg.syms[sid]
    return s["file"], s["name"]


def sample(rg: RefGraph, rng: random.Random, n_deps: int = 40, n_impact: int = 25):
    """Candidate enumeration is in sorted symbol order; ``rng`` only chooses among candidates."""
    deps, impact = [], []
    for sid in sorted(rg.refs):
        s = rg.syms[sid]
        fn = s["node"]
        ws, we_full = fn.lineno, fn.end_lineno
        if we_full - ws < 3:
            continue
        we = min(we_full, ws + 39)
        one = {t for t, ln in rg.refs[sid] if ws <= ln <= we and t != sid}
        one_keys = {_key(rg, t) for t in one}
        if any(k[0] != s["file"] for k in one_keys):
            two = set()
            for t in one:
                kind = rg.syms[t]["kind"]
                if kind in ("function", "method"):
                    two |= {u for u, _ in rg.refs.get(t, [])}
                elif kind == "class":
                    two |= set(rg.classes[t]["bases"])
            two_keys = {_key(rg, u) for u in two} - one_keys - {_key(rg, sid)}
            deps.append((sid, ws, we, sorted(one_keys), sorted(two_keys)))
        callers = rg.callers.get(sid, set()) - {sid}
        ck = {_key(rg, c) for c in callers}
        if any(k[0] != s["file"] for k in ck) and len(ck) <= 30 and not s["name"].startswith("__"):
            impact.append((sid, ws, min(we_full, ws + 8), sorted(ck)))
    return rng.sample(deps, min(n_deps, len(deps))), rng.sample(impact, min(n_impact, len(impact)))


def generate(corpora: list[tuple[str, list[dict]]], seed: int = DEFAULT_SEED) -> list[Query]:
    """One shared RNG across corpora, in manifest order (matches the Phase-1 audit sampling)."""
    rng = random.Random(seed)
    queries: list[Query] = []
    for name, files in corpora:
        rg = RefGraph(files)
        deps, impact = sample(rg, rng)
        for i, (sid, ws, we, one, two) in enumerate(deps):
            path = sid[0]
            js = [Judgment(sym_key(*k), 1, k[0] != path) for k in one]
            js += [Judgment(sym_key(*k), 2, k[0] != path) for k in two]
            queries.append(Query(f"t1-{name}-deps-{i:03d}", name, "deps", path, sid[1], ws, we, js))
        for i, (sid, ws, we, callers) in enumerate(impact):
            path = sid[0]
            js = [Judgment(sym_key(*k), 1, k[0] != path) for k in callers]
            queries.append(Query(f"t1-{name}-impact-{i:03d}", name, "impact", path, sid[1], ws, we, js))
    return queries


def save(queries: list[Query], meta: dict, data_dir: Path = DATA_DIR) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    with open(data_dir / "queries.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for q in queries:
            fh.write(json.dumps(asdict(q), sort_keys=True) + "\n")
    meta = {"t1_version": T1_VERSION, "reference_version": REFERENCE_VERSION, **meta}
    (data_dir / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def load(data_dir: Path = DATA_DIR) -> tuple[list[Query], dict]:
    meta = json.loads((data_dir / "meta.json").read_text(encoding="utf-8"))
    queries = []
    with open(data_dir / "queries.jsonl", encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            d["judgments"] = [Judgment(**j) for j in d["judgments"]]
            queries.append(Query(**d))
    return queries, meta
