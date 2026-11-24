# `eval/`: retrieval evaluation for RepoSense

Offline, deterministic evaluation of RepoSense's context retrieval. Design: [`docs/retrieval/02-design.md`](../docs/retrieval/02-design.md) §6. Results notes: [`docs/retrieval/results/`](../docs/retrieval/results/).

Nothing here makes network calls. External services used by the backend (Pinecone, NVIDIA embeddings, google.genai) are replaced by counting fakes in [`offline.py`](offline.py).

## Quick start

From the repository root, with the backend virtualenv (Python 3.12):

```
python -m eval.run                       # T1 benchmark, v1 baselines S0–S4, scaling experiment (~90 s)
python -m eval.run_v2                    # T1 benchmark, v2 systems (A = BM25F + v2 packing, ...)
python -m eval.compare eval/results/t1-v2:A eval/results/t1-v1-baselines:S2_fixed_pipeline+bm25_stage1
python -m eval.run_t2                    # T2 history benchmark, v2 systems (clones 6 pinned repos, ~80 MB; ~5 min)
python -m eval.run_t2_v1                 # T2, v1 baseline S2 (indexes every parent snapshot with v1; ~30 min)
python -m eval.t2_analysis               # T2 diagnostics: graph distance of judged symbols, per-channel ranks, RRF displacement
python -m eval.compare eval/results/t2-v2:EA eval/results/t2-v2:A --view sym_all
python -m eval.gate t1 FRESH/metrics.json COMMITTED/metrics.json   # regression gate (used by CI)
python -m eval.kg_eval stats             # knowledge graph v2: cost, invariants, structure, agreement (M1)
python -m eval.kg_eval score             # gold labels -> per-tier precision/recall, writes backend/kg/calibration.json
python -m unittest discover -s eval/tests -t .
python -m unittest discover -s backend/tests -t .
```

The tests use only the standard library (`unittest`); `pytest` can also collect them.

## Layout

| Path | Purpose |
|---|---|
| `offline.py` | Bootstraps backend code against SQLite with fake external services and call counters; `deterministic_uuids` |
| `corpora.py`, `manifest/corpora.json` | Pinned corpora (RepoSense at a git commit via `git archive`; libraries by exact installed version) with content fingerprints. Loading fails on drift unless `--allow-corpus-drift` |
| `reference/resolver.py` | **Frozen** ground-truth resolver (`REFERENCE_VERSION`). Never edited together with the system under test |
| `benchmarks/t1.py`, `data/t1/` | T1 structural benchmark: generator, frozen queries + judgments (`queries.jsonl`), `meta.json` |
| `benchmarks/t2.py`, `data/t2/`, `manifest/t2_repos.json` | T2 history benchmark: miner, frozen queries + judgments, TREC qrels, [dataset card](data/t2/README.md), pinned repositories |
| `run_v2.py`, `run_t2.py`, `run_t2_v1.py`, `t2_analysis.py` | v2 systems on T1; v2 systems and S2 on T2; T2 diagnostics |
| `lexical.py` | Baseline tokenizer + Okapi BM25 (deterministic tie-breaking) |
| `metrics.py`, `stats.py` | P/R/Hit/MRR/nDCG@K, R@all; bootstrap CI, paired randomization test, Holm |
| `systems/v1.py`, `systems/v1_kg.py` | Replay of the audited v1 stack from git at `V1_COMMIT`, systems S0–S4, v1 KG edge-quality statistics |
| `run.py` | Runs a benchmark; writes `metrics.json` + `per_query.jsonl` to `results/<name>/`, TREC run files to `.cache/runs/<name>/` |
| `compare.py` | Paired comparison of two systems (delta, 95% CI, p, Holm-adjusted p) |
| `results/` | Committed result sets (small JSON) |
| `.cache/` | SQLite indexes, TREC runs, logs (git-ignored) |

## T1 in one paragraph

337 synthetic PR diffs over 6 pinned Python corpora (RepoSense backend @ `887d2fd`, httpx 0.28.1, starlette 1.0.0, fastapi 0.136.1, PyGithub 2.9.1, pydantic 2.13.4), seed 13.
- `deps` (224): a function is rewritten; relevant = the in-repo symbols it references (hop 1) and their references (hop 2).
- `impact` (113): a signature changes; relevant = its callers.

Views: `sym_xfile` (primary), `sym_all`, `2hop_only`, `graded_1+2hop`, `file_xfile`.

**T1 rewards structural retrieval by construction.** Use it for regression gates and to locate pipeline loss, not to claim that graph retrieval beats text retrieval (design §6.2).

## T2 in one paragraph

300 real commits (the 50 most recent qualifying ones in each of httpx, starlette, flask, click, requests, attrs, pinned). Query = the diff of the commit's largest modified non-test file + its message; base = the parent commit; relevant = the symbols the same commit changed in its other files (1,122; 749 of them tests). Views: `sym_all` (primary), `sym_code`, `sym_tests`, `file`. Co-change history is restricted to commits older than the query. **T2 under-rewards context that is read but not changed** (callees, contracts) and never judges the anchor file's own symbols; read the [dataset card](data/t2/README.md) before quoting it.

## Systems currently implemented

| ID | What it is |
|---|---|
| `S0_as_shipped` | v1 `get_relevant_context` as shipped at `887d2fd` (empty context: finding W1) |
| `S1_fixed_pipeline+oracle_stage1` | v1 pipeline with the hotfix; first stage returns the ground-truth nodes (upper bound of v1 post-processing) |
| `S2_fixed_pipeline+bm25_stage1` | v1 pipeline with the hotfix; BM25 first stage (stand-in for the unmeasured dense stage) |
| `S3_bm25_raw` | BM25 ranking of v1 nodes, top 50, no post-processing |
| `S4_current_graph_only` | Outgoing v1 edges of the changed file's node |

The v1 sources are executed from git (`systems/v1.py:V1_COMMIT`), so these baselines stay reproducible while `backend/` changes. The live, hotfixed `backend/services/rag.py` is tested to behave exactly like `S1`/`S2`'s pipeline (`backend/tests/test_rag_regression.py`).

## Determinism contract

Same inputs give byte-identical `metrics.json`, except `lat_ms` and `latency_ms`, independent of `PYTHONHASHSEED`. Specifically:
- corpora are fingerprinted
- query sampling enumerates candidates in sorted order with a seeded RNG
- BM25 visits query terms in sorted order and breaks ties by insertion order
- v1 node UUIDs are seeded during indexing
- each simulated review uses a fresh DB session
- oracle order is lexicographic, cross-file first

## Adding a system

Add an adapter under `systems/` that returns a ranked list of document keys per query (`"path::name"` for symbols, `"path"` for files) and, for context builders, the packed items. Then register it in `run.py`. Report a change with `eval.compare` against the relevant baseline, never with a single unpaired number.

## Changing ground truth

Edit `reference/resolver.py` or `benchmarks/t1.py` only together with bumping `REFERENCE_VERSION` / `T1_VERSION`, running `python -m eval.run --regenerate`, and writing a results note that re-baselines every system.
