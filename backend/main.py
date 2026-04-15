"""
GitHub Skill-Based Developer Finder — Backend
FastAPI + GitHub REST API + Google Gemini + pdfkit
"""

import json
import logging
import os
import re
import tempfile
import time
from typing import Optional

import pdfkit
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def clean_text(text: str) -> str:
    """Strip markdown formatting and collapse whitespace."""
    if not text:
        return ""
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)  # ![img](url)
    text = re.sub(r'\[.*?\]\(.*?\)', '', text)   # [label](url)
    text = re.sub(r'[#*_>`~\-]{1,}', ' ', text) # markdown symbols
    text = re.sub(r'https?://\S+', '', text)     # bare URLs
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def github_get(url: str, params: dict = None, raw: bool = False) -> Optional[requests.Response]:
    """GET with retry + rate-limit handling. Returns Response or None."""
    hdrs = dict(GH_HEADERS)
    if raw:
        hdrs["Accept"] = "application/vnd.github.v3.raw"

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.debug("GET %s (attempt %d)", url, attempt)
            res = requests.get(url, headers=hdrs, params=params, timeout=15)

            if res.status_code == 200:
                return res

            if res.status_code in (403, 429):
                reset = int(res.headers.get("X-RateLimit-Reset", 0))
                wait  = max(reset - int(time.time()), RATE_LIMIT_WAIT)
                log.warning("Rate limit hit — waiting %ds (attempt %d/%d)", wait, attempt, MAX_RETRIES)
                time.sleep(wait)
                continue

            if res.status_code == 404:
                log.debug("404 %s — skipping", url)
                return None

            log.warning("HTTP %d for %s (attempt %d/%d)", res.status_code, url, attempt, MAX_RETRIES)

        except requests.RequestException as exc:
            log.warning("Request error %s: %s (attempt %d/%d)", url, exc, attempt, MAX_RETRIES)

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)

    log.error("All %d attempts failed for %s", MAX_RETRIES, url)
    return None


def gh_json(url: str, params: dict = None) -> Optional[dict | list]:
    """Return parsed JSON or None."""
    res = github_get(url, params=params)
    if res is not None:
        try:
            return res.json()
        except Exception:
            pass
    return None


def get_readme(username: str, repo: str) -> str:
    """Fetch, clean, and truncate a repo README to README_LIMIT chars."""
    url = f"https://api.github.com/repos/{username}/{repo}/readme"
    res = github_get(url, raw=True)
    if res is not None:
        return clean_text(res.text)[:README_LIMIT]
    return "No README"


def detect_skills(repo: dict, skills: list[str]) -> list[str]:
    """Return which skills appear in the repo's combined metadata."""
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
    """Compact text block for one user sent inside the batch prompt."""
    lines = [f"username: {user['username']}"]
    for repo in user["repos"][:REPOS_PER_USER]:
        readme_snippet = repo["readme"][:README_PROMPT]
        lines.append(
            f"  repo: {repo['name']} | lang: {repo['language']} | "
            f"desc: {repo['description']} | readme: {readme_snippet}"
        )
    return "\n".join(lines)


def generate_summaries_batch(users: list[dict]) -> dict[str, str]:
    """
    Send a batch of up to GEMINI_BATCH users to Gemini in one call.
    Returns { username -> summary_string }.
    Falls back to "Summary not available." per user on any failure.
    """
    fallback = {u["username"]: "Summary not available." for u in users}

    user_blocks = "\n\n".join(build_user_block(u) for u in users)
    usernames   = [u["username"] for u in users]

    prompt = (
        "You are a technical recruiter. For each developer below, write a concise "
        "3-4 sentence professional summary covering their primary skills, technologies, "
        "and project experience.\n\n"
        "Return ONLY a valid JSON array in this exact format — no markdown, no extra text:\n"
        '[{"username": "...", "summary": "..."}, ...]\n\n'
        f"Developers:\n{user_blocks}"
    )

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            log.info("Gemini batch call — %d users (attempt %d/%d)", len(users), attempt, MAX_RETRIES)
            response = gemini_client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
            raw = response.text.strip()

            # Strip accidental markdown code fences
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)

            parsed: list[dict] = json.loads(raw)
            result = {item["username"]: item["summary"] for item in parsed}

            # Fill any missing usernames with fallback
            for u in usernames:
                if u not in result:
                    result[u] = "Summary not available."

            log.info("Gemini batch succeeded — %d summaries received", len(result))
            return result

        except json.JSONDecodeError as exc:
            log.warning("Gemini JSON parse failed (attempt %d/%d): %s", attempt, MAX_RETRIES, exc)
        except Exception as exc:
            log.warning("Gemini call failed (attempt %d/%d): %s", attempt, MAX_RETRIES, exc)

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)

    log.error("All Gemini attempts failed for this batch — using fallback summaries")
    return fallback


