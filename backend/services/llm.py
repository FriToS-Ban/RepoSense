from asyncio.log import logger
import json
from google import genai
from backend.core.config import settings
import logging
logger = logging.getLogger(__name__)

def get_review_from_llm(pr_title: str, pr_author: str, repo_full_name: str, pr_diff: str, rag_context: str = ""):
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

    
    MAX_DIFF_CHARS = 48000
    if len(pr_diff) > MAX_DIFF_CHARS:
        pr_diff = pr_diff[:MAX_DIFF_CHARS]
        pr_diff += "\n\n[Diff truncated due to size — only first 48000 chars reviewed]"
    
    context_section = ""
    if rag_context:
        context_section = f"""
    RELEVANT CODEBASE CONTEXT (files related to this PR):
    {rag_context[:12000]}
    ---
    """

    user_message = f"""
    Here is the Pull Request diff to review:

    PR Title: {pr_title}
    Author: {pr_author}
    Repository: {repo_full_name}

    {context_section}
    Diff:
    {pr_diff}
    """

    if settings.NVIDIA_API_KEY:
        from openai import OpenAI
        client = OpenAI(
            base_url="https://integrate.api.nvidia.com/v1",
            api_key=settings.NVIDIA_API_KEY
        )
        response = client.chat.completions.create(
            model=settings.NVIDIA_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            max_tokens=2000,
            temperature=0.2
        )
        content = response.choices[0].message.content
    elif settings.GEMINI_API_KEY:
        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        response = client.models.generate_content(model=settings.GEMINI_MODEL, contents=user_message, config={"system_instruction": system_prompt})
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
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse LLM response as JSON: {e}\nRaw response: {content}")
        raise Exception(f"LLM returned invalid JSON: {e}")
