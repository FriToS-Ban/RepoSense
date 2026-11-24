"""Named retrieval configurations chosen by the milestones' experiments.

TUNED_LEXICAL: the lexical-channel settings selected on T2-train by eval/tune_lexical.py (M6;
configuration "rand20", +.029 R@budget on T2-train over the M2 defaults) and validated on
T2-test (docs/retrieval/results/M6.md).  RetrievalConfig's own defaults stay at the M2 values
so that earlier systems remain reproducible.
"""

TUNED_LEXICAL: dict = {
    "bm25f_weights": {"name": 1.0, "qual": 1.5, "sig": 3.0, "doc": 0.5, "body": 1.0},
    "bm25_k1": 0.8,
    "bm25_b": 0.75,
    "pr_text_weight": 2.0,
    "context_weight": 0.5,
    "lexical_k": 50,
    "min_rel_score": 0.15,
    "max_candidates": 60,
    "residual_weight": 2.0,
}
