"""
GitHub Skill-Based Developer Finder — Backend
FastAPI + GitHub REST API + Google Gemini + pdfkit
"""

import base64
import json
import logging
import os
import re
import tempfile
import time
from typing import Generator, Optional

import pdfkit
import requests
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from google import genai
from google.genai import types

# ─────────────────────────────────────────────────────────────────────────────
# BOOTSTRAP
# ─────────────────────────────────────────────────────────────────────────────

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("github_finder")

app = FastAPI(title="GitHub Skill Finder")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

GITHUB_TOKEN   = os.getenv("GITHUB_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

GH_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}

MAX_RETRIES     = 3
RETRY_DELAY     = 4    # seconds between retries
RATE_LIMIT_WAIT = 60   # seconds to wait on 403/429
README_LIMIT    = 500  # chars stored per README
README_PROMPT   = 400  # chars sent to Gemini per README
REPOS_PER_USER  = 5    # max repos included in Gemini prompt per user
GEMINI_BATCH    = 25   # users per Gemini batch call

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL  = "gemini-2.5-flash"


# ─────────────────────────────────────────────────────────────────────────────
# SSE HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def sse(event: str, data: dict) -> str:
    """Format a single SSE message."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ─────────────────────────────────────────────────────────────────────────────
# GITHUB HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'\[.*?\]\(.*?\)', '', text)
    text = re.sub(r'[#*_>`~\-]{1,}', ' ', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def github_get(url: str, params: dict = None, raw: bool = False) -> Optional[requests.Response]:
    hdrs = dict(GH_HEADERS)
    if raw:
        hdrs["Accept"] = "application/vnd.github.v3.raw"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            res = requests.get(url, headers=hdrs, params=params, timeout=15)
            if res.status_code == 200:
                return res
            if res.status_code in (403, 429):
                reset = int(res.headers.get("X-RateLimit-Reset", 0))
                wait  = max(reset - int(time.time()), RATE_LIMIT_WAIT)
                log.warning("Rate limit — waiting %ds", wait)
                time.sleep(wait)
                continue
            if res.status_code == 404:
                return None
            log.warning("HTTP %d for %s (attempt %d)", res.status_code, url, attempt)
        except requests.RequestException as exc:
            log.warning("Request error %s: %s (attempt %d)", url, exc, attempt)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)

    log.error("All attempts failed for %s", url)
    return None


def gh_json(url: str, params: dict = None) -> Optional[dict | list]:
    res = github_get(url, params=params)
    if res is not None:
        try:
            return res.json()
        except Exception:
            pass
    return None


def get_readme(username: str, repo: str) -> str:
    url = f"https://api.github.com/repos/{username}/{repo}/readme"
    res = github_get(url, raw=True)
    if res is not None:
        return clean_text(res.text)[:README_LIMIT]
    return "No README"


def detect_skills(repo: dict, skills: list[str]) -> list[str]:
    combined = " ".join([
        repo.get("name") or "",
        repo.get("description") or "",
        repo.get("language") or "",
        " ".join(repo.get("topics") or []),
    ]).lower()
    return [s for s in skills if s in combined]


# ─────────────────────────────────────────────────────────────────────────────
# GEMINI — BATCHED SUMMARY
# ─────────────────────────────────────────────────────────────────────────────

def build_user_block(user: dict) -> str:
    lines = [f"username: {user['username']}"]
    for repo in user["repos"][:REPOS_PER_USER]:
        lines.append(
            f"  repo: {repo['name']} | lang: {repo['language']} | "
            f"desc: {repo['description']} | readme: {repo['readme'][:README_PROMPT]}"
        )
    return "\n".join(lines)


def generate_summaries_batch(users: list[dict]) -> dict[str, str]:
    fallback  = {u["username"]: "Summary not available." for u in users}
    usernames = [u["username"] for u in users]
    prompt = (
        "You are a technical recruiter. For each developer below, write a concise "
        "3-4 sentence professional summary covering their primary skills, technologies, "
        "and project experience.\n\n"
        "Return ONLY a valid JSON array — no markdown, no extra text:\n"
        '[{"username": "...", "summary": "..."}, ...]\n\n'
        f"Developers:\n{ chr(10).join(build_user_block(u) for u in users) }"
    )

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
            raw = response.text.strip()
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
            parsed = json.loads(raw)
            result = {item["username"]: item["summary"] for item in parsed}
            for u in usernames:
                if u not in result:
                    result[u] = "Summary not available."
            return result
        except json.JSONDecodeError as exc:
            log.warning("Gemini JSON parse failed (attempt %d): %s", attempt, exc)
        except Exception as exc:
            log.warning("Gemini call failed (attempt %d): %s", attempt, exc)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)

    return fallback


# ─────────────────────────────────────────────────────────────────────────────
# CORE PIPELINE  (generator — yields SSE strings)
# ─────────────────────────────────────────────────────────────────────────────

def run_pipeline(skills: list[str], N: int) -> Generator[str, None, None]:
    """
    Runs the full pipeline and yields SSE events at each real stage.
    Final event is either:
      event: done   data: { "pdf": "<base64>" }
      event: error  data: { "detail": "..." }
    """

    k          = len(skills)
    multiplier = k * 2
    query      = " ".join(skills)

    log.info("=== START | skills=%s | N=%d ===", skills, N)

    # ── Stage 2: Search ───────────────────────────────────────────────────────
    yield sse("status", {"message": f"Searching GitHub for developers with: {', '.join(skills)}…"})

    user_map: dict[str, dict] = {}
    remaining_users = N
    page            = 1
    full_match_set: set[str] = set()

    while remaining_users > 0:
        repos_to_fetch  = remaining_users * multiplier
        collected_repos = 0

        while collected_repos < repos_to_fetch:
            data  = gh_json(
                "https://api.github.com/search/repositories",
                params={"q": query, "per_page": 100, "page": page},
            )
            items = (data or {}).get("items", [])
            if not items:
                break

            yield sse("status", {
                "message": f"Page {page} — fetched {collected_repos + len(items)} repos so far…"
            })

            for repo in items:
                owner = repo["owner"]["login"]
                if owner not in user_map:
                    user_map[owner] = {"repos": [], "skills": set()}
                user_map[owner]["repos"].append(repo)
                found = detect_skills(repo, skills)
                user_map[owner]["skills"].update(found)

            collected_repos += len(items)
            page += 1

            current_full = sum(1 for d in user_map.values() if len(d["skills"]) == k)
            if current_full >= N:
                break

        prev_count     = len(full_match_set)
        full_match_set = {u for u, d in user_map.items() if len(d["skills"]) == k}
        new_found      = len(full_match_set) - prev_count
        remaining_users = N - len(full_match_set)

        yield sse("status", {
            "message": f"Found {len(full_match_set)} matching developer(s) so far…"
        })

        if new_found == 0:
            break

    selected = list(full_match_set)[:N]
    log.info("Selected %d users", len(selected))

    if not selected:
        yield sse("error", {"detail": "No users found matching all the provided skills."})
        return

    yield sse("status", {
        "message": f"Found {len(selected)} developer(s). Fetching profiles and READMEs…"
    })

    # ── Stage 4: Data Preparation ─────────────────────────────────────────────
    yield sse("status", {"message": "Fetching profiles and READMEs from GitHub…"})
    processed: list[dict] = []

    for idx, username in enumerate(selected, 1):
        profile   = gh_json(f"https://api.github.com/users/{username}") or {}
        repos_data = []

        for repo in user_map[username]["repos"]:
            readme = get_readme(username, repo["name"])
            repos_data.append({
                "name":        repo.get("name", ""),
                "description": repo.get("description") or "No description",
                "topics":      repo.get("topics") or [],
                "language":    repo.get("language") or "Unknown",
                "stars":       repo.get("stargazers_count", 0),
                "readme":      readme,
            })

        matched_skills = list(user_map[username]["skills"])
        processed.append({
            "username":       username,
            "name":           profile.get("name") or username,
            "bio":            profile.get("bio") or "No bio",
            "location":       profile.get("location") or "Not specified",
            "email":          profile.get("email") or "Not Public",
            "profile_url":    profile.get("html_url") or f"https://github.com/{username}",
            "followers":      profile.get("followers", 0),
            "skills":         matched_skills,
            "matched_skills": len(matched_skills),
            "repos":          repos_data,
            "repo_count":     len(repos_data),
            "stars":          sum(r["stars"] for r in repos_data),
        })

    # ── Stage 5: Gemini Summaries ─────────────────────────────────────────────
    yield sse("status", {"message": "Generating AI summaries with Gemini…"})

    for i in range(0, len(processed), GEMINI_BATCH):
        batch   = processed[i: i + GEMINI_BATCH]
        results = generate_summaries_batch(batch)
        for user in batch:
            user["summary"] = results.get(user["username"], "Summary not available.")

    # ── Stage 6: Ranking ──────────────────────────────────────────────────────
    yield sse("status", {"message": "Ranking developers…"})

    processed.sort(key=lambda x: (
        -x["matched_skills"],
        -x["repo_count"],
        -x["stars"],
        x["username"],
    ))
    for i, u in enumerate(processed, 1):
        u["rank"] = i

    # ── Stage 7: PDF ──────────────────────────────────────────────────────────
    yield sse("status", {"message": "Building PDF report…"})

    html = _build_html(processed, skills)
    tmp  = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp.close()

    try:
        pdfkit.from_string(html, tmp.name, options={"encoding": "UTF-8", "quiet": ""})
    except OSError:
        yield sse("error", {"detail": "PDF generation failed: wkhtmltopdf is not installed or not in PATH."})
        return
    except Exception as exc:
        yield sse("error", {"detail": f"PDF generation failed: {exc}"})
        return

    # Send PDF as base64 in the final SSE event
    with open(tmp.name, "rb") as f:
        pdf_b64 = base64.b64encode(f.read()).decode()

    os.unlink(tmp.name)

    yield sse("status", {"message": "Done! Downloading your report…"})
    yield sse("done", {"pdf": pdf_b64})
    log.info("=== DONE ===")


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/generate-resume")
async def generate_resume(payload: dict):
    raw_skills: list = payload.get("skills", [])
    N: int = int(payload.get("count", 10))

    skills = list(dict.fromkeys(s.strip().lower() for s in raw_skills if s.strip()))

    if not skills:
        return StreamingResponse(
            iter([sse("error", {"detail": "At least one skill is required."})]),
            media_type="text/event-stream",
        )
    if N < 1:
        return StreamingResponse(
            iter([sse("error", {"detail": "Count must be at least 1."})]),
            media_type="text/event-stream",
        )

    return StreamingResponse(
        run_pipeline(skills, N),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────────────────────────────────────
# PDF HTML BUILDER
# ─────────────────────────────────────────────────────────────────────────────

def _build_html(users: list[dict], query_skills: list[str]) -> str:
    skill_list_str = ", ".join(query_skills)

    cards = ""
    for user in users:
        skill_badges = "".join(f'<span class="badge">{s}</span>' for s in user["skills"])
        cards += f"""
        <div class="card">
          <div class="card-header">
            <div class="rank">#{user['rank']}</div>
            <div class="identity">
              <div class="fullname">{user['name']}</div>
              <div class="username">
                <a href="{user['profile_url']}" class="gh-link">@{user['username']}</a>
              </div>
            </div>
          </div>
          <table class="meta">
            <tr>
              <td class="label">Email</td>
              <td>{user['email']}</td>
              <td class="label">Location</td>
              <td>{user['location']}</td>
            </tr>
            <tr>
              <td class="label">Followers</td>
              <td colspan="3">{user['followers']}</td>
            </tr>
          </table>
          <div class="section">Bio</div>
          <div class="bio">{user['bio']}</div>
          <div class="section">Matched Skills</div>
          <div class="badges">{skill_badges}</div>
          <div class="section">About</div>
          <div class="summary">{user['summary']}</div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: Arial, sans-serif;
    font-size: 13px;
    color: #1a1a1a;
    background: #fff;
    padding: 36px 40px;
  }}
  .report-title {{ font-size: 20px; font-weight: 700; color: #0d1f2d; margin-bottom: 4px; }}
  .report-sub   {{ font-size: 11px; color: #777; margin-bottom: 32px; }}
  .card {{
    border: 1px solid #d8e0e8;
    border-radius: 8px;
    padding: 20px 24px;
    margin-bottom: 24px;
    page-break-inside: avoid;
  }}
  .card-header {{
    display: flex;
    align-items: center;
    gap: 14px;
    margin-bottom: 14px;
    border-bottom: 1px solid #eef1f4;
    padding-bottom: 12px;
  }}
  .rank     {{ background:#0d1f2d; color:#fff; font-size:15px; font-weight:700; padding:6px 12px; border-radius:6px; white-space:nowrap; }}
  .fullname {{ font-size:17px; font-weight:700; color:#0d1f2d; }}
  .username {{ font-size:12px; color:#555; margin-top:2px; }}
  .gh-link  {{ color:#1a6faf; text-decoration:none; }}
  .meta     {{ width:100%; border-collapse:collapse; margin-bottom:12px; font-size:12px; }}
  .meta td  {{ padding:3px 12px 3px 0; vertical-align:top; }}
  .meta .label {{ font-weight:700; color:#555; width:80px; }}
  .section  {{ font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:0.6px; color:#999; margin:12px 0 5px; }}
  .bio      {{ color:#333; line-height:1.5; }}
  .badges   {{ display:flex; flex-wrap:wrap; gap:4px; }}
  .badge    {{ background:#1a6faf; color:#fff; font-size:11px; padding:2px 9px; border-radius:4px; }}
  .summary  {{ color:#333; line-height:1.6; }}
</style>
</head>
<body>
<div class="report-title">GitHub Developer Report</div>
<div class="report-sub">Skills: {skill_list_str} &nbsp;·&nbsp; {len(users)} developer(s)</div>
{cards}
</body>
</html>"""
