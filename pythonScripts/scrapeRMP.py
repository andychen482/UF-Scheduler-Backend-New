import requests
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
import glob
import os
import sqlite3
import datetime

url = "https://www.ratemyprofessors.com/graphql"
db_path = "pythonScripts/RateMyProfessorData.sqlite"

stale_days = 7
max_workers = 6
max_retries = 3
backoff_base_seconds = 2

# Collect all .json files in "courses"
course_files = [f for f in glob.glob("courses/*.json")]
clean_course_files = [f for f in course_files if f.endswith("_clean.json")]

professor = set()

# Build professor set from all relevant files
for cf in course_files:
    with open(cf, "r") as file:
        data = json.load(file)
        for course in data:
            for section in course["sections"]:
                for instructor in section["instructors"]:
                    if instructor["name"]:
                        professor.add(instructor["name"])

# Dictionary to store professor data
professor_data = {}
lock = threading.Lock()


headers = {
    'Accept': 'application/json',
    'Accept-Encoding': 'gzip, deflate, br, zstd',
    'Accept-Language': 'en-US,en;q=0.9,es;q=0.8',
    'Authorization': 'Basic dGVzdDp0ZXN0',
    'Connection': 'keep-alive',
    'Content-Type': 'application/json',
    'Cookie': 'ccpa-notice-viewed-02=true; cid=AuVRpwXwX2-20231004',
    'DNT': '1',
    'Host': 'www.ratemyprofessors.com',
    'Origin': 'https://www.ratemyprofessors.com',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'none',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36'
}

conn = sqlite3.connect(db_path, check_same_thread=False)
conn.execute(
    """
    CREATE TABLE IF NOT EXISTS professor_cache (
        professor_name TEXT PRIMARY KEY,
        avg_rating REAL,
        avg_difficulty REAL,
        professor_id TEXT,
        last_scraped_at TEXT,
        last_status INTEGER
    )
    """
)
conn.commit()


def utc_now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def parse_timestamp(value):
    if not value:
        return None
    try:
        cleaned = value[:-1] if value.endswith("Z") else value
        return datetime.datetime.fromisoformat(cleaned)
    except ValueError:
        return None


def is_stale(last_scraped_at, now):
    timestamp = parse_timestamp(last_scraped_at)
    if timestamp is None:
        return True
    return now - timestamp >= datetime.timedelta(days=stale_days)


def upsert_cache(
    professor_name,
    avg_rating=None,
    avg_difficulty=None,
    professor_id=None,
    last_scraped_at=None,
    last_status=None
):
    with lock:
        conn.execute(
            """
            INSERT INTO professor_cache (
                professor_name,
                avg_rating,
                avg_difficulty,
                professor_id,
                last_scraped_at,
                last_status
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(professor_name) DO UPDATE SET
                avg_rating = COALESCE(excluded.avg_rating, professor_cache.avg_rating),
                avg_difficulty = COALESCE(excluded.avg_difficulty, professor_cache.avg_difficulty),
                professor_id = COALESCE(excluded.professor_id, professor_cache.professor_id),
                last_scraped_at = excluded.last_scraped_at,
                last_status = excluded.last_status
            """,
            (
                professor_name,
                avg_rating,
                avg_difficulty,
                professor_id,
                last_scraped_at,
                last_status
            ),
        )
        conn.commit()


cached_rows = {}
for row in conn.execute(
    """
    SELECT professor_name, avg_rating, avg_difficulty, professor_id, last_scraped_at, last_status
    FROM professor_cache
    """
):
    cached_rows[row[0]] = {
        "avg_rating": row[1],
        "avg_difficulty": row[2],
        "professor_id": row[3],
        "last_scraped_at": row[4],
        "last_status": row[5],
    }

now = datetime.datetime.utcnow()
stale_professors = [
    name
    for name in professor
    if is_stale(cached_rows.get(name, {}).get("last_scraped_at"), now)
]

for name, cached in cached_rows.items():
    if cached.get("avg_rating") is not None or cached.get("avg_difficulty") is not None or cached.get("professor_id") is not None:
        professor_data[name] = {
            "avgRating": cached.get("avg_rating"),
            "avgDifficulty": cached.get("avg_difficulty"),
            "professorID": cached.get("professor_id"),
        }

