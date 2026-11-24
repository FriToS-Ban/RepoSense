# RepoSense Retrieval v2: Architecture Decision Records

Companion to [`02-design.md`](02-design.md). Each record states the problem, existing approaches, the decision, the alternatives, the trade-offs, the expected benefit, and how the decision will be validated. "Evidence" cites the Phase 1 audit ([`01-phase1-audit.md`](01-phase1-audit.md)) or the design spikes (design doc, Appendix A).

Status of all records: **Proposed** (direction approved 2026-11-24). A record moves to *Accepted* when its validation in Phase 3 passes, or to *Superseded* with a link to its replacement.

---

## ADR-01: Property graph in the existing SQL store; no graph database, no RDF

**Problem.** RepoSense needs typed entities and relations with properties (confidence, provenance, evidence lines), multi-hop traversal at review time, and transactional updates.

**Existing approaches.**
- Native graph databases (Neo4j, Memgraph): Cypher, index-free adjacency.
- RDF triple stores (SPARQL, OWL reasoning; Wikidata-style).
- Code-intelligence systems: Kythe and SCIP store cross-reference graphs as indexed facts; Joern stores code property graphs in an embedded graph DB.
- In-process graph libraries (NetworkX).

**Decision.** A property graph whose source of truth is SQL tables (`kg_symbols`, `kg_edges`, …) in the existing Postgres (SQLite for tests/eval). Traversal and ranking run on an immutable in-memory CSR snapshot per `(repo, commit)`.

**Alternatives.**
- *Neo4j*: a second datastore with its own consistency and backup story, for graphs of 10³–10⁵ nodes per repo that fit in memory. Cypher adds nothing that the handful of fixed traversal patterns need.
- *RDF*: the strengths are open-world integration, shared vocabularies, and reasoning. RepoSense has one producer, one schema, and no federation. Edge properties (confidence, evidence) need reification or RDF-star, which is pure overhead here.
- *NetworkX*: dict-of-dicts with high per-edge memory, and it adds a dependency for algorithms (local-push PPR, BFS) that are ~50 lines each.

**Trade-offs.** No ad-hoc graph query language: new traversal patterns are code, not queries. Snapshot builds cost a DB read per cache miss.

**Expected benefit.** One transactional store (consistency invariants enforceable in one transaction), no new infrastructure, millisecond traversals.

**Validation.**
- Snapshot load < 2 s and traversal p95 < 50 ms on a 10⁵-symbol repo.
- Invariants hold after every index/update.
- If a T2 failure analysis ever needs ad-hoc graph querying, revisit.

---

## ADR-02: Symbol-level granularity with stable identities

**Problem.** v1's retrieval unit was a 1,500-char chunk named by bare identifier. That caused mid-token splits, `(file, name)` collisions (up to 1,148 nodes), per-run UUIDs (no reconciliation), and the fact that only file chunk 0 was ever embedded.

**Existing approaches.**
- Fixed-size or recursive text chunking (common in RAG frameworks).
- AST-aware chunking of functions/classes (e.g. code-search datasets such as CodeSearchNet use function-level units).
- Statement-level code property graphs (Yamaguchi et al., 2014).
- Symbol-level cross-reference indexes (Kythe, SCIP).

**Decision.**
- Nodes are referable symbols: module, class, function, method. Nested closures are not nodes.
- Identity = `sha1(repo, path, qualname)`.
- Long bodies are split only for embedding/rendering, never as graph entities.

**Alternatives.**
- *Chunks*: the measured failure modes above.
- *Statement-level CPG*: 10–100× more nodes. It suits vulnerability pattern matching, not context retrieval.
- *File-level*: too coarse for a 12k-char budget. v1's file-level import edges reach 95% of dependency files but only 26% of dependency symbols.

