# RepoSense — AI GitHub PR Review Agent
### Spec for Antigravity / AI App Builder

---

## 🧠 What You Are Building

**RepoSense** is a web application that acts as an AI-powered code reviewer for GitHub Pull Requests.

When a developer opens or updates a PR in a connected GitHub repository, RepoSense:
1. Receives the event via a GitHub webhook
2. Fetches the PR diff (the actual code changes)
3. Sends the diff to an LLM (Claude or GPT-4o) with a structured review prompt
4. Posts the review back as inline comments on the GitHub PR — exactly like a human reviewer would
5. Shows a dashboard with PR history and code quality trends

---

## 👤 User Flow (Step by Step)

### Step 1 — Landing Page
- Hero section explaining what RepoSense does
- A single CTA button: **"Connect GitHub"**
- Show 3 feature bullets: Auto Review, Inline Comments, Quality Trends

### Step 2 — GitHub OAuth Login
- User clicks "Connect GitHub"
- Redirect to GitHub OAuth (scope: `repo`, `pull_requests`, `write:discussion`)
- On callback, store the access token securely in the backend session
- Redirect to the Dashboard

### Step 3 — Dashboard (Post Login)
- Show a list of the user's GitHub repositories (fetched via GitHub API)
- Each repo has a toggle: **"Enable RepoSense"**
- When enabled, the app registers a webhook on that repo via GitHub API
- Show a table of recent PRs reviewed with columns: Repo, PR Title, Status, Issues Found, Date

### Step 4 — PR Review Flow (Backend, Automatic)
- GitHub sends a webhook POST to `/api/webhook/github` when a PR is opened or updated
- Backend fetches the PR diff using GitHub API
- Backend sends the diff to the LLM with the review prompt (see Prompt section below)
- LLM returns structured review comments
- Backend posts each comment as an inline review comment on the PR via GitHub API
- Stores the review result in the database

### Step 5 — PR Detail Page
- User can click any reviewed PR in the dashboard
- See the full AI review: list of issues, severity (critical / warning / suggestion), file name, line number, explanation
- Show a quality score (0–100) calculated from number and severity of issues

### Step 6 — Analytics Page
- Line chart: Code Quality Score over time per repo
- Bar chart: Issues by category (Security, Performance, Logic, Style)
- Most common issue types across all PRs

---

## 🗄️ Database Schema

Use **PostgreSQL** (via Supabase or Railway).

### Table: `users`
```
id (uuid, primary key)
github_id (string, unique)
github_username (string)
access_token (string, encrypted)
created_at (timestamp)
```

### Table: `repositories`
```
id (uuid, primary key)
user_id (uuid, foreign key → users.id)
github_repo_id (string)
repo_name (string)
repo_full_name (string)   -- e.g. "username/reposense"
webhook_id (string)       -- GitHub webhook ID for cleanup
is_active (boolean)
created_at (timestamp)
```

### Table: `pull_requests`
```
id (uuid, primary key)
repo_id (uuid, foreign key → repositories.id)
github_pr_number (integer)
pr_title (string)
pr_author (string)
status (enum: pending, reviewed, failed)
quality_score (integer, 0-100)
reviewed_at (timestamp)
created_at (timestamp)
```

### Table: `review_comments`
```
id (uuid, primary key)
pr_id (uuid, foreign key → pull_requests.id)
file_path (string)
line_number (integer)
severity (enum: critical, warning, suggestion)
category (enum: security, performance, logic, style, documentation)
comment_body (text)
github_comment_id (string)   -- ID of the posted GitHub comment
created_at (timestamp)
```

---

## 🔌 API Endpoints

### Authentication
```
GET  /api/auth/github          → Redirect to GitHub OAuth
GET  /api/auth/callback        → Handle OAuth callback, create session
POST /api/auth/logout          → Clear session
GET  /api/auth/me              → Return current user info
```

