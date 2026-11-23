# RepoSense: Phase 1 Audit (Knowledge Graph & Retrieval)

Audit of `main` @ `887d2fd`. No repository files were modified. All experiments ran from a scratch harness that imports and executes RepoSense's **real** `backend/services/indexer.py` and `backend/services/rag.py`. Only the external services (Pinecone, NVIDIA embeddings, google.genai) were replaced by in-memory fakes that count every call.

---

## 0. TL;DR

1. **Production RAG has returned empty context on every review since 2026-05-27** (commit `7ce9e6d`). In `rag.py:134`, `all_matched_nodes.extend(...)` runs before the variable is assigned on line 136. That raises `UnboundLocalError`, which the blanket `except` at `rag.py:194` swallows, and the function returns `""`. I reproduced it by executing the real function: log line `Failed to get RAG context: cannot access local variable 'all_matched_nodes'...`. Since that commit, the LLM has reviewed diffs with **zero** retrieved context.
2. **Fixing that bug is not enough. Even with a perfect first-stage retriever (oracle), the current pipeline delivers only 40% of a change's cross-file dependencies** (95% CI 35–47%) and 60% of its cross-file callers to the LLM. 74–91% of the 12,000-char budget goes to the changed file's own nodes, which the diff already shows. Half of the cross-file ground-truth symbols the oracle supplies are then dropped by dedup and budget.
3. **The "knowledge graph" cannot be traversed in the way the retrieval code assumes.** Function nodes have **0** outgoing edges in every corpus. Call edges start at *file* nodes and are matched by bare name. In bigger repos, 55–65% of bare-name call-site edges are spurious or point at the wrong target. Same-file calls are never captured. There are no reverse (caller) traversals. In the pipeline, graph expansion added **~0 relevant nodes** per query.
4. **The graph decays with use.** One push touching a central file (`httpx/_models.py`) deletes **35% of the repository's edges** permanently: incoming edges are deleted and never rebuilt. One full re-index leaves **54% of Pinecone vectors orphaned**, and about 64% of top-5 slots then point at deleted nodes.
5. **The actual retrieval problem is not "semantic search".** The query is a unified diff and the consumer is an LLM with a fixed context budget. The information need is mostly *structural*: what the change depends on, what depends on it, which contracts it must satisfy. Plain identifier-aware BM25, with no graph, already reaches deps R@20 = 0.46 and impact R@20 = 0.77, far above the pipeline. The gap comes from representation and pipeline design, not from a missing model.
6. **Dense retrieval (nv-embedqa-e5-v5) was not measured.** There is no API key in the environment and no local model. Separately, documents are embedded with `input_type="query"` (`rag.py:35`), which misuses an asymmetric retriever.
7. **Security, outside IR scope but urgent:** `backend/.env.save` (GitHub OAuth client secret, Gemini API key, JWT secret) and `reposense.db` (a `gho_…` GitHub OAuth access token) are committed to git.

---

## 1. What problem RepoSense's retrieval actually solves

| Aspect | Reality in code |
|---|---|
| Query | A unified diff of a PR (`review.py:64`), split into hunks |
| Corpus | One repository's Python/JS/TS code (Pinecone filter `repo_id`) |
| Consumer | One LLM call with `context[:12000]` + `diff[:48000]` (`llm.py:38-47`) |
| Output | A single context string; no scores, no provenance, not persisted |
| Information need | (a) definitions the changed code uses, (b) code that uses the changed symbols (impact), (c) contracts: base classes, overridden methods, interfaces, (d) tests of changed code, (e) similar/sibling patterns |

This framing drives everything that follows. It is **change-context retrieval**: closer to dependency and impact analysis than to ad-hoc semantic search. In code, "entities" are symbols, and **entity resolution is name binding**, which static analysis can do nearly exactly. LLM-based KG extraction, as in GraphRAG-style pipelines, would be strictly worse here than a compiler-style resolver.

---

## 2. Architecture map (as implemented, not as documented)

