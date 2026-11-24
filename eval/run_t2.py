"""Run v2 retrieval systems on T2 (history-based benchmark).

    python -m eval.run_t2                     # all systems -> eval/results/t2-v2/

For each query (oldest first, per repository):
  1. base snapshot = every code .py file at the parent commit (FileFacts cached by blob);
  2. the co-change index is fed every commit strictly older than the query commit (no leakage);
  3. each system builds a context from the real anchor diff (git diff parent..sha -- anchor),
     the HEAD content of the anchor, and the commit message as PR text;
  4. metrics: the packed context as a ranked list of path::qualname keys (a class shown in
     full covers its methods) against the judged symbols (views: sym_all, sym_code,
     sym_tests, file).

Output format matches eval.run, so systems compare with eval.compare (paired by qid).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from eval.benchmarks import t2
from eval.metrics import ir_metrics, mean_metrics
from eval.run import PER_QUERY_METRICS, _write_json
from eval.stats import bootstrap_ci

EVAL_DIR = Path(__file__).resolve().parent
MAX_FILE_BYTES = 1_000_000


def systems():
    from backend.retrieval.pipeline import RetrievalConfig

    return {
        "A": RetrievalConfig(channels=("lexical",)),
        "E": RetrievalConfig(channels=("structural",)),
        "E_no_overlay": RetrievalConfig(channels=("structural",), overlay=False),
        "C": RetrievalConfig(channels=("cochange",)),
        "EA": RetrievalConfig(channels=("structural", "lexical")),
        "EAC": RetrievalConfig(channels=("structural", "lexical", "cochange")),
        # diagnostic ablations (M4): query plan off; structural restricted to other files
        "E_noplan": RetrievalConfig(channels=("structural",), use_plan=False),
        "E_xfile": RetrievalConfig(channels=("structural",), structural_same_file=False),
        "EA_noplan": RetrievalConfig(channels=("structural", "lexical"), use_plan=False),
        # M5: multi-hop (alpha chosen on the T1 dev half; see docs/retrieval/results/M5.md)
        "E_ppr": RetrievalConfig(channels=("structural",), multihop="ppr"),
        "E_bfs": RetrievalConfig(channels=("structural",), multihop="bfs"),
        "P": RetrievalConfig(channels=("ppr",)),
        "EA_ppr": RetrievalConfig(channels=("structural", "lexical"), multihop="ppr"),
        "EA_bfs": RetrievalConfig(channels=("structural", "lexical"), multihop="bfs"),
        "PA": RetrievalConfig(channels=("ppr", "lexical")),
    }


def sweep_systems():
    """M5 restart-probability sweep, reported on T2 for transparency (alpha is chosen on T1-dev)."""
    from backend.retrieval.pipeline import RetrievalConfig

    out = {}
    for a in (0.1, 0.2, 0.3, 0.5, 0.7):
        out[f"P_a{a}"] = RetrievalConfig(channels=("ppr",), ppr_alpha=a)
        out[f"E_ppr_a{a}"] = RetrievalConfig(channels=("structural",), multihop="ppr", ppr_alpha=a)
    return out


class Snapshotter:
    """Code files of a repository at a commit, with FileFacts cached by (path, blob)."""

    def __init__(self, repo: Path):
        self.repo = repo
        self.cache: dict[tuple[str, str], object] = {}
        self.text: dict[str, str] = {}

    def files_at(self, commit: str) -> list[tuple[str, str]]:
        out = []
        for line in t2.git(self.repo, "ls-tree", "-r", "-l", "--full-tree", commit).splitlines():
            meta, path = line.split("\t", 1)
            _mode, typ, blob, size = meta.split()
            if typ == "blob" and t2.is_code(path) and size != "-" and int(size) < MAX_FILE_BYTES:
                out.append((path, blob))
        return out

    def _read_blobs(self, blobs: list[str]) -> None:
        missing = [b for b in blobs if b not in self.text]
        if not missing:
            return
        raw = t2.git_bytes(self.repo, "cat-file", "--batch", input_bytes=("\n".join(missing) + "\n").encode())
        pos = 0
        for b in missing:
            nl = raw.index(b"\n", pos)
            _sha, _typ, size = raw[pos:nl].split()
            start = nl + 1
            end = start + int(size)
            self.text[b] = raw[start:end].decode("utf-8", "replace").replace("\r\n", "\n")
            pos = end + 1

    def facts_at(self, commit: str):
        from backend.kg.extract_python import extract

        files = self.files_at(commit)
        self._read_blobs([b for _, b in files])
        facts, contents = [], {}
        for path, blob in files:
            key = (path, blob)
            if key not in self.cache:
                self.cache[key] = extract(path, self.text[blob])
            facts.append(self.cache[key])
            contents[path] = self.text[blob]
        return facts, contents


@dataclass
class QueryContext:
    """Everything a system needs to answer one T2 query (base snapshot = the parent commit)."""
    spec: dict
    q: object  # t2.Query
    repo: Path
    facts: list
    contents: dict[str, str]
    graph: object  # backend.kg.model.Graph
    retr: object  # backend.retrieval.pipeline.RepoRetriever
    diff: str
    head_anchor: str
    cochange: object  # backend.retrieval.cochange.CoChangeIndex (history strictly older than the query)

    def head(self, path: str) -> str | None:
        return self.head_anchor if path == self.q.anchor else self.contents.get(path)


def iter_queries(queries=None, repos=None):
    """Yield a QueryContext per query, per repository oldest first.  The co-change index is fed
    every commit strictly older than the query commit before the query is answered."""
    from backend.kg.build import build_graph
    from backend.retrieval.cochange import CoChangeIndex
    from backend.retrieval.pipeline import RepoRetriever, RetrievalConfig

    queries = queries if queries is not None else t2.load()[0]
    for spec in t2.manifest():
        if repos and spec["name"] not in repos:
            continue
        repo = t2.ensure_clone(spec)
        commits = t2.log_commits(repo, spec["pin"])
        snapper = Snapshotter(repo)
        cc = CoChangeIndex()
        ci = 0
        for q in sorted((q for q in queries if q.repo == spec["name"]), key=lambda q: (q.time, q.qid)):
            while ci < len(commits) and commits[ci]["time"] < q.time:
                cc.add(p for _st, p, _old in t2.code_files(commits[ci]))
                ci += 1
            facts, contents = snapper.facts_at(q.parent)
            graph = build_graph(spec["name"], facts)
            retr = RepoRetriever(graph, RetrievalConfig(), facts=facts)
            yield QueryContext(spec, q, repo, facts, contents, graph, retr,
                               t2.git(repo, "diff", q.parent, q.sha, "--", q.anchor),
                               t2.git(repo, "show", f"{q.sha}:{q.anchor}"), cc)


def ranked_keys(items, snap) -> list[str]:
    """The packed context as a ranked list of path::qualname keys (seeds excluded); a class
    rendered in full also covers its methods."""
    from backend.kg.model import Kind

    ranked = []
    for it in items:
        if it.index is None or it.rendering == "seed":
            continue
        s = snap.symbols[it.index]
        ranked.append(f"{s.path}::{s.qualname}")
        if it.rendering == "full" and s.kind == Kind.CLASS:
            ranked += [f"{snap.symbols[c].path}::{snap.symbols[c].qualname}" for c in snap.children(it.index)]
    return ranked


def evaluate(names: list[str], out_dir: Path, repos=None) -> dict:
    from backend.kg.model import Kind

    queries, meta = t2.load()
    configs = {**systems(), **sweep_systems()}
    per = defaultdict(list)
    diag = defaultdict(list)
    failure = defaultdict(Counter)
    rows = {}
    t_repo, current = time.perf_counter(), None
    for ctx in iter_queries(queries, repos):
        q, retr = ctx.q, ctx.retr
        snap = retr.snap
        if q.repo != current:
            if current:
                print(f"[t2] {current}: {time.perf_counter() - t_repo:.1f}s", flush=True)
            t_repo, current = time.perf_counter(), q.repo
        all_keys = {f"{s.path}::{s.qualname}" for s in snap.symbols if s.kind != Kind.MODULE}
        judged = set(q.grades("sym_all"))
        row = {"qid": q.qid, "corpus": q.repo, "category": "commit", "m": {}}
        for name in names:
            res = retr.build_context(ctx.diff, ctx.head, q.message, config=configs[name], cochange=ctx.cochange.related)
            ranked = ranked_keys(res.items, snap)
            for view in t2.VIEWS:
                rl = [k.split("::")[0] for k in ranked] if view == "file" else ranked
                m = ir_metrics(rl, q.grades(view))
                if m is None:
                    continue
                m["lat_ms"] = res.trace["timings_ms"]["total_ms"]
                per[("commit", name, view)].append(m)
                per[(f"{q.repo}:commit", name, view)].append(m)
                row["m"].setdefault(name, {})[view] = [round(m[x], 4) for x in PER_QUERY_METRICS]
            tr = res.trace
            noise = Counter()
            for it in res.items:
                if it.index is None or it.rendering == "seed":
                    continue
                s = snap.symbols[it.index]
                noise["packed_anchor_file"] += int(s.path == q.anchor)  # unjudged by construction on T2
                for ch in {c.split(":")[0] for c in it.channel.split("+")}:
                    noise[f"packed_{ch}"] += 1
                    noise[f"packed_relevant_{ch}"] += int(f"{s.path}::{s.qualname}" in judged)
            diag[("commit", name)].append({
                "ctx_chars": tr["chars"], "ctx_items": len(tr["packed"]), "seeds": len(tr["seeds"]),
                "structural_change": 1.0 if tr["structural_change"] else 0.0,
                "candidates": len(tr["candidates"]), "total_ms": tr["timings_ms"]["total_ms"],
                **{k: noise.get(k, 0) for k in ("packed_lexical", "packed_relevant_lexical", "packed_structural",
                                                "packed_relevant_structural", "packed_cochange",
                                                "packed_relevant_cochange", "packed_anchor_file")},
            })
            cand = {c["key"] for c in tr["candidates"]}
            for j in q.judgments:
                if j.key not in all_keys:
                    failure[("commit", name)]["not_in_base_snapshot"] += 1
                elif j.key in ranked:
                    failure[("commit", name)]["in_context"] += 1
                elif j.key in cand:
                    failure[("commit", name)]["generated_then_packed_out"] += 1
                else:
                    failure[("commit", name)]["not_generated"] += 1
        rows[q.qid] = row
    if current:
        print(f"[t2] {current}: {time.perf_counter() - t_repo:.1f}s", flush=True)

    result = {
        "meta": {"benchmark": "t2", "t2_version": meta["t2_version"], "systems": names, "python": sys.version.split()[0]},
        "metrics": {f"{c}|{s}|{v}": {**mean_metrics(r), "queries": len(r)} for (c, s, v), r in per.items()},
        "diagnostics": {f"{c}|{s}": mean_metrics(r) for (c, s), r in diag.items()},
        "failure_breakdown": {f"{c}|{s}": dict(sorted(v.items())) for (c, s), v in failure.items()},
        "ci95_R@all_sym_all": {}, "ci95_R@all_sym_code": {},
    }
    for (c, s, v), r in per.items():
        if ":" not in c and v in ("sym_all", "sym_code"):
            mean, lo, hi = bootstrap_ci([x["R@all"] for x in r])
            result[f"ci95_R@all_{v}"][f"{c}|{s}"] = [round(mean, 3), round(lo, 3), round(hi, 3)]
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "metrics.json", result)
    with open(out_dir / "per_query.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for qid in sorted(rows):
            fh.write(json.dumps(rows[qid], sort_keys=True) + "\n")
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--systems", default="all")
    ap.add_argument("--out", default=str(EVAL_DIR / "results" / "t2-v2"))
    ap.add_argument("--repos", default="", help="comma-separated subset (default: all)")
    args = ap.parse_args(argv)
    names = {"all": list(systems()), "sweep": list(sweep_systems())}.get(args.systems) or args.systems.split(",")
    res = evaluate(names, Path(args.out), [r for r in args.repos.split(",") if r] or None)
    cols = ("P@5", "R@10", "R@20", "MRR", "nDCG@10", "R@all", "n")
    for view in ("sym_all", "sym_code", "sym_tests", "file"):
        print(f"\n{view}")
        for key, m in sorted(res["metrics"].items()):
            c, s, v = key.split("|")
            if c == "commit" and v == view:
                print(f"  {s:13s}" + "".join(f" {x}={m.get(x, float('nan')):.3f}" for x in cols) + f"  q={m['queries']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
