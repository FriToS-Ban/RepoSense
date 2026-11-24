# RepoSense Retrieval v2: Technical Design (Phase 2)

| | |
|---|---|
| Status | Proposed. Direction approved 2026-11-24 |
| Inputs | [`01-phase1-audit.md`](01-phase1-audit.md) (baseline S0–S4); design spikes (Appendix A) |
| Decision records | [`03-decisions.md`](03-decisions.md) (ADR-01 … ADR-12) |
| Scope | Knowledge representation, indexing, query execution, ranking, context construction, evaluation |

---

## 0. Summary

RepoSense retrieves **context for reviewing a code change**. The query is a unified diff (plus PR title and description). The consumer is one LLM call with a fixed context budget. The information need is mostly structural:
- what the change depends on
- what depends on what it changed
- which contracts it must honor (base classes, overrides)
- which tests exercise it
- what historically changes with it

v2 replaces chunk-and-embed with four things:

1. **A symbol-level code property graph.** Modules, classes, functions, and methods, with stable identities. Typed edges (`CONTAINS`, `IMPORTS`, `CALLS`, `REFERENCES`, `INHERITS`, `OVERRIDES`, `TESTS`, `CO_CHANGED`), each carrying provenance and a calibrated confidence. Stored in the existing SQL database and traversed from an in-memory CSR snapshot. No graph database, no RDF.
2. **Diff → graph translation (entity linking).** The changed files are parsed at the PR head as an *overlay* on the base graph. Hunks are linked to the symbols they change ("seeds"). A rule-based **query planner** chooses which relations to expand from the change type (body edit, signature change, deletion, new symbol, test change).
3. **Complementary candidate channels fused into one ranking.** Structural (typed expansion + local-push Personalized PageRank), lexical (BM25F over symbol fields, aimed at what structure could not resolve), co-change (mined from history), and dense (pluggable; kept only if it adds measurable recall).
4. **Budget-aware, relation-aware context construction.** The budget is the product. Callees are rendered as signatures and docstrings, callers as call-site windows, bases as class headers plus the overridden method. Selection is a greedy knapsack with containment dedup, per-seed diversity, and provenance labels.

Everything is measured by a three-tier benchmark (structural / history-based / end-to-end), with stage-attributed failure analysis and paired significance tests.

**Evidence that this is the right shape** (Phase 1 + spikes, details in Appendix A):

| Finding | Number | Design consequence |
|---|---|---|
| Changed-file nodes take 74–91% of the v1 budget | oracle upper bound only .40 (deps) / .60 (impact) | Packing v2 with seed-only bodies brings the oracle to **.86–1.00** |
| Lexical retrieval wastes slots on self-hits | 79% of BM25 top-5 come from the changed file | Exclude seeds and the changed file from candidate channels |
| BM25 over symbols, with exclusions and compact renderings, no graph | deps R@budget **.049 → .638**, impact **.201 → .870** | Lexical channel plus packing is already a 13× improvement; the graph has to beat *that* |
| Static resolution coverage of 15,777 call sites | 39.9% resolved in-repo; ~32% builtin/external; light type inference adds only **+1.2 pts** | No investment in type inference early; a low-confidence fallback tier + lexical residual channel handle the rest |
| Full parse + global resolution | **2.2 s** for 97k LOC (pure Python) | Incremental indexing = re-parse changed files + *global* re-resolution + diff-apply. No incremental name resolution needed |
| Compact rendering size | methods: median 38–86 chars vs 136–383 full; classes p90 ≤1k compact vs up to 9.4k full | 3–8× more context items per budget |

---

## 1. Scope, assumptions, and defaults for open decisions

| Topic | Default taken in this design | Can be changed by |
|---|---|---|
| Languages | **Python first** (stdlib `ast`). JS/TS later via tree-sitter, same schema | ADR-03 |
| Dense retrieval | **Pluggable, evaluated, off by default.** No result depends on it | Your decision: NVIDIA key / local model / defer |
| History benchmark (T2) | Designed now; **needs approval to clone pinned public repos** before M4 | Your decision |
| Independent resolver oracle for edge-quality eval | Hand-labeled stratified sample; optionally `jedi` as an eval-only dependency | Your decision (download) |
| Context budget | 12,000 chars (current `llm.py` slice), configurable; token estimate tracked alongside | config |
| LLM | Unchanged (Qwen3-Coder via NIM); **no LLM calls inside retrieval** | — |

**Non-goals:** graph database, RDF/SPARQL, GraphRAG community summaries, LLM-based extraction or query rewriting, distributed infrastructure, dashboards.

---

## 2. Design principles (each traced to evidence)

| # | Principle | Evidence |
|---|---|---|
| P1 | The retrieval unit is a **symbol**, not a character chunk | v1 chunk names, `(file, bare name)` collisions (up to 1,148 nodes), mid-token splits |
| P2 | **Structure first, text for the residual.** Resolvable relations come from the resolver; lexical and dense channels target what it could not resolve and NL in the PR description | 39.9% of call sites resolve in-repo; S4 file-level recall .95 but symbol-level .26 |
| P3 | **The budget is the product.** Optimize Recall@budget with relation-aware renderings | Oracle .40 → .86–1.00 from packing alone |
| P4 | **Never spend retrieval on what the LLM already has** | 79% self/changed-file top-5; 95% top-1 is the query function |
| P5 | **Every edge has provenance and a calibrated confidence** | Bare-name call edges 35–45% precise on bigger repos |
| P6 | **Index state is a pure function of (repo, commit SHA, extractor version)** | 35% edge loss per hub push; 54% orphan vectors after re-index |
| P7 | **Every stage is observable and evaluable offline** | v1 returned empty context for six months unnoticed |
| P8 | **No abstraction without a second implementation.** Interfaces only for vector/embedding backends (Pinecone/local, NVIDIA/none) | Constraint from the brief |

---

## 3. Knowledge graph

### 3.1 Model

A **property graph** in four parts:
- typed nodes and typed, directed edges
- key/value properties on both
- an edge identity of `(src, dst, type)`, with evidence aggregated over occurrences
- persisted in SQL tables (Postgres in production, SQLite in tests and eval)

Traversal and ranking run on an immutable in-memory **GraphSnapshot** (CSR adjacency per edge type and direction) built per `(repo, commit_sha)`. Rationale and alternatives: ADR-01, ADR-02.

### 3.2 Node types

Nodes are the **referable** program entities. Nested functions (closures) are *not* nodes: they cannot be referenced from outside their parent, and their code is part of the parent's body. Nested classes are nodes (`Outer.Inner`).

#### Module
- **Why:** unit of import, ownership, and history. Anchors module-level code: constants, registrations, `__all__`.
- **Information:** `path`, dotted module name, docstring, module-level statement summary (assigned names, top-level calls), `is_package`, `is_test`, `blob_sha`.
- **Extraction:** one per parsed `.py` file. Module name comes from the repo-relative path; `__init__.py` maps to the package.
- **Update:** re-extracted when the file's blob SHA changes; deleted when the file is removed or renamed.
- **Retrieval benefit:** file-level fallback when symbol resolution fails; IMPORTS-scoped name fallback (§3.5 R5); co-change is mined at module granularity.
- **Cost:** O(file bytes) parse. One row per file.

