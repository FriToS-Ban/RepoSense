# DECISIONS.md — Architectural Decisions, Evolution & Challenges

> This document captures the **full reasoning** behind every major architectural choice, pivot, and trade-off made while building **RepoSense** — an AI-powered GitHub Pull Request review platform that uses Retrieval-Augmented Generation (RAG) and a code knowledge graph.
>
> It is written so that future-me (or an interviewer) can understand not just *what* changed, but *why* it changed, what problems forced the change, what alternatives were considered, and what difficulties were encountered along the way.

---

## 1. Original Vision (First Push — May 2026)

### The problem I wanted to solve

Most automated code-review tools either:
- produce generic, low-signal comments, or
- require heavy configuration / proprietary agents that sit inside the IDE.

I wanted something that felt like a senior engineer who had actually read the entire codebase and then left precise, actionable inline comments on the exact lines that changed.

### Core product loop (from the original `instructions.md`)

1. User lands on a clean marketing page and clicks **“Connect GitHub”**.
2. GitHub OAuth grants the necessary scopes (`repo`, `pull_requests`, `write:discussion`).
3. User sees a list of their repositories and toggles “Enable RepoSense” on the ones they care about.
4. Enabling a repo registers a GitHub webhook.
5. Whenever a Pull Request is opened or updated (`opened` / `synchronize`), GitHub fires the webhook.
6. The backend:
   - verifies the webhook signature,
   - immediately returns 200,
   - kicks off a background task,
   - fetches the PR diff,
   - sends the diff to an LLM with a carefully engineered system prompt,
   - receives a structured JSON array of issues,
   - posts each issue as a true **inline review comment** on the correct file and line in GitHub,
   - stores the result in the database.
7. The user can later open the dashboard, see quality scores over time, drill into any PR, and view analytics.

### Original quality-score formula (still used today)
score = 100
- 15 × number of critical issues
- 5  × number of warnings
- 1  × number of suggestions
floor at 0


This formula was chosen because it is completely transparent, easy to explain to users, and correlates reasonably well with the actual severity of problems found.

---

### Initial technology choices and the reasoning behind them

| Layer              | Choice                                      | Why it was chosen at the time |
|--------------------|---------------------------------------------|-------------------------------|
| Backend framework  | FastAPI                                     | Excellent async support, automatic OpenAPI docs, perfect for webhooks + background tasks |
| ORM / Database     | SQLAlchemy + PostgreSQL                     | Mature, type-friendly, easy to evolve the schema later |
| Authentication     | GitHub OAuth 2.0 + JWT in httpOnly cookies  | No password management, industry standard, works well with SPA |
| LLM                | Google Gemini (via `google-generativeai`)   | Free tier, decent structured output, simple Python SDK |
| Frontend           | React 18 + Vite + Tailwind + Recharts       | Fast iteration, dark-theme friendly (GitHub aesthetic), good charting |
| Deployment mindset | Local development + ngrok for webhooks      | Fastest possible feedback loop |

The very first commit already contained a working (later revoked) set of credentials and a full product specification, showing that the project was intended to be runnable from day one rather than a pure research prototype.

---

## 2. Major Architectural Decisions

### Decision 1 — Process webhooks asynchronously with FastAPI BackgroundTasks

**Context**  
GitHub expects the webhook endpoint to respond with a 2xx status within a few seconds. Doing an LLM call (which can take 5–20 seconds) and then posting multiple review comments synchronously would cause frequent timeouts and retries.

**Alternatives considered**
- Celery + Redis / RabbitMQ
- ARQ / RQ
- Simply making the endpoint wait (bad idea)

**Decision**  
Use FastAPI’s built-in `BackgroundTasks`. The webhook handler does the minimum amount of work (signature verification + payload parsing), returns 200 immediately, and schedules `process_pr_review(...)` as a background task.

**Trade-offs**
- No automatic retries, dead-letter queues, or observability.
- If the process crashes mid-review, the review is lost.
- Acceptable for an MVP and for personal use. Would need to be upgraded for production multi-tenant usage.

**Difficulty encountered**  
Occasionally the background task would fail silently because of missing database sessions or expired tokens. This forced me to add better logging and explicit session management inside the background function.

---

### Decision 2 — Force the LLM to return pure structured JSON

**Context**  
Inline comments are only useful if they contain an exact file path and line number. Free-form natural language is almost worthless for this use-case.

**Implementation**  
A long, carefully worded system prompt that:
- forbids any text outside the JSON array,
- defines the exact schema (file_path, line_number, severity, category, comment),
- tells the model to only flag real problems (bugs, security, performance, logic) and to ignore minor style issues,
- asks for a concrete fix suggestion in every comment.

Temperature is kept low (0.2) to reduce creativity.

**Difficulties**
- Models still occasionally wrap the JSON in markdown fences or add a short preamble.
- Had to write a robust extraction helper that strips markdown, finds the first `[` and last `]`, and falls back to an empty list on parse failure.
- Diff size management became important; very large diffs had to be truncated or chunked before being sent to the model.

---

### Decision 3 — Switch from Google Gemini to NVIDIA NIM (Qwen3-Coder-480B)