```
GitHub ──OAuth──► auth.py ──JWT cookie──► React SPA (dashboard/analytics only; no retrieval UI)

enable repo (repos.py:48) ─┬─► create webhook (pull_request, push)
                           └─► [crawl_permission] index_repository_background  (daemon thread)
                                   │
                                   ▼
          github.get_repo_contents (github.py:109): recursive Contents API, 1 HTTP call per dir + per file,
                     ALL file types < 100KB, silent partial failure on non-200
                                   │
                                   ▼
          indexer.index_files (indexer.py:208)
            pass 1: file nodes (1500-char chunks, 200 overlap); Python ast.walk / JS regex → function/class nodes (chunked)
            pass 2: edges imports/calls/inherits (heuristic, bare-name/substring matching)
            pass 3: for each node: new Pinecone client + list_indexes + new OpenAI client + 1 embed + 1 upsert
                                   │
                    ┌──────────────┴──────────────┐
                    ▼                             ▼
     Postgres code_nodes / code_edges     Pinecone (1024-d cosine, metadata: repo_id,node_id,path,name,type)
     (UUID PKs, NO secondary indexes)

push webhook (webhooks.py:49) ─► reindex_changed_files (added+modified only; removed ignored)

PR webhook (webhooks.py:35) ─► BackgroundTasks: review.process_pr_review
      diff ─► rag.get_relevant_context  [currently raises → ""]
          ─► llm.get_review_from_llm (Qwen3-Coder via NVIDIA NIM, JSON array)
          ─► store ReviewComment, quality score ─► post inline comments to GitHub
```

There are **no tests, benchmarks, retrieval logs, or evaluation code** anywhere in the repository.

---

## 3. Lifecycle trace and what is lost at each step

| Stage | Implementation | Information lost / defects |
|---|---|---|
| Ingestion | Contents API crawl (`github.py:109-141`) | No commit SHA pinned, so snapshots are unversioned. Every file type is downloaded before filtering. Non-200 responses are silently skipped, yet `is_indexed=True` is still set (`indexer.py:286`). Rate limit is ~5000 calls/hr, so large repos index partially. Push re-index ignores `removed` files (`webhooks.py:57-58`), so deleted and renamed files live forever. |
| Parsing (Python) | `ast.walk` over every node (`indexer.py:37`) | Nested functions become top-level nodes. Methods lose their class (bare `name`, no qualname). No line numbers, signatures, docstrings, decorators, or module-level code beyond the first file chunk. |
| Parsing (JS/TS) | Per-line regex + brace counting, 100-line cap (`indexer.py:68-129`) | Verified failures: class methods not extracted; object methods missed; single-param arrows (`x => …`) missed; `const total = (a + b)` treated as a function; a `'}'` inside a string truncates the body; components over 100 lines are cut (e.g. `Landing.jsx`, 360 lines); a brace-less body swallows the next 100 lines. |
| Chunking | Fixed 1500-char windows (`indexer.py:16-29`) | Splits mid-token or mid-statement. Chunks are separate nodes named `foo__chunk1`. Only **chunk 0 of each file** is embedded or used for edges (`indexer.py:240-241`). |
| Identity | `uuid4` per node per index run | IDs are not stable across re-indexes, so vectors and edges cannot be diffed or reconciled. In `(file, bare name)`, `__init__` of different classes in one file collide. Up to 1148 colliding nodes in the monorepo corpus. |
| Edges | `indexer.py:133-204` | See §4. No `defines`/`contains` edge, although the enum declares it. No same-file edges. No line or provenance. Duplicate call edges (one per call site, not deduplicated). |
| Embedding | `nv-embedqa-e5-v5`, text = path+name+content, `input_type="query"` for documents | Asymmetric retriever used symmetrically. One HTTP call per node, plus a client constructor and `list_indexes` per node (`rag.py:39-43`). Embedded text is 1.4–2.2× the source size because class, method, and file chunks overlap. |
| Storage | Postgres + Pinecone | No index on `repo_id`, `file_path`, `source_id`, `target_id`, so every lookup is a sequential scan. Pinecone vectors are never deleted (see §6.4). |
| Query formation | `extract_diff_chunks` (`rag.py:63-91`) | The file header (`index…/---/+++`) becomes its own "hunk" query. On real RepoSense commits this is 1 of every 2–3 queries. With the 8-chunk cap (`rag.py:138`), a 10-file commit queries only 8 of 30 chunks, and later files get no retrieval. Hunks are embedded raw, with `+`/`-` markers and both old and new lines. |
| Candidate generation | Pinecone top-5 per chunk + up to 3 *outgoing* edges of each hit (`rag.py:141-157`) | Top-5 is tiny. Hits are fetched one at a time (N+1 SQL). Edge order is arbitrary. Function nodes have no outgoing edges. Reverse edges (callers) are never used. |
| Ranking | none | **Similarity scores are discarded.** The only order is "changed-file nodes first, then insertion order", stable-sorted "functions/classes before files" (`rag.py:170`). |
| Dedup | `(file_path, bare name)`, keep longest (`rag.py:96-110`) | Merges different methods that share a name. Drops the other chunks of long functions. |
| Packing | greedy into 12,000 chars (`rag.py:172-190`) | Whole changed files (all their function, class, and file chunks) are placed first and exhaust the budget. |
| Results | string → LLM prompt | Not persisted, so no offline evaluation of production retrieval is possible. |