#### Class
- **Why:** the unit of contracts (base/override) and of method-resolution order. Renders cheaply as a header.
- **Information:** qualname, bases (expressions + resolved ids), decorators, docstring, class-level annotated attributes, method signatures (derived), span, `is_abstract`/`is_protocol`/`is_enum`/`is_dataclass` flags.
- **Extraction:** `ClassDef` at module level or nested in classes.
- **Update:** with its module.
- **Retrieval benefit:** INHERITS/OVERRIDES targets. Compact rendering (header + method signatures) gives the LLM the type's interface in about 200 chars (spike C).
- **Cost:** O(class body). Methods are separate nodes, so content is not duplicated: the class body field stores only the header and class-level statements.

#### Function (module-level) and Method (in a class)
- **Why:** the primary unit of change and of dependency. Most diff hunks fall inside one.
- **Information:** qualname, name, kind (`function`/`method`/`staticmethod`/`classmethod`/`property`), signature text (incl. annotations), docstring, body, span, decorators, `is_async`, `is_private`, `is_test`, `content_hash` (normalized: whitespace and comments stripped), `signature_hash`.
- **Extraction:** `FunctionDef`/`AsyncFunctionDef`. `@overload` variants and conditional redefinitions with the same qualname become one node with multiple spans. Property getter/setter/deleter are merged.
- **Update:** with its module. `content_hash` lets unchanged symbols keep their embeddings and lexical postings.
- **Retrieval benefit:** seeds, callees, callers, overrides, tests are all functions or methods.
- **Cost:** O(body). 1.2–3.5 resolved references per symbol in the corpora measured (spike B).

**Considered and rejected as node types for now:**

| Candidate | Why not (yet) |
|---|---|
| Character chunks | Violates P1. Long bodies are split only for *embedding* and *rendering*, never as graph entities |
| Variables / attributes / fields (data-flow nodes) | Needs type inference to be precise; spike A shows the cheap inference tier adds +1.2 pts. Revisit if T2 failure analysis shows "missing data-flow relation" as a dominant failure |
| Statement-level CPG (AST/CFG/PDG, Yamaguchi et al. 2014) | Built for vulnerability pattern queries; increases graph size 10–100×; the retrieval unit stays the symbol |
| External library symbols | Their code cannot be put into context. Kept as unresolved-reference *facts* (e.g. "uses `requests.get`") for lexical and feature use |
| Commit / author / PR nodes | No retrieval benefit beyond aggregated co-change; avoids storing personal data |

### 3.3 Edge types

Every edge has these properties:
- `confidence ∈ (0,1]`
- `resolution` tier (§3.5)
- `weight` (occurrence count or support)
- `evidence` (source line numbers and role)
- `extractor_version`

Edges always point at **definitions**: re-export chains are followed to the defining module.