### Repositories
```
GET  /api/repos                → List user's GitHub repos
POST /api/repos/enable         → Enable RepoSense on a repo (registers webhook)
POST /api/repos/disable        → Disable RepoSense (deletes webhook)
GET  /api/repos/:id/stats      → Get quality stats for a repo
```

### Pull Requests
```
GET  /api/prs                  → List all reviewed PRs for the user
GET  /api/prs/:id              → Get full review detail for one PR
GET  /api/prs/:id/comments     → Get all AI review comments for a PR
```

### Webhook (Public, no auth)
```
POST /api/webhook/github       → Receive GitHub webhook events
```

### Analytics
```
GET  /api/analytics/overview   → Quality score trend, total issues, total PRs
GET  /api/analytics/categories → Issues grouped by category
```

---

## 🤖 LLM Integration

### Model
Use **Claude claude-sonnet-4-20250514** via the Anthropic API or **GPT-4o** via OpenAI API.

### System Prompt
```
You are an expert senior software engineer performing a thorough code review.
You will be given a GitHub Pull Request diff.

Your job is to review the code and return a structured list of review comments.

Rules:
- Only comment on things that actually matter: bugs, security issues, performance problems, logic errors, and serious code quality issues.
- Do NOT comment on minor style issues unless they are significant.
- Do NOT praise the code. Only flag problems.
- Be specific. Reference the exact file and line number.
- Explain WHY something is a problem, not just that it is one.
- Suggest a concrete fix for each issue.

Respond ONLY with a valid JSON array. No preamble, no markdown, no explanation outside the JSON.

Each item in the array must have exactly these fields:
{
  "file_path": "src/utils/auth.py",
  "line_number": 42,
  "severity": "critical" | "warning" | "suggestion",
  "category": "security" | "performance" | "logic" | "style" | "documentation",
  "comment": "Explanation of the issue and how to fix it."
}

If there are no issues, return an empty array: []
```

### User Message to LLM
```
Here is the Pull Request diff to review:

PR Title: {pr_title}
Author: {pr_author}
Repository: {repo_full_name}

Diff:
{pr_diff}
```

### Quality Score Calculation
```
Start at 100.
For each critical issue:   subtract 15 points
For each warning:          subtract 5 points
For each suggestion:       subtract 1 point
Minimum score is 0.
```

---

## 🎨 UI Pages & Components

