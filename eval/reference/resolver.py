"""Reference (ground-truth) code graph for Python, built with scope-aware static analysis.

Resolves names through: local scope shadowing, module-level defs, `import`/`from`
imports (absolute + relative), re-export chains through __init__ files, star
imports, `self.`/`cls.` method lookup through resolved base classes, `super()`,
and `Class.method` / `module.attr` attribute chains.

Deliberately conservative (precision-oriented): calls through instance variables
whose type would need inference (e.g. `client = Client(); client.get()`) are NOT
resolved, so ground truth under-counts true dependencies.

FROZEN: this module defines the T1 ground truth.  It is versioned independently of
the production resolver (backend/kg, milestone M1) so that ground truth does not
drift when the system under test changes.  Any behavioural change must bump
REFERENCE_VERSION and regenerate eval/data/t1 (see eval/README.md).
"""
import ast
from collections import defaultdict

REFERENCE_VERSION = "1.0"

FUNC = (ast.FunctionDef, ast.AsyncFunctionDef)


class ModuleInfo:
    def __init__(self, name, file, tree, is_pkg):
        self.name, self.file, self.tree, self.is_pkg = name, file, tree, is_pkg
        self.defs = {}
        self.imports = {}
        self.star = []
        self.import_targets = []  # (lineno, ('mod', m) | ('sym', m, n))

    @property
    def pkg(self):
        return self.name if self.is_pkg else self.name.rpartition(".")[0]


def _module_level_stmts(body):
    for st in body:
        yield st
        if isinstance(st, ast.If):
            yield from _module_level_stmts(st.body)
            yield from _module_level_stmts(st.orelse)
        elif isinstance(st, ast.Try):
            yield from _module_level_stmts(st.body)
            for h in st.handlers:
                yield from _module_level_stmts(h.body)
            yield from _module_level_stmts(st.orelse)


