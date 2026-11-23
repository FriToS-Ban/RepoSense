"""Global name resolution: FileFacts of a whole repository -> Graph (design §3.5).

Every name/attribute chain read by a symbol is resolved left to right through a small
state machine:

    module ("mod"), namespace-package prefix ("ns"), class object ("class"), instance of
    a class ("inst"), function/method ("sym"), super() proxy ("super"), external ("ext")

Each step has a tier (model.Tier); an edge carries the weakest tier of its chain.
Prefixes that resolve to symbols become edges too (``Engine.step()`` references the class
``Engine`` and calls the method ``Engine.step``).  When a called chain cannot be
resolved and its receiver is not known to be external, the R5 fallback proposes the
methods of that name in classes defined in this module or a directly imported module.
"""
from __future__ import annotations

import builtins
import posixpath
from collections import Counter, defaultdict

from backend.kg.model import (TIER_ORDER, Edge, EdgeType, FileFacts, Flag, Graph, ImportFact, Kind, RefFact, Symbol,
                              SymbolFact, Tier, symbol_id)

BUILTINS = frozenset(dir(builtins))
DEFAULT_CONFIDENCE = {Tier.LOCAL: 1.0, Tier.IMPORT: 1.0, Tier.MEMBER: 0.95, Tier.TYPE_INFERRED: 0.85, Tier.FALLBACK: 0.5}
R5_MAX_CANDIDATES = 5
MAX_EVIDENCE_LINES = 50
MAX_DEPTH = 12
# R5 never guesses method names that untyped receivers overwhelmingly use on builtins / loggers
# (dev-sample finding: .get/.items/.append/.update on dicts and lists were 0/24 correct).
_BUILTIN_TYPES = (dict, list, set, frozenset, str, bytes, bytearray, tuple, int, float, complex)
NO_FALLBACK = frozenset(n for t in _BUILTIN_TYPES for n in dir(t)) | frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"})


