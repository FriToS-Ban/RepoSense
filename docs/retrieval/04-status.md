# Retrieval v2: status after M11 (2026-11-24)

Read this first, then the results notes it links. Design: [`02-design.md`](02-design.md) (§9 milestone table, all rows marked); decisions: [`03-decisions.md`](03-decisions.md) (outcome notes on ADR-06, 08, 09, 10, 11, 12).

## Where things stand

- **Production serves v2 by default** (`RETRIEVAL_ENGINE=v2`; `v1` is the rollback for one release). The served system is **V2**: lexical BM25F with the M6 settings, structural 1-hop and PPR candidates, a logistic reranker trained on T2-train, callees on changed lines pinned first, no overlay ([M11](results/M11.md)).
- **CI** gates, on every push to `main`:
  - unit tests;
  - T1 (v1 baselines and v2 systems);
  - the KG structure gate and gold scores;
  - T2: six pinned repositories, every system in `eval/run_t2.py:systems()` including V2.

## The results that matter

| | S2 (v1) | A (lexical) | E+A (RRF) | **V2** |
|---|---|---|---|---|
| T2 R@budget (300 real commits) | .045 | .617 | .630 | **.681** |
| T1 deps / impact R@budget (circular) | .049 / .201 | .599 / .899 | 1.000 / .995 | .962 / .995 |
| T3 context sufficiency (152 injected bugs) | .145 | .961 | 1.000 | 1.000 |

Findings, in order of importance:
1. **v1 was far worse than any v2 system** on every benchmark. On T3 it shows the needed context for one bug in seven ([M10](results/M10.md)).
2. **On real history, the graph helps only as a ranking signal inside a learned ranker.** With flat RRF, E+A ≈ A (M4). One-hop expansion reaches 26% of co-changed symbols (M4). PPR beats BFS on T2 but not on T1 (M5). Graph features add .036 under learned fusion; graph-as-generator does not help significantly (M9).
3. **T1 and T2 disagree repeatedly.** Every choice was checked on all tiers; tuning used T2-train only.
4. **A model trained on history learns its label biases** (tests up, read-only callees down). The callee pin corrects the part T1 and T3 can measure (M9, M11).
5. **The incremental index equals a full index** on 240 real commit pairs; the diff-apply is 40× faster after an ORM fix (M7).

## Open, in priority order

1. **T3 LLM detection run** (paid). Run `python -m eval.run_t3 --llm none,A_tuned,V2` with `LLM_*` environment variables. It is the only measurement of whether better context changes what the reviewer catches, and the arbiter for V2's `sym_code` weakness (−.048 vs A_tuned on code-only co-changes, n.s.).
2. **Dense channel measurement** (paid). Run `eval/run_t2_dense.py` with `DENSE_*` variables, against the amended ADR-10 keep rule (fusion p < .01 and unique recall above the hashing control). The PR-text path is the likeliest win (M6: PR text is worth .061).
3. **V2 latency.** p95 is 193 ms vs a ~170 ms target. Score only pool members in the lexical feature pass and reuse PPR for the two-hop feature.
4. **Pooled judgments for T2** (design §6.2). All T2 packed-precision numbers are lower bounds until they exist.
5. **A harder T3.** Bugs whose context is not named on the changed line: docstring preconditions, return contracts, `super()` targets.
6. **Rank metric artifact.** A full class inserts its methods into the ranked list (M5 §T1). Treat them as tied at the class's rank at the next re-baseline.
7. **After one release:** delete v1 (`code_nodes`, `code_edges`, Pinecone paths). JS/TS extraction via tree-sitter. Alembic migrations.

## Secrets

`backend/.env.save` and `reposense.db` are committed and contain real credentials. They must be rotated and purged from history (a history rewrite: the owner's decision). Nothing in `eval/` reads them; paid harnesses read keys from the environment only.
