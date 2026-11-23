"""Budget-aware, relation-aware context construction (design §5.5, ADR-07).

Every candidate has several renderings:
  compact   signature + docstring summary (+ method signatures for classes)
  callsite  the candidate's signature + lines around its calls/references to a seed
            (how an impact candidate, a caller, must be shown)
  full      the candidate's source (capped)
Selection is a greedy multiple-choice knapsack:
  1. seeds get their (capped) source;
  2. candidates enter in rank order at their cheapest *useful* rendering
     (callsite for callers of a seed, compact otherwise);
  3. remaining budget upgrades candidates to full source in rank order;
  4. containment: a class rendered in full covers its methods.
Each item carries a provenance label so the reader knows why it is there.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from backend.kg.model import EdgeType, Kind
from backend.kg.snapshot import GraphSnapshot

FULL_CAP = 3000
DOC_CAP = 300
CALLSITE_WINDOW = 3
DEP_TYPES = (EdgeType.CALLS, EdgeType.REFERENCES)


@dataclass
class Item:
    index: int | None  # snapshot index (None for seeds added by the change)
    key: str  # path::qualname
    rendering: str  # seed | full | compact | callsite
    text: str
    label: str
    rank: int = 0
    score: float = 0.0
    channel: str = ""

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass
class PackResult:
    items: list[Item]
    chars: int
    considered: int
    dropped: list[dict] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(i.text for i in self.items)


def _header(path: str, qualname: str, label: str) -> str:
    return f"### {path}::{qualname or '<module>'}  [{label}]"


def _doc(sym) -> str:
    d = (sym.docstring or "").strip().split("\n\n")[0].strip()
    return (d[:DOC_CAP] + "…") if len(d) > DOC_CAP else d


def render_compact(snap: GraphSnapshot, i: int, label: str) -> str:
    s = snap.symbols[i]
    parts = [_header(s.path, s.qualname, label), s.signature.strip() or s.name]
    if s.kind == Kind.METHOD and s.parent_id in snap.index:
        parts.insert(1, f"# in {snap.symbols[snap.index[s.parent_id]].signature.strip().splitlines()[0]}")
    doc = _doc(s)
    if doc:
        parts.append(f'    """{doc}"""')
    if s.kind == Kind.CLASS:
        sigs = [snap.symbols[c].signature.strip().splitlines()[0] for c in snap.children(i)
                if snap.symbols[c].kind == Kind.METHOD and snap.symbols[c].signature]
        parts += ["    " + x for x in sigs[:20]]
    return "\n".join(parts)


def render_full(snap: GraphSnapshot, i: int, label: str) -> str:
    s = snap.symbols[i]
    body = s.body if len(s.body) <= FULL_CAP else s.body[:FULL_CAP] + "\n    # … truncated"
    return _header(s.path, s.qualname, label) + "\n" + body


def render_callsite(snap: GraphSnapshot, i: int, lines: list[int], label: str) -> str:
    s = snap.symbols[i]
    src = s.body.split("\n")
    keep: set[int] = set()
    for ln in lines[:4]:
        off = ln - s.start_line
        keep.update(range(max(0, off - CALLSITE_WINDOW), min(len(src), off + CALLSITE_WINDOW + 1)))
    out, prev = [], None
    for k in sorted(keep):
        if prev is not None and k != prev + 1:
            out.append("    # …")
        out.append(src[k])
        prev = k
    head = s.signature.strip().splitlines()[0] if s.signature else s.name
    return _header(s.path, s.qualname, label) + "\n" + head + "\n    # …\n" + "\n".join(out)


def relation_to_seeds(snap: GraphSnapshot, i: int, seed_idx: list[int]) -> tuple[str | None, list[int], int | None]:
    """('caller', evidence lines, seed) if i uses a seed; ('callee', [], seed) if a seed uses i."""
    for s in seed_idx:
        for t in DEP_TYPES:
            lines = snap.evidence.get((i, s, t))
            if lines:
                return "caller", lines, s
    for s in seed_idx:
        for t in DEP_TYPES:
            if (s, i, t) in snap.evidence:
                return "callee", [], s
    return None, [], None


def pack(snap: GraphSnapshot, seeds, candidates: list[tuple[int, float, str]], budget: int = 12000,
         seed_cap_frac: float = 0.4) -> PackResult:
    """candidates: [(snapshot index, score, channel)] in rank order."""
    items: list[Item] = []
    used = 0
    seed_budget = int(budget * seed_cap_frac)
    per_seed = max(400, seed_budget // max(len(seeds), 1))
    for sd in seeds:
        body = sd.text if len(sd.text) <= per_seed else sd.text[:per_seed] + "\n    # … truncated"
        text = _header(sd.path, sd.qualname, f"changed: {sd.change}") + "\n" + body
        if used + len(text) > budget:
            continue
        items.append(Item(sd.index, sd.key, "seed", text, f"changed: {sd.change}"))
        used += len(text)
    seed_idx = [sd.index for sd in seeds if sd.index is not None]
    seed_names = {sd.index: sd.qualname for sd in seeds if sd.index is not None}

    # phase A: cheapest useful rendering, in rank order
    placed: dict[int, Item] = {}
    dropped = []
    for rank, (i, score, channel) in enumerate(candidates, 1):
        rel, lines, s = relation_to_seeds(snap, i, seed_idx)
        if rel == "caller":
            label = f"uses {seed_names[s]} · line {lines[0]}"
            text = render_callsite(snap, i, lines, label)
            rendering = "callsite"
        else:
            label = f"used by {seed_names[s]}" if rel == "callee" else f"{channel} match #{rank}"
            text = render_compact(snap, i, label)
            rendering = "compact"
        if used + len(text) > budget:
            dropped.append({"index": i, "rank": rank, "reason": "budget"})
            continue
        it = Item(i, f"{snap.symbols[i].path}::{snap.symbols[i].qualname}", rendering, text, label, rank, score, channel)
        placed[i] = it
        used += len(text)
    # phase B: upgrade to full source in rank order
    for i, it in sorted(placed.items(), key=lambda kv: kv[1].rank):
        full = render_full(snap, i, it.label)
        delta = len(full) - len(it.text)
        if delta > 0 and used + delta <= budget:
            it.text, it.rendering = full, "full"
            used += delta
    # containment: a full class covers its methods
    full_classes = {i for i, it in placed.items() if it.rendering == "full" and snap.symbols[i].kind == Kind.CLASS}
    for i in list(placed):
        if snap.parent[i] in full_classes:
            used -= placed[i].chars
            dropped.append({"index": i, "rank": placed[i].rank, "reason": "contained"})
            del placed[i]
    items += sorted(placed.values(), key=lambda it: it.rank)
    return PackResult(items, used, len(candidates), dropped)