---

## 4. The knowledge graph as implemented

### 4.1 Schema

| Node | Extraction | Notes |
|---|---|---|
| `file` | every code file, chunked | Only chunk 0 is embedded or linked |
| `function` | Python `FunctionDef` anywhere (incl. nested, methods); JS regex | bare name, chunked |
| `class` | Python `ClassDef`; JS regex | content includes all methods (duplicated) |

| Edge | Source → Target | Extraction rule |
|---|---|---|
| `imports` | file → file | `module in target_path_dotted` **substring** match (`import os` can link to `routes/repos.py`; `from . import x` matches the first file) |
| `calls` | **file** → function | bare callee name (incl. `obj.attr` → `attr`) looked up in a dict of function names from *other* files; last writer wins |
| `inherits` | class → class | `ast.Name` bases only; first class anywhere with that bare name |
| `defines` | declared, **never created** | |

### 4.2 Measured edge quality vs a scope-aware reference resolver

The reference resolver (harness `refgraph.py`) resolves local shadowing, absolute and relative imports, re-export chains, star imports, `self`/`cls`/`super()` through resolved MROs, and `module.attr`/`Class.method` chains. It is conservative: it does not infer instance types.

| Corpus | files / LOC | imports P / R | name-call-site P / R ¹ | call targets ambiguous ² | inherits P / R | same-file call pairs never captured |
|---|---|---|---|---|---|---|
| reposense | 17 / 1.6k | 0.90 / 0.90 | 1.00 / 1.00 | 0% | n/a | 13 |
| httpx | 23 / 8.8k | 0.96 / 1.00 | 0.79 / 1.00 | 75% | 1.00 / 1.00 | 98 |
| starlette | 34 / 6.6k | 0.88 / 0.99 | **0.35** / 1.00 | 81% | 1.00 / 1.00 | 86 |
| fastapi | 46 / 9.7k | 0.91 / 0.93 | 1.00 / 1.00 | 23% | 0.85 / 0.85 | 59 |
| pygithub | 174 / 35k | 0.86 / 0.95 | 0.87 / 1.00 | 5% | 1.00 / 0.97 | 121 |
| pydantic | 102 / 37k | 0.62 / 0.83 | **0.45** / 0.96 | 54% | 0.72 / 0.84 | 379 |
| monorepo (5 libs, one repo) | 379 / 97k | **0.59** / 0.87 | **0.43** / 0.95 | 44% | 0.86 / 0.90 | 743 |

¹ Precision of current call edges created by bare `foo(...)` call sites, where reference resolution is reliable. Errors are calls to locals, builtins, or external libraries, and same-file definitions linked to a same-named function elsewhere.
² Fraction of current call edges whose target name has ≥2 definitions in the repo, so the edge picks one arbitrarily.

Structural facts:
- **0% of function nodes have outgoing edges** in every corpus. Graph expansion from a function or method hit (the most common kind of hit) yields nothing.
- Import edges are the only edges with decent quality, and they are file → file. From a changed file they reach **95% of the files** containing its cross-file dependencies, but only 26% of the dependency *symbols* (S4 below).
- Precision degrades as the repo grows (pydantic, monorepo): bare-name matching collides more.

---