**Timeline**  
Mid-to-late May 2026 (`changed gemini api to nvidia` commit).

**Why the switch became necessary**
1. The original `google-generativeai` package was being deprecated in favour of the newer `google-genai` client, causing repeated breakage.
2. Free-tier rate limits and occasional model unavailability made the product unreliable.
3. I wanted a model that was more specialised in code understanding and generation.

**Decision**  
Move to NVIDIA’s OpenAI-compatible endpoint (`https://integrate.api.nvidia.com/v1`) and use the official `openai` Python client. The model chosen was `qwen/qwen3-coder-480b-a35b-instruct`.

**Later related change**  
Embeddings were also moved from Gemini’s `text-embedding-004` to NVIDIA’s `nvidia/nv-embedqa-e5-v5` (dimension changed from 768 → 1024). This required recreating the Pinecone index.

**Lessons**
- Relying on a single free-tier provider is fragile.
- OpenAI-compatible APIs make swapping providers relatively painless.
- Always keep the embedding model and the generation model under the same vendor when possible to simplify billing and rate-limit management.

---

### Decision 4 — Introduce a full RAG pipeline + Code Knowledge Graph  
*(the single biggest architectural leap)*

**Commit**  
`added RAG knowledge graph with pinecone, updated review flow…` (26 May)

**Problem with pure-diff reviews**  
The LLM only ever saw the lines that changed. It had zero knowledge of:
- the surrounding function,
- callers of a modified function,
- the class hierarchy,
- imported utilities,
- similar patterns elsewhere in the codebase.

This led to many false positives (“this variable is undefined” when it was defined three functions above) and missed deeper issues.

**Solution architecture**

1. When a user enables a repository they are now asked for an optional **crawl permission**.
2. If granted, a background indexer walks the entire repository (skipping `node_modules`, `.git`, `__pycache__`, large files, etc.).
3. For every supported file (Python + later JS/TS) the indexer extracts:
   - `CodeNode` records (file, function, class)
   - `CodeEdge` records (imports, calls, inherits, defines)
4. Each node’s content is embedded and stored in Pinecone together with metadata (`repo_id`, `node_id`, `file_path`, etc.).
5. On a PR review the changed hunks are embedded, the top-k most relevant nodes are retrieved, related nodes are pulled via the graph edges, and the resulting context is injected into the LLM prompt.

**New database tables**
- `code_nodes`
- `code_edges`
- Two new boolean flags on `repositories`: `crawl_permission` and `is_indexed`

**Frontend impact**  
The enable-repo modal now has a second checkbox. The dashboard shows a live status indicator (“Indexing…”, “Knowledge graph ready”).

**Trade-offs**
- Indexing a large monorepo can take several minutes and consumes embedding tokens.
- The knowledge graph can become stale (addressed in a later decision).
- Storage cost in Pinecone grows with the number of indexed repositories.

---

### Decision 5 — Per-chunk diff embedding + context deduplication

**Commit**  
`improved RAG with per-chunk diff embedding and context deduplication`

**Problem**  
Embedding the entire diff as a single vector was too coarse. A large PR that touched five unrelated modules would retrieve a noisy mix of context.

**Solution**
1. Split the diff into individual hunks using the `diff --git` and `@@` markers.
2. Embed each hunk separately (capped at 8 hunks to control cost/latency).
3. Retrieve top-k nodes for every hunk.
4. Run a deduplication step that keeps only the richest version of each `(file_path, name)` pair.
5. Enforce a hard character budget (≈12 000 characters) when building the final context string, truncating gracefully when necessary.
6. Prefer more specific nodes (functions/classes) over whole-file nodes when ranking.

This change produced noticeably higher-quality and more focused review comments.

---

### Decision 6 — Support for JavaScript / TypeScript and richer edges

Originally the indexer only handled Python reasonably well. A subsequent commit extended parsing to `.js`, `.ts`, `.jsx`, `.tsx` and improved extraction of call and import edges. This made the knowledge graph useful for full-stack and frontend-heavy repositories.

---

### Decision 7 — Push-event driven incremental re-indexing

**Problem**  
Once a repository was indexed, any subsequent commits made the knowledge graph stale. A function that was heavily refactored would still appear in the old form in Pinecone.

**Solution**
- Register both `pull_request` *and* `push` events when creating the webhook.
- On a `push` event, collect the set of added/modified files, filter to code extensions, and re-index only those files in the background.
- This keeps the graph reasonably fresh without the cost of a full re-crawl.

**Difficulty**  
Had to be careful about race conditions (a PR review starting while a re-index was still running) and about not re-indexing the same file multiple times when a push contained many commits.

---

### Decision 8 — Always inject the directly changed files as context

Even with sophisticated RAG, the single most useful pieces of context are often the full contents of the files that actually appear in the diff. A later commit therefore always pulls the `CodeNode`s belonging to the changed file paths and places them at the front of the context window before any vector-retrieved nodes.

---

### Decision 9 — Authentication & cookie hardening for real deployments

