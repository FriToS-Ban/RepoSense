"""Review-context retrieval v2.  Design: docs/retrieval/02-design.md §5.

    diff       unified-diff parser
    seeds      diff -> changed symbols (seeds) + query plan
    lexical    BM25F over symbol fields
    packing    budget-aware, relation-aware context construction
    pipeline   RepoRetriever.build_context(diff, head_content, pr_text) -> ContextResult (text, items, trace)
"""
