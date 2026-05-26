import httpx
from github import Github, Auth
from github.GithubException import GithubException
from backend.core.config import settings
import base64

def get_github_client(access_token: str) -> Github:
    auth = Auth.Token(access_token)
    return Github(auth=auth)

def list_user_repositories(access_token: str):
    g = get_github_client(access_token)
    user = g.get_user()
    # We can fetch repos where the user is an owner or has admin rights
    # For simplicity, returning first 100 repos
    repos = []
    for repo in user.get_repos(type="owner"):
        repos.append({
            "id": str(repo.id),
            "name": repo.name,
            "full_name": repo.full_name
        })
    return repos

def create_webhook(access_token: str, repo_full_name: str) -> str:
    """Creates a webhook and returns its ID."""
    g = get_github_client(access_token)
    repo = g.get_repo(repo_full_name)
    
    config = {"url": f"{settings.BACKEND_URL}/api/webhook/github",
              "content_type": "json",
              "secret": settings.GITHUB_WEBHOOK_SECRET
              }
    
    try:
        hook = repo.create_hook(
            name="web",
            config=config,
            events=["pull_request"],
            active=True
        )
        return str(hook.id)
    except GithubException as e:
        # 422 means hook already exists
        if e.status == 422:
            # Try to find and return existing hook id
            hooks = repo.get_hooks()
            for h in hooks:
                if h.config.get("url") == config["url"]:
                    return str(h.id)
        raise e

def delete_webhook(access_token: str, repo_full_name: str, hook_id: str):
    g = get_github_client(access_token)
    repo = g.get_repo(repo_full_name)
    try:
        hook = repo.get_hook(int(hook_id))
        hook.delete()
    except GithubException:
        pass  # Ignore if it doesn't exist

def get_pr_diff(access_token: str, repo_full_name: str, pr_number: int) -> str:
    g = get_github_client(access_token)
    repo = g.get_repo(repo_full_name)
    # The get_pull method returns a PullRequest object. Wait, PyGithub doesn't natively return diff string easily.
    # Alternatively, use httpx with accept header 'application/vnd.github.v3.diff'
    
    url = f"https://api.github.com/repos/{repo_full_name}/pulls/{pr_number}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github.v3.diff"
    }
    # Sync request for simplicity in background task
    response = httpx.get(url, headers=headers)
    response.raise_for_status()
    return response.text

def post_review_comments(access_token: str, repo_full_name: str, pr_number: int, comments: list):
    """
    comments format: list of dicts with file_path, line_number, comment.
    Note: GitHub's API for review comments requires a commit_id and position. 
    To simplify, we can post them as regular issue comments for the PR, 
    or just post a single consolidated PR review.
    The spec says: "Backend posts each comment as an inline review comment on the PR via GitHub API".
    Actually, to post an inline comment we need the latest commit id, file path, and line number.
    """
    g = get_github_client(access_token)
    repo = g.get_repo(repo_full_name)
    pr = repo.get_pull(pr_number)
    
    commit_id = pr.get_commits().reversed[0].sha
    
    for c in comments:
        try:
            # line_number requires mapping to diff hunks in GitHub, which is notoriously difficult.
            # PyGithub's create_review_comment takes: body, commit_id, path, line (only for PR reviews in API v3 with line param)
            pr.create_review_comment(
                body=c["comment"],
                commit_id=commit_id,
                path=c["file_path"],
                line=c["line_number"],
                side="RIGHT"
            )
        except GithubException as e:
            # Fallback to general comment if line is invalid
            pr.create_issue_comment(f"**Issue in {c['file_path']} at line {c['line_number']}**\n\n{c['comment']}")

def get_repo_contents(access_token: str, repo_full_name: str, path: str = "") -> list:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    results = []
    
    def fetch_recursive(current_path):
        url = f"https://api.github.com/repos/{repo_full_name}/contents/{current_path}"
        response = requests.get(url, headers=headers)
        if response.status_code != 200:
            return
        items = response.json()
        if not isinstance(items, list):
            return
        for item in items:
            if item["type"] == "dir":
                if item["name"] not in [".git", "node_modules", "__pycache__", ".venv", "dist", "build"]:
                    fetch_recursive(item["path"])
            elif item["type"] == "file" and item.get("size", 0) < 100000:
                file_res = requests.get(item["url"], headers=headers)
                if file_res.status_code == 200:
                    file_data = file_res.json()
                    content = ""
                    if file_data.get("encoding") == "base64":
                        try:
                            content = base64.b64decode(file_data["content"]).decode("utf-8", errors="ignore")
                        except Exception:
                            pass
                    results.append({"path": item["path"], "content": content})
    
    fetch_recursive(path)
    return results