**Trade-offs.** Module-level code needs its own representation (the module node's statement summary). Identity changes on rename, so renames need explicit detection.

**Expected benefit.** Exact dedup and containment, stable reconciliation with vector stores, relation-aware rendering per symbol.

**Validation.** Invariants 2–3 (unique qualname, nesting); zero orphan vectors after re-index (vs 54% in v1).

---

## ADR-03: Static name resolution with tiered confidence; no LLM extraction; Python first

**Problem.** Relations must be correct enough to traverse. v1's bare-name call edges were 35–45% precise on larger repos, missed all same-file calls, and gave functions no outgoing edges.

**Existing approaches.**
- LLM/OpenIE triple extraction (GraphRAG, HippoRAG on text corpora).
- Heuristic name matching (v1).
- Compiler-grade indexers: SCIP indexers (scip-python is built on Pyright), Kythe, Jedi, Pyright.
- Scope-graph / stack-graph name resolution (Néron et al., 2015; GitHub stack graphs).
- Tree-sitter for language-agnostic parsing.

**Decision.**
- An in-house resolver over stdlib `ast`, covering scopes, imports, re-export chains, MRO, `self`/`super`.
- Tiers R1–R5 carry a confidence calibrated as measured precision on a gold sample.
- R6 (repo-wide bare name) is disabled.
- JS/TS later via tree-sitter with the same schema.

**Alternatives.**
- *LLM extraction*: non-deterministic, costly per file, and strictly less precise than name binding for code. Code entity resolution *is* name binding.
- *scip-python / Pyright*: the most precise option because of type inference, but it adds a Node.js toolchain and opaque failure modes. Spike A shows light type inference (R4) recovers only +1.2% of call sites, which does not justify the dependency *now*.
- *Jedi*: pure Python and useful as an **independent evaluation oracle**, but slow for whole-repo indexing.
- *Stack graphs*: elegant incremental resolution, but heavyweight (Rust, TSG rule files). ADR-08 shows global re-resolution is cheap enough.

**Trade-offs.** Dynamic Python (metaprogramming, `getattr`, DI containers) stays unresolved. Spike A puts at most ~23% of call sites in the "possibly in-repo but unresolved" class. These go to the R5 fallback and to the lexical channel as residual terms.

**Expected benefit.** Edge precision ≥ .95 for R1–R3 (to be verified). Same-file edges and function out-edges exist. Every edge carries a meaningful confidence.

**Validation.**
- Gold sample: ≥50 call sites per tier, hand-labeled; optionally Jedi cross-check.
- Per-tier precision and recall.
- The T2 failure taxonomy measures the residual share of `relation_missing`.

---

## ADR-04: The query is a diff translated into typed seeds and a relation plan (entity linking), not an embedded hunk

**Problem.** v1 embedded raw hunks, including file-header pseudo-hunks, capped at 8. The nearest neighbours of a hunk are the hunk's own code: 95% of BM25 top-1 hits are the query function itself, and 79% of top-5 hits come from the changed file.

**Existing approaches.**
- Query-by-example code search (embed the snippet).
- Entity linking in text IR (map mentions to KB entities, then retrieve by entity).
- Change impact analysis from changed program entities (Bohner & Arnold, 1996).
- SWE-bench-style issue → file retrieval (BM25 over files).

**Decision.**
- Parse the PR's changed files at head as an overlay on the base snapshot.
- Link changed lines to enclosing symbols, producing seeds with change types.
- Resolve references on changed lines ("mentions").
- Keep unresolved identifiers as residual lexical terms.
- A deterministic rule table maps change types to relation priorities (the query planner).

**Alternatives.**
- *Embedding hunks* (v1): measured self-retrieval waste.
- *LLM query rewriting*: excluded by the brief and unnecessary, since a diff decomposes deterministically.
- *Base-only seeds* (no overlay): misses new symbols and new references introduced by the PR.

**Trade-offs.** Needs the head content of changed files (k fetches, cacheable). The rule table is hand-designed. LTR features (relation type) let ranking correct its priorities.

**Expected benefit.** Zero candidate slots wasted on already-visible code. Impact retrieval for signature changes and deletions, which v1 could not do at all (S4 impact recall .009).

**Validation.**
- Plan vs uniform-expansion ablation on T1 and T2.
- Seed-linking accuracy on T2: does the seed set contain the symbols the commit modified in the anchor file? Measured directly.

---

## ADR-05: Complementary candidate channels, fused with RRF first and a linear LTR model second

**Problem.** No single signal covers the information need. Structure covers resolvable dependencies (39.9% of call sites). Text covers unresolved identifiers and NL in PR descriptions. History covers logical coupling.

**Existing approaches.**
- Score fusion (CombSUM/CombMNZ) with score normalization (min-max, z-score).
- Reciprocal Rank Fusion (Cormack et al., 2009).
- Learning to rank (pointwise LR, RankNet, LambdaMART; Burges 2010).
- Hybrid sparse+dense search in production engines.

**Decision.** Channels are structural, lexical (BM25F, residual-oriented), co-change, and dense (optional). RRF (`k=60`) is the baseline fusion. A pointwise logistic-regression ranker over about 20 interpretable features is the target, trained with temporal splits and leave-one-repo-out CV on T2.

**Alternatives.**
- *Score normalization + weighted sum*: brittle across channels with incomparable score distributions (PPR mass vs BM25 vs cosine).
- *LambdaMART*: stronger on large data, but T2 will have hundreds to low thousands of queries. A linear model is less prone to overfit, and its weights can be read.
- *Cascade (graph first, text only on failure)*: discards the complementary evidence fusion uses.

**Trade-offs.** LTR needs labeled training data, which T2 provides with leakage controls. RRF ignores score magnitudes.

**Expected benefit.** Recall of the union of channels, with precision recovered by ranking.

**Validation.** Channel-union oracle recall (upper bound); RRF vs LTR vs best single channel on T2 with paired randomization tests; per-channel ablations.

---

## ADR-06: Multi-hop via approximate Personalized PageRank (local push); typed BFS as baseline

**Problem.** Some needed context is 2 hops away: a callee's callee, a caller of a renamed wrapper, the base of a base. Unbounded expansion floods the budget.

**Existing approaches.**
- Bounded BFS with relation constraints.
- Personalized PageRank / random walk with restart (Haveliwala 2002; Jeh & Widom 2003); HippoRAG applies PPR over a KG for multi-hop QA.
- Local push approximation (Andersen, Chung & Lang 2006).
- GNN-based retrieval; graph embeddings (node2vec).

**Decision.** Local-push PPR from seeds over a typed, direction-aware, confidence-weighted transition graph, with restart α and tolerance ε tuned on T1-dev. Typed BFS (depth ≤ 2) is the ablation.

**Alternatives.**
- *GNNs / node2vec*: training per repo, retraining on every push, and opaque. PPR on the explicit graph captures proximity without training.
- *Global PageRank*: query-independent, so it only measures popularity, which is a feature, not a ranker.

**Trade-offs.** Hyperparameters (α, type weights) need tuning; there are only a few, and they are inspectable.

**Expected benefit.** Graded proximity that ranks 2-hop candidates below 1-hop without hard cut-offs. Cost O(1/(ε·α)), independent of repo size.

**Validation.** `deps-2hop` and `contract` categories (T1), and T2 overall. PPR vs BFS vs 1-hop-only; α sweep; scaling curve of latency vs repo size.

**Outcome (M5, [results/M5.md](results/M5.md)).**
- Both methods deliver second-hop context.
- **The benchmarks disagree on which is better.** Typed BFS wins on T1, whose second hop is callee chains. PPR wins on T2's real co-changes: +.040 R@budget, p_holm 4e-4. Pure PPR raises graph-only recall from .222 to .336.
- α = 0.1 was chosen on T1-dev; T2's curve is flat for α ≤ 0.5.
- PPR stays the multi-hop method. Fused by flat RRF it *lowers* E+A's recall, so its score enters the M9 reranker as a feature instead.
- The `contract` category is deferred: override relations connect 2 of 1,122 judged T2 symbols.

---

## ADR-07: Context construction as budgeted, relation-aware selection

**Problem.** The consumer has a fixed budget. v1 filled 74–91% of it with the changed file's own nodes, which capped even an oracle at .40 (deps) / .60 (impact) cross-file recall.

**Existing approaches.**
- Top-k truncation (most RAG systems).
- MMR diversification (Carbonell & Goldstein 1998).
- Knapsack-style budgeted summarization and selection.
- Prompt compression.
- Position effects in long contexts (Liu et al., 2024).

**Decision.**
- Seeds get capped bodies.
- Every other candidate gets a relation-specific rendering: callee → signature + docstring; caller → call-site window; base → header + method.
- Greedy multiple-choice knapsack: admit cheap renderings in rank order, then upgrade by utility.
- Containment dedup; per-seed quotas.
- Grouped ordering with provenance labels.

**Alternatives.**
- *Top-k full bodies*: spike D gives .855 (deps) vs 1.00 with compact renderings under an oracle, and .345 vs .638 with BM25 candidates.
- *LLM-based compression*: excluded (LLM call in retrieval).

**Trade-offs.** Compact renderings can omit code the LLM needed, which is why the primary metric counts only relation-appropriate renderings. T3 measures the end effect.

**Expected benefit.** The largest single measured gain available: oracle .40 → 1.00, BM25 .049 → .638 (deps).

**Validation.** Recall@budget and `covered_full` on T1 and T2; budget sweep (4k–24k); T3 detection rate with and without compact renderings and provenance labels.

---

## ADR-08: Incremental indexing = file-local extraction cache + global re-resolution + transactional diff-apply

**Problem.** v1 push re-indexing deleted all incoming edges of changed files and never rebuilt them (−35% of edges after one hub-file push). Full re-indexing orphaned 54% of vectors. Deletions and renames were ignored.

**Existing approaches.**
- Delete-and-rebuild per file (v1, broken).
- Full rebuild per commit.
- Fully incremental name resolution (stack graphs; incremental computation frameworks such as Salsa in rust-analyzer).
- Content-addressed caching (Bazel-style).

**Decision.**
- Per-file `FileFacts` cached by blob SHA.
- Run global resolution and derivation over all facts on every update (measured 2.2 s at 97k LOC).
- Compute node/edge set differences against storage and apply them in one transaction.
- A vector outbox keeps embeddings consistent.
- A per-repo lock serializes updates.

**Alternatives.**
- *Incremental resolution*: correct but complex (reverse-dependency invalidation, MRO changes, re-exports). It is only worth it if global resolution becomes a bottleneck, at roughly 10× larger repos.
- *Full rebuild including re-embedding*: wasteful when content-hash caching makes re-embedding incremental anyway.

**Trade-offs.** O(repo) CPU per push (seconds), so very large monorepos would need the incremental path later.

**Expected benefit.** Index state is a pure function of `(repo, sha, extractor_version)`. Consistency is provable by an equivalence test.

**Validation.** Property test: `update(index(A), A→B) == index(B)` on ≥ 200 real consecutive commit pairs from T2 repos. Zero orphan vectors. Push latency measured vs repo size.

**Outcome (M7, [results/M7.md](results/M7.md)).** 240/240 code-changing first-parent commit pairs are equivalent field by field, with zero orphaned edges and exact outbox rows. The median update takes 0.24–0.62 s including SQLite writes. Global re-resolution on every update is affordable at these sizes (the in-memory build is 0.06–0.16 s).

Measuring the update exposed a quadratic ORM synchronisation cost in the diff-apply: 94.7 s for one commit. Core-level executemany statements bring it to 2.3 s.

---

## ADR-09: In-house BM25F over symbol fields, built in memory per snapshot

**Problem.** Lexical matching is the strongest measured baseline (deps R@20 .456, impact .772 cross-file), but v1 had none, and code needs identifier-aware tokenization and field weighting.

**Existing approaches.**
- Postgres full-text search (`tsvector`/`ts_rank`, not BM25, NL tokenizers).
- Lucene/Elasticsearch/OpenSearch, Tantivy (BM25 with fields).
- BM25F (Robertson et al., 2004).
- SPLADE-style learned sparse retrieval.

**Decision.**
- In-house BM25F over fields `name`, `qualname`/`path`, `signature`, `docstring`, `body identifiers`, `comments/strings`.
- Camel/snake sub-tokenization; built in memory from `kg_symbols` per snapshot.
- Query terms weighted toward residual identifiers.
- Seeds and changed files excluded.

**Alternatives.**
- *Elasticsearch/OpenSearch*: an external service for indexes of ≤10⁵ documents per repo that build in milliseconds.
- *Postgres FTS*: wrong ranking function and NL-oriented tokenization.
- *SPLADE*: needs a model and an inference path, and it is untested on code diffs. A possible Phase 4 ablation.

**Trade-offs.** Index memory per cached snapshot (small) and an in-house implementation to test, which unit tests against hand-computed scores cover.

**Expected benefit.** Spike D: BM25 over symbols with exclusions + v2 packing gives deps .638 / impact .870 vs v1 S2 .049 / .201. BM25F field weighting is expected to add on top.

**Validation.** BM25 vs BM25F vs field-ablated variants on T1-dev → T2. Residual weighting on/off. PRF (RM3) on/off.

**Outcome (M6, [results/M6.md](results/M6.md)).** Tuning moved to T2-train, because T1 disagrees with history twice.
- **Fields.** The body field carries the channel (−.161 without it); the docstring field slightly hurts; the name field was over-weighted.
- **Query.** The PR text adds .061; the residual boost adds a little; PRF does not help.
- **Selected configuration.** T2-test R@budget +.025 (p .03), nDCG@10 +.026 (p_holm .017); T1 deps −.045; T3 unchanged. It is adopted for production v2 (`presets.TUNED_LEXICAL`).
- **Query-time field weights** make one index per snapshot serve every configuration.

---

## ADR-10: Dense retrieval is a pluggable, evaluated channel, kept only if it adds measurable recall

**Problem.** v1 relied entirely on `nv-embedqa-e5-v5`:
- a text-QA retriever
- documents embedded with `input_type="query"`
- one HTTP call per node

Its quality on this task is unmeasured.

**Existing approaches.**
- General text embedders (E5 family, with query/passage asymmetry).
- Code-specialized embedders.
- Late-interaction models (ColBERT).
- Hosted vector DBs (Pinecone) vs local exact or ANN search.

**Decision.**
- `EmbeddingBackend` (NVIDIA | local | none) and `VectorBackend` (Pinecone | local brute force) interfaces.
- Correct passage/query types; batched calls; a content-hash cache.
- Off by default until M8 shows that hybrid C beats lexical A on T2 (p < 0.01) or that dense contributes ≥ 5% unique relevant candidates at acceptable cost.

**Alternatives.**
- *Keep dense as the backbone* (v1): no evidence that it suits diff → dependency retrieval; similar-code retrieval is a different need.
- *Drop dense entirely*: forfeits NL (PR description → docstring) matching without measuring it.

**Trade-offs.** One more channel to maintain; network cost if hosted.

**Expected benefit.** Hypothesis to test: dense helps mainly for PR-description queries and for "similar pattern" context, and little for dependencies.

**Validation.** M8: compare nv-embedqa (with correct input types) against at least one code-specialized model on T2, measuring unique-relevant contribution, fusion gain, latency, and cost.

---

## ADR-11: Reranking via a feature-based linear model; cross-encoders only as an ablation

**Problem.** After fusion, ordering decides what fits in the budget.

**Existing approaches.**
- Cross-encoder rerankers (Nogueira & Cho, 2019, trained on MS MARCO NL passages).
- LLM rerankers (listwise/pairwise prompting).
- Learning-to-rank over features (RankNet/LambdaMART).

**Decision.** A linear LTR model (ADR-05) over structural, lexical, co-change, dense, and node features. A cross-encoder is evaluated only if a code-capable model can run locally.

**Alternatives.**
- *Generic MS MARCO cross-encoder*: trained on NL query–passage pairs, so domain mismatch for diff → code. It costs seconds of CPU for ~100 candidates.
- *LLM reranker*: excluded by the brief (an LLM call per retrieval).

**Trade-offs.** A linear model cannot learn complex interactions. The interpretability is deliberate: weights show *why* items rank.

**Expected benefit.** Most of the reranking gain at negligible latency, with explainable weights.

**Validation.** D vs C, F vs E+A on T2 (paired tests); cross-encoder ablation on the same pool if feasible; latency per review.

---

## ADR-12: Three-tier evaluation with circularity and leakage controls

**Problem.** Ground truth derived from static analysis rewards graph methods by construction. Co-change ground truth misses read-only context. Neither measures review quality.

**Existing approaches.**
- Cranfield-style test collections with pooled judgments (TREC).
- Synthetic query generation.
- History-mined benchmarks (co-change prediction; SWE-bench localization).
- End-to-end task success.
- Significance testing (Smucker et al., 2007).

**Decision.**
- **T1** (structural, synthetic): regression gates and diagnostics only.
- **T2** (history-mined, temporal splits, pinned repos): the primary benchmark for claims.
- **T3** (bug injection with LLM review): end-to-end arbiter, used sparingly.
- TREC formats, bootstrap CIs, paired randomization tests, Holm–Bonferroni, stage-attributed failure taxonomy, pooled manual judgments for unjudged items.

**Alternatives.**
- *T1 only*: circular.
- *LLM-as-judge relevance*: cheap but must itself be validated against human labels. A possible Phase 4 addition for pooling, not a ground-truth source.
- *End-to-end only*: expensive, noisy, and does not localize failures.

**Trade-offs.** T2 needs cloning public repos (approval pending). T3 costs LLM tokens. Manual pooling costs human time.

**Expected benefit.** Claims that survive the obvious objections: circularity, leakage, noise, multiple comparisons.

**Validation.** M0 reproduces the Phase 1 numbers exactly. T2 is frozen with a dataset card (selection criteria, filters, known biases) before any v2 system is tuned on it.