Several commits addressed the classic SPA + cross-origin problems:
- JWT stored in `httpOnly`, `Secure`, `SameSite=None` (or `Lax`) cookies.
- Explicit `FRONTEND_URL` and `BACKEND_URL` environment variables so nothing is hard-coded to `localhost`.
- A global React `AuthContext` that guards `/dashboard`, `/analytics`, and `/pr/:id` routes and redirects unauthenticated users.
- Proper CORS configuration on the FastAPI side.

These changes were necessary before the application could be deployed anywhere other than pure local development.

---

### Decision 10 — Frontend UX & polish

Later commits focused on:
- A proper landing page with clear value proposition.
- Live knowledge-graph status indicators on the dashboard.
- A tooltip that explains exactly how the quality score is calculated.
- Consistent dark theme matching GitHub’s own UI.
- Cleaner PR detail and analytics pages.

These were not architectural, but they dramatically improved the perceived quality of the product.

---

## 3. Difficulties Encountered (and how they were solved)

| Difficulty | Impact | How it was resolved |
|------------|--------|---------------------|
| Gemini SDK deprecation + rate limits | Reviews started failing randomly | Migrated generation and embeddings to NVIDIA NIM |
| LLM occasionally returning non-JSON | Parsing crashes | Robust extraction helper + graceful fallback to empty review |
| Very large PR diffs | Context-window overflow / high cost | Chunking, truncation, and a hard character budget |
| Stale knowledge graph after pushes | Irrelevant or outdated context | Push webhook + incremental re-indexing |
| Cross-origin cookie issues in production | Users appeared logged-out after OAuth | Explicit cookie flags + environment-driven URLs |
| Indexing large monorepos | Timeouts and high embedding cost | Size filters, junk-directory skipping, background tasks |
| Webhook signature verification | Security risk if omitted | Mandatory HMAC-SHA256 check using `GITHUB_WEBHOOK_SECRET` |
| Hard-coded localhost URLs | Deployment friction | Everything driven by `.env` variables |
| Dependency conflicts (psycopg2, pydantic-settings, google packages) | Install failures on fresh machines | Explicit pinning and use of `psycopg2-binary` |
| Silent background-task failures | Lost reviews | Better logging + explicit session handling inside background functions |

---

## 4. Current High-Level Architecture (as of latest main)

┌─────────────────┐
│  React Frontend │  (AuthContext, Dashboard, Analytics, PR Detail)
└────────┬────────┘
│ OAuth + JWT cookies
┌────────▼────────┐
│   FastAPI       │
│  ┌────────────┐ │
│  │ Auth routes│ │
│  │ Repo mgmt  │ │  ← enable/disable + optional crawl
│  │ Webhooks   │ │  ← PR + Push events
│  │ Analytics  │ │
│  └────────────┘ │
│        │        │
│  Background     │
│  Tasks          │
│  ┌────────────┐ │
│  │ Review     │ │  1. fetch diff
│  │ Pipeline   │ │  2. RAG retrieval (Pinecone + graph)
│  │            │ │  3. LLM (NVIDIA Qwen3-Coder)
│  │            │ │  4. post inline comments
│  │            │ │  5. persist score + comments
│  └────────────┘ │
│  ┌────────────┐ │
│  │ Indexer    │ │  full crawl + incremental re-index
│  └────────────┘ │
└────────┬────────┘
│
┌────▼────┐   ┌──────────┐
│PostgreSQL│   │ Pinecone │
│ (graph + │   │ (vectors)│
│  reviews)│   └──────────┘
└─────────┘
text

---

## 5. What I Would Do Differently Next Time

1. **Start with a real job queue** (Celery, ARQ, or Temporal) instead of FastAPI BackgroundTasks. The lack of retries and observability became painful.
2. **Use tree-sitter** (or an equivalent) for language-agnostic AST extraction from day one instead of hand-rolled Python + JS parsers.
3. **Expose indexing progress** in the UI (percentage + estimated time) and a “Force re-index” button.
4. **Persist the raw LLM response** for later evaluation, debugging, and prompt iteration.
5. **Introduce hybrid search** (BM25 + vector) or a cheaper embedding model once cost became noticeable.
6. **Add concurrency / rate-limit controls** on the review pipeline so a single very active repository cannot starve others.
7. **Encrypt access tokens at rest** instead of storing them in plain text (acceptable for a personal project, not for production).

---

## 6. Evolution Timeline (condensed)

| Approximate Date | Milestone |
|------------------|---------|
| 21 May 2026      | Initial vision + full product specification uploaded |
| 23–25 May        | Gemini integration, dependency stabilisation, first working end-to-end reviews |
| 26 May           | **RAG + Pinecone + knowledge graph** introduced; crawl permission added |
| 26 May           | Generation model switched from Gemini → NVIDIA Qwen3-Coder |
| 27 May           | Embeddings also moved to NVIDIA NV-EmbedQA; per-chunk RAG + deduplication |
| 27–28 May        | Direct file context, push-based re-indexing, dashboard status indicators |
| Late May–June    | Auth hardening, removal of hard-coded localhost, UI polish, README finalisation |

---

*This is a living document. Update it whenever a significant architectural decision is made or a major difficulty is overcome.*