def generate_all_summaries(processed: list[dict]) -> None:
    """Split users into batches of GEMINI_BATCH and call Gemini per batch."""
    for i in range(0, len(processed), GEMINI_BATCH):
        batch   = processed[i: i + GEMINI_BATCH]
        log.info("Gemini batch %d–%d", i, i + len(batch) - 1)
        results = generate_summaries_batch(batch)
        for user in batch:
            user["summary"] = results.get(user["username"], "Summary not available.")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN ENDPOINT
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/generate-resume")
def generate_resume(payload: dict):

    # ── Stage 0: Preprocess ───────────────────────────────────────────────────
    raw_skills: list = payload.get("skills", [])
    N: int = int(payload.get("count", 10))

    skills = list(dict.fromkeys(s.strip().lower() for s in raw_skills if s.strip()))

    if not skills:
        raise HTTPException(status_code=400, detail="At least one skill is required.")
    if N < 1:
        raise HTTPException(status_code=400, detail="Count must be at least 1.")

    k          = len(skills)
    multiplier = k * 2          # Stage 1: multiplier = k × 2
    query      = " ".join(skills)

    log.info("=== START | skills=%s | N=%d | k=%d | multiplier=%d ===", skills, N, k, multiplier)

    # user_map: { username -> { repos: [raw_repo_dict, ...], skills: set() } }
    user_map: dict[str, dict] = {}

    # ── Stage 2: Search ───────────────────────────────────────────────────────
    log.info("--- SEARCH ---")

    remaining_users = N
    page            = 1
    full_match_set: set[str] = set()

    while remaining_users > 0:
        repos_to_fetch  = remaining_users * multiplier
        collected_repos = 0
        log.info("Outer loop | remaining=%d | repos_to_fetch=%d | page=%d",
                 remaining_users, repos_to_fetch, page)

        while collected_repos < repos_to_fetch:
            data  = gh_json(
                "https://api.github.com/search/repositories",
                params={"q": query, "per_page": 100, "page": page},
            )
            items = (data or {}).get("items", [])

            if not items:
                log.info("No more results at page %d — stopping search", page)
                break

            log.info("Page %d → %d repos fetched (collected so far: %d)",
                     page, len(items), collected_repos + len(items))

            for repo in items:
                owner = repo["owner"]["login"]
                if owner not in user_map:
                    user_map[owner] = {"repos": [], "skills": set()}
                user_map[owner]["repos"].append(repo)
                found = detect_skills(repo, skills)
                user_map[owner]["skills"].update(found)

            collected_repos += len(items)
            page += 1

            # Inner loop: stop early if we already have enough full matches
            current_full = sum(1 for d in user_map.values() if len(d["skills"]) == k)
            if current_full >= N:
                log.info("Reached N=%d full matches — stopping inner loop", N)
                break

        # Recount after this outer iteration
        prev_count     = len(full_match_set)
        full_match_set = {u for u, d in user_map.items() if len(d["skills"]) == k}
        new_found      = len(full_match_set) - prev_count

        log.info("Full-match users: %d | new this round: %d", len(full_match_set), new_found)

        remaining_users = N - len(full_match_set)

        if new_found == 0:
            log.info("No new full-match users — stopping search")
            break

    log.info("Search complete | full_match=%d / %d needed", len(full_match_set), N)

    # ── Stage 3: Final user list ──────────────────────────────────────────────
    selected = list(full_match_set)[:N]
    log.info("Selected %d users for processing", len(selected))

    if not selected:
        raise HTTPException(
            status_code=404,
            detail="No users found matching all the provided skills.",
        )

    # ── Stage 4: Data Preparation ─────────────────────────────────────────────
    log.info("--- DATA PREPARATION ---")
    processed: list[dict] = []

    for username in selected:
        log.info("Fetching profile + READMEs for: %s", username)

        profile = gh_json(f"https://api.github.com/users/{username}") or {}

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

    # ── Stage 5: Gemini Summaries (batched) ───────────────────────────────────
    log.info("--- GEMINI SUMMARIES (batch_size=%d) ---", GEMINI_BATCH)
    generate_all_summaries(processed)

    # ── Stage 6: Ranking ──────────────────────────────────────────────────────
    log.info("--- RANKING ---")
    processed.sort(key=lambda x: (
        -x["matched_skills"],
        -x["repo_count"],
        -x["stars"],
        x["username"],
    ))
    for i, u in enumerate(processed, 1):
        u["rank"] = i
        log.info("  #%d %s | skills=%d | repos=%d | stars=%d",
                 i, u["username"], u["matched_skills"], u["repo_count"], u["stars"])

    # ── Stage 7: PDF Generation ───────────────────────────────────────────────
    log.info("--- PDF GENERATION ---")
    html = _build_html(processed, skills)

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    tmp.close()

    try:
        pdfkit.from_string(html, tmp.name, options={"encoding": "UTF-8", "quiet": ""})
        log.info("PDF written to %s", tmp.name)
    except OSError as exc:
        log.error("wkhtmltopdf not found: %s", exc)
        raise HTTPException(
            status_code=500,
            detail="PDF generation failed: wkhtmltopdf is not installed or not in PATH.",
        )
    except Exception as exc:
        log.error("PDF generation error: %s", exc)
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {exc}")

    log.info("=== DONE ===")
    return FileResponse(tmp.name, filename="github_report.pdf", media_type="application/pdf")