## 5. The retrieval pipeline, line by line

`get_relevant_context(repo_id, diff, db, top_k=5, changed_file_paths)`:

1. `get_pinecone_index()`: builds a client and calls `list_indexes` (network).
2. Splits the diff into chunks. Header-only pseudo-chunks are included (bug).
3. Loads **all** nodes of every changed file ("direct nodes"). **Crashes here** (UnboundLocalError) → `""`.
4. For each of the first 8 chunks: embed (network), Pinecone top-5 (network), fetch each hit by id (1 SQL query each), append up to 3 outgoing-edge targets (1 SQL query each).
5. Dedup by `(file, bare name)`, keep longest.
6. Stable sort: non-file nodes first. **The Pinecone score is never read.**
7. Greedy pack into 12,000 chars in that order.

---

## 6. Baseline evaluation

### 6.1 Method

- **Corpora (6 + 1 merged):** RepoSense's own backend, plus `httpx`, `starlette`, `fastapi`, PyGithub, and `pydantic` from the local venv (real library code, no tests or history). For scaling, a "monorepo" merges five libraries as one repo_id.
- **Indexing:** real `index_files` into SQLite. The fake Pinecone stores the exact text that would have been embedded.
- **Queries (337 total):** synthetic PR diffs in real unified-diff format, seed 13.
  - `deps` (224): a function or method (≤40 lines) rewritten. Ground truth: in-repo symbols referenced inside the hunk (mean 2.98; 2.14 in other files). Also graded 1-hop = 2 / 2-hop = 1, and a 2-hop-only view.
  - `impact` (113): signature change of a function. Ground truth: its callers (mean 2.9; 2.5 cross-file).
- **Views:** `sym_xfile` (main: cross-file symbols, because changed-file nodes are injected by design), `sym_all`, `file_xfile`, `2hop_only`.
- **Systems:**
  - **S0** as shipped (real function).
  - **S1** pipeline with the 1-line bug fixed + **oracle** first stage (returns ground-truth nodes). This is a model-independent *upper bound* on what the current post-processing can deliver.
  - **S2** fixed pipeline + BM25 first stage (stand-in for the unmeasurable dense stage).
  - **S3** raw BM25 ranking (identifier-split tokens, Okapi k1=1.2, b=0.75), top-50 candidates, no pipeline.
  - **S4** current graph only: outgoing edges of the changed file.
- **Metrics:** P@K, R@K, Hit@K, MRR, graded nDCG@K, and `R@all` (recall over the whole returned list or context). Bootstrap 95% CIs for headline numbers.

### 6.2 Results: cross-file symbols (`sym_xfile`)

> **Reproduction note (M0, 2026-11-24).** `python -m eval.run` reproduces every S0, S2, S3, and S4 cell below exactly. The S1 oracle bound depended on Python set iteration order in this harness. Under a fixed oracle order it is deps R@all **.388 [.330, .450]** and impact **.598**. Bootstrap CIs differ in the third decimal because each CI now uses its own seeded resampling stream. The canonical baselines are in [`results/M0.md`](results/M0.md).

**deps (224 queries)**

| System | P@5 | R@5 | R@10 | R@20 | MRR | nDCG@10 | R@all | items returned |
|---|---|---|---|---|---|---|---|---|
| S0 as shipped | 0 | 0 | 0 | 0 | 0 | 0 | **0** | 0 |
| S1 fixed + oracle stage 1 | .027 | .048 | .164 | .355 | .054 | .077 | **.404** [.345, .467] | 16.2 (12k chars) |
| S2 fixed + BM25 stage 1 | .002 | .006 | .015 | .046 | .010 | .007 | **.049** [.027, .074] | 17.1 (12k chars) |
| S3 BM25 raw | .048 | .134 | .259 | .456 | .103 | .118 | .620 [.564, .673] | 50 (ranked list) |
| S4 current graph only | .018 | .058 | .143 | .205 | .045 | .062 | .257 [.209, .308] | 21.2 (unranked) |

**impact (113 queries)**

