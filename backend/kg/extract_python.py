"""Per-file extraction of Python facts with the standard-library ``ast`` module.

``extract(path, content)`` is a pure function: it never looks at other files.  It records
symbols (module, classes, functions, methods; nested classes; NOT closures), imports with
their binding scope, and every maximal name/attribute chain that is *read* together with
its owner symbol, role and scope.  Name binding happens later, globally (resolve.py).
"""
from __future__ import annotations

import ast
import hashlib
import re

from backend.kg.model import EXTRACTOR_VERSION, FileFacts, Flag, ImportFact, Kind, RefFact, SymbolFact

TEST_PATH = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$")
UNWRAP = {"Optional", "Union", "Annotated", "ClassVar", "Final", "Required", "NotRequired", "ReadOnly", "InitVar"}
LITERALS = (ast.Constant, ast.JoinedStr, ast.List, ast.Dict, ast.Set, ast.Tuple,
            ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp)
ENUM_BASES = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}
MAX_BODY = 8000


def module_of(path: str) -> tuple[str, bool]:
    """Path-derived dotted module name and whether the file is a package ``__init__``."""
    p = path[:-3] if path.endswith(".py") else path
    is_pkg = p == "__init__" or p.endswith("/__init__")
    if is_pkg:
        p = p[: -len("__init__")].rstrip("/")
    return p.replace("/", "."), is_pkg


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _ast_hash(*nodes) -> str:
    h = hashlib.sha1()
    for n in nodes:
        h.update(ast.dump(n, annotate_fields=False, include_attributes=False).encode() if isinstance(n, ast.AST) else str(n).encode())
        h.update(b"\0")
    return h.hexdigest()


def chain_of(expr) -> list | None:
    """``a.b.c`` -> ["a", ["b", "c"]]; None for anything that is not a pure name/attribute chain."""
    attrs = []
    while isinstance(expr, ast.Attribute):
        attrs.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        return [expr.id, attrs[::-1]]
    return None


def annotation_chains(ann) -> list[list]:
    """Candidate class chains named by an annotation; unwraps Optional/Union/X | None/strings."""
    if ann is None:
        return []
    if isinstance(ann, ast.Constant):
        if isinstance(ann.value, str):
            try:
                return annotation_chains(ast.parse(ann.value, mode="eval").body)
            except SyntaxError:
                return []
        return []
    if isinstance(ann, ast.BinOp) and isinstance(ann.op, ast.BitOr):
        return annotation_chains(ann.left) + annotation_chains(ann.right)
    if isinstance(ann, ast.Subscript):
        base = chain_of(ann.value)
        if base is None:
            return []
        last = base[1][-1] if base[1] else base[0]
        if last in UNWRAP:
            elts = list(ann.slice.elts) if isinstance(ann.slice, ast.Tuple) else [ann.slice]
            if last == "Annotated":
                elts = elts[:1]
            return [c for e in elts for c in annotation_chains(e)]
        return [base]  # a container (List[X], dict[...]): the value is the container, not X
    ch = chain_of(ann)
    return [ch] if ch else []


def _all_args(args: ast.arguments) -> list[ast.arg]:
    out = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
    if args.vararg:
        out.append(args.vararg)
    if args.kwarg:
        out.append(args.kwarg)
    return out


def _locals(fn) -> list[str]:
    names, glob = set(), set()
    for n in ast.walk(fn):
        if isinstance(n, ast.arg):
            names.add(n.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n is not fn:
            names.add(n.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, ast.Global):
            glob.update(n.names)
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name:
            names.add(n.name)
        elif isinstance(n, ast.MatchMapping) and n.rest:
            names.add(n.rest)
    return sorted(names - glob)


def _type_env(fn) -> dict[str, list]:
    env: dict[str, list] = {}

    def add(name, ch):
        lst = env.setdefault(name, [])
        if ch not in lst:
            lst.append(ch)

    for a in _all_args(fn.args):
        for ch in annotation_chains(a.annotation):
            add(a.arg, ch)
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Call):
            ch = chain_of(n.value.func)
            if ch:
                add(n.targets[0].id, ch)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            for ch in annotation_chains(n.annotation):
                add(n.target.id, ch)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if isinstance(item.optional_vars, ast.Name) and isinstance(item.context_expr, ast.Call):
                    ch = chain_of(item.context_expr.func)
                    if ch:
                        add(item.optional_vars.id, ch)
    return env


