# T2: history-based retrieval benchmark (dataset card, v1.0)

Real commits from pinned open-source Python repositories. The ground truth is what the commit's author *changed*, not what a static analyser says, so T2 is the first benchmark in this project on which graph and text retrieval can be compared without circularity (design §6.2, results in `docs/retrieval/results/M4.md`).

## Files

| File | Content |
|---|---|
| `queries.jsonl` | 300 queries. Each row holds `qid`, `repo`, `sha`, `parent`, `time`, `anchor`, `message`, `files_changed` and `judgments` (`key` = `path::qualname`, `test` flag) |
| `meta.json` | Version, pins, per-repo counts |
| `qrels.sym_all.txt`, `qrels.file.txt` | The same judgments in TREC qrels format (`qid 0 doc grade`) |

Repositories and pins: `eval/manifest/t2_repos.json` (httpx, starlette, flask, click, requests, attrs; BSD-3-Clause, Apache-2.0, MIT). Miner: `eval/benchmarks/t2.py`. Regenerate with `python -m eval.benchmarks.t2`; clones go to `eval/.cache/t2/repos` (~80 MB).

## Construction

- **Candidate commits:** non-merge commits reachable from the pin that change 2–8 code `.py` files (top-level `docs/`, `examples/`, `scripts/`, `benchmarks/` excluded) and at most 600 lines in total. Release, formatting, lint, merge and revert subjects are skipped.
- **Query:** the diff of the commit's *anchor*, which is the modified non-test file with the most changed lines. The commit message is the PR text. The base snapshot is the parent commit.
- **Judgments:** every class, function or method that the same commit changed in its *other* modified `.py` files and that already existed in the parent. Removed lines map through the parent's version, added lines through the commit's version. Each judgment is tagged as test or code by path.
- **Split:** the 50 most recent qualifying commits per repository form the test set. Everything older is history: the co-change index and, later, the learning-to-rank training data use only commits strictly older than the query commit.

## Statistics

| Repo | Queries | Judged symbols | Query dates |
|---|---|---|---|
| attrs | 50 | 225 | 2024-03 – 2026-11 |
| click | 50 | 141 | 2026-04 – 2026-11 |
| flask | 50 | 179 | 2023-03 – 2026-10 |
| httpx | 50 | 291 | 2022-11 – 2025-09 |
| requests | 50 | 116 | 2018-07 – 2026-06 |
| starlette | 50 | 170 | 2025-09 – 2026-10 |
| **Total** | **300** | **1,122** (749 test, 373 code) | |

- Median: 2 judgments and 2 changed code files per query.
- 134 queries have at least one code judgment (view `sym_code`); 222 have at least one test judgment (`sym_tests`).
- Views: `sym_all`, `sym_code`, `sym_tests`, `file` (judged files).

## Known biases: read before quoting T2 numbers

1. **Changed, not consulted.** The ground truth contains only what the commit *modified*. Context that the author read without changing (a callee whose contract matters, a base class) is unjudged, so T2 under-rewards dependency retrieval. Measured (M4): judged symbols one hop from the seeds are callers 256 times and callees 31 times.
2. **Anchor file is never judged.** The anchor's own symbols are the query, so same-file context is unjudged by construction. This matters for the structural channel, which offers same-file callees: 28% of System E's packed items are in the anchor file.
3. **Tests dominate.** 67% of judgments are test functions that were updated with the code. Whether a reviewer needs those tests in context is debatable; `sym_code` reports the code-only view.
4. **Tangled commits** add noise. The 2–8 file and 600-line filters bound it but do not remove it.
5. **Small and Python-only.** 300 queries is below the design target (500–1,000). 95% CIs on mean recall are about ±0.045.

The design (§6.2) calls for a pooled judgment subset to estimate how many unjudged items are relevant. It has not been built yet; until it exists, packed-precision numbers on T2 are lower bounds.