# ─────────────────────────────────────────────────────────────────────────────
# PDF HTML BUILDER  — clean resume style, no repo details
# ─────────────────────────────────────────────────────────────────────────────

def _build_html(users: list[dict], query_skills: list[str]) -> str:
    skill_list_str = ", ".join(query_skills)

    cards = ""
    for user in users:
        skill_badges = "".join(
            f'<span class="badge">{s}</span>' for s in user["skills"]
        )
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
  .report-title {{
    font-size: 20px;
    font-weight: 700;
    color: #0d1f2d;
    margin-bottom: 4px;
  }}
  .report-sub {{
    font-size: 11px;
    color: #777;
    margin-bottom: 32px;
  }}
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
  .rank {{
    background: #0d1f2d;
    color: #fff;
    font-size: 15px;
    font-weight: 700;
    padding: 6px 12px;
    border-radius: 6px;
    white-space: nowrap;
  }}
  .fullname {{
    font-size: 17px;
    font-weight: 700;
    color: #0d1f2d;
  }}
  .username {{ font-size: 12px; color: #555; margin-top: 2px; }}
  .gh-link  {{ color: #1a6faf; text-decoration: none; }}
  .meta {{
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 12px;
    font-size: 12px;
  }}
  .meta td {{ padding: 3px 12px 3px 0; vertical-align: top; }}
  .meta .label {{ font-weight: 700; color: #555; width: 80px; }}
  .section {{
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: #999;
    margin: 12px 0 5px;
  }}
  .bio     {{ color: #333; line-height: 1.5; }}
  .badges  {{ display: flex; flex-wrap: wrap; gap: 4px; }}
  .badge   {{
    background: #1a6faf;
    color: #fff;
    font-size: 11px;
    padding: 2px 9px;
    border-radius: 4px;
  }}
  .summary {{ color: #333; line-height: 1.6; }}
</style>
</head>
<body>
<div class="report-title">GitHub Developer Report</div>
<div class="report-sub">Skills: {skill_list_str} &nbsp;·&nbsp; {len(users)} developer(s)</div>
{cards}
</body>
</html>"""
