"""T3: does each retrieval system put the context needed to catch an injected bug in front of
the reviewer?  (design §6.2, M10)

    python -m eval.run_t3                     # context sufficiency, all systems (free, offline)
    LLM_BASE_URL=... LLM_MODEL=... LLM_API_KEY=... python -m eval.run_t3 --llm A,none
                                              # review detection with a real LLM (paid; asks nothing,
                                              # refuses without the three variables)

Context sufficiency: the case's needed item is in the packed context in a rendering that shows
what is needed (eval/benchmarks/t3.py).  A full class covers its methods.

Review detection (--llm): the production review prompt (parsed from backend/services/llm.py)
with the system's context; detected = a comment on the mutated file within +-3 lines of the
injected line.  Each case also has a control (the same line with a harmless trailing comment),
whose flagged rate estimates false positives.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path

from eval.benchmarks import t2, t3
from eval.stats import bootstrap_ci, holm, paired_randomization_test

EVAL_DIR = Path(__file__).resolve().parent
OUT = EVAL_DIR / "results" / "t3"


def systems():
    from eval.run_t2 import systems as t2_systems

    keep = ("A", "E", "EA", "P", "PA", "E_ppr", "E_bfs")  # co-change needs history; T3 cases sit at the pin
    out = {k: v for k, v in t2_systems().items() if k in keep}
    try:
        from eval.run_t2 import ltr_systems

        out.update(ltr_systems())
    except ImportError:
        pass
    return out


def covered_keys(items, snap) -> dict[str, str]:
    """key -> rendering, a full class also covering its methods."""
    from backend.kg.model import Kind

    out = {}
    for it in items:
        if it.index is None or it.rendering == "seed":
            continue
        s = snap.symbols[it.index]
        out.setdefault(f"{s.path}::{s.qualname}", it.rendering)
        if it.rendering == "full" and s.kind == Kind.CLASS:
            for c in snap.children(it.index):
                out.setdefault(f"{snap.symbols[c].path}::{snap.symbols[c].qualname}", "full")
    return out


def sufficient(case, cov: dict[str, str]) -> bool:
    ok = ("callsite", "full") if case.needed_rendering == "callsite" else ("compact", "full", "callsite")
    return any(cov.get(k) in ok for k in case.needed)


def review_prompt() -> str:
    src = (EVAL_DIR.parent / "backend" / "services" / "llm.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "system_prompt" for t in node.targets):
            return node.value.value
    raise RuntimeError("system_prompt not found in backend/services/llm.py")


def llm_review(diff: str, context: str, repo: str) -> list[dict]:
    import requests

    url, model, key = (os.environ[k] for k in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY"))
    ctx = f"\n    RELEVANT CODEBASE CONTEXT (files related to this PR):\n    {context[:12000]}\n    ---\n" if context else ""
    user = f"\n    Here is the Pull Request diff to review:\n\n    PR Title: Update\n    Author: dev\n    Repository: {repo}\n" \
           f"\n    {ctx}\n    Diff:\n    {diff}\n    "
    r = requests.post(f"{url.rstrip('/')}/chat/completions", timeout=180, headers={"Authorization": f"Bearer {key}"},
                      json={"model": model, "temperature": 0.0, "max_tokens": 2000,
                            "messages": [{"role": "system", "content": review_prompt()}, {"role": "user", "content": user}]})
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"].strip()
    text = text[text.find("["): text.rfind("]") + 1] if "[" in text else "[]"
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return []


def detected(comments: list[dict], case) -> bool:
    return any(str(c.get("file_path", "")).endswith(case.path) and abs(int(c.get("line_number") or -99) - case.line) <= 3
               for c in comments)


def control(case, content: str) -> tuple[str, str]:
    """The same line with a harmless trailing comment: a review that flags it is a false positive."""
    from dataclasses import replace

    return t3.make_diff(replace(case, after=case.before.rstrip() + "  # reviewed"), content)


def run(names: list[str], llm: list[str] | None = None) -> dict:
    from backend.kg.build import build_graph
    from backend.retrieval.pipeline import RepoRetriever
    from eval.run_t2 import Snapshotter

    cases = t3.load()
    cfgs = systems()
    per = defaultdict(list)  # (system, mutation) -> [0/1]
    rows = []
    det = defaultdict(list)
    for spec in t2.manifest():
        repo_cases = [c for c in cases if c.repo == spec["name"]]
        if not repo_cases:
            continue
        sn = Snapshotter(t2.ensure_clone(spec))
        facts, contents = sn.facts_at(spec["pin"])
        retr = RepoRetriever(build_graph(spec["name"], facts), facts=facts)
        for c in repo_cases:
            diff, head = t3.make_diff(c, contents[c.path])
            hc = (lambda p, _h=head, _p=c.path: _h if p == _p else contents.get(p))
            row = {"cid": c.cid, "mutation": c.mutation, "sufficient": {}}
            for name in names:
                res = retr.build_context(diff, hc, "", config=cfgs[name])
                ok = sufficient(c, covered_keys(res.items, retr.snap))
                row["sufficient"][name] = int(ok)
                per[(name, c.mutation)].append(int(ok))
                per[(name, "all")].append(int(ok))
                if llm and name in llm:
                    det[(name, "detected")].append(int(detected(llm_review(diff, res.text, c.repo), c)))
                    ctrl_diff, ctrl_head = control(c, contents[c.path])
                    ctrl = retr.build_context(ctrl_diff, (lambda p, _h=ctrl_head, _p=c.path: _h if p == _p else contents.get(p)),
                                              "", config=cfgs[name])
                    det[(name, "control_flagged")].append(int(detected(llm_review(ctrl_diff, ctrl.text, c.repo), c)))
            if llm and "none" in llm:
                det[("none", "detected")].append(int(detected(llm_review(diff, "", c.repo), c)))
                det[("none", "control_flagged")].append(int(detected(llm_review(control(c, contents[c.path])[0], "", c.repo), c)))
            rows.append(row)
        print(f"[t3] {spec['name']}: {len(repo_cases)} cases", flush=True)
    result = {"cases": len(cases), "systems": names, "sufficiency": {}, "paired_vs_A": {}}
    for (name, mut), v in sorted(per.items()):
        mean, lo, hi = bootstrap_ci(v)
        result["sufficiency"][f"{name}|{mut}"] = {"rate": round(mean, 4), "ci95": [round(lo, 4), round(hi, 4)], "n": len(v)}
    if "A" in names:
        a = [r["sufficient"]["A"] for r in rows]
        tests = []
        for name in names:
            if name == "A":
                continue
            b = [r["sufficient"][name] for r in rows]
            d, p = paired_randomization_test(b, a)
            tests.append((name, d, p))
        for (name, d, p), ph in zip(tests, holm([t[2] for t in tests])):
            result["paired_vs_A"][name] = {"delta": round(d, 4), "p": round(p, 4), "p_holm": round(ph, 4)}
    if det:
        result["llm"] = {f"{k[0]}|{k[1]}": round(statistics.fmean(v), 4) for k, v in sorted(det.items())}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "metrics.json").write_text(json.dumps(result, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    with open(OUT / "per_case.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for r in rows:
            fh.write(json.dumps(r, sort_keys=True) + "\n")
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--systems", default="all")
    ap.add_argument("--llm", default="", help="systems to review with an LLM (paid), e.g. 'none,A'")
    args = ap.parse_args(argv)
    names = list(systems()) if args.systems == "all" else args.systems.split(",")
    llm = [s for s in args.llm.split(",") if s]
    if llm and not all(os.environ.get(k) for k in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")):
        print("LLM review needs LLM_BASE_URL, LLM_MODEL and LLM_API_KEY in the environment (paid API calls)")
        return 2
    res = run(names, llm or None)
    for name in names:
        s = res["sufficiency"][f"{name}|all"]
        by = " ".join(f"{m}={res['sufficiency'][f'{name}|{m}']['rate']:.2f}" for m in t3.TYPES
                      if f"{name}|{m}" in res["sufficiency"])
        pv = res["paired_vs_A"].get(name)
        print(f"{name:10s} {s['rate']:.3f} [{s['ci95'][0]:.3f}, {s['ci95'][1]:.3f}]  {by}"
              + (f"  vs A {pv['delta']:+.3f} p_holm={pv['p_holm']}" if pv else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
