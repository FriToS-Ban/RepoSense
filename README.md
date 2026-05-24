# RepoSense — AI GitHub PR Review Agent

> RepoSense automatically reviews your GitHub Pull Requests using Claude AI, posting inline comments directly on your PRs — just like a senior engineer on your team. Connect a repo, open a PR, and get a structured code review in seconds.


---

## Tech Stack

**Frontend:** React, Tailwind CSS, Recharts, React Router
**Backend:** FastAPI (Python), SQLAlchemy, PyGithub
**Database:** PostgreSQL (Supabase)
**AI:** Claude `claude-sonnet-4-20250514` via Anthropic API
**Deployment:** Vercel (frontend), Render (backend), Supabase (database)

---

## Features

- **Automatic PR Reviews** — Triggered by GitHub webhooks whenever a PR is opened or updated
- **Inline Comments** — AI review comments posted directly on the PR diff, file by file and line by line
- **Quality Scoring** — Every PR gets a 0–100 quality score based on issue count and severity
- **Analytics Dashboard** — Track code quality trends over time across all your repositories
- **Severity Triage** — Issues categorized as Critical, Warning, or Suggestion across Security, Performance, Logic, Style, and Documentation

---

## How the AI Review Works

When a PR is opened or updated:

1. GitHub sends a webhook event to the RepoSense backend
2. The backend fetches the full PR diff via the GitHub API
3. The diff is sent to **Claude (`claude-sonnet-4-20250514`)** with a structured system prompt that instructs it to act as a senior engineer — flagging only real problems (bugs, security issues, logic errors, performance problems), explaining *why* each issue matters, and suggesting concrete fixes
4. Claude responds with a JSON array of review comments, each containing the file path, line number, severity, category, and explanation
5. The backend posts each comment as an inline review comment on the GitHub PR
6. A quality score is calculated: starts at 100, subtracts 15 per critical issue, 5 per warning, and 1 per suggestion

**The prompt explicitly instructs the model to skip minor style nitpicks and not to praise the code** — only actionable, meaningful feedback is posted.

If a PR diff is very large (>8000 tokens), it is truncated and a note is added to the review.

---

## Setup & Local Development

### Prerequisites

- Python 3.11+
- Node.js 18+
- PostgreSQL database (or a [Supabase](https://supabase.com) project)
- A [GitHub OAuth App](https://github.com/settings/developers)
- An [Anthropic API key](https://console.anthropic.com)

### 1. Clone the repo

```bash
git clone https://github.com/your-username/reposense.git
cd reposense
```

### 2. Configure environment variables

**Backend** — create `backend/.env`:

```env
# GitHub OAuth App
GITHUB_CLIENT_ID=your_github_client_id
GITHUB_CLIENT_SECRET=your_github_client_secret
GITHUB_WEBHOOK_SECRET=your_webhook_secret

# LLM
ANTHROPIC_API_KEY=your_anthropic_api_key

# Database
DATABASE_URL=postgresql://user:password@localhost:5432/reposense

# App
SECRET_KEY=your_random_secret_key
FRONTEND_URL=http://localhost:5173
BACKEND_URL=http://localhost:8000
```

**Frontend** — create `frontend/.env`:

```env
VITE_API_URL=http://localhost:8000
```

### 3. Run the backend

```bash
cd backend
python -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt
alembic upgrade head            # Run database migrations
uvicorn main:app --reload --port 8000
```

### 4. Run the frontend

```bash
cd frontend
npm install
npm run dev
```

The app will be available at `http://localhost:5173`.

### 5. Expose your local backend for webhooks (dev only)

GitHub needs a public URL to send webhook events. Use [ngrok](https://ngrok.com):

```bash
ngrok http 8000
```

Copy the `https://....ngrok.io` URL and set it as your webhook URL in GitHub (or in the RepoSense dashboard when enabling a repo). Update `BACKEND_URL` in your `.env` accordingly.

---

## Deployment

| Layer    | Platform  | Notes                                              |
|----------|-----------|----------------------------------------------------|
| Frontend | Vercel    | Connect GitHub repo, auto-deploys on push          |
| Backend  | Render    | Set all env vars in the dashboard                  |
| Database | Supabase  | Free PostgreSQL, copy the connection string        |

The GitHub webhook URL must be your public Render backend URL:
```
https://your-app.onrender.com/api/webhook/github
```

> **Note:** Render's free tier spins down after inactivity. For demos, enable Render's always-on feature or add an uptime ping service.

---

## Project Structure

```
reposense/
├── backend/
│   ├── main.py                 # FastAPI app entry point
│   ├── routers/
│   │   ├── auth.py             # GitHub OAuth endpoints
│   │   ├── repos.py            # Repository management
│   │   ├── prs.py              # PR review endpoints
│   │   ├── webhook.py          # GitHub webhook handler
│   │   └── analytics.py        # Analytics endpoints
│   ├── services/
│   │   ├── github.py           # GitHub API client (PyGithub)
│   │   ├── llm.py              # Claude API integration
│   │   └── review.py           # Review processing logic
│   ├── models.py               # SQLAlchemy models
│   └── requirements.txt
└── frontend/
    ├── src/
    │   ├── pages/
    │   │   ├── Landing.jsx
    │   │   ├── Dashboard.jsx
    │   │   ├── PRDetail.jsx
    │   │   └── Analytics.jsx
    │   └── components/
    │       ├── Navbar.jsx
    │       ├── QualityBadge.jsx
    │       ├── SeverityBadge.jsx
    │       ├── RepoToggle.jsx
    │       └── PRTable.jsx
    └── package.json
```

---

## API Overview

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/auth/github` | Redirect to GitHub OAuth |
| GET | `/api/auth/callback` | Handle OAuth callback |
| GET | `/api/repos` | List user's GitHub repos |
| POST | `/api/repos/enable` | Enable RepoSense + register webhook |
| POST | `/api/repos/disable` | Disable RepoSense + delete webhook |
| GET | `/api/prs` | List all reviewed PRs |
| GET | `/api/prs/:id` | Full review detail for one PR |
| POST | `/api/webhook/github` | Receive GitHub webhook events |
| GET | `/api/analytics/overview` | Quality score trends and totals |

---

## Security Notes

- Webhook signature verification (`X-Hub-Signature-256`) is enforced on every incoming webhook — unauthenticated requests are rejected
- GitHub access tokens are stored encrypted in the database; the frontend never sees them
- The frontend receives a short-lived JWT stored in an `httpOnly` cookie
- Webhook processing is fully asynchronous — the endpoint returns `200` immediately and processes the LLM review in the background, keeping GitHub happy

---

## License

MIT
