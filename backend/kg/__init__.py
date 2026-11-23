"""RepoSense code knowledge graph (v2).  Design: docs/retrieval/02-design.md §3-4.

    extract_python  per-file facts (pure, cacheable)
    resolve         global name resolution with confidence tiers R1-R5
    derive          MRO, OVERRIDES, TESTS
    build           build_graph / build_from_files / validate (invariants)
    store           SQL persistence by transactional diff-apply
    snapshot        immutable in-memory CSR view for traversal
"""
