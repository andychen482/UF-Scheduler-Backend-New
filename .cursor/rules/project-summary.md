Project summary: UF course + RateMyProfessors scraper backend

Purpose
- Scrapes UF course catalog data for specific terms, enriches instructors with RateMyProfessors stats, and serves search + prerequisite graphs via a Flask API.
- Scheduled scraping runs via GitHub Actions.

Repository structure
- `server.py`: Flask API. Builds per-term SQLite FTS5 databases on startup from `courses/*_final.json`, then serves search and prerequisite graph endpoints.
- `pythonScripts/UFCourseGrabber.py`: Multi-threaded scraper for UF course catalog API. Produces term JSON files, de-dupes, normalizes times, and emits `_clean` files.
- `pythonScripts/scrapeRMP.py`: Scrapes RateMyProfessors GraphQL for each instructor found in course data, writes `RateMyProfessorData.json`, merges into `_clean` course files, and outputs `_final` files.
- `pythonScripts/RateMyProfessorData.json`: Cached RMP data (avg rating, avg difficulty, legacy ID).
- `courses/`: Large JSON datasets for each term. Files ending with `_final.json` are enriched with RMP data and used by `server.py`.
- `TrieModule.py`: Standalone Trie implementation (not used by current server code).
- `.github/workflows/scraper.yml`: GitHub Actions workflow to run both scrapers and push updated data.
- `requirements.txt`: Python dependencies.

Data flow (scrape + enrich)
1) `pythonScripts/UFCourseGrabber.py <term> <year> ...`
   - Calls UF schedule API across 16 threads.
   - Merges thread files into a term file `UF_<date>_<year>_<term>.json`.
   - De-dupes and normalizes into `*_clean.json` and deletes the raw file.
2) `pythonScripts/scrapeRMP.py`
   - Builds professor list from all `courses/*.json`.
   - Fetches RMP data via GraphQL.
   - Writes `pythonScripts/RateMyProfessorData.json`.
   - Merges professor stats into each `_clean` file and outputs `_final`.
   - Deletes the `_clean` files after merge.

API behavior (`server.py`)
- On startup:
  - Loads all `courses/*_final.json`.
  - For each (year, term), creates `courses_{year}_{term}.db` with FTS5.
  - Precomputes per-department prerequisite graphs using `networkx`.
- Endpoints:
  - `POST /api/get_courses`:
    - Body: `searchTerm`, `itemsPerPage`, `startFrom`, `year`, `term`.
    - FTS5 prefix search across code/name/description/prereqs/instructors.
  - `POST /generate_a_list`:
    - Body: `selectedMajorServ`, `selectedCoursesServ`, `year`, `term`.
    - Returns a graph (nodes + edges) of prerequisites for a major.

Scheduling
- GitHub Actions `scraper.yml` runs on cron `"0 */4 * * *"` (every 4 hours) and on manual dispatch.
- Workflow order:
  - Run `UFCourseGrabber.py` for summer/fall 2025 and spring 2026.
  - Run `scrapeRMP.py`.
  - Commit and push updated data with message "Hourly update from web scrapers".

Key conventions & filenames
- Course data files: `courses/UF_<date>_<year>_<term>_final.json`.
  - `year` is two digits (e.g., `25`), `term` in `{spring, summer, fall}`.
  - `server.py` parses year/term from filenames to select the proper DB.
- Generated SQLite: `courses_{year}_{term}.db` (created on API startup).

Operational notes
- The course JSON files are very large; avoid loading them fully unless needed.
- `scrapeRMP.py` uses concurrent requests and a short sleep between successful matches to reduce API load.
- The CORS allowlist includes `ufscheduler.com` and `localhost:3000`.

Quick start (local)
- Install deps: `pip install -r requirements.txt`
- Run API: `python server.py`
- Run scrapers (example): `python pythonScripts/UFCourseGrabber.py summer 25 fall 25 spring 26` then `python pythonScripts/scrapeRMP.py`

Known gaps
- No tests in this repo.
- `TrieModule.py` appears unused by current API.