| System | P@5 | R@5 | R@10 | R@20 | MRR | nDCG@10 | R@all |
|---|---|---|---|---|---|---|---|
| S0 as shipped | 0 | 0 | 0 | 0 | 0 | 0 | **0** |
| S1 fixed + oracle | .032 | .072 | .244 | .579 | .080 | .104 | .597 [.511, .685] |
| S2 fixed + BM25 | .005 | .022 | .084 | .196 | .032 | .034 | .201 [.138, .277] |
| S3 BM25 raw | .120 | .416 | .614 | .772 | .204 | .295 | .862 [.808, .914] |
| S4 current graph only | .002 | .009 | .009 | .009 | .003 | .004 | .009 |

**Multi-hop (deps, 2-hop-only ground truth, 140 queries):** S1 R@all .199, S2 .179, S3 R@20 .138, S4 .058. Nothing in the current system targets 2-hop context. The 2-hop hits that do appear are incidental (same-file nodes).

**File-level (`file_xfile`, deps):** S4 R@all **.952**. The import graph already knows which files matter, but the pipeline never traverses from the changed file, and its edges are file-level.

Budget reading: about 16–17 nodes fit in 12k chars, so S3's R@20 is roughly the "budget-equivalent" of the pipeline's R@all.

### 6.3 Failure analysis: where cross-file ground truth is lost

| | in context | retrieved by stage 1, then dropped (dedup/budget) | never retrieved by stage 1 |
|---|---|---|---|
| deps, S1 (oracle) | 46% | **50%** | 4% |
| deps, S2 (BM25) | 4% | 7% | **89%** |
| impact, S1 (oracle) | 45% | 30% | 25% (top-5 cap) |
| impact, S2 (BM25) | 13% | 9% | **78%** |

Pipeline diagnostics (means per query):
- Share of the 12k-char context taken by **the changed file's own nodes**: deps 91% (S1) / 85% (S2); impact 86% / 74%. The budget is exhausted in 46–88% of queries.
- Graph-expansion step: 0.07–0.6 nodes added per query, **0.00–0.01 relevant**.
- Stage-1 self-retrieval: in raw BM25, the top-1 hit is the query's own function 95% of the time, and 79% of the top-5 come from the changed file. A dense hunk-to-code retriever has the same property: the most similar code to a hunk is the code it came from. With `top_k=5`, the current design spends most of its candidate slots on context it already has.
- Header pseudo-hunks: on 5 real RepoSense commits, 23 of 65 query chunks were file headers, and 36 chunks fell beyond the 8-chunk cap.

### 6.4 Index consistency (real `reindex_changed_files` / `index_files` on httpx)

| Event | Edges | Edges into hub file from other files | Orphaned vectors |
|---|---|---|---|
| Initial index | 540 | 79 | 0 / 715 |
| Push touching `_models.py` (content unchanged) | **349** (−35%) | **0** | 134 / 849 (16%) |
| Then one full re-index | n/a | n/a | **849 / 1564 (54%)** |

After one full re-index, **64% of top-5 retrieval slots** point at deleted nodes, which `rag.py:149-151` silently drops. Effective top-k collapses from 5 to about 2. The decay is monotone: every push to a widely imported file disconnects it from the graph.

### 6.5 Scaling (same queries, 5.5× larger single repo; BM25 raw, deps `sym_xfile`)

| | R@10 | R@20 | R@50 | MRR |
|---|---|---|---|---|
| starlette alone → monorepo | .231 → .226 | .425 → .381 | .625 → .477 | .097 → .082 |
| httpx alone → monorepo | .387 → .337 | .567 → .521 | .691 → .620 | .123 → .110 |

Lexical retrieval degrades moderately with repo size. Graph edge precision degrades more: imports .88–.96 → .59, name-calls → .43.

### 6.6 Cost model (indexing and per review)

Measured call counts from the instrumented fakes:
- **Indexing:** for every embedded node, 1 `list_indexes` + 1 embed + 1 upsert (3 sequential round trips), plus 2 client constructions. PyGithub: 4,046 embedded nodes → 12,138 sequential HTTP calls, on top of ~1 GitHub API call per file and directory. Local CPU time of `index_files` grows superlinearly (7.7s for 5.2k nodes → 24.5s for 11.7k), consistent with the O(files × nodes) scans in `extract_*_edges` and the second pass.
- **Per review (single-file diff):** 2 embed calls + 2 Pinecone queries + ~22 SQL statements (N+1). Up to 8+8 network calls for multi-file diffs. Every SQL lookup is a sequential scan because there are no secondary indexes.
- Local latencies (SQLite, fake network) are 20–50 ms per review and **not representative**. Production latency is dominated by the network calls above and was not measurable here.

