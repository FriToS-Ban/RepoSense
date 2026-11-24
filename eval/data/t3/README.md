# T3: bug-injection benchmark (dataset card, v1.0)

152 single-line bugs injected into real code of the six pinned T2 repositories (`eval/manifest/t2_repos.json`, at their pins). Each bug can only be caught with context from **another file**. Generator: `eval/benchmarks/t3.py`; results notes: `docs/retrieval/results/M10.md`.

## Files

`cases.jsonl` holds one case per line: `cid`, `repo`, `pin`, `mutation`, `path`, `line`, `before`, `after`, `needed` (path::qualname keys, any one suffices), `needed_rendering`, `description`. `meta.json` holds the version, salt and counts.

## Mutations

| Type | Change | Needed context | Cases |
|---|---|---|---|
| `arg_swap` | The first two positional arguments of a call to a function or method in another file are swapped | The callee's signature | 35 |
| `drop_arg` | A cross-file call loses its last positional argument, which the callee requires | The callee's signature | 60 |
| `add_param` | A function called from other (non-test) files gets a new required trailing parameter; callers unchanged | A caller's call site (call-site window or full body) | 57 |

## Construction

- **Site selection.** Sites are chosen with the **frozen reference resolver** (`eval/reference`), not with the system under test. A call qualifies only when the reference resolver binds it to exactly one cross-file target. Calls must sit on one line, with no starred arguments.
- **Exclusions.** Signature mutations skip multi-line signatures, `*args`/`**kwargs`, keyword-only parameters, parameters with defaults and dunder methods. Test files are never mutated and never count as callers.
- **Sampling.** Per (repo, type), candidates are ordered by `sha1("t3-v1" ‖ …)`, at most one case per line, up to 10 cases. httpx (1), starlette (6), flask (8) and attrs (0) have fewer qualifying `arg_swap` sites.
- **Diff.** The mutated line in a ±3-line hunk. The base is the pin. HEAD is the base plus the mutation.

## Known limitations

- **The needed symbol's name is on the changed line**, so lexical retrieval finds callees easily. T3 v1.0 therefore barely separates v2 systems (all reach 96–100% context sufficiency) while separating v1 from v2 sharply. Bugs whose context is not named in the diff would make T3 harder: violated docstring preconditions, return-contract misuse, a base method removed under a `super()` call.
- **"Needed" is defined by the mutation**, using the reference resolver. It is the minimum context, not all useful context.
- **Detection needs an LLM.** It costs money and has not been run. Context sufficiency is necessary for detection but not sufficient.
