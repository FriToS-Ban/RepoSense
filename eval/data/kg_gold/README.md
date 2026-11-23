# Knowledge-graph gold labels (milestone M1)

Hand-labelled samples used to measure and calibrate the v2 resolver (`backend/kg`).

## Files

| File | What it is |
|---|---|
| `dev_precision_items.jsonl`, `dev_labels.jsonl` | **Development sample** (210 edges, resolver `kg-py-1.0`, random sampling). Used to find resolver errors. **Not** used for any reported number |
| `precision_items.jsonl`, `precision_labels.jsonl` | **Held-out precision sample** (210 edges, resolver `kg-py-1.1`). Stratified by tier: R1–R4 40 each, R5 50; round-robin across the 6 T1 corpora. Selection is by `sha1("gold-v1" ‖ path ‖ line ‖ chain ‖ target)`, so it is stable |
| `recall_items.jsonl`, `recall_labels.jsonl` | **Recall sample**: every call site (81) in 2 functions per corpus with 4–15 calls. Functions are chosen by `sha1("gold-v1" ‖ path ‖ qualname)`. The truth labels do not depend on the system, so any resolver version can be scored on them |

Regenerate the item files with `python -m eval.kg_eval sample`; score with `python -m eval.kg_eval score`. Scoring writes `eval/results/m1-kg/gold_scores.json` and `backend/kg/calibration.json`.

## Labelling protocol

- **Precision item = one resolved occurrence** (a reference at `at` whose chain resolves to `target`).
  - `correct`: `target` is a definition the reference can denote at run time. For polymorphic calls, any possible dispatch target counts. A Protocol or interface definition counts when the receiver is statically typed by it (e.g. `ujson = cast(_UjsonModule, …)`).
  - `incorrect`: the target cannot be what is referenced. Typical cases: a builtin or external object, a different class, a data attribute rather than a method.
  - `unsure` (unused so far): excluded from precision and reported separately.
- **Recall item = one call site.**
  - `truth` is the in-repo definition that is called (`path::qualname`).
  - `ext` means a builtin, an external library, or a dynamic callable (parameter, injected attribute) with no static in-repo target.
  - `self` means a recursive call. It is excluded from scoring because self-loops are dropped by design.
- **Annotator:** `claude` (the assistant), by reading the source around each item, with the source checked directly where the sheet was ambiguous (notes in the label files). These are **not human labels**. A human spot-check, or `jedi` as an independent oracle, would strengthen them.

## Known biases

- 25 of the 210 held-out precision items coincide with development items (independent draws from the same population).
- The recall sample is small (26 in-repo call sites) and dominated by easy direct calls; its Wilson interval is wide ([0.81, 0.99]).
- Site-packages corpora contain no tests, so `TESTS` edges are exercised only by unit fixtures.