#### CONTAINS: Module → Class|Function, Class → Method|Class
- **Why:** hierarchy for rendering (a method's class header), containment dedup in packing, and seeds' parents.
- **Information:** structural nesting.
- **Extraction:** AST nesting. **Update:** with module. **Cost:** stored as `parent_id` on the symbol row; no edge rows.
- **Retrieval benefit:** "method changed → include class header"; avoids packing a method and its full class twice.

#### IMPORTS: Module → Module
- **Why:** coarse dependency that is always resolvable. Scopes the name-fallback tier. Drives the reverse-dependency index for incremental updates.
- **Information:** imported module (definition module after re-export resolution), imported names, line.
- **Extraction:** `import`/`from … import`, absolute and relative, star imports; resolved against the repo module table. External imports are kept as facts only.
- **Update:** with module; global re-resolution pass.
- **Retrieval benefit:** low-weight channel feature; restricts R5 fallback candidates. v1 import edges already reach 95% of dependency *files* (S4).
- **Cost:** O(imports). Precision of v1 substring matching was .59–.96; v2 resolution is exact by construction for in-repo modules.

#### CALLS: Function|Method|Module → Function|Method|Class
- **Why:** the core dependency/impact relation.
- **Information:** callee; call-site lines (evidence); `role ∈ {call, instantiate, super_call}`; whether any call site lies on a line changed in the PR (computed at query time).
- **Extraction:** resolver (§3.5) over `ast.Call` nodes in the caller's scope.
- **Update:** global re-resolution after any changed file.
- **Retrieval benefit:** deps (out-edges), impact (in-edges). Callers are rendered as **call-site windows**, which is exactly what a reviewer checks after a signature change.
- **Cost:** O(call sites × resolution-chain depth). 15.7k call sites resolve in about 2.2 s together with everything else.

#### REFERENCES: Function|Method|Module|Class → Class|Function|Method
- **Why:** non-call uses: type annotations, `isinstance`, decorators, callbacks (`add_task(process_pr_review)`), class-constant access (`NodeType.file`). These define data contracts that calls miss.
- **Information:** `role ∈ {annotation, isinstance, decorator, callback, attribute, import_use}`, lines.
- **Extraction:** resolver over `Name`/`Attribute` loads not in call position, plus annotations.
- **Update / Cost:** as CALLS.
- **Retrieval benefit:** deps for types. Phase 1 ground truth contained these: `process_pr_review → PullRequest, PRStatus`.

#### INHERITS: Class → Class
- **Why:** contracts, MRO for `self.`/`super()` resolution, override derivation.
- **Information:** base order index.
- **Extraction:** resolved base expressions, including `module.Base` (v1 handled only bare names).
- **Update:** with module; MRO recomputed globally.
- **Retrieval benefit:** "contract" category; class header of the base in context.
- **Cost:** O(classes). v1 precision/recall was already high (.72–1.0); v2 adds qualified bases.

#### OVERRIDES: Method → Method (derived)
- **Why:** a change to a base method affects every override. A change to an override must respect the base contract. No other edge expresses this.
- **Information:** overridden method; `role ∈ {override, implements_abstract}`.
- **Extraction:** for each method `C.m`, the first `B.m` in `MRO(C)[1:]`. Derived after INHERITS. `implements_abstract` when the base method is `@abstractmethod` or the base is a `Protocol`.
- **Update:** recomputed globally (cheap).
- **Retrieval benefit:** contract candidates both directions. The cost is near zero.

#### TESTS: Function|Method (test) → Symbol (derived)
- **Why:** reviewers ask whether the change is tested, and whether tests changed accordingly.
- **Information:** `confidence 1.0` for a direct CALLS/REFERENCES from a test symbol; `0.5` via one helper hop inside test modules; `0.3` for a module-name convention match (`tests/test_foo.py` ↔ `foo.py`).
- **Extraction:** derived from CALLS/REFERENCES where the source `is_test` (path patterns: `tests/`, `test_*.py`, `*_test.py`, `conftest.py`; names `test_*`).
- **Update:** global derivation.
- **Retrieval benefit:** "tests" category (only measurable on T2: site-packages corpora ship without tests).
- **Cost:** O(test edges).

#### CO_CHANGED: Module ↔ Module (history; symbol-level later if justified)
- **Why:** captures **logical coupling invisible to static analysis**, the evolutionary coupling of Gall et al. (1998) and Zimmermann et al. (2004/2005): string-dispatched handlers, config ↔ code, serializers ↔ models, code ↔ tests, schema ↔ migrations.
- **Information:**
  - `support(a,b)`: time-decayed count of commits touching both
  - `n(a)`
  - `confidence(a→b) = support(a,b) / n(a)`
  - `last_seen`
- **Extraction:**
  - Commits with 2–K code files (K = 20 by default) to suppress tangled and mass changes (Herzig & Zeller, MSR 2013).
  - Each commit contributes `exp(-Δt/τ)`.
  - Cold start: last N commits of the default branch (bounded API or git history).
  - Incremental: from each `push` event.
- **Update:** append-only aggregation; renames re-key paths.
- **Retrieval benefit:** co-change channel and feature. **Leakage control:** evaluation only uses history strictly before the query's base commit (§6.2).
- **Cost:** O(Σ files_per_commit²), bounded by K. Stored in its own table (not CSR-hot).

### 3.4 Identity, provenance, confidence

- **Symbol id** = `sha1(repo_id ⟂ path ⟂ qualname)[:16]`. It is stable across re-indexes, which makes diff-apply and vector reconciliation possible. Renames produce new ids. Rename *pairs* are detected at query time (§5.2) and, during indexing, recorded as `renamed_from` when `content_hash` matches.
- **Provenance** on every node/edge: `extractor_version`, `snapshot_sha`, `resolution` tier, evidence lines. Edges from history carry the commit window.
- **Confidence tiers** (priors; calibrated in M1):

| Tier | Rule | Prior | Spike-A share of all call sites |
|---|---|---|---|
| R1 | Local definition in the same module scope | 1.00 | included in R1–R3: **39.9%** |
| R2 | Import-resolved (absolute/relative, re-export chains, star) | 1.00 | ″ |
| R3 | `self`/`cls`/`super()` through the resolved MRO; `Class.attr` | 0.95 | ″ |
| R4 | Light type inference: parameter/variable annotations, `x = Cls(...)`, `self.a = Cls(...)`, class-body annotations | 0.85 | +1.2% |
| R5 | **Import-scoped name fallback**: `x.m()` with unknown `x`; candidates = methods named `m` in classes defined in modules this module (transitively, depth 1) imports; confidence `0.5/|candidates|`; dropped if `|candidates| > 5` | 0.5/n | ≤ 23% are candidates (untyped locals 12.0%, other expressions 7.1%, self attributes 3.8%) |
| R6 | Repo-wide bare-name match (v1 behavior) | n/a | **disabled**; ablation only |
| H1 | Co-change association | assoc. confidence | n/a |

**Calibration:** set each tier's confidence to its measured precision on a stratified, hand-labeled gold sample, optionally cross-checked against `jedi`. A confidence then *means* something and can be multiplied along paths. *As implemented in M1:* the Laplace posterior mean (k+1)/(n+2), with 40–50 items per tier and R5 split by candidate count. The Wilson lower bound would rank tiers by sample size rather than accuracy. Measured values: R1–R4 1.00 (40/40 each), R5 .56. See [results/M1.md](results/M1.md).

### 3.5 Entity resolution (= name binding) algorithm

For each module:
1. **Collect** definitions (module/class scope), import bindings, star imports, `__all__`.
2. **Resolve names** inside each symbol body:
   - local scope (params, assignments, comprehension targets, `global`/`nonlocal`)
   - enclosing class (for methods via `self`/`cls`)
   - module definitions
   - imports
   - star imports
   - builtins (recorded as external)
3. **Resolve attribute chains** left to right: module → submodule or exported symbol (following re-export chains to the definition, cycle-guarded); class → MRO member; `self`/`super()` → MRO member; else R4 type inference, else R5 fallback, else unresolved (kept as a residual fact with the identifier text).
4. **Emit** edges with tier and evidence. Aggregate multiple occurrences of `(src, dst, type)` into one edge (`weight = count`, `evidence = lines`).
5. **Derive** MRO (C3 linearization over in-repo bases; external bases are opaque), OVERRIDES, TESTS.

This is essentially the reference resolver from Phase 1, upgraded with tiers R4–R5, roles, and spans. It reproduced hand-checked results on RepoSense and httpx (audit §4.2), and its cost is measured (spike B).

### 3.6 Consistency invariants (checked by `kg.validate()` in tests and after every index/update)

1. Every edge's `src` and `dst` exist in the same `(repo, snapshot)`.
2. Every non-module symbol has exactly one `parent_id`, and spans nest within the parent's span.
3. `qualname` is unique per `(repo, path)`; ids are a pure function of `(repo, path, qualname)`.
4. `OVERRIDES(m, b)` ⇒ `parent(b)` ∈ MRO(`parent(m)`) and `name(m) = name(b)`.
5. Vector-store ids ⊆ symbol ids with `embedded_hash = content_hash` (after outbox drain).
6. **Update equivalence:** `apply(update(index(A), A→B)) == index(B)`, i.e. same node and edge sets. Property-tested on random commit pairs (§4.6).

### 3.7 Storage

SQL (Postgres prod / SQLite eval). Sketch; exact types are fixed in M1:

```sql
kg_snapshots (repo_id, commit_sha, extractor_version, status, stats_json, created_at,
              PRIMARY KEY (repo_id, commit_sha))            -- repositories.current_snapshot_sha points here
kg_files     (repo_id, path, blob_sha, language, facts_json, extractor_version,
              PRIMARY KEY (repo_id, path))                  -- per-file extraction cache (FileFacts)
kg_symbols   (id PK, repo_id, path, qualname, name, kind, parent_id, start_line, end_line,
              signature, docstring, body, flags, content_hash, signature_hash)
              INDEX (repo_id, path), INDEX (repo_id, name)
kg_edges     (repo_id, src_id, dst_id, type, confidence, resolution, weight, evidence_json,
              PRIMARY KEY (src_id, dst_id, type)), INDEX (dst_id, type), INDEX (repo_id)
kg_cochange  (repo_id, a_path, b_path, support, n_a, n_b, last_seen, PRIMARY KEY (repo_id, a_path, b_path))
kg_vector_outbox (repo_id, symbol_id, op, content_hash, created_at)   -- drained into the vector backend
retrieval_traces (id, pr_id, repo_id, snapshot_sha, config_hash, created_at, trace_json)
```

**GraphSnapshot** (in memory, per `(repo, sha)`, LRU-cached):
- the symbol table (`id → idx`, `qualname → idx`, `(path, line) → innermost symbol` interval index)
- CSR arrays per edge type in both directions (`array('i')` offsets/targets + `array('f')` confidences/weights)
- the BM25F index

At 10⁵ symbols and 10⁶ edges the CSR arrays are about 8 MB per direction. Build time is dominated by the SQL read.

---

## 4. Indexing architecture

```
push/enable ──► acquire(sha) ──► extract changed files ──► resolve (global) ──► derive ──► diff-apply ──► outbox ──► vectors
                 tarball@sha      FileFacts (cached by blob)   R1–R5 tiers      MRO/OVR/TESTS   one txn       (optional)
                                                                                                   └──► co-change aggregate (push events)
```

### 4.1 Acquisition
- One request: `GET /repos/{o}/{r}/tarball/{sha}`, streamed. This replaces v1's per-file and per-directory Contents API crawl, which made one call per file and failed silently. The commit SHA is pinned, so the snapshot is reproducible.
- Filter by extension and size *before* decoding. Skip vendored and generated directories (`node_modules`, `dist`, `build`, `.venv`, `venv`, `site-packages`, `*_pb2.py`), configurable.

### 4.2 Per-file extraction → `FileFacts`
- Pure function of `(path, content, extractor_version)`. It returns symbols, imports, and *unresolved* references with their scope context (enclosing symbol, local names, `self` name, line).
- Cached in `kg_files` keyed by `blob_sha`. Files whose blob did not change are never re-parsed.
- This isolates per-file work in the same way as GitHub's stack graphs, where file-local graphs make updates incremental, without needing their incremental *resolution*.

### 4.3 Global resolution and derivation
- Builds the module table from all FileFacts, resolves every reference (§3.5), then derives MRO, OVERRIDES, and TESTS.
- **Measured cost (spike B):** 0.03–0.94 s per library and **2.18 s for the 97k-LOC merged corpus**, pure Python, single core, including parsing. Re-running global resolution on every push is therefore cheaper and simpler than incremental name resolution (ADR-08).

### 4.4 Persistence by diff (consistency by construction)
- Compute `(nodes_new, edges_new)` and diff them against the stored rows for the repo: insert, update (hash changed), and delete sets. Apply them in **one transaction** with the snapshot row flip.
- Deleted or changed symbols enqueue `kg_vector_outbox` rows in the *same* transaction. A drainer applies them to the vector backend idempotently (upsert by deterministic id / delete by id). Orphan vectors become impossible by construction (invariant 5).

### 4.5 Lexical and dense indexes
- **BM25F** is built in memory from `kg_symbols` when the snapshot loads (≈ms per 1k symbols). Nothing is persisted, so no consistency problem.
- **Dense** (optional): embed only symbols whose `content_hash` changed. Batched requests. Documents use `input_type="passage"` and queries use `"query"`, fixing v1's misuse. A cache keyed by `(model, input_type, text_hash)` means re-indexing the same content costs zero embedding calls. Pinecone namespace = repo; ids = symbol ids. A local brute-force backend serves eval and small repos.

### 4.6 Incremental update on `push`
1. Read `before`/`after` SHAs from the payload. Use the compare API to get added, modified, removed, and renamed files. v1 ignored `removed` and renames.
2. Re-extract FileFacts for added and modified files at `after`. Drop removed files. Re-key renamed ones.
3. Run global resolution and derivation over all FileFacts (cached ones included).
4. Diff-apply (§4.4). Update co-change with the pushed commits.
5. Serialize per repo: a per-repo advisory lock (`pg_advisory_xact_lock`), and a pending update coalesces into the next one. This removes v1's thread races between a full index and push re-indexes.

**Correctness test (property-based):** for random consecutive commit pairs of real repositories (T2 corpora), `update(index(A), A→B)` must equal `index(B)` (invariant 6).

### 4.7 Cost model

| Operation | v1 | v2 |
|---|---|---|
| Acquire repo | 1 call per file + per dir | 1 call (tarball) |
| Index N symbols | 3N sequential calls (list_indexes + embed + upsert) | 0 network calls without dense; with dense, ⌈changed/batch⌉ embed + ⌈changed/batch⌉ upsert |
| Edge pass | O(files × nodes) Python scans; 24.5 s at 11.7k nodes | ≈2 s parse+resolve at 97k LOC |
| Push touching k files | k fetches; destroys incoming edges | 1 compare + k fetches; exact |
| DB lookups | sequential scans (no indexes) | indexed |

---

## 5. Query execution pipeline

```
diff, PR title/body, base/head SHAs
   │
   ▼  S0 parse diff (files → hunks → old/new line sets; renames, deletions, binary)
   ▼  S1 overlay + seed linking (entity linking) + query plan
   ▼  S2 candidate channels:  structural │ lexical (residual) │ co-change │ dense (opt)
   ▼  S3 fusion & ranking (RRF → linear LTR)
   ▼  S4 context construction (relation-aware renderings, budgeted selection, ordering, labels)
   ▼  S5 trace (persisted)
context string ──► llm.get_review_from_llm (unchanged)
```

### 5.1 S0: Diff parsing
A real unified-diff parser replaces v1's `extract_diff_chunks`. Output per file:
- old and new path, status (added, modified, deleted, renamed), binary flag
- hunks with old and new ranges
- the sets of added, removed, and modified-context lines

No header pseudo-hunks, no 8-chunk cap: hunks are not embedded one by one any more.

### 5.2 S1: Overlay, seed linking, query planning
- **Overlay.**
  - Fetch the head content of changed `.py` files (cached by blob SHA) and extract their FileFacts.
  - Resolve their references against the **base snapshot's** symbol table, with the overlay's own symbols shadowing the base symbols of those paths.
  - Base symbols of changed paths that are absent at head are **removed**. A head symbol with a new qualname but the same `content_hash` or signature as a removed one is a **rename** pair.
  - Cost: milliseconds plus k file fetches.
- **Seeds.** Each changed line maps to its innermost enclosing symbol through an interval index:
  - new lines map into the overlay
  - removed lines map into the base

  Each seed carries a change type:

| Change type | Detection |
|---|---|
| `added` | qualname not in base |
| `removed` | qualname not at head |
| `renamed` | rename pair |
| `signature_changed` | `signature_hash` differs |
| `body_changed` | otherwise |
| `test_changed` | seed `is_test` |

- **Mentions.** References that occur *on changed lines* and resolve to in-repo symbols are "mention" candidates with a strong prior. Unresolved identifiers on changed lines become **residual terms** for the lexical channel.
- **Query plan.** Rule-based and deterministic; it decides which relations get candidate quota:

| Seed change type | Relations expanded (priority order) |
|---|---|
| body_changed | callees/refs on changed lines → other callees → base method (OVERRIDES↑) → tests |
| signature_changed | **callers (impact)** → overrides (both ways) → callees on changed lines → tests |
| removed / renamed | **callers** (they now break) → overrides → tests |
| added | callees/refs → sibling methods of the parent class → base contract |
| test_changed | tested symbols (TESTS↓) → their callees |
| module-level change | importers (IMPORTS↑) → referenced symbols |

This is the "query-to-graph translation": the diff becomes a set of typed seeds and a relation plan, with no LLM involved.

### 5.3 S2: Candidate channels

**Structural.**
- *1-hop typed expansion* per the plan. Each candidate records its best path: type, direction, confidence product, and whether evidence lies on a changed line.
- *Multi-hop:* approximate **Personalized PageRank by local push** (Andersen, Chung & Lang, 2006) from the seeds, over a weighted transition graph:
  - deps relations forward, impact relations reversed
  - weights `w_type × confidence`, restart probability α
  - cost O(1/(ε·α)), independent of graph size

  PPR supplies 2-hop candidates and a smooth proximity feature. BFS-with-types is the ablation (ADR-06). α, ε, and the type weights are tuned on T1-dev and validated on T2.
- *Popularity damping:* candidates with very high fan-in (logging helpers, base utilities) are down-weighted through a feature, not a hard rule.

**Lexical (BM25F, residual-oriented).**
- One document per symbol. Fields and initial weights, tuned in M6:

| Field | Initial weight |
|---|---|
| `name` | 3.0 |
| `qualname` + `path` tokens | 1.5 |
| `signature` | 2.0 |
| `docstring` | 1.0 |
| body identifiers | 1.0 |
| comments/strings | 0.5 |

  Per-field length normalization follows Robertson, Zaragoza & Taylor (2004).
- Tokenizer: full identifier plus camel/snake sub-tokens; language-keyword stoplist; no stemming on identifiers.
- Query terms:

| Term source | Weight |
|---|---|
| Residual unresolved identifiers on changed lines | 2.0 |
| All identifiers on changed lines | 1.0 |
| PR title/body tokens | 1.0, NL field-weighted toward `docstring` |

- **Exclusions:** seeds and all symbols of changed files (P4).
- Pseudo-relevance feedback (RM3) is an ablation, not a default.

**Co-change.** Modules co-changed with the seeds' modules (confidence ≥ threshold). Symbols inside them are ranked by lexical similarity to the changed lines, or by structural proximity if any exists.

**Dense (optional).**
- Query = changed lines with ±3 lines of context, and separately the PR title/body.
- Documents = symbol compact rendering + body prefix, within the model's input limit.
- Kept only if it adds unique relevant candidates on T2 at acceptable cost (ADR-10).

### 5.4 S3: Fusion and ranking
- **Baseline: Reciprocal Rank Fusion** (Cormack, Clarke & Büttcher, 2009), `k = 60`, per-channel weights. Robust and needs no score calibration.
- **Target: a linear learning-to-rank model.** Logistic regression, pointwise; pairwise as an ablation. Implemented in-house (a few dozen lines). Trained on T2-train (temporal split) with leave-one-repo-out cross-validation; weights reported for interpretability. Features:
  - **structural:** best-path relation type (one-hot: callee/caller/base/override/test/2-hop/mention), path confidence product, evidence-on-changed-line, PPR score (log), number of seeds reached, edge weight (call count)
  - **lexical:** BM25F score and rank, residual-term exact-name match
  - **co-change:** confidence, log support, recency
  - **dense:** cosine and rank (if enabled)
  - **node:** kind, log size, log fan-in, `is_test`, `is_private`, package distance to seed
- **Per-seed diversity:** a round-robin quota guarantees each seed at least `m` candidates before any seed gets more, so one large hunk cannot crowd out the others.

### 5.5 S4: Context construction (budgeted, relation-aware)

Each candidate has relation-specific **renderings**, with a cost (chars) and a utility (rank score × rendering gain):

| Relation to seed | Default rendering | Upgrade |
|---|---|---|
| Seed (changed symbol) | head-version body, capped at 4,000 chars; if larger, signature + windows around hunks | n/a |
| Callee / referenced | signature + docstring summary (+ class header line for methods) | full body if top-ranked and small |
| Caller (impact) | **call-site window** (±3 lines around evidence lines) + caller signature | full body |
| Base class / overridden method | class header + method signature + docstring | full method |
| Override (subclass) | method signature + class line | full method |
| Test | test signature + call-site window | full test |
| Co-change / lexical-only | compact | full |

Selection algorithm:
1. Seeds first, within a seed cap: at most 40% of the budget in total.
2. Greedy multiple-choice knapsack. Every ranked candidate first enters at its cheapest useful rendering in rank order, within per-seed quotas. Remaining budget then **upgrades** the highest-utility items to fuller renderings.
3. Containment dedup: a full class covers its methods; a caller window inside an included body is dropped.
4. Ordering: grouped by seed, then by relation priority from the plan, strongest items first. This puts critical context at the edges of the prompt, where LLMs use it best (Liu et al., 2024, "Lost in the Middle").
5. Each item gets a **provenance label**, e.g. `### httpx/_models.py :: Response.json  [callee of Client.send · called on changed line 912]`.
6. An optional **change-summary header** of at most 400 chars, derived from the graph: "`Client.send` signature changed; 4 callers in 3 files; overrides `BaseClient.send`". Graph-based context construction; its value is measured in T3 only.

**Spike D evidence:** with seed-only bodies and compact renderings, the oracle reaches **1.00** recall (deps and impact) at 2.2k chars. That leaves most of the budget for upgrades and for recall from imperfect channels. Full-body-only packing reaches .855/.943, while lexical-only candidates gain the most from compact renderings (.345 → .638 deps).

### 5.6 S5: Trace
A persisted JSON trace per review:
- seeds and change types
- the plan
- per-channel candidates with scores
- fused ranks
- packing decisions (rendering, included or dropped, reason)
- the final chars used
- config hash and snapshot SHA

This makes production retrieval evaluable offline: production traces can be replayed and judged, and failures can be diagnosed by stage.

### 5.7 Per-review latency and cost budget (targets, to be measured)

| Stage | Network | CPU target (p95, 10⁵-symbol repo) |
|---|---|---|
| Snapshot load (cache miss) | DB read | < 2 s; cache hit ≈ 0 |
| S0 + S1 overlay | k head-file fetches (cached by blob) | < 50 ms |
| S2 structural (1-hop + local-push PPR) | — | < 50 ms |
| S2 lexical BM25F | — | < 50 ms |
| S2 dense (optional) | 1–2 embed + 1 vector query | network-bound |
| S3 + S4 | — | < 20 ms |

v1 for comparison: up to 8 embed + 8 vector queries + about 22 SQL statements per changed file, with sequential scans.

---

## 6. Evaluation framework

### 6.1 Your questions → measurements

| Question | Measurement |
|---|---|
| Which queries fail? | Per-query metrics in TREC run files; worst-k lists per category and corpus |
| Why do they fail? | Stage-attributed failure taxonomy (§6.4) per ground-truth item |
| Are relevant entities missing? | `entity_missing` rate (ground-truth symbol not in the KG) |
| Are relationships missing? | `relation_missing`: no KG path ≤ 2 from any seed. On T1, compared with the reference graph; on T2, the share of "non-structural" coupling |
| Is candidate generation poor? | `not_generated` rate; per-channel recall; channel-union recall (oracle fusion) |
| Is ranking poor? | `ranked_out` rate; oracle-ranking ablation (perfect ranking of the generated pool) |
| Is the graph introducing noise? | Per-channel packed precision; ablation without structural/PPR; share of budget spent on non-relevant structural items |
| Does reranking improve relevance? | LTR vs RRF, paired randomization test on nDCG@10 and Recall@budget |
| Does retrieval degrade as the dataset grows? | Scaling curves: metrics, latency, and index time vs #symbols (monorepo merges on T1; large repos on T2) |
| Cost? | Index time, peak memory (`tracemalloc`), rows, per-stage latency, network calls per index/review |

### 6.2 Benchmarks

**T1: Structural (synthetic diffs, static-analysis ground truth).** Exists in the Phase 1 harness; ported in M0.
- Corpora: RepoSense (pinned commit via `git archive`), plus pinned-version libraries. The manifest records sdist/sha256; files are cached locally.
- Categories:
  - `deps-1hop`
  - `deps-2hop`
  - `impact`
  - `contract` (override/base): new
  - `cross-file` vs `same-file`
- **Role and circularity control:** the ground-truth resolver is frozen in `eval/reference/` and versioned independently of the production resolver. Even so, T1 rewards graph methods by construction. **T1 is used for regression gates and pipeline-loss diagnostics, not for graph-vs-text claims.**

**T2: History-based (real commits, non-circular).** Built in M4; needs cloning approval.
- Corpora: 6–8 Python repos with real history and tests, pinned SHAs, medium size. Candidates are BSD/MIT projects such as httpx, starlette, flask, click, rich, attrs, requests. Final list in M4.
- Query mining:
  - non-merge commits (or squash-merged PRs) with 2–8 code files changed, after a **temporal cutoff**
  - filters for formatting-only and mass-rename commits, and tangled commits (size)
- Query: the diff of the commit's largest code file (the anchor), plus the commit subject as the NL description.
- Ground truth: symbols modified in the *other* files of the same commit (grade 2) and their modules (grade 1, for file-level metrics). Changed tests are tagged and become the `tests` category.
- Snapshot = parent commit, which exercises full and incremental indexing on real history.
- **Leakage controls:**
  - co-change is computed only from commits before the parent
  - the LTR model is trained on commits before the test window
  - per-repo leave-one-out
- **Known biases:**
  - co-change ground truth omits context that was *read but not changed* (e.g. a callee the author consulted), so it under-rewards deps
  - tangled commits add noise
  - both are reported, with a pooled manual-judgment subset (TREC-style pooling of top-k across systems) to estimate unjudged-relevant rates
- Size target: 500–1,000 queries.

**T3: End-to-end (optional, LLM cost).** M10.
- Inject bugs that are only detectable with cross-file context, into real code at pinned SHAs:

| Mutation | Context needed to catch it |
|---|---|
| Argument swap / wrong arity against a callee in another file | callee signature |
| Removed or renamed base method still called via `super()` | base class |
| Signature change without updating callers | callers |
| Wrong return-type use | callee's return contract |
| Violated documented precondition | callee docstring |

- Run the real LLM review under each retrieval configuration.
- Score: detection (comment within ±3 lines of the injected line), plus a false-positive rate on unmutated control diffs.
- 100–200 cases. Measures whether better retrieval produces better reviews, the question T1/T2 cannot answer.

### 6.3 Metrics and statistics
- **Ranked list:** P@K, R@K, Hit@K, MRR, nDCG@K (graded, gains 2^g−1; Järvelin & Kekäläinen 2002) for K ∈ {5, 10, 20}.
- **Context:**
  - **Recall@budget**: the fraction of ground-truth symbols present in the packed context *in a relation-appropriate rendering*. A caller counts only if its call-site window or body is included.
  - Secondary: `covered_full`, packed precision, and the budget share by relation and by channel.
- **Statistics:**
  - bootstrap 95% CIs (2,000 resamples)
  - **paired randomization (permutation) tests**, 10⁴ permutations, for system comparisons (Smucker, Allan & Carterette, 2007)
  - Holm–Bonferroni correction across ablation grids
  - effect sizes reported, not just p-values
- **Implementation:** in-house metric code with unit tests against hand-computed cases; run files in TREC format so `trec_eval` can cross-check.

### 6.4 Failure taxonomy (per ground-truth item, first failing stage)
1. `entity_missing`: no KG node for the symbol (extraction error, unsupported construct)
2. `relation_missing`: for structural channels, no path ≤ 2 from any seed (T1: although the reference graph has one; T2: "non-structural coupling")
3. `not_generated`: in no channel's candidate list
4. `ranked_out`: generated, but its fused rank is beyond what packing could reach
5. `packed_out`: ranked within reach but excluded by budget, quota, or containment
6. `covered`

**Noise accounting:** every packed non-ground-truth item is attributed to its contributing channels. "Unjudged" is distinguished from "non-relevant" using the pooled judgments.

### 6.5 Systems under test

| ID | System |
|---|---|
| S0–S4 | Phase 1 baselines (as shipped; fixed+oracle; fixed+BM25; BM25 raw; v1 graph) |
| **A** | BM25F lexical + v2 packing |
| **B** | Dense + v2 packing *(needs the dense decision)* |
| **C** | A + B, RRF |
| **D** | C + LTR rerank |
| **E** | Graph-guided: seeds + plan + typed 1-hop + PPR + v2 packing |
| **F** | E + C + co-change + LTR (full v2) |
| Oracles | perfect candidates / perfect ranking (upper bounds per stage) |

**Ablations:**
- each channel removed
- each edge type removed
- confidence tiers R4/R5 on/off
- PPR vs BFS, and the α sweep
- RRF vs LTR
- rendering tiers on/off
- per-seed quotas on/off
- query plan vs uniform expansion
- residual weighting on/off
- PRF on/off
- budget sweep (4k/8k/12k/24k chars)

### 6.6 Regression gates
- `pytest` unit tests: diff parser, extractor, resolver (fixture modules for every resolution rule), invariants, PPR, BM25F, packing, metrics.
- `tests/test_quality_gate.py` runs T1-mini on a pinned RepoSense commit plus one pinned library. Assertions: fixed thresholds on Recall@budget and nDCG@10, call counts, and **no network**. Deterministic (seeded, pinned inputs), so the tolerance is zero for T1-mini.
- `eval/compare.py` prints metric deltas, CIs, and p-values, plus a failure-taxonomy diff between two runs. Required for any retrieval PR.

### 6.7 Layout and formats
```
eval/
  manifest/            corpora: name, source (git sha | sdist url+sha256), license
  reference/           frozen ground-truth resolver (versioned)
  benchmarks/t1/ t2/ t3/   generators/miners; output: queries.jsonl + qrels (TREC) per corpus
  systems/             adapters: s0..s4 (v1 replay), A..F (v2 configs)
  metrics.py stats.py failure.py
  run.py compare.py    CLI: run a system on a benchmark → run file + metrics.json + traces
  results/             committed metrics JSON for baselines and milestones (small)
```

---

## 7. Code organization: what changes where

| Area | Change |
|---|---|
| `backend/kg/` (new) | `extract_python.py` (FileFacts), `resolve.py` (tiers, MRO), `derive.py` (OVERRIDES/TESTS/CO_CHANGED), `store.py` (diff-apply, outbox), `snapshot.py` (CSR, interval index, LRU), `validate.py` (invariants), `indexer.py` (acquire → apply; push update) |
| `backend/retrieval/` (new) | `diff.py`, `seeds.py` (overlay, linking, plan), `structural.py` (typed expansion, local-push PPR), `lexical.py` (BM25F), `cochange.py`, `dense.py` (backend interface: Pinecone, local, none), `fusion.py` (RRF, LTR), `packing.py`, `pipeline.py`, `trace.py` |
| `backend/services/rag.py` | becomes a facade: `RETRIEVAL_ENGINE=v1|v2` feature flag; v1 kept until cutover |
| `backend/services/indexer.py` | facade delegating to `kg.indexer` under the flag |
| `backend/services/github.py` | add `get_tarball(sha)`, `get_file_at(path, ref)`, `compare(base, head)` |
| `backend/api/routes/webhooks.py` | push: pass `before`/`after`, removed and renamed files; PR: pass base/head SHAs, title, body |
| `backend/services/review.py` | pass head/base SHAs + title/body to retrieval; store the trace id |
| `backend/models/models.py`, `main.py` | kg_* and trace tables with indexes; replace raw `ALTER TABLE` startup SQL with Alembic migrations (already a dependency, unused) |
| `eval/`, `backend/tests/` | new (§6) |
| Dependencies | `pytest` (dev). `numpy` only when dense/LTR lands. `tree-sitter` only for JS/TS. Optionally `jedi` (eval-only). Nothing else |

---

## 8. Migration strategy

1. **Hotfix v1 first (M0):** the one-line `rag.py` fix plus a regression test, so production stops reviewing blind while v2 is built. Note: this raises LLM prompt size by up to 12k chars per review.
2. **Build v2 alongside v1** behind `RETRIEVAL_ENGINE` (default `v1`). New tables only; v1 tables untouched.
3. **No data migration.** v1 graph data is decayed (orphans, deleted edges) and at the wrong granularity, so v2 does a full index into kg_* tables. Pinecone moves to namespace-per-repo with symbol ids if dense is enabled. The old index is dropped at cutover.
4. **Shadow mode (optional):** compute v2 context and traces for real PRs without sending them to the LLM, and compare offline.
5. **Cutover criteria:**
   - F (or the best configuration) beats S2-fixed and A on T2 Recall@budget with p < 0.01
   - no T1 regression
   - p95 latency within budget
   - invariant and update-equivalence tests green
   - then: flip the default, keep v1 code for one release, then delete `code_nodes`/`code_edges` and the v1 paths
6. **Rollback:** the flag returns to v1. v2 tables are inert.

---

## 9. Prioritized implementation plan (Phase 3 milestones)

Ordering follows **measured loss magnitudes** first, then dependencies. Each milestone delivers:
- the algorithm, documented
- the affected modules
- tests
- a benchmark run
- a comparison against the baseline
- a short results note in `docs/retrieval/results/`

| M | Deliverable | Why now (evidence) | Exit criteria |
|---|---|---|---|
| **M0** ✅ | `eval/` package (port of the Phase 1 harness: metrics + stats with unit tests, T1 generator, S0–S4 adapters); v1 hotfix + regression test; test setup (stdlib `unittest`, pytest-compatible) | Nothing can be claimed without it; W1 went unnoticed | **Done:** [results/M0.md](results/M0.md). S0/S2/S3/S4 reproduced exactly; S1 made deterministic (.388); 25 tests green |
| **M1** ✅ | KG v2 for Python: FileFacts, resolver tiers R1–R5, derive, store with diff-apply, snapshot, invariants; edge-quality eval vs gold sample (+ optional jedi); tier calibration | Graph precision 35–45% on bigger repos; zero function out-edges | **Done:** [results/M1.md](results/M1.md). 0 violations; R1–R4 precision 1.00 (40/40 each), R5 .56; recall .96 |
| **M2** ✅ | Query pipeline skeleton: diff parser, overlay, seeds, plan, BM25F (default weights), v2 packing, traces → **System A** | Packing is the dominant measured loss (oracle .40 → 1.00) | **Done:** [results/M2.md](results/M2.md). Recall within budget: deps .599, impact .899 (vs S2: +.55 / +.70, p_holm 8e-4); p95 67 ms |
| **M3** ✅ | Structural channel (typed 1-hop, plan-driven) + RRF → **System E** and **E+A** | Graph has the right files (S4 file recall .95) but not the symbols | **Done:** [results/M3.md](results/M3.md). E+A deps R@10 .939, recall within budget 1.00 (circular on T1); lexical packed items only 5–8% judged relevant |
| **M4** ✅ | **T2 benchmark** (cloning approval) + co-change mining + channel | The first non-circular test of graph vs text | **Done:** [results/M4.md](results/M4.md). T2 v1.0: 300 commits, 1,122 judged symbols. A .617 R@budget vs S2 .045; E+A .630 (n.s. vs A) but R@10 −.063 (p_holm .011); 1-hop graph reaches 26% of co-changed symbols; co-change as an RRF channel −.076 |
| **M5** ✅ | Local-push PPR multi-hop; `deps-2hop` and `contract` categories | Nothing in v1 targets 2-hop (R .06–.20) | **Done:** [results/M5.md](results/M5.md). T1 2-hop R@budget .04 → .62 (PPR) / .68 (BFS); on T2, PPR > BFS (+.040, p_holm 4e-4) and pure PPR .336 vs 1-hop .222; flat RRF with lexical lowers recall; α = 0.1 (T1-dev); `contract` deferred |
| **M6** | BM25F field weights + query term weighting (residual boost, PR text), PRF ablation | Lexical channel is the strongest single baseline | Tuned on T1-dev, validated on T2; report per-field contribution |
| **M7** ✅ | Incremental indexing v2: tarball acquisition, compare-driven updates, outbox, locks; update-equivalence property tests on T2 history | v1 loses 35% of edges per hub push | **Done:** [results/M7.md](results/M7.md). 240/240 code-changing real commit pairs equivalent, zero orphans; diff-apply 40× faster (ORM synchronisation bug); v2 wired behind `RETRIEVAL_ENGINE` |
| **M8** | Dense channel (per your decision): backends, passage/query types, embedding-model comparison → **B**, **C** | Unmeasured; v1 misconfigured | Dense kept only if C > A on T2 (p < 0.01) or it adds ≥5% unique relevant candidates at acceptable cost |
| **M9** | LTR reranker (features §5.4) → **D**, **F**; cross-encoder only as an ablation if a code-capable model is available locally | Fusion quality matters once channels exist | F vs E+A vs C on T2; feature weights reported |
| **M10** | T3 end-to-end bug-injection (optional, LLM cost) | Proves review quality, not just retrieval | Detection-rate deltas with CIs; FP rate on controls |
| **M11** | Cutover (§8), v1 removal, JS/TS via tree-sitter as a follow-on | — | Cutover criteria met |

Phase 4 (research and optimization) runs the ablation grid of §6.5 continuously from M3 on. Phase 5 consolidates `docs/retrieval/` into the project's technical presentation.

---

## 10. Risks and open questions

| Risk | Mitigation |
|---|---|
| T1 circularity flatters the graph | T1 is restricted to gates and diagnostics; graph-vs-text claims require T2/T3 |
| T2 co-change ground truth under-rewards read-only context (deps) | Report per category; pooled manual judgments; T3 as the arbiter |
| Dynamic Python (metaprogramming, `getattr`, DI frameworks) defeats static resolution | R5 fallback + lexical residual + co-change; failure taxonomy will show the residual size |
| Head-version fetch per changed file adds latency | Cache by blob SHA; fetch in parallel; reconstruct from base + patch when base == snapshot |
| LTR overfits small T2 | Leave-one-repo-out CV, temporal splits, ≤ 25 features, L2 regularization |
| Compact renderings hide the code the LLM needed | Recall@budget counts relation-appropriate renderings only; `covered_full` reported; T3 checks the end effect |
| Scope creep into "production" work | Production items are included only where they protect consistency, reproducibility, or evaluability (M7, traces) |

---

## Appendix A: Design spikes (scratch harness, 2026-11-24)

Same corpora and queries as the Phase 1 audit (seed 13): 224 deps + 113 impact queries.

**A. Call-site resolution coverage (15,777 call sites, 6 corpora)**

| Class | Share |
|---|---|
| Resolved in-repo (R1–R3) | 39.9% |
| Builtins | 21.2% |
| External import names / external module attributes | 6.1% / 4.7% |
| Attribute call on **untyped** local/param | 12.0% |
| Other expression bases (call results, subscripts, chains) | 7.1% |
| `self.attr.m()` with uninferred attribute type | 3.8% |
| Callable locals/params | 1.7% |
| **Resolved by light type inference (R4)** | **+1.2%** |
| Unresolved bare names | 0.9% |

Per corpus, in-repo resolution ranges from 9.5% (RepoSense: mostly ORM/SDK calls) to 70.0% (PyGithub).

**B. Full parse + global resolution time (pure Python, single core):** RepoSense 0.03 s, httpx 0.15 s, starlette 0.17 s, fastapi 0.14 s, pydantic 0.77 s, PyGithub 0.94 s, merged 97k-LOC monorepo 2.18 s.

**C. Rendering sizes (chars, median / p90)**

| | full | compact (signature + docstring summary; classes + method signatures) |
|---|---|---|
| methods | 136–383 / 762–2,751 | 38–86 / 188–1,691 |
| functions | 277–648 / 531–5,841 | 82–134 / 154–2,035 |
| classes | 263–2,380 / 765–9,407 | 30–429 / 37–987 |

**D. Packing simulation (cross-file Recall@12k chars; seed = the changed symbol only)**

| Candidates | Rendering | deps | impact | items packed |
|---|---|---|---|---|
| v1 pipeline, oracle (S1) | v1 | .404 | .597 | 16 |
| oracle | full bodies | .855 | .943 | 2.3–2.6 |
| oracle | compact | **1.000** | **1.000** | 2.9–3.0 |
| oracle | top-3 full + compact | .865 | .963 | 2.5–2.7 |
| v1 pipeline, BM25 (S2) | v1 | .049 | .201 | 17 |
| BM25 over symbols, changed file excluded | full | .345 | .668 | 8.4–9.5 |
| ″ | compact | **.638** | **.870** | 32–33 |
| ″ | top-3 full + compact | .524 | .773 | 24 |

Caveat for D: coverage here counts any rendering. Production Recall@budget (§6.3) counts only relation-appropriate renderings. For example, a caller's signature alone does not show the call site, so the impact numbers above are optimistic for the compact rendering. That is the reason for call-site windows in §5.5.

## Appendix B: References

- Robertson & Zaragoza (2009). *The Probabilistic Relevance Framework: BM25 and Beyond.* FnTIR.
- Robertson, Zaragoza & Taylor (2004). *Simple BM25 Extension to Multiple Weighted Fields.* CIKM.
- Cormack, Clarke & Büttcher (2009). *Reciprocal Rank Fusion Outperforms Condorcet and Individual Rank Learning Methods.* SIGIR.
- Haveliwala (2002). *Topic-Sensitive PageRank.* WWW. Jeh & Widom (2003). *Scaling Personalized Web Search.* WWW.
- Andersen, Chung & Lang (2006). *Local Graph Partitioning using PageRank Vectors.* FOCS.
- Gutiérrez et al. (2024). *HippoRAG: Neurobiologically Inspired Long-Term Memory for LLMs.* NeurIPS. (PPR over a KG for multi-hop retrieval)
- Edge et al. (2024). *From Local to Global: A Graph RAG Approach to Query-Focused Summarization.* (Microsoft GraphRAG)
- Yamaguchi, Golde, Arp & Rieck (2014). *Modeling and Discovering Vulnerabilities with Code Property Graphs.* IEEE S&P.
- Néron, Tolmach, Visser & Wachsmuth (2015). *A Theory of Name Resolution.* ESOP. GitHub *stack graphs* (2021–22); Sourcegraph *SCIP*; Google *Kythe*.
- Gall, Hajek & Jazayeri (1998). *Detection of Logical Coupling Based on Product Release History.* ICSM.
- Zimmermann, Weißgerber, Diehl & Zeller (2004/2005). *Mining Version Histories to Guide Software Changes.* ICSE / TSE.
- Herzig & Zeller (2013). *The Impact of Tangled Code Changes.* MSR.
- Bacchelli & Bird (2013). *Expectations, Outcomes, and Challenges of Modern Code Review.* ICSE.
- Jimenez et al. (2024). *SWE-bench.* ICLR. Zhang et al. (2023). *RepoCoder.* EMNLP.
- Wang et al. (2022). *Text Embeddings by Weakly-Supervised Contrastive Pre-training (E5).*
- Nogueira & Cho (2019). *Passage Re-ranking with BERT.* Burges (2010). *From RankNet to LambdaRank to LambdaMART.*
- Carbonell & Goldstein (1998). *MMR.* SIGIR. Liu et al. (2024). *Lost in the Middle.* TACL.
- Järvelin & Kekäläinen (2002). *Cumulated Gain-Based Evaluation of IR Techniques.* TOIS.
- Smucker, Allan & Carterette (2007). *A Comparison of Statistical Significance Tests for IR Evaluation.* CIKM.