def merge_course_and_professor_data(course_file, professor_data_file):
    with open(course_file, 'r') as f:
        courses = json.load(f)

    with open(professor_data_file, 'r') as f:
        professors = json.load(f)

    for course in courses:
        for section in course['sections']:
            for instructor in section['instructors']:
                instructor_name = instructor["name"]
                if instructor_name in professors:
                    instructor.update(professors[instructor_name])

    # Save merged data to a new file
    merged_file_name = course_file.replace('_clean', '_final')
    with open(merged_file_name, 'w') as f:
        json.dump(courses, f, indent=4)
    
    os.remove(course_file)


def fetch_professor_data(prof):
  print(f"Fetching data for professor {prof}...")
  query = """
  query NewSearchTeachersQuery($query: TeacherSearchQuery!) {
      newSearch {
          teachers(query: $query) {
              didFallback
              edges {
                  cursor
                  node {
                      id
                      legacyId
                      firstName
                      lastName
                      avgRatingRounded
                      numRatings
                      wouldTakeAgainPercentRounded
                      wouldTakeAgainCount
                      teacherRatingTags {
                          id
                          legacyId
                          tagCount
                          tagName
                      }
                      mostUsefulRating {
                          id
                          class
                          isForOnlineClass
                          legacyId
                          comment
                          helpfulRatingRounded
                          ratingTags
                          grade
                          date
                          iWouldTakeAgain
                          qualityRating
                          difficultyRatingRounded
                          teacherNote{
                              id
                              comment
                              createdAt
                              class
                          }
                          thumbsDownTotal
                          thumbsUpTotal
                      }
                      avgDifficultyRounded
                      school {
                          name
                          id
                      }
                      department
                  }
              }
          }
      }
  }
  """
  variables = {"query": {"text": prof, "schoolID": "U2Nob29sLTExMDA="}}
  payload = {
      "query": query,
      "variables": variables
  }

  local_data = {}
  last_status = None

  for attempt in range(max_retries + 1):
      response = requests.post(url, json=payload, headers=headers)
      last_status = response.status_code

      if response.status_code == 429:
          if attempt < max_retries:
              backoff = backoff_base_seconds * (2 ** attempt)
              time.sleep(backoff)
              continue
          print(f"Failed to fetch data for professor {prof}, status code: 429")
          upsert_cache(
              prof,
              last_scraped_at=utc_now_iso(),
              last_status=429
          )
          return None

      if response.status_code != 200:
          print(f"Failed to fetch data for professor {prof}, status code: {response.status_code}")
          upsert_cache(
              prof,
              last_scraped_at=utc_now_iso(),
              last_status=response.status_code
          )
          return None

      try:
          response_data = response.json()

          if response_data and "data" in response_data:
              teacher_edges = response_data.get("data", {}).get(
                  "newSearch", {}).get("teachers", {}).get("edges", [])
              for edge in teacher_edges:
                  node = edge["node"]
                  if node["numRatings"] > 0 and (node["firstName"] + " " + node["lastName"]).lower() == prof.lower():
                      local_data[prof] = {
                          "avgRating": node.get("avgRatingRounded"),
                          "avgDifficulty": node.get("avgDifficultyRounded"),
                          "professorID": node.get("legacyId")
                      }
                      break  # Break once we found a valid teacher node
          break
      except json.JSONDecodeError:
          print(f"Failed to decode JSON for professor {prof}.")
          last_status = 500
          break
      except Exception as e:
          print(f"Error processing data for professor {prof}: {str(e)}")
          last_status = 500
          break

  now_iso = utc_now_iso()
  if prof in local_data:
      upsert_cache(
          prof,
          avg_rating=local_data[prof].get("avgRating"),
          avg_difficulty=local_data[prof].get("avgDifficulty"),
          professor_id=local_data[prof].get("professorID"),
          last_scraped_at=now_iso,
          last_status=200
      )
  else:
      upsert_cache(
          prof,
          last_scraped_at=now_iso,
          last_status=last_status
      )

  with lock:
      professor_data.update(local_data)

  # If we successfully found data for this professor, enforce a short spacing
  # between successful requests to avoid hammering the API.
  if prof in local_data:
      time.sleep(0.5)

with ThreadPoolExecutor(max_workers=max_workers) as executor:
    futures = [executor.submit(fetch_professor_data, prof) for prof in stale_professors]
    for future in as_completed(futures):
        # Just to handle any exceptions that might have been raised inside our function
        future.result()

# Saving professor data to RateMyProfessorData.json
with open("pythonScripts/RateMyProfessorData.json", "w") as outfile:
    json.dump(professor_data, outfile, indent=4)
    print("Professor data saved to RateMyProfessorData.json")

# Now merge professor data into each file
for cf in clean_course_files:
    merge_course_and_professor_data(cf, "pythonScripts/RateMyProfessorData.json")

conn.close()