# GitHub Skill-Based Developer Finder & Resume Generator

A full-stack application that searches GitHub for developers matching a given set of skills, ranks them using a deterministic algorithm, generates AI-powered professional summaries via Google Gemini, and exports a clean resume-style PDF report.

---

## Demo

> Enter skills → click Generate Report → download a ranked PDF of matching GitHub developers.

---

## Features

- **Skill-based GitHub search** — finds developers whose repositories match ALL specified skills using keyword detection across repo name, description, language, and topics
- **Tag-based skill input** — add skills one at a time with Enter key; remove them with a click
- **Deterministic ranking** — users ranked by matched skills → repo count → total stars → username
- **AI summaries** — Google Gemini generates a professional 3–4 sentence developer profile per user, processed in batches of 25
- **Resume-style PDF** — clean, structured PDF with rank, name, GitHub link, email, location, bio, skills, and AI summary
- **Retry + rate-limit handling** — all GitHub and Gemini API calls retry up to 3 times with exponential backoff; rate limit headers are respected
- **Live status updates** — frontend shows real-time progress messages during generation

---

## Tech Stack

| Layer     | Technology                          |
|-----------|-------------------------------------|
| Frontend  | React.js                            |
| Backend   | FastAPI (Python)                    |
| GitHub    | GitHub REST API v3                  |
| AI        | Google Gemini 2.5 Flash             |
| PDF       | pdfkit + wkhtmltopdf                |
| Env       | python-dotenv                       |

---

## Project Structure

```
├── backend/
│   ├── main.py            # FastAPI app — all search, ranking, Gemini, PDF logic
│   ├── requirements.txt   # Python dependencies
│   └── .env               # API keys (not committed)
│
└── frontend/
    ├── src/
    │   ├── App.js         # React UI — skill tags, form, status, download
    │   └── App.css        # Styles
    └── package.json
```

---

## How It Works

### Stage 0 — Preprocessing
Skills are lowercased and deduplicated. A multiplier `k × 2` (where `k` = number of skills) controls how many repos to fetch per search round.

### Stage 1 — GitHub Search
Calls `GET /search/repositories?q=<skills>` with pagination. For each repo, skill keywords are matched against the combined repo name + description + language + topics. Users accumulate matched skills across all their repos.

### Stage 2 — Full Match Filter
Only users who match **all** provided skills are selected. The search loop continues until enough users are found or GitHub returns no more results.

### Stage 3 — Data Preparation
For each selected user:
- Fetches full profile (`name`, `bio`, `location`, `email`, `followers`)
- Fetches README for every repo (cleaned, limited to 500 chars)

### Stage 4 — Gemini AI Summaries (Batched)
Users are split into batches of 25. A single Gemini API call per batch returns a JSON array of professional summaries. Falls back gracefully if parsing fails.

### Stage 5 — Ranking
```
Primary:   matched_skills  DESC
Secondary: repo_count      DESC
Tertiary:  total_stars     DESC
Final:     username        ASC
```

### Stage 6 — PDF Generation
Builds an HTML document with one card per developer, then converts to PDF using `pdfkit` + `wkhtmltopdf`.

---

## Getting Started

### Prerequisites

- Python 3.11+
- Node.js 18+
- [wkhtmltopdf](https://wkhtmltopdf.org/downloads.html) installed and in PATH
- A [GitHub Personal Access Token](https://github.com/settings/tokens)
- A [Google Gemini API Key](https://aistudio.google.com/app/apikey)

---

### Backend Setup

```bash
cd backend

# Create and activate virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux

# Install dependencies
pip install -r requirements.txt

# Create .env file
```

Create `backend/.env`:

```env
GITHUB_TOKEN=your_github_personal_access_token
GEMINI_API_KEY=your_gemini_api_key
```

```bash
# Start the server
uvicorn main:app --reload
```

Backend runs at `http://localhost:8000`

---

### Frontend Setup

```bash
cd frontend
npm install
npm start
```

Frontend runs at `http://localhost:3000`

---

## Usage

1. Open `http://localhost:3000`
2. Type a skill (e.g. `react`) and press **Enter** — it appears as a tag
3. Add more skills the same way
4. Set the number of developers to find
5. Click **Generate Report**
6. Wait for the process to complete (GitHub search + README fetch + Gemini summaries)
7. PDF downloads automatically

---

## API Reference

### `POST /generate-resume`

**Request body:**
```json
{
  "skills": ["react", "node", "mongodb"],
  "count": 10
}
```

**Response:** `application/pdf` — downloadable PDF file

**Error responses:**
| Status | Reason |
|--------|--------|
| 400    | No skills provided or count < 1 |
| 404    | No users found matching all skills |
| 500    | PDF generation failed (wkhtmltopdf missing) |

---

## Configuration

All tunable constants are at the top of `backend/main.py`:

| Constant         | Default | Description                              |
|------------------|---------|------------------------------------------|
| `MAX_RETRIES`    | 3       | API call retry attempts                  |
| `RETRY_DELAY`    | 4s      | Delay between retries                    |
| `RATE_LIMIT_WAIT`| 60s     | Wait time on GitHub rate limit           |
| `README_LIMIT`   | 500     | Max chars stored per README              |
| `README_PROMPT`  | 400     | Max README chars sent to Gemini          |
| `REPOS_PER_USER` | 5       | Max repos per user in Gemini prompt      |
| `GEMINI_BATCH`   | 25      | Users per Gemini batch call              |

---

## Environment Variables

| Variable        | Description                        |
|-----------------|------------------------------------|
| `GITHUB_TOKEN`  | GitHub Personal Access Token       |
| `GEMINI_API_KEY`| Google Gemini API key              |

> **Never commit your `.env` file.** It is already listed in `.gitignore`.

---

## Dependencies

### Backend
```
fastapi
uvicorn[standard]
requests
python-dotenv
pdfkit
google-genai
```

### Frontend
```
react
react-dom
react-scripts
```

---

## Known Limitations

- GitHub Search API returns a maximum of 1000 results per query (10 pages × 100 per page). For very specific multi-skill queries, fewer results may be available.
- Gemini 503 errors (service overload) are retried automatically but may still fail under heavy load.
- `email` is only available if the GitHub user has made it public.
- PDF generation requires `wkhtmltopdf` to be installed separately — it is not a Python package.

---

## License

MIT
