"""T3: bug-injection benchmark (design §6.2, M10).

A case is a real file of a pinned T2 repository with ONE injected bug whose detection needs
context from another file.  Mutation sites are chosen with the FROZEN reference resolver
(eval/reference), never with the system under test:

  arg_swap    a call to a function/method defined in another file gets its first two
              positional arguments swapped                       needs: the callee's signature
  drop_arg    a call loses its last positional argument, which the callee requires (no default)
                                                                  needs: the callee's signature
  add_param   a function called from other files gets a new required parameter, callers
              unchanged                                           needs: a caller's call site

The diff is the mutation alone (base = the pin, head = base + mutation).  Two measurements:

  * context sufficiency (free): is the needed item in the packed context, in a rendering
    that shows what is needed (any rendering shows a callee's signature; a caller counts
    only with its call site or full body)?   -> eval/run_t3.py
  * review detection (paid; needs an LLM API key): does the reviewer comment within +-3
    lines of the injected line?  Controls: the same files with a no-op change measure false
    positives.                                                  -> eval/run_t3.py --llm

Cases are sampled per (repo, mutation type) in a seeded hash order, so the set is stable.
"""
from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from eval.benchmarks import t2

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "t3"
T3_VERSION = "1.0"
PER_TYPE_PER_REPO = 10
SALT = "t3-v1"
TYPES = ("arg_swap", "drop_arg", "add_param")


@dataclass
class Case:
    cid: str
    repo: str
    pin: str
    mutation: str
    path: str  # file containing the injected change
    line: int  # 1-based line of the injected change (head side)
    before: str  # original line
    after: str  # mutated line
    needed: list  # path::qualname keys; any one of them suffices
    needed_rendering: str  # "any" (signature visible) | "callsite" (call site or full body)
    description: str


def _h(*parts) -> str:
    return hashlib.sha1("|".join([SALT, *map(str, parts)]).encode()).hexdigest()


def _qual(ref, sid) -> str:
    return sid[1]  # reference symbol ids are (file, qualname)


def _callee_name(func) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _positional_params(fn: ast.FunctionDef, is_method: bool) -> list[ast.arg]:
    ps = list(fn.args.posonlyargs) + list(fn.args.args)
    return ps[1:] if is_method and ps else ps


def _is_static(fn) -> bool:
    return any(isinstance(d, ast.Name) and d.id in ("staticmethod",) for d in fn.decorator_list)


def candidates(spec: dict, files: dict[str, str]):
    from eval.reference import RefGraph

    ref = RefGraph([{"path": p, "content": c} for p, c in sorted(files.items())])
    out = {t: [] for t in TYPES}
    for sid, s in sorted(ref.syms.items(), key=lambda kv: str(kv[0])):
        if s["kind"] not in ("function", "method") or t2.TEST_RE.search(s["file"]):
            continue
        fn = s["node"]
        lines = ref.lines[s["file"]]
        source = "\n".join(lines)
        resolved = {}
        for t, ln in ref.call_refs.get(sid, []):
            resolved.setdefault((ln, ref.syms[t]["name"]), set()).add(t)
        # --- call-site mutations inside s (callee resolved by the reference resolver, unambiguously)
        for n in ast.walk(fn):
            if not isinstance(n, ast.Call) or n.lineno != n.end_lineno or any(isinstance(a, ast.Starred) for a in n.args):
                continue
            targets = resolved.get((n.lineno, _callee_name(n.func)), set())
            if len(targets) != 1:
                continue
            tid = next(iter(targets))
            g = ref.syms[tid]
            if g["kind"] not in ("function", "method") or g["file"] == s["file"] or t2.TEST_RE.search(g["file"]):
                continue
            gfn = g["node"]
            if gfn.args.vararg:
                continue
            params = _positional_params(gfn, g["kind"] == "method" and not _is_static(gfn))
            line = lines[n.lineno - 1]
            key_g = f"{g['file']}::{_qual(ref, tid)}"
            if len(n.args) >= 2 and len(params) >= 2:
                a, b = n.args[0], n.args[1]
                sa, sb = ast.get_source_segment(source, a), ast.get_source_segment(source, b)
                if sa and sb and sa != sb and a.lineno == b.lineno == n.lineno:
                    new = line[: a.col_offset] + sb + line[a.end_col_offset: b.col_offset] + sa + line[b.end_col_offset:]
                    out["arg_swap"].append(Case("", spec["name"], spec["pin"], "arg_swap", s["file"], n.lineno, line, new,
                                                [key_g], "any", f"swapped arguments {sa!r} and {sb!r} of {g['name']}()"))
            n_def = len(gfn.args.defaults)
            required = len(params) - n_def
            if n.args and not n.keywords and len(n.args) == required and required >= 1:
                last = n.args[-1]
                if last.lineno == n.lineno:
                    prev_end = n.args[-2].end_col_offset if len(n.args) >= 2 else None
                    if prev_end is not None:
                        new = line[:prev_end] + line[last.end_col_offset:]
                    else:
                        new = line[: last.col_offset] + line[last.end_col_offset:]
                    if new != line:
                        out["drop_arg"].append(Case("", spec["name"], spec["pin"], "drop_arg", s["file"], n.lineno, line,
                                                    new, [key_g], "any", f"dropped required argument of {g['name']}()"))
        # --- signature mutation of s itself, if called from other files
        callers = sorted(c for c in ref.callers.get(sid, ()) if ref.syms[c]["file"] != s["file"]
                         and not t2.TEST_RE.search(ref.syms[c]["file"]))
        if not callers or fn.args.vararg or fn.args.kwarg or fn.name.startswith("__"):
            continue
        def_line_no = fn.lineno  # the def line (fn.lineno already skips decorators on Python >= 3.8)
        while def_line_no <= len(lines) and not lines[def_line_no - 1].lstrip().startswith(("def ", "async def ")):
            def_line_no += 1
        line = lines[def_line_no - 1] if def_line_no <= len(lines) else ""
        if not line.rstrip().endswith(":") or "(" not in line or line.count("(") != line.count(")"):
            continue  # multi-line signatures are skipped
        if fn.args.kwonlyargs or fn.args.defaults:
            continue  # keep the mutation unambiguous: a new trailing required positional parameter
        open_i, depth, close = line.index("(", line.index("def ")), 0, None
        for k in range(open_i, len(line)):
            depth += {"(": 1, ")": -1}.get(line[k], 0)
            if depth == 0:
                close = k
                break
        if close is None:
            continue
        inner = line[open_i + 1: close].strip()
        sep = ", " if inner and not inner.endswith(",") else (" " if inner else "")
        new = line[:close] + sep + "strict_mode" + line[close:]
        out["add_param"].append(Case("", spec["name"], spec["pin"], "add_param", s["file"], def_line_no, line, new,
                                     [f"{ref.syms[c]['file']}::{_qual(ref, c)}" for c in callers], "callsite",
                                     f"new required parameter of {fn.name}() with {len(callers)} cross-file callers"))
    return out