def weaker(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if TIER_ORDER[a] >= TIER_ORDER[b] else b


def chain_text(head: str, attrs: list[str]) -> str:
    return ".".join([head] + list(attrs))


class _Module:
    __slots__ = ("name", "path", "is_pkg", "sid", "defs", "bindings", "stars", "classes", "imported")

    def __init__(self, ff: FileFacts, sid: str):
        self.name, self.path, self.is_pkg, self.sid = ff.module, ff.path, ff.is_package, sid
        self.defs: dict[str, str] = {}  # module-level symbol name -> sid
        self.bindings: dict[str, ImportFact] = {}  # module-level import bindings
        self.stars: list[str] = []
        self.classes: list[str] = []  # every class sid defined in this module (incl. nested)
        self.imported: set[str] = set()  # canonical names of in-repo modules this module imports


class Resolver:
    def __init__(self, repo_id: str, facts: list[FileFacts], confidence: dict | None = None, collect: bool = False):
        self.repo = repo_id
        self.facts = sorted(facts, key=lambda f: f.path)
        self.conf = {**DEFAULT_CONFIDENCE, **(confidence or {})}
        self.collect = collect
        self.occurrences: list[dict] = []
        self.graph = Graph(repo_id)
        self.modules: dict[str, _Module] = {}
        self.by_path: dict[str, _Module] = {}
        self.alias: dict[str, str] = {}
        self.ambiguous_aliases = 0
        self.prefixes: set[str] = set()
        self.fact: dict[str, SymbolFact] = {}
        self.sym_module: dict[str, _Module] = {}
        self.members: dict[str, dict[str, str]] = defaultdict(dict)
        self.parent_class: dict[str, str] = {}
        self.scope_imports: dict[tuple[str, str], dict[str, ImportFact]] = defaultdict(dict)
        self.bases: dict[str, list[str]] = {}
        self.methods_by_name: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._attr_type_cache: dict[tuple[str, str], str | None] = {}
        self._var_cache: dict[tuple[str, str], tuple | None] = {}

    # ================================================================== setup
    def _register_modules(self):
        package_dirs = {posixpath.dirname(f.path) for f in self.facts if posixpath.basename(f.path) == "__init__.py"}
        for ff in self.facts:
            sid = symbol_id(self.repo, ff.path, "")
            m = _Module(ff, sid)
            self.modules[ff.module] = m
            self.by_path[ff.path] = m
        for ff in self.facts:
            names = [ff.module]
            # import name relative to the nearest ancestor directory that is not a package (src layouts)
            d = posixpath.dirname(ff.path)
            if d in package_dirs:
                root = d
                while root in package_dirs:
                    root = posixpath.dirname(root)
                rel = ff.path[len(root) + 1 :] if root else ff.path
                names.append(rel[:-3].replace("/", ".").removesuffix(".__init__"))
            for n in names:
                if n in self.alias and self.alias[n] != ff.module:
                    self.ambiguous_aliases += 1
                    continue
                self.alias[n] = ff.module
                parts = n.split(".")
                for i in range(1, len(parts) + 1):
                    self.prefixes.add(".".join(parts[:i]))

    def _register_symbols(self):
        g = self.graph
        for ff in self.facts:
            mod = self.by_path[ff.path]
            for sf in ff.symbols:
                sid = symbol_id(self.repo, ff.path, sf.qualname)
                pid = symbol_id(self.repo, ff.path, sf.parent) if sf.parent is not None else None
                g.symbols[sid] = Symbol(sid, self.repo, ff.path, sf.qualname, sf.name, sf.kind, pid, sf.start, sf.end,
                                        sf.signature, sf.docstring, sf.body, sf.flags, sf.content_hash, sf.signature_hash)
                self.fact[sid] = sf
                self.sym_module[sid] = mod
                if sf.parent == "":
                    mod.defs[sf.name] = sid
                if sf.kind == Kind.CLASS:
                    mod.classes.append(sid)
                if pid is not None and pid in self.fact and self.fact[pid].kind == Kind.CLASS:
                    self.members[pid][sf.name] = sid
                    self.parent_class[sid] = pid
                    if sf.kind == Kind.METHOD:
                        self.methods_by_name[(mod.name, sf.name)].append(sid)
            for imp in ff.imports:
                if imp.scope == "":
                    if imp.star:
                        mod.stars.append(imp.module)
                    else:
                        mod.bindings[imp.binding] = imp
                elif not imp.star:
                    self.scope_imports[(ff.path, imp.scope)][imp.binding] = imp

    # ================================================================== module-level name resolution
    def canonical(self, dotted: str) -> str | None:
        return self.alias.get(dotted)

    def resolve_export(self, canon: str, name: str, depth: int = 0):
        """``from canon import name`` -> state.  None = in-repo module but not a symbol (e.g. a variable)."""
        mod = self.modules.get(canon)
        if mod is None or depth > MAX_DEPTH:
            return None
        if name in mod.defs:
            return self._sym_state(mod.defs[name])
        imp = mod.bindings.get(name)
        if imp is not None:
            st = self.resolve_binding(imp, depth + 1)
            return None if st and st[0] == "ext" else st
        var = self._module_var(mod, name)
        if var:
            return var
        sub = self.canonical(f"{mod.name}.{name}" if mod.name else name)
        if sub:
            return ("mod", sub)
        for star in mod.stars:
            c = self.canonical(star)
            if c and c != canon:
                st = self.resolve_export(c, name, depth + 1)
                if st:
                    return st
        return None

    def resolve_binding(self, imp: ImportFact, depth: int = 0):
        if imp.name is None:
            target = imp.module if imp.asname else imp.module.split(".")[0]
            c = self.canonical(target)
            if c:
                return ("mod", c)
            if target in self.prefixes:
                return ("ns", target)
            return ("ext", imp.module)
        full = f"{imp.module}.{imp.name}" if imp.module else imp.name
        c = self.canonical(full)
        if c:
            return ("mod", c)
        cm = self.canonical(imp.module)
        if cm:
            return self.resolve_export(cm, imp.name, depth)
        if full in self.prefixes:
            return ("ns", full)
        return ("ext", imp.module)

    def _module_var(self, mod: _Module, name: str):
        """Type of a module-level instance (``NotSet = _NotSetType()``) as ("inst", class) or None."""
        key = (mod.sid, name)
        if key in self._var_cache:
            return self._var_cache[key]
        self._var_cache[key] = None  # cycle guard
        res = None
        for ch in self.fact[mod.sid].type_env.get(name, []):
            st, _ = self._resolve_chain_state(mod.sid, ch, scope="local", use_types=False)
            res = self._as_instance(st)
            if res:
                break
        self._var_cache[key] = res
        return res

    def _as_instance(self, st):
        """Value of calling/annotating with ``st``: a class gives its instance; a function gives its
        annotated return type (``x = self._send(...)`` with ``def _send(...) -> Response``)."""
        if not st:
            return None
        if st[0] == "class":
            return ("inst", st[1])
        if st[0] == "inst":
            return st
        if st[0] == "sym":
            for ch in self.fact[st[1]].returns:
                rs, _ = self._resolve_chain_state(st[1], ch, scope="outer", use_types=False)
                if rs and rs[0] == "class":
                    return ("inst", rs[1])
        return None

    def _sym_state(self, sid: str):
        return ("class", sid) if self.fact[sid].kind == Kind.CLASS else ("sym", sid)

    # ================================================================== classes: bases, MRO, members
    def _resolve_bases(self):
        g = self.graph
        for mod in self.modules.values():
            for cid in mod.classes:
                sf = self.fact[cid]
                resolved = []
                for ch in sf.bases:
                    st, tier = self._resolve_chain_state(cid, ch, scope="outer")
                    if st and st[0] == "class" and st[1] != cid:
                        resolved.append(st[1])
                        self._add_edge(cid, st[1], EdgeType.INHERITS, tier or Tier.LOCAL, "base", sf.start, n=1)
                self.bases[cid] = list(dict.fromkeys(resolved))
        from backend.kg.derive import c3_mro

        g.mro = {cid: c3_mro(cid, self.bases) for cid in self.bases}

    def lookup_member(self, cid: str, attr: str, skip_self: bool = False) -> str | None:
        mro = self.graph.mro.get(cid, [cid])
        for c in mro[1:] if skip_self else mro:
            m = self.members.get(c, {}).get(attr)
            if m:
                return m
        return None

    def lookup_attr_type(self, cid: str, attr: str) -> str | None:
        key = (cid, attr)
        if key in self._attr_type_cache:
            return self._attr_type_cache[key]
        self._attr_type_cache[key] = None  # cycle guard
        result = None
        for c in self.graph.mro.get(cid, [cid]):
            for ch in self.fact[c].attr_types.get(attr, []):
                st, _ = self._resolve_chain_state(c, ch, scope="outer")
                if st and st[0] == "class":
                    result = st[1]
                    break
            if result:
                break
        self._attr_type_cache[key] = result
        return result

    # ================================================================== chain resolution
    def lookup_name(self, owner_sid: str, name: str, scope: str, use_types: int = 3):
        """-> (state, tier).  state None = unknown/local; ("ext", ...) = builtin or external."""
        sf = self.fact[owner_sid]
        mod = self.sym_module[owner_sid]
        if scope == "local" and sf.kind != Kind.MODULE:
            imp = self.scope_imports.get((mod.path, sf.qualname), {}).get(name)
            if imp is not None:
                st = self.resolve_binding(imp)
                return st, (Tier.TYPE_INFERRED if st and st[0] == "inst" else Tier.IMPORT)
            if name in sf.locals:
                if use_types:
                    for ch in sf.type_env.get(name, []):
                        # bounded depth: r = c.fetch() needs the inferred type of c first
                        st, _ = self._resolve_chain_state(owner_sid, ch, scope="local", use_types=use_types - 1)
                        inst = self._as_instance(st)
                        if inst:
                            return inst, Tier.TYPE_INFERRED
                return None, None
        if scope == "class" and sf.kind == Kind.CLASS and name in self.members.get(owner_sid, {}):
            return self._sym_state(self.members[owner_sid][name]), Tier.LOCAL
        if scope == "outer" and owner_sid in self.parent_class:
            cls = self.parent_class[owner_sid]
            if name in self.members.get(cls, {}):
                return self._sym_state(self.members[cls][name]), Tier.LOCAL
        if name in mod.defs:
            return self._sym_state(mod.defs[name]), Tier.LOCAL
        imp = mod.bindings.get(name)
        if imp is not None:
            st = self.resolve_binding(imp)
            return st, (Tier.TYPE_INFERRED if st and st[0] == "inst" else Tier.IMPORT)
        for star in mod.stars:
            c = self.canonical(star)
            if c:
                st = self.resolve_export(c, name)
                if st:
                    return st, (Tier.TYPE_INFERRED if st[0] == "inst" else Tier.IMPORT)
        if use_types:
            var = self._module_var(mod, name)
            if var:
                return var, Tier.TYPE_INFERRED
        if name in BUILTINS:
            return ("ext", name), None
        return None, None

    def member(self, state, attr: str):
        kind = state[0]
        if kind == "mod":
            st = self.resolve_export(state[1], attr)
            return st, (Tier.TYPE_INFERRED if st and st[0] == "inst" else Tier.IMPORT)
        if kind == "ns":
            d = f"{state[1]}.{attr}"
            c = self.canonical(d)
            if c:
                return ("mod", c), Tier.IMPORT
            return (("ns", d) if d in self.prefixes else None), Tier.IMPORT
        if kind in ("class", "inst"):
            m = self.lookup_member(state[1], attr)
            if m:
                return self._sym_state(m), Tier.MEMBER
            t = self.lookup_attr_type(state[1], attr)
            if t:
                return ("inst", t), Tier.TYPE_INFERRED
            return None, None
        if kind == "super":
            m = self.lookup_member(state[1], attr, skip_self=True)
            return (self._sym_state(m) if m else None), Tier.MEMBER
        return None, None

    def _head_state(self, owner_sid: str, head: str, scope: str, use_types: int = 3):
        sf = self.fact[owner_sid]
        cls = self.parent_class.get(owner_sid)
        if head == "<super>":
            return (("super", cls) if cls else None), Tier.MEMBER
        if head in ("<expr>", "<lit>"):
            return None, None
        if scope == "local" and cls and sf.self_name and head == sf.self_name:
            return (("class" if sf.flags & Flag.CLASSMETHOD else "inst"), cls), Tier.MEMBER
        return self.lookup_name(owner_sid, head, scope, use_types)

    def _resolve_chain_state(self, owner_sid: str, chain: list, scope: str, use_types: int = 3):
        head, attrs = chain[0], chain[1]
        state, tier = self._head_state(owner_sid, head, scope, use_types)
        for a in attrs:
            if state is None or state[0] in ("ext", "sym"):
                return None, None
            state, step = self.member(state, a)
            tier = weaker(tier, step)
        return state, tier

    # ================================================================== references -> edges
    def _fallback_candidates(self, mod: _Module, name: str) -> list[str]:
        cands = list(self.methods_by_name.get((mod.name, name), []))
        for canon in sorted(mod.imported):
            cands += self.methods_by_name.get((canon, name), [])
        return sorted(set(cands))

    def resolve_ref(self, owner_sid: str, ref: RefFact):
        """-> ([(target_sid, tier, edge_type, role, n_candidates, via)], unresolved: bool)."""
        state, tier = self._head_state(owner_sid, ref.head, ref.scope)
        head_state = state
        via = [state[0] if state else ("?" if ref.head not in ("<expr>", "<lit>") else ref.head)]
        hits = []
        sf = self.fact[owner_sid]
        self_head = ref.scope == "local" and sf.self_name is not None and ref.head == sf.self_name
        if state and state[0] in ("sym", "class") and not self_head:  # `cls.x` is not a reference to "my class"
            hits.append((state[1], tier, not ref.attrs))
        prev_kind = state[0] if state else None
        unknown_receiver = state is None  # head is an untyped local, an unknown name, or an expression
        for i, a in enumerate(ref.attrs):
            if state is None or state[0] == "ext":
                break
            if state[0] == "sym":  # attribute of a function object (f.cache_clear): nothing to resolve
                state, unknown_receiver = None, False
                break
            prev_kind = state[0]
            state, step = self.member(state, a)
            tier = weaker(tier, step)
            via.append(state[0] if state else "?")
            if state is None:
                unknown_receiver = prev_kind in ("class", "inst")  # e.g. self.client with unknown type
                break
            if state[0] in ("sym", "class"):
                hits.append((state[1], tier, i == len(ref.attrs) - 1))
        results = []
        for sid, t, final in hits:
            if final and ref.role == "call":
                role = "super_call" if ref.head == "<super>" else ("instantiate" if self.fact[sid].kind == Kind.CLASS else "call")
                results.append((sid, t, EdgeType.CALLS, role, 1, via))
            else:
                results.append((sid, t, EdgeType.REFERENCES, ref.role if final else "attribute", 1, via))
        final_hit = any(final for _, _, final in hits)
        external = bool(head_state and head_state[0] == "ext") or ref.head == "<lit>"
        last = ref.attrs[-1] if ref.attrs else ""
        if (ref.role == "call" and ref.attrs and not final_hit and unknown_receiver and not external
                and not (last.startswith("__") and last.endswith("__")) and last not in NO_FALLBACK):
            cands = self._fallback_candidates(self.sym_module[owner_sid], last)
            if 1 <= len(cands) <= R5_MAX_CANDIDATES:
                for c in cands:
                    results.append((c, Tier.FALLBACK, EdgeType.CALLS, "call", len(cands), via + ["fallback"]))
        local_head = ref.scope == "local" and (ref.head in sf.locals or ref.head == sf.self_name)
        head_unknown = head_state is None and ref.head not in ("<expr>", "<lit>") and not local_head
        broke_in_repo = bool(ref.attrs) and not final_hit and state is None and prev_kind in ("mod", "ns", "class", "inst")
        return results, (not results and (head_unknown or broke_in_repo))

    def _confidence(self, tier: str, n: int) -> float:
        if tier == Tier.FALLBACK:
            key = f"R5@{min(n, 3)}"
            return self.conf.get(key, self.conf[Tier.FALLBACK] / n)
        return self.conf[tier]

    def _add_edge(self, src, dst, etype, tier, role, line, n=1):
        if src == dst:
            return
        key = (src, dst, etype)
        conf = self._confidence(tier, n)
        e = self.graph.edges.get(key)
        if e is None:
            e = Edge(src, dst, etype, conf, tier, role, 0)
            self.graph.edges[key] = e
        elif conf > e.confidence:
            e.confidence, e.resolution = conf, tier
        e.weight += 1
        if len(e.lines) < MAX_EVIDENCE_LINES and line not in e.lines:
            e.lines.append(line)
        e.roles.append(role)

    def _resolve_imports(self):
        for ff in self.facts:
            mod = self.by_path[ff.path]
            for imp in ff.imports:
                owner = symbol_id(self.repo, ff.path, imp.scope)
                if owner not in self.fact:
                    owner = mod.sid
                targets = []
                if imp.name is None or imp.star:
                    c = self.canonical(imp.module)
                    if c:
                        targets.append(c)
                else:
                    full = f"{imp.module}.{imp.name}" if imp.module else imp.name
                    c = self.canonical(full) or self.canonical(imp.module)
                    if c:
                        targets.append(c)
                    st = self.resolve_binding(imp)
                    if st and st[0] in ("sym", "class"):
                        self._add_edge(owner, st[1], EdgeType.REFERENCES, Tier.IMPORT, "import_use", imp.line)
                for c in targets:
                    if c != mod.name:
                        mod.imported.add(c)
                        self._add_edge(mod.sid, self.modules[c].sid, EdgeType.IMPORTS, Tier.IMPORT, "import", imp.line)

    def _resolve_refs(self):
        residual = defaultdict(list)
        for ff in self.facts:
            for ref in ff.refs:
                if ref.role == "base":
                    continue  # handled as INHERITS
                owner = symbol_id(self.repo, ff.path, ref.owner)
                results, unresolved = self.resolve_ref(owner, ref)
                for sid, tier, etype, role, n, via in results:
                    self._add_edge(owner, sid, etype, tier, role, ref.line, n)
                    if self.collect and sid != owner:
                        self.occurrences.append({
                            "owner": owner, "path": ff.path, "line": ref.line, "chain": chain_text(ref.head, ref.attrs),
                            "role": ref.role, "scope": ref.scope, "target": sid, "tier": tier, "type": etype, "n": n,
                            "via": "→".join(via)})
                if unresolved:
                    txt = chain_text(ref.head, ref.attrs)
                    if txt not in residual[owner] and len(residual[owner]) < 50:
                        residual[owner].append(txt)
        self.graph.residual = dict(residual)

    # ================================================================== driver
    def run(self) -> Graph:
        self._register_modules()
        self._register_symbols()
        self._resolve_bases()
        self._resolve_imports()
        self._resolve_refs()
        for e in self.graph.edges.values():
            e.role = Counter(e.roles).most_common(1)[0][0]
            e.lines.sort()
        from backend.kg.derive import derive_overrides, derive_tests

        derive_overrides(self)
        derive_tests(self.graph)
        g = self.graph
        g.stats = {
            "files": len(self.facts),
            "parse_errors": sum(1 for f in self.facts if f.parse_error),
            "symbols": dict(sorted(Counter(s.kind for s in g.symbols.values()).items())),
            "edges": dict(sorted(Counter(e.type for e in g.edges.values()).items())),
            "edges_by_tier": dict(sorted(Counter(f"{e.type}:{e.resolution}" for e in g.edges.values()).items())),
            "ambiguous_module_aliases": self.ambiguous_aliases,
            "residual_chains": sum(len(v) for v in g.residual.values()),
        }
        return g
