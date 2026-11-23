"""Data model of the RepoSense code knowledge graph (v2).

See docs/retrieval/02-design.md §3.  Two layers:

* ``FileFacts``: everything extracted from ONE file, independent of every other file
  (symbols, imports, unresolved references with their scope context).  Pure function of
  (path, content, EXTRACTOR_VERSION); JSON-serialisable; cached per file.
* ``Graph``: the result of global resolution over all FileFacts of a repository:
  ``Symbol`` nodes with stable ids and typed ``Edge``s with provenance and confidence.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

EXTRACTOR_VERSION = "kg-py-1.2"


class Kind:
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"


class EdgeType:
    IMPORTS = "IMPORTS"  # module -> module
    CALLS = "CALLS"  # symbol -> function | method | class (instantiation)
    REFERENCES = "REFERENCES"  # non-call use: annotation, isinstance, decorator, argument, attribute, import_use
    INHERITS = "INHERITS"  # class -> class
    OVERRIDES = "OVERRIDES"  # method -> method (derived)
    TESTS = "TESTS"  # test symbol -> symbol (derived)
    # CONTAINS is stored as Symbol.parent_id, CO_CHANGED arrives with history mining (M4).


class Tier:
    """Resolution tiers (design §3.4).  Order = increasing uncertainty."""

    LOCAL = "R1"  # definition in the same module scope
    IMPORT = "R2"  # import-resolved (relative/absolute, re-export chains, star)
    MEMBER = "R3"  # self / cls / super() / Class.attr through the resolved MRO
    TYPE_INFERRED = "R4"  # parameter/variable annotations, x = Cls(...), self.a = Cls(...)
    FALLBACK = "R5"  # x.m() with unknown x: methods named m in classes of this or directly imported modules
    DERIVED = "D"  # OVERRIDES / TESTS


TIER_ORDER = {Tier.LOCAL: 1, Tier.IMPORT: 2, Tier.MEMBER: 3, Tier.TYPE_INFERRED: 4, Tier.FALLBACK: 5, Tier.DERIVED: 0}


class Flag:
    IS_TEST = 1  # test function/method (test_* in a test file, or a method of a Test* class)
    IN_TEST_FILE = 2
    PRIVATE = 4
    ASYNC = 8
    ABSTRACT = 16
    PROPERTY = 32
    STATICMETHOD = 64
    CLASSMETHOD = 128
    OVERLOADED = 256
    PROTOCOL = 512
    ENUM = 1024
    DATACLASS = 2048


def symbol_id(repo_id: str, path: str, qualname: str) -> str:
    """Stable id: identical across re-indexes of unchanged code (design ADR-02)."""
    return hashlib.sha1(f"{repo_id}\x00{path}\x00{qualname}".encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------- per-file facts
Chain = list  # [head, [attr, ...]]   head: identifier | "<self>" | "<super>" | "<expr>" | "<lit>"


@dataclass
class SymbolFact:
    qualname: str  # "" for the module itself; "C", "C.m", "f", "Outer.Inner"
    name: str
    kind: str
    parent: str | None  # qualname of the parent ("" = module); None for the module
    spans: list[list[int]]  # [[start, end], ...] (several for @overload / redefinitions)
    signature: str = ""
    docstring: str = ""
    body: str = ""
    flags: int = 0
    content_hash: str = ""
    signature_hash: str = ""
    decorators: list[str] = field(default_factory=list)
    bases: list[Chain] = field(default_factory=list)  # classes: base-class expressions
    self_name: str | None = None  # methods: name of the first parameter (self / cls)
    locals: list[str] = field(default_factory=list)  # functions: names bound in the body
    type_env: dict[str, list[Chain]] = field(default_factory=dict)  # local name -> candidate type chains
    attr_types: dict[str, list[Chain]] = field(default_factory=dict)  # classes: attribute -> type chains
    returns: list[Chain] = field(default_factory=list)  # functions: return-annotation chains

    @property
    def start(self) -> int:
        return min(s[0] for s in self.spans)

    @property
    def end(self) -> int:
        return max(s[1] for s in self.spans)


@dataclass
class ImportFact:
    scope: str  # qualname of the binding scope ("" = module level)
    module: str  # absolute module name as written, or path-derived for relative imports
    name: str | None  # `from module import name`; None for `import module`
    asname: str | None
    line: int
    relative: bool = False
    star: bool = False

    @property
    def binding(self) -> str | None:
        if self.star:
            return None
        if self.name is None:
            return self.asname or self.module.split(".")[0]
        return self.asname or self.name


@dataclass
class RefFact:
    owner: str  # qualname of the owning symbol ("" = module)
    head: str
    attrs: list[str]
    line: int
    role: str  # call | reference | annotation | decorator | isinstance | argument | base | default
    scope: str = "local"  # local (function body) | outer (decorators, defaults, annotations) | class (class body)


@dataclass
class FileFacts:
    path: str
    module: str  # path-derived dotted name ("backend.services.rag", "pkg" for pkg/__init__.py)
    is_package: bool
    content_hash: str
    symbols: list[SymbolFact] = field(default_factory=list)
    imports: list[ImportFact] = field(default_factory=list)
    refs: list[RefFact] = field(default_factory=list)
    parse_error: str | None = None
    extractor_version: str = EXTRACTOR_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "FileFacts":
        d = json.loads(text)
        d["symbols"] = [SymbolFact(**s) for s in d["symbols"]]
        d["imports"] = [ImportFact(**i) for i in d["imports"]]
        d["refs"] = [RefFact(**r) for r in d["refs"]]
        return cls(**d)


# ---------------------------------------------------------------------------- resolved graph
@dataclass
class Symbol:
    id: str
    repo_id: str
    path: str
    qualname: str
    name: str
    kind: str
    parent_id: str | None
    start_line: int
    end_line: int
    signature: str
    docstring: str
    body: str
    flags: int
    content_hash: str
    signature_hash: str


@dataclass
class Edge:
    src: str
    dst: str
    type: str
    confidence: float
    resolution: str  # tier of the strongest evidence
    role: str  # most frequent role
    weight: int  # number of occurrences
    lines: list[int] = field(default_factory=list)  # evidence: source lines in src's file (capped)
    roles: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str, str]:
        return self.src, self.dst, self.type


@dataclass
class Graph:
    repo_id: str
    symbols: dict[str, Symbol] = field(default_factory=dict)
    edges: dict[tuple[str, str, str], Edge] = field(default_factory=dict)
    mro: dict[str, list[str]] = field(default_factory=dict)  # class id -> linearisation (in-repo classes only)
    residual: dict[str, list[str]] = field(default_factory=dict)  # owner id -> unresolved in-repo-looking chains
    stats: dict = field(default_factory=dict)