def generate() -> list[Case]:
    from eval.run_t2 import Snapshotter

    cases = []
    for spec in t2.manifest():
        repo = t2.ensure_clone(spec)
        sn = Snapshotter(repo)
        files_at = sn.files_at(spec["pin"])
        sn._read_blobs([b for _, b in files_at])
        files = {p: sn.text[b] for p, b in files_at}
        by_type = candidates(spec, files)
        for typ in TYPES:
            seen_lines = set()
            picked = []
            for c in sorted(by_type[typ], key=lambda c: _h(c.repo, c.mutation, c.path, c.line, c.after)):
                if (c.path, c.line) in seen_lines:
                    continue
                seen_lines.add((c.path, c.line))
                picked.append(c)
                if len(picked) >= PER_TYPE_PER_REPO:
                    break
            for k, c in enumerate(sorted(picked, key=lambda c: (c.path, c.line))):
                c.cid = f"t3-{spec['name']}-{typ}-{k:02d}"
                cases.append(c)
        print(spec["name"], {t: len(by_type[t]) for t in TYPES}, flush=True)
    return cases


def make_diff(case: Case, content: str) -> tuple[str, str]:
    """(unified diff, head content) of the mutation."""
    lines = content.split("\n")
    assert lines[case.line - 1] == case.before, (case.cid, "pin drifted")
    head = lines[:]
    head[case.line - 1] = case.after
    n_lines = len(lines) - (1 if content.endswith("\n") else 0)  # the text after a final newline is not a line
    lo, hi = max(1, case.line - 3), min(n_lines, case.line + 3)
    hunk = [f" {x}" for x in lines[lo - 1: case.line - 1]] + [f"-{case.before}", f"+{case.after}"] + \
           [f" {x}" for x in lines[case.line: hi]]
    n = hi - lo + 1
    diff = (f"diff --git a/{case.path} b/{case.path}\nindex 0000000..1111111 100644\n--- a/{case.path}\n"
            f"+++ b/{case.path}\n@@ -{lo},{n} +{lo},{n} @@\n" + "\n".join(hunk) + "\n")
    return diff, "\n".join(head)


def save(cases: list[Case]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DATA_DIR / "cases.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        for c in cases:
            fh.write(json.dumps(asdict(c), sort_keys=True) + "\n")
    counts = {}
    for c in cases:
        counts.setdefault(c.repo, {}).setdefault(c.mutation, 0)
        counts[c.repo][c.mutation] += 1
    (DATA_DIR / "meta.json").write_text(json.dumps({"t3_version": T3_VERSION, "per_type_per_repo": PER_TYPE_PER_REPO,
                                                    "salt": SALT, "counts": counts}, indent=1, sort_keys=True) + "\n",
                                       encoding="utf-8")


def load() -> list[Case]:
    with open(DATA_DIR / "cases.jsonl", encoding="utf-8") as fh:
        return [Case(**json.loads(line)) for line in fh if line.strip()]


if __name__ == "__main__":
    cs = generate()
    save(cs)
    print("total", len(cs))