class RefGraph:
    def __init__(self, files):
        self.modules, self.by_file, self.syms, self.classes = {}, {}, {}, {}
        self.lines = {}
        for f in files:
            if not f["path"].endswith(".py"):
                continue
            try:
                tree = ast.parse(f["content"])
            except SyntaxError:
                continue
            p = f["path"][:-3]
            is_pkg = p.endswith("/__init__") or p == "__init__"
            if is_pkg:
                p = p[: -len("/__init__")] if "/" in p else ""
            mod = p.replace("/", ".")
            mi = ModuleInfo(mod, f["path"], tree, is_pkg)
            self.modules[mod] = mi
            self.by_file[f["path"]] = mi
            self.lines[f["path"]] = f["content"].split("\n")
        self._prefixes = set()
        for m in self.modules:
            parts = m.split(".")
            for i in range(1, len(parts) + 1):
                self._prefixes.add(".".join(parts[:i]))
        for mi in self.modules.values():
            self._collect(mi)
        for cid, ci in self.classes.items():
            mi = ci["module"]
            for b in ci["node"].bases:
                r = self._resolve_expr(b, mi, set(), None, None)
                if r and r[0] == "sym" and self.syms[r[1]]["kind"] == "class" and r[1] != cid:
                    ci["bases"].append(r[1])
        self.refs = {}
        self.call_refs = {}
        self.name_calls = {}
        for sid, s in self.syms.items():
            if s["kind"] in ("function", "method"):
                self.refs[sid], self.call_refs[sid] = self._refs_of(sid, s)
        self.callers = defaultdict(set)
        for sid, rl in self.refs.items():
            for t, _ in rl:
                if t != sid:
                    self.callers[t].add(sid)

    # ---------------------------------------------------------------- collect
    def is_module(self, m):
        return m in self._prefixes

    def _collect(self, mi):
        for st in _module_level_stmts(mi.tree.body):
            if isinstance(st, FUNC):
                sid = (mi.file, st.name)
                self.syms[sid] = dict(kind="function", name=st.name, file=mi.file, node=st, module=mi, cls=None)
                mi.defs[st.name] = sid
            elif isinstance(st, ast.ClassDef):
                cid = (mi.file, st.name)
                self.syms[cid] = dict(kind="class", name=st.name, file=mi.file, node=st, module=mi, cls=None)
                self.classes[cid] = dict(methods={}, bases=[], node=st, module=mi)
                mi.defs[st.name] = cid
                for b in st.body:
                    if isinstance(b, FUNC):
                        mid = (mi.file, f"{st.name}.{b.name}")
                        self.syms[mid] = dict(kind="method", name=b.name, file=mi.file, node=b, module=mi, cls=cid)
                        self.classes[cid]["methods"][b.name] = mid
        pkg_parts = mi.pkg.split(".") if mi.pkg else []
        for n in ast.walk(mi.tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.asname:
                        mi.imports[a.asname] = ("mod", a.name)
                    else:
                        top = a.name.split(".")[0]
                        mi.imports.setdefault(top, ("mod", top))
                    mi.import_targets.append((n.lineno, ("mod", a.name)))
            elif isinstance(n, ast.ImportFrom):
                if n.level:
                    up = n.level - 1
                    base_parts = pkg_parts[: len(pkg_parts) - up] if up else pkg_parts
                    base = ".".join(base_parts)
                    full = ".".join(x for x in (base, n.module or "") if x)
                else:
                    full = n.module or ""
                for a in n.names:
                    if a.name == "*":
                        mi.star.append(full)
                        mi.import_targets.append((n.lineno, ("mod", full)))
                    else:
                        mi.imports[a.asname or a.name] = ("sym", full, a.name)
                        mi.import_targets.append((n.lineno, ("sym", full, a.name)))

    # ---------------------------------------------------------------- resolve
    def resolve_name(self, mi, name, depth=0):
        if depth > 10:
            return None
        if name in mi.defs:
            return ("sym", mi.defs[name])
        imp = mi.imports.get(name)
        if imp:
            if imp[0] == "mod":
                return ("mod", imp[1]) if self.is_module(imp[1]) else None
            _, m2, n2 = imp
            full = f"{m2}.{n2}" if m2 else n2
            if self.is_module(full):
                return ("mod", full)
            if m2 in self.modules and self.modules[m2] is not mi:
                return self.resolve_name(self.modules[m2], n2, depth + 1)
            return None
        for sm in mi.star:
            if sm in self.modules and self.modules[sm] is not mi:
                r = self.resolve_name(self.modules[sm], name, depth + 1)
                if r:
                    return r
        return None

    def lookup_method(self, cid, attr, depth=0, seen=None):
        seen = seen or set()
        if depth > 12 or cid in seen:
            return None
        seen.add(cid)
        ci = self.classes.get(cid)
        if not ci:
            return None
        if attr in ci["methods"]:
            return ci["methods"][attr]
        for b in ci["bases"]:
            r = self.lookup_method(b, attr, depth + 1, seen)
            if r:
                return r
        return None

    def resolve_attr(self, base, attr):
        if base[0] == "mod":
            sub = f"{base[1]}.{attr}"
            if sub in self.modules:
                return ("mod", sub)
            if base[1] in self.modules:
                r = self.resolve_name(self.modules[base[1]], attr)
                if r:
                    return r
            return ("mod", sub) if self.is_module(sub) else None
        if base[0] == "sym" and self.syms[base[1]]["kind"] == "class":
            m = self.lookup_method(base[1], attr)
            return ("sym", m) if m else None
        return None

    def _resolve_expr(self, e, mi, locals_, cls, self_name):
        if isinstance(e, ast.Name):
            if cls and self_name and e.id == self_name:
                return ("self", cls)
            if e.id in locals_:
                return None
            return self.resolve_name(mi, e.id)
        if isinstance(e, ast.Attribute):
            v = e.value
            if isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "super" and cls:
                for b in self.classes[cls]["bases"]:
                    m = self.lookup_method(b, e.attr)
                    if m:
                        return ("sym", m)
                return None
            base = self._resolve_expr(v, mi, locals_, cls, self_name)
            if not base:
                return None
            if base[0] == "self":
                m = self.lookup_method(base[1], e.attr)
                return ("sym", m) if m else None
            return self.resolve_attr(base, e.attr)
        return None

    def _refs_of(self, sid, s):
        fn, mi, cls = s["node"], s["module"], s["cls"]
        a = fn.args
        locals_ = {x.arg for x in a.posonlyargs + a.args + a.kwonlyargs}
        if a.vararg:
            locals_.add(a.vararg.arg)
        if a.kwarg:
            locals_.add(a.kwarg.arg)
        is_static = any(isinstance(d, ast.Name) and d.id == "staticmethod" for d in fn.decorator_list)
        self_name = None
        if cls and not is_static and (a.posonlyargs + a.args):
            self_name = (a.posonlyargs + a.args)[0].arg
        skip = {id(x) for d in fn.decorator_list for x in ast.walk(d)}
        glob = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
                locals_.add(n.id)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not fn:
                locals_.add(n.name)
            elif isinstance(n, ast.arg) and n not in a.args:
                locals_.add(n.arg)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                locals_.add(n.name)
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                glob.update(n.names)
        locals_ -= glob
        if self_name:
            locals_.discard(self_name)
        refs, calls = [], []
        name_calls = []  # every bare-Name call site: (name, resolved sym or None)
        for n in ast.walk(fn):
            if id(n) in skip:
                continue
            if isinstance(n, (ast.Name, ast.Attribute)) and isinstance(n.ctx, ast.Load):
                r = self._resolve_expr(n, mi, locals_, cls, self_name)
                if r and r[0] == "sym" and r[1] != sid:
                    refs.append((r[1], n.lineno))
            if isinstance(n, ast.Call):
                r = self._resolve_expr(n.func, mi, locals_, cls, self_name)
                if r and r[0] == "sym" and r[1] != sid:
                    calls.append((r[1], n.lineno))
                if isinstance(n.func, ast.Name):
                    name_calls.append((n.func.id, r[1] if (r and r[0] == "sym") else None))
        self.name_calls[sid] = name_calls
        return refs, calls

    # ---------------------------------------------------------------- reference edge sets
    def import_file_pairs(self):
        out = set()
        for mi in self.modules.values():
            for _, t in mi.import_targets:
                tgt = None
                if t[0] == "mod":
                    tgt = self.modules.get(t[1])
                else:
                    full = f"{t[1]}.{t[2]}" if t[1] else t[2]
                    tgt = self.modules.get(full) or self.modules.get(t[1])
                if tgt and tgt is not mi:
                    out.add((mi.file, tgt.file))
        return out

    def call_pairs(self):
        """(src_file, tgt_file, tgt_bare_name) for resolved calls to functions/methods."""
        cross, same = set(), set()
        for sid, cl in self.call_refs.items():
            for t, _ in cl:
                ts = self.syms[t]
                if ts["kind"] not in ("function", "method"):
                    continue
                key = (sid[0], ts["file"], ts["name"])
                (same if ts["file"] == sid[0] else cross).add(key)
        return cross, same

    def inherit_pairs(self):
        out = set()
        for cid, ci in self.classes.items():
            for b in ci["bases"]:
                out.add(((cid[0], cid[1]), (b[0], self.syms[b]["name"])))
        return out
