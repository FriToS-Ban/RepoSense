import json
import google.generativeai as genai
from backend.core.config import settings

def get_review_from_llm(pr_title: str, pr_author: str, repo_full_name: str, pr_diff: str):
    system_prompt = """
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
"""

    user_message = f"""
Here is the Pull Request diff to review:

PR Title: {pr_title}
Author: {pr_author}
Repository: {repo_full_name}

Diff:
{pr_diff}
"""

    if settings.GEMINI_API_KEY:
        genai.configure(api_key=settings.GEMINI_API_KEY)
        model = genai.GenerativeModel('gemini-1.5-flash', system_instruction=system_prompt)
        response = model.generate_content(user_message)
        content = response.text
    else:
        raise Exception("No LLM API key configured")

    # Clean up markdown JSON block if exists
    content = content.strip()
    if content.startswith("```json"):
        content = content[7:]
    if content.startswith("```"):
        content = content[3:]
    if content.endswith("```"):
        content = content[:-3]
    content = content.strip()

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        return []
