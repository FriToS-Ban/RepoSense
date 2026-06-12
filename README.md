# RepoSense

It is an AI-powered code review platform that automatically reviews your GitHub Pull Requests using RAG (Retrieval Augmented Generation) and LLM analysis. When a PR is opened or updated, RepoSense fetches the diff, retrieves relevant codebase context from a knowledge graph, and posts inline review comments directly on the PR in GitHub.

## Screenshots

### Landing Page
![Landing Page](docs/screenshots/landing.png)

### Dashboard & Repositories
![Dashboard](docs/screenshots/dashboard.png)

### Code Quality Trends & Analytics
![Analytics](docs/screenshots/analytics.png)

### Detailed Pull Request Reviews
![PR Detail](docs/screenshots/pr_detail.png)

---

## Features

- **Automatic PR Reviews** — triggers on every PR open or update via GitHub webhooks
- **RAG-Enhanced Analysis** — indexes your entire codebase into a knowledge graph for context-aware reviews
- **Inline GitHub Comments** — posts review comments directly on the exact line of code in GitHub
- **Quality Scoring** — assigns a quality score (0–100) to each PR based on issues found
- **Knowledge Graph** — extracts functions, classes, and relationships (imports, calls, inheritance) from Python and JS/TS files
- **Per-chunk Diff Embedding** — each changed hunk is embedded separately for targeted context retrieval
- **Dashboard** — view all repositories, PR history, and quality scores
- **Analytics** — track code quality trends, issue categories, and averages over time
- **PR Detail View** — see all review comments with severity, category, and file location
- **Multi-user** — each user connects their own GitHub account independently

---

## Tech Stack

**Frontend**
- React 18, Vite, Tailwind CSS
- React Router, Recharts
- React Context API (Global Authentication Context)
- Lucide React icons

**Backend**
- FastAPI (Python)
- SQLAlchemy + PostgreSQL
- PyGithub, HTTPX

**AI / ML**
- NVIDIA NIM API (LLM inference — Qwen3 Coder 480B)
- NVIDIA NV-EmbedQA (embeddings)
- Pinecone (vector database)

**Auth**
- GitHub OAuth 2.0
- JWT cookies

---

## How It Works

1. User connects GitHub account via OAuth
2. User enables a repository — optionally grants crawl permission
3. If crawl permission granted, RepoSense indexes the entire codebase:
   - Extracts file, function, and class nodes
   - Detects import, call, and inheritance edges
   - Stores embeddings in Pinecone
4. When a PR is opened/updated, GitHub sends a webhook to the backend
5. Backend fetches the PR diff, retrieves relevant codebase context via RAG
6. LLM reviews the diff with full context and returns structured comments
7. Comments are posted inline on the PR in GitHub
8. PR appears in dashboard with quality score and full review detail
9. Frontend manages global session status (`AuthContext`) and guards routes (`/dashboard`, `/analytics`, `/pr/:id`) to prevent unauthorized access while redirecting authenticated users to the dashboard.

### Quality Score Formula

Starts at **100** and deducts:
- **−15** per critical issue
- **−5** per warning  
- **−1** per suggestion

Minimum score is **0**.

---

## Local Setup

### Prerequisites

- Python 3.11+
- Node.js 18+
- PostgreSQL database
- GitHub OAuth App
- NVIDIA NIM API key — [build.nvidia.com](https://build.nvidia.com)
- Pinecone account — [pinecone.io](https://pinecone.io)

### 1. Clone the repository

```bash
git clone https://github.com/FriToS-Ban/RepoSense.git
cd RepoSense
```

### 2. Create a GitHub OAuth App

Go to GitHub → Settings → Developer settings → OAuth Apps → New OAuth App

| Field | Value |
|---|---|
| Homepage URL | `http://localhost:5173` |
| Authorization callback URL | `http://localhost:8000/api/auth/callback` |

Save the **Client ID** and **Client Secret**.

### 3. Backend setup

```bash
cd backend
pip install -r requirements.txt
```

Create a `.env` file in the `backend` folder:

```env
GITHUB_CLIENT_ID=your_github_client_id
GITHUB_CLIENT_SECRET=your_github_client_secret
GITHUB_WEBHOOK_SECRET=any_random_string
NVIDIA_API_KEY=your_nvidia_api_key
NVIDIA_MODEL=qwen/qwen3-coder-480b-a35b-instruct
PINECONE_API_KEY=your_pinecone_api_key
PINECONE_INDEX_NAME=reposense
DATABASE_URL=postgresql://user:password@localhost/reposense
SECRET_KEY=any_random_secret_string
FRONTEND_URL=http://localhost:5173
BACKEND_URL=http://localhost:8000
ENVIRONMENT=development
```

Start the backend:

```bash
cd ..
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

### 4. Frontend setup

```bash
cd frontend
npm install
```

Create a `.env` file in the `frontend` folder:

```env
VITE_API_URL=http://localhost:8000
```

Start the frontend:

```bash
npm run dev
```

### 5. Expose backend for webhooks (local development)

GitHub needs a public URL to send webhook events. Use [ngrok](https://ngrok.com):

```bash
ngrok http 8000
```

Copy the ngrok URL (e.g. `https://abc123.ngrok.io`) and update:
- `BACKEND_URL` in your backend `.env`
- The callback URL in your GitHub OAuth App

### 6. Open the app

Go to `http://localhost:5173`, connect your GitHub account, and enable a repository.

---

## Project Structure

```
RepoSense/
├── backend/
│   ├── api/
│   │   ├── deps.py
│   │   └── routes/
│   │       ├── analytics.py
│   │       ├── auth.py
│   │       ├── prs.py
│   │       ├── repos.py
│   │       └── webhooks.py
│   ├── core/
│   │   ├── config.py
│   │   ├── database.py
│   │   └── security.py
│   ├── models/
│   │   └── models.py
│   ├── services/
│   │   ├── github.py
│   │   ├── indexer.py
│   │   ├── llm.py
│   │   ├── rag.py
│   │   └── review.py
│   ├── main.py
│   └── requirements.txt
└── frontend/
    ├── src/
    │   ├── components/
    │   │   ├── GithubIcon.jsx
    │   │   └── Navbar.jsx
    │   ├── context/
    │   │   └── AuthContext.jsx
    │   ├── pages/
    │   │   ├── Analytics.jsx
    │   │   ├── Dashboard.jsx
    │   │   ├── Landing.jsx
    │   │   └── PRDetail.jsx
    │   ├── App.jsx
    │   └── main.jsx
    └── package.json
```

---

## Environment Variables Reference

| Variable | Description |
|---|---|
| `GITHUB_CLIENT_ID` | GitHub OAuth App client ID |
| `GITHUB_CLIENT_SECRET` | GitHub OAuth App client secret |
| `GITHUB_WEBHOOK_SECRET` | Secret for verifying webhook payloads |
| `NVIDIA_API_KEY` | NVIDIA NIM API key for LLM and embeddings |
| `NVIDIA_MODEL` | NVIDIA model string for reviews |
| `PINECONE_API_KEY` | Pinecone vector database API key |
| `PINECONE_INDEX_NAME` | Pinecone index name (default: `reposense`) |
| `DATABASE_URL` | PostgreSQL connection string |
| `SECRET_KEY` | Secret for signing JWT tokens |
| `FRONTEND_URL` | Frontend base URL (for CORS and redirects) |
| `BACKEND_URL` | Backend base URL (for OAuth callback and webhooks) |
| `ENVIRONMENT` | `development` or `production` |

---

## License

MIT