### Tech Stack for Frontend
- **React** with **Tailwind CSS**
- Use **Recharts** for all charts
- Use **React Router** for navigation
- Dark theme by default (background: #0d1117, similar to GitHub dark)

### Pages

#### `/` — Landing Page
- Navbar with logo and "Connect GitHub" button
- Hero: Large headline, subtitle, CTA button
- Features section: 3 cards (Auto Review, Inline Comments, Trends)
- How it works: 3 steps with icons
- Footer

#### `/dashboard` — Main Dashboard (protected)
- Top stats bar: Total PRs Reviewed, Average Quality Score, Total Issues Found
- Repository list with enable/disable toggles
- Recent PR reviews table: repo name, PR title, quality score badge, date, link to detail

#### `/pr/:id` — PR Review Detail (protected)
- PR title, author, repo, date
- Quality score shown as a large circular gauge
- List of review comments grouped by file
- Each comment shows: severity badge (color-coded), category tag, file path + line number, comment text

#### `/analytics` — Analytics (protected)
- Line chart: Quality score over last 30 days per repo
- Bar chart: Issue count by category
- Top 5 most flagged files

#### Common Components
- `<Navbar />` — Logo, nav links, user avatar with logout
- `<QualityBadge score={85} />` — Color: green >80, yellow 50-80, red <50
- `<SeverityBadge severity="critical" />` — Red / Yellow / Blue
- `<RepoToggle repo={repo} onToggle={fn} />` — Toggle with loading state
- `<PRTable prs={[]} />` — Sortable table of PRs

---

## ⚙️ Backend Stack

- **Framework:** FastAPI (Python)
- **Database:** PostgreSQL via SQLAlchemy ORM
- **Authentication:** GitHub OAuth 2.0 + JWT session tokens stored in httpOnly cookies
- **Task Queue:** Use background tasks (FastAPI BackgroundTasks) for processing webhooks asynchronously — do NOT make the webhook endpoint wait for the LLM response
- **GitHub API client:** Use the `PyGithub` library

### Webhook Processing Flow
```
1. POST /api/webhook/github receives event
2. Verify GitHub webhook signature (X-Hub-Signature-256 header) — REQUIRED for security
3. If event is "pull_request" and action is "opened" or "synchronize":
   a. Return 200 immediately
   b. Kick off background task: process_pr_review(pr_number, repo_full_name)
4. Background task:
   a. Fetch PR diff from GitHub API
   b. Send diff to LLM
   c. Parse JSON response
   d. Post inline comments to GitHub PR
   e. Save review to database
   f. Calculate and save quality score
```

---

## 🔐 Environment Variables

```
# GitHub OAuth App
GITHUB_CLIENT_ID=
GITHUB_CLIENT_SECRET=
GITHUB_WEBHOOK_SECRET=      # Secret used to verify webhook signatures

# LLM
ANTHROPIC_API_KEY=           # If using Claude
OPENAI_API_KEY=              # If using GPT-4o

# Database
DATABASE_URL=                # PostgreSQL connection string

# App
SECRET_KEY=                  # For JWT signing
FRONTEND_URL=                # e.g. https://reposense.vercel.app
BACKEND_URL=                 # e.g. https://reposense-api.onrender.com
```

---

## 🚀 Deployment

| Layer | Platform | Notes |
|---|---|---|
| Frontend | Vercel | Connect GitHub repo, auto-deploy on push |
| Backend | Render | Free tier, set env vars in dashboard |
| Database | Supabase | Free PostgreSQL, copy connection string |

### Important Deployment Notes
- The GitHub webhook URL must be the **public Render URL**, e.g. `https://reposense-api.onrender.com/api/webhook/github`
- Render free tier spins down after inactivity — for demos, use Render's always-on or add a simple uptime ping
- Set `FRONTEND_URL` in backend for CORS configuration

---

## ✅ What To Build First (Suggested Order)

1. **Backend: GitHub OAuth** — get login working first, everything depends on it
2. **Backend: Repo listing** — call GitHub API, return user's repos
3. **Frontend: Landing + Dashboard shell** — static UI with mock data
4. **Backend: Webhook endpoint** — receive and verify GitHub events
5. **Backend: LLM integration** — send diff, parse response
6. **Backend: Post comments to GitHub** — the core feature
7. **Frontend: Wire up real data** — replace mock data with API calls
8. **Frontend: PR detail page** — show the actual review
9. **Backend + Frontend: Analytics** — charts and trends
10. **Polish: Error states, loading states, empty states**

---

## ⚠️ Known Hard Parts (Do Not Let AI Skip These)

1. **Webhook signature verification** — GitHub signs every webhook. You must verify `X-Hub-Signature-256`. If you skip this, anyone can fake a webhook. PyGithub has a helper for this.

2. **Async webhook processing** — The LLM call can take 10–30 seconds. GitHub expects a 200 response within 10 seconds or it marks the webhook as failed. Always return 200 immediately and process in the background.

3. **Diff size limits** — Some PRs have massive diffs. If the diff exceeds ~8000 tokens, truncate it and add a note in the review: "Diff truncated due to size. Showing first N files."

4. **GitHub API rate limits** — GitHub allows 5000 requests/hour per authenticated user. You won't hit this in dev, but log it.

5. **OAuth token storage** — Never store raw access tokens in localStorage on the frontend. Store them in the backend database, return a short-lived JWT to the frontend instead.

---

## 📝 README Must Include

- What RepoSense does (2 sentences)
- Live demo link
- Screenshot of dashboard and a reviewed PR
- Setup instructions (env vars, how to run locally)
- How the AI review works (the prompt strategy)
- Tech stack badges