class _Extractor:
    def __init__(self, path: str, content: str):
        self.path = path
        self.lines = content.split("\n")
        self.module, self.is_pkg = module_of(path)
        self.package = self.module if self.is_pkg else self.module.rpartition(".")[0]
        self.in_test_file = bool(TEST_PATH.search(path))
        self.symbols: dict[str, SymbolFact] = {}
        self.imports: list[ImportFact] = []
        self.refs: list[RefFact] = []

    # ------------------------------------------------------------------ text helpers
    def _text(self, start: int, end: int) -> str:
        return "\n".join(self.lines[start - 1 : end])

    def _header(self, node) -> str:
        body0 = node.body[0].lineno if node.body else node.lineno
        end = body0 - 1 if body0 > node.lineno else node.lineno
        return self._text(node.lineno, end).rstrip()

    def _outline(self, start: int, end: int, children) -> str:
        """Source of [start, end] with nested def/class blocks collapsed to their header lines."""
        keep, cur = [], start
        for ch in children:
            cs = min([d.lineno for d in ch.decorator_list] + [ch.lineno])
            if cs < cur:
                continue
            keep.append(self._text(cur, cs - 1))
            keep.append(self._header(ch) + (" ..." if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)) else ""))
            cur = ch.end_lineno + 1
        keep.append(self._text(cur, end))
        return "\n".join(k for k in keep if k)[:MAX_BODY]

    # ------------------------------------------------------------------ symbols
    def run(self, tree: ast.Module):
        defs = [s for s in tree.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        non_defs = [s for s in tree.body if s not in defs]
        self.symbols[""] = SymbolFact(
            qualname="", name=self.module, kind=Kind.MODULE, parent=None, spans=[[1, max(1, len(self.lines))]],
            docstring=ast.get_docstring(tree) or "", body=self._outline(1, len(self.lines), defs),
            flags=Flag.IN_TEST_FILE if self.in_test_file else 0, content_hash=_ast_hash(*non_defs),
            type_env=_module_type_env(tree),
        )
        self._block(tree.body, owner="", scope="local")

    def _block(self, stmts, owner: str, scope: str):
        """Module or class body: definitions become symbols; other statements are scanned for refs."""
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._function(st, owner)
            elif isinstance(st, ast.ClassDef):
                self._class(st, owner)
            elif isinstance(st, (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)) or type(st).__name__ == "TryStar":
                # conditional definitions (if TYPE_CHECKING:, try: import ...) at module/class level
                for fname, value in ast.iter_fields(st):
                    if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                        self._block(value, owner, scope)
                    elif fname == "handlers":
                        for h in value:
                            if h.type is not None:
                                self._walk(h.type, owner, scope, "reference")
                            self._block(h.body, owner, scope)
                    elif isinstance(value, ast.AST):
                        self._walk(value, owner, scope, "reference")
                    elif isinstance(value, list):
                        for v in value:
                            if isinstance(v, ast.AST):
                                self._walk(v, owner, scope, "reference")
            else:
                self._walk(st, owner, scope, "reference")

    def _add_symbol(self, fact: SymbolFact) -> SymbolFact:
        old = self.symbols.get(fact.qualname)
        if old is None:
            self.symbols[fact.qualname] = fact
            return fact
        if old.kind != fact.kind and fact.kind == Kind.CLASS:
            # `if TYPE_CHECKING: def X(...)` + `else: class X:` -> the class is the runtime definition
            fact.spans = old.spans + fact.spans
            self.symbols[fact.qualname] = fact
            return fact
        # @overload variants, property setters, conditional redefinitions: one node, several spans
        old.spans.append(fact.spans[0])
        old.body = (old.body + "\n" + fact.body)[:MAX_BODY]
        if not (fact.flags & Flag.OVERLOADED) or old.flags & Flag.OVERLOADED:
            old.signature = fact.signature or old.signature
            old.signature_hash = content_hash(old.signature_hash + fact.signature_hash)
        old.flags |= fact.flags
        old.docstring = old.docstring or fact.docstring
        old.content_hash = content_hash(old.content_hash + fact.content_hash)
        old.decorators += [d for d in fact.decorators if d not in old.decorators]
        old.locals = sorted(set(old.locals) | set(fact.locals))
        for k, v in fact.type_env.items():
            old.type_env.setdefault(k, []).extend(c for c in v if c not in old.type_env[k])
        old.bases += [b for b in fact.bases if b not in old.bases]
        return old

    def _function(self, node, parent: str):
        qual = f"{parent}.{node.name}" if parent else node.name
        parent_kind = self.symbols[parent].kind
        kind = Kind.METHOD if parent_kind == Kind.CLASS else Kind.FUNCTION
        decos = [ast.unparse(d) for d in node.decorator_list]
        flags = Flag.IN_TEST_FILE if self.in_test_file else 0
        if node.name.startswith("_") and not (node.name.startswith("__") and node.name.endswith("__")):
            flags |= Flag.PRIVATE
        if isinstance(node, ast.AsyncFunctionDef):
            flags |= Flag.ASYNC
        for d in decos:
            last = d.split("(")[0].split(".")[-1]
            if last == "abstractmethod":
                flags |= Flag.ABSTRACT
            elif last in ("property", "cached_property", "setter", "getter", "deleter"):
                flags |= Flag.PROPERTY
            elif last == "staticmethod":
                flags |= Flag.STATICMETHOD
            elif last == "classmethod":
                flags |= Flag.CLASSMETHOD
            elif last == "overload":
                flags |= Flag.OVERLOADED
        if self.in_test_file and node.name.startswith("test"):
            flags |= Flag.IS_TEST
        positional = node.args.posonlyargs + node.args.args
        self_name = positional[0].arg if (kind == Kind.METHOD and not flags & Flag.STATICMETHOD and positional) else None
        start = min([d.lineno for d in node.decorator_list] + [node.lineno])
        fact = self._add_symbol(SymbolFact(
            qualname=qual, name=node.name, kind=kind, parent=parent, spans=[[start, node.end_lineno]],
            signature=self._header(node), docstring=ast.get_docstring(node) or "",
            body=self._text(start, node.end_lineno)[:MAX_BODY], flags=flags,
            content_hash=_ast_hash(node), signature_hash=_ast_hash(node.args, node.returns or "", *node.decorator_list),
            decorators=decos, self_name=self_name, locals=_locals(node), type_env=_type_env(node),
            returns=annotation_chains(node.returns),
        ))
        if kind == Kind.METHOD and self_name:
            self._self_attr_types(node, self_name, self.symbols[parent], fact.type_env)
        for d in node.decorator_list:
            self._walk(d, qual, "outer", "decorator")
        for a in _all_args(node.args):
            if a.annotation is not None:
                self._walk(a.annotation, qual, "outer", "annotation")
        for d in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]:
            self._walk(d, qual, "outer", "default")
        if node.returns is not None:
            self._walk(node.returns, qual, "outer", "annotation")
        for st in node.body:
            self._walk(st, qual, "local", "reference")

    def _self_attr_types(self, fn, self_name, cls_fact: SymbolFact, env):
        for n in ast.walk(fn):
            target, chains = None, []
            if isinstance(n, ast.Assign) and len(n.targets) == 1:
                target = n.targets[0]
                if isinstance(n.value, ast.Call):
                    ch = chain_of(n.value.func)
                    chains = [ch] if ch else []
                elif isinstance(n.value, ast.Name):
                    chains = env.get(n.value.id, [])
            elif isinstance(n, ast.AnnAssign):
                target, chains = n.target, annotation_chains(n.annotation)
            if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                    and target.value.id == self_name and chains):
                lst = cls_fact.attr_types.setdefault(target.attr, [])
                lst.extend(c for c in chains if c not in lst)

    def _class(self, node: ast.ClassDef, parent: str):
        qual = f"{parent}.{node.name}" if parent else node.name
        decos = [ast.unparse(d) for d in node.decorator_list]
        # generic bases (`HTTPConnection[StateT]`, `PaginatedListBase[T]`) inherit from the subscripted class
        bases = [c for c in (chain_of(b.value if isinstance(b, ast.Subscript) else b) for b in node.bases) if c]
        base_names = {(b[1][-1] if b[1] else b[0]) for b in bases}
        flags = Flag.IN_TEST_FILE if self.in_test_file else 0
        if node.name.startswith("_"):
            flags |= Flag.PRIVATE
        if "Protocol" in base_names:
            flags |= Flag.PROTOCOL
        if base_names & ENUM_BASES:
            flags |= Flag.ENUM
        if any(d.split("(")[0].split(".")[-1] == "dataclass" for d in decos):
            flags |= Flag.DATACLASS
        if "ABC" in base_names or any(k.arg == "metaclass" and ast.unparse(k.value).endswith("ABCMeta") for k in node.keywords):
            flags |= Flag.ABSTRACT
        start = min([d.lineno for d in node.decorator_list] + [node.lineno])
        children = [s for s in node.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        non_defs = [s for s in node.body if s not in children]
        attr_types: dict[str, list] = {}
        for st in non_defs:
            if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
                attr_types.setdefault(st.target.id, []).extend(annotation_chains(st.annotation))
            elif isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name) and isinstance(st.value, ast.Call):
                ch = chain_of(st.value.func)
                if ch:
                    attr_types.setdefault(st.targets[0].id, []).append(ch)
        self._add_symbol(SymbolFact(
            qualname=qual, name=node.name, kind=Kind.CLASS, parent=parent, spans=[[start, node.end_lineno]],
            signature=self._header(node), docstring=ast.get_docstring(node) or "",
            body=self._outline(start, node.end_lineno, children), flags=flags,
            content_hash=_ast_hash(*node.bases, *node.keywords, *node.decorator_list, *non_defs),
            signature_hash=_ast_hash(*node.bases, *node.keywords, *node.decorator_list),
            decorators=decos, bases=bases, attr_types=attr_types,
        ))
        for b in node.bases:
            self._walk(b, qual, "outer", "base")
        for k in node.keywords:
            self._walk(k.value, qual, "outer", "reference")
        for d in node.decorator_list:
            self._walk(d, qual, "outer", "decorator")
        self._block(node.body, owner=qual, scope="class")

    # ------------------------------------------------------------------ imports and references
    def _import(self, node, owner: str):
        if isinstance(node, ast.Import):
            for a in node.names:
                self.imports.append(ImportFact(owner, a.name, None, a.asname, node.lineno))
            return
        relative = bool(node.level)
        if relative:
            parts = self.package.split(".") if self.package else []
            up = node.level - 1
            parts = parts[: len(parts) - up] if up <= len(parts) else []
            module = ".".join([p for p in parts if p] + ([node.module] if node.module else []))
        else:
            module = node.module or ""
        for a in node.names:
            if a.name == "*":
                self.imports.append(ImportFact(owner, module, None, None, node.lineno, relative, star=True))
            else:
                self.imports.append(ImportFact(owner, module, a.name, a.asname, node.lineno, relative))

    def _emit(self, node, owner: str, scope: str, role: str):
        attrs = []
        cur = node
        while isinstance(cur, ast.Attribute):
            attrs.append(cur.attr)
            cur = cur.value
        attrs.reverse()
        inner = None
        if isinstance(cur, ast.Name):
            head = cur.id
        elif isinstance(cur, ast.Call) and isinstance(cur.func, ast.Name) and cur.func.id == "super":
            head = "<super>"
        elif isinstance(cur, LITERALS):
            head, inner = "<lit>", cur
        else:
            head, inner = "<expr>", cur
        if head not in ("<expr>", "<lit>") or attrs:
            self.refs.append(RefFact(owner, head, attrs, node.lineno, role, scope))
        if inner is not None:
            self._walk(inner, owner, scope, "reference")

    def _walk(self, node, owner: str, scope: str, role: str):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            self._import(node, owner)
            return
        if isinstance(node, (ast.Name, ast.Attribute)):
            if isinstance(node.ctx, ast.Load):
                self._emit(node, owner, scope, role)
            elif isinstance(node, ast.Attribute):
                self._walk(node.value, owner, scope, "reference")
            return
        if role == "annotation" and isinstance(node, ast.Constant) and isinstance(node.value, str):
            try:
                parsed = ast.parse(node.value, mode="eval").body
            except SyntaxError:
                return
            for n in ast.walk(parsed):
                if hasattr(n, "lineno"):
                    n.lineno = node.lineno
            self._walk(parsed, owner, scope, "annotation")
            return
        if isinstance(node, ast.Call):
            self._walk(node.func, owner, scope, "call")
            isinst = isinstance(node.func, ast.Name) and node.func.id in ("isinstance", "issubclass")
            for i, a in enumerate(node.args):
                self._walk(a, owner, scope, "isinstance" if isinst and i == 1 else "argument")
            for kw in node.keywords:
                self._walk(kw.value, owner, scope, "argument")
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for d in getattr(node, "decorator_list", []):
                self._walk(d, owner, scope, "decorator")
            for a in _all_args(node.args):
                if a.annotation is not None:
                    self._walk(a.annotation, owner, scope, "annotation")
            for d in list(node.args.defaults) + [d for d in node.args.kw_defaults if d is not None]:
                self._walk(d, owner, scope, "default")
            if getattr(node, "returns", None) is not None:
                self._walk(node.returns, owner, scope, "annotation")
            for st in node.body if isinstance(node.body, list) else [node.body]:
                self._walk(st, owner, scope, "reference")
            return
        if isinstance(node, ast.ClassDef):  # local class inside a function: part of the owner
            for b in node.bases:
                self._walk(b, owner, scope, "reference")
            for d in node.decorator_list:
                self._walk(d, owner, scope, "decorator")
            for st in node.body:
                self._walk(st, owner, scope, "reference")
            return
        if isinstance(node, ast.AnnAssign):
            self._walk(node.annotation, owner, scope, "annotation")
            if node.value is not None:
                self._walk(node.value, owner, scope, "reference")
            self._walk(node.target, owner, scope, "reference")
            return
        child_role = "annotation" if role == "annotation" else "reference"  # list[Server] | None
        for child in ast.iter_child_nodes(node):
            self._walk(child, owner, scope, child_role)