### 6.7 Threats to validity (read before quoting any number)

1. **Dense retrieval is unmeasured.** S2 substitutes BM25 for the dense stage. The S1 oracle bound is model-independent and is the robust result.
2. **Ground truth is derived from static analysis**, so it is biased toward structural relevance and under-counts dependencies that need type inference. Reusing the same resolver to build the evaluated graph would make graph methods win by construction. Phase 2 must add non-circular ground truth (see §8).
3. **Synthetic diffs** (whole-function `+` hunks, single file, no PR description) are cleaner than real PRs.
4. Corpora are library code without tests or history. Per-corpus variance is large (e.g. PyGithub's deps R@10 is much lower).

---

## 7. Weakness catalogue (ranked)

| # | Severity | Weakness | Evidence |
|---|---|---|---|
| W1 | Critical | Context is always empty (UnboundLocalError swallowed) | `rag.py:134-136`; reproduced |
| W2 | Critical | Budget starvation: changed-file nodes take 74–91% of context | §6.3 |
| W3 | Critical | No ranking: scores discarded; order = injection order | `rag.py:170` |
| W4 | High | Graph granularity wrong: file → fn edges, functions have no out-edges, no reverse traversal, no same-file edges | §4.2; S4 impact R = .009 |
| W5 | High | Edge extraction by bare name and substring → 55–65% wrong in larger repos | §4.2 |
| W6 | High | Incremental indexing destroys edges and orphans vectors; deletes and renames ignored | §6.4 |
| W7 | High | Query formation: header pseudo-hunks, 8-chunk cap, raw diff text as query, self-retrieval | §6.3 |
| W8 | Medium | Asymmetric embedding model used with `input_type="query"` for documents | `rag.py:35`; every index-time call in every corpus |
| W9 | Medium | Symbol identity: bare names, `__chunk` names, per-run UUIDs, `(file, name)` collisions merged | §3 |
| W10 | Medium | JS/TS extraction is regex-based and demonstrably wrong on common syntax | §3 |
| W11 | Medium | No evaluation, tests, or retrieval logging, so regressions like W1 go unnoticed for months | repo has 0 tests |
| W12 | Medium | Indexing cost: 3 round trips per node; O(n²) edge pass; no DB indexes | §6.6 |

---

## 8. Highest-impact opportunities (what I recommend, in order)

These are hypotheses with a validation plan, not claims.

1. **Make retrieval observable and testable first.** Move this harness into the repo as `eval/`. Add retrieval-trace logging (which candidates, which channel, scores, what was packed). Add a regression test that would have caught W1.
2. **Re-model the graph at symbol granularity** with stable identities `(repo, path, qualname)`, content hashes, line spans, signatures, and docstrings. Typed edges: `CONTAINS`, `IMPORTS`, `CALLS` (symbol → symbol, with call-site lines), `REFERENCES`, `INHERITS`, `OVERRIDES`, `TESTS`, and, as the only non-static relation, `CO_CHANGED` mined from git history (evolutionary coupling; Zimmermann et al., ICSE 2004 / TSE 2005). Every edge carries provenance (extractor, rule, commit SHA) and a confidence that becomes a ranking feature: import-resolved 1.0, MRO-resolved high, bare-name fallback = 1/|candidates|.
3. **Turn query formation into entity linking.** Map each hunk's line ranges to the enclosing symbols (seeds). Link identifiers in changed lines to the symbol table. Expand typed neighborhoods: callees and referenced types (deps), callers (impact), bases and overrides (contracts), tests. Compare BFS-with-type-constraints against Personalized PageRank from seeds (Haveliwala 2002; HippoRAG, Gutiérrez et al. 2024, uses PPR over a KG for multi-hop retrieval).
4. **Treat lexical and dense retrieval as complementary channels, not the backbone.** Use BM25F with fields (name, qualname, signature, docstring, body, path), excluding seed symbols and self-hits. Re-evaluate dense with correct `input_type="passage"` and against code-specialized embedders. Keep dense only if it adds recall beyond graph + lexical on the benchmark. Fuse with RRF (Cormack et al. 2009) as the baseline, then a small learned linear ranker over interpretable features.
5. **Replace greedy packing with budget-aware context construction.** Changed-symbol bodies first, then dependency *signatures and docstrings* (cheap), then full bodies by score per char, with MMR-style diversification. Evaluate with **Recall@budget**: recall at the token budget actually given to the LLM, which is the metric that matters here.
6. **Fix incremental consistency.** Use deterministic IDs and delete vectors with nodes. Re-resolve incoming edges via a reverse-import index. Handle removed and renamed files. Version the index by commit SHA. Incremental name resolution per file is a solved design problem (GitHub's stack graphs; Sourcegraph SCIP); use them as references, not dependencies.
7. **Build a three-tier benchmark** so that graph methods are not graded by their own construction:
   - **T1 structural** (this harness): diagnostics and regression gates.
   - **T2 history-based, non-circular:** real merged PRs and commits from pinned OSS repos. Query = part of the change; relevant = symbols changed in held-out files of the same change (cf. SWE-bench's gold-patch file localization).
   - **T3 end-to-end (optional, LLM cost):** injected bugs that are only detectable with cross-file context (e.g. a changed call that violates a callee contract in another file). Detection rate per retrieval configuration.

## 9. Where I'd push back on the brief

- **No graph database, no RDF.** One owner, one schema, per-repo graphs of 10^3–10^5 nodes, no federation or inference needs. A property graph in the existing SQL store plus an in-memory CSR adjacency per repo for traversal and PPR is simpler and faster. RDF/SPARQL buys nothing here.
- **No GraphRAG community summaries.** They target global sensemaking questions over corpora (Edge et al. 2024), need LLM calls at index time, and do not match a diff → dependency information need.
- **Cross-encoder reranking is a weak bet for diff → code pairs.** I know of no well-validated code-diff cross-encoder, and generic MS MARCO-style models are trained on NL passages. I would run it as one ablation and expect a feature-based ranker over graph distance, edge type and confidence, BM25, dense, and co-change to do better per millisecond.
- **Graph embeddings (node2vec etc.)** need retraining on every push to a per-repo graph, and PPR on the explicit graph usually carries the same proximity signal. Only as an ablation.
- **LLM-based query decomposition or expansion** is excluded by your constraints and unnecessary: a diff decomposes deterministically into hunks → symbols → identifiers.
- **Python first.** Get the representation and evaluation right on Python with stdlib `ast`. Then add JS/TS via tree-sitter, which is the one new parsing dependency I can justify.

## 10. Decisions needed from you

1. **Approve the direction:** symbol-level property graph in the existing SQL store + in-memory traversal; diff → entity-linked seeds; multi-channel candidates; budget-aware packing; evaluation-first.
2. **Benchmark data (T2):** may I clone a handful of pinned public OSS repos with history? That is a download.
3. **Dense channel:** (a) provide an NVIDIA key and accept cost and non-determinism, (b) let me install a local embedding model (pip download, large), or (c) defer dense and evaluate graph + lexical first.
4. **Language scope:** Python-only for Phases 2–3?
5. **Security (your action):** revoke and rotate the GitHub OAuth client secret, the `gho_` token stored in `reposense.db`, the Gemini key, and the JWT `SECRET_KEY`. Then untrack both files. Consider a history rewrite.

---

## Appendix: reproduce

The Phase 1 harness was a throwaway scratch harness outside the repository: `boot.py` (service fakes + call counters), `refgraph.py` (reference resolver), `bench.py` (indexing, queries, systems S0–S4, metrics), `consistency.py`, `js_check.py`, `summarize.py`. Milestone M0 of the Phase 2 plan (`02-design.md` §9) ports it into `eval/` as a tested, reproducible package. M0's exit criterion is that it reproduces the numbers in this document exactly (same corpora, seed 13).

Status: findings accepted; direction approved 2026-11-24. Design: [`02-design.md`](02-design.md). Decisions: [`03-decisions.md`](03-decisions.md).