def _module_type_env(tree) -> dict[str, list]:
    """Module-level instances: ``NotSet = _NotSetType()``, ``X: Cls = ...``."""
    env: dict[str, list] = {}
    for st in _module_level(tree.body):
        if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name) and isinstance(st.value, ast.Call):
            ch = chain_of(st.value.func)
            if ch:
                env.setdefault(st.targets[0].id, []).append(ch)
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
            env.setdefault(st.target.id, []).extend(annotation_chains(st.annotation))
    return env


def _module_level(stmts):
    for st in stmts:
        yield st
        if isinstance(st, (ast.If, ast.Try)):
            yield from _module_level(st.body)
            yield from _module_level(st.orelse)
            for h in getattr(st, "handlers", []):
                yield from _module_level(h.body)


def extract(path: str, content: str) -> FileFacts:
    module, is_pkg = module_of(path)
    facts = FileFacts(path=path, module=module, is_package=is_pkg, content_hash=content_hash(content),
                      extractor_version=EXTRACTOR_VERSION)
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError) as e:
        n = max(1, content.count("\n") + 1)
        facts.symbols = [SymbolFact(qualname="", name=module, kind=Kind.MODULE, parent=None, spans=[[1, n]],
                                    content_hash=content_hash(content))]
        facts.parse_error = f"{type(e).__name__}: {e}"
        return facts
    ex = _Extractor(path, content)
    ex.run(tree)
    facts.symbols = list(ex.symbols.values())
    facts.imports = ex.imports
    facts.refs = ex.refs
    return facts
