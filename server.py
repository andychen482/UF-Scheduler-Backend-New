from flask import Flask, request, jsonify
from flask_cors import CORS
import json
import glob
import sqlite3
import re
import networkx as nx
import gc
import os

app = Flask(__name__)
CORS(app, origins=[
    'http://ufscheduler.com',
    'https://ufscheduler.com',
    'http://www.ufscheduler.com',
    'https://www.ufscheduler.com',
    'http://localhost:3000'
])

# -------------------------------------------------------------------
# 1. Locate and load all course JSON files corresponding to (_{year}_{term}_final.json)
#    Create a dictionary of code->course for each (year, term).
# -------------------------------------------------------------------

json_files = glob.glob('courses/*_final.json')

# Instead, add a new global dictionary to hold all pre-built graphs:
major_graph_map = {}  # {(year, term): {departmentName: nx.DiGraph}}

# Keep the parse_year_term_from_filename, get_connection, etc. as they are...

def parse_year_term_from_filename(filename):
    """
    Parses the filename to extract the year and term.
    Assumes filename ends with something like: '_{year}_{term}_final.json'
    For example: 'UF_Feb-21-2025_25_fall_final.json' -> (year='25', term='fall')
    """
    # This is a simple approach; adapt to your file naming as needed.
    # We'll split on underscores and take indices from the end.
    parts = os.path.basename(filename).split('_')
    # e.g. ['UF', 'Feb-21-2025', '25', 'fall', 'final.json']
    # year might be parts[-3], term might be parts[-2] (depending on your naming)
    year = parts[-3]
    term = parts[-2]
    # strip off any file extension from term if needed, e.g. "fall_final.json" -> "fall"
    if 'final.json' in term:
        term = term.replace('final.json', '')
    return year, term

# -------------------------------------------------------------------
# 2. Create/Open SQLite Database(s) for each (year, term)
#    and Create FTS Table with Prefixes, then insert data.
# -------------------------------------------------------------------

def get_connection(db_name):
    """
    Returns a connection to the given SQLite database.
    """
    return sqlite3.connect(db_name)

# Create a new in-memory structure to store all courses by (year, term):
year_term_course_list = {}  # {(year, term): [courses...]}

def init_db_for_file(json_path):
    """
    - Parses (year, term) from json_path
    - Creates a DB named 'courses_{year}_{term}.db'
    - Initializes the FTS table (with prefix) if needed
    - Inserts all courses specific to that file
    """
    year, term = parse_year_term_from_filename(json_path)
    db_name = f'courses_{year}_{term}.db'
    
    with open(json_path) as f:
        all_courses = json.load(f)

    conn = get_connection(db_name)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")

    cur.execute('''
        CREATE VIRTUAL TABLE IF NOT EXISTS courses_fts
        USING fts5(
            code,
            codeWithSpace,
            name,
            description,
            instructors,
            json_data UNINDEXED,
            prefix='2 3 4'
        )
    ''')

    row_count = cur.execute('SELECT count(*) FROM courses_fts;').fetchone()[0]
    if row_count == 0:
        for course in all_courses:
            code = course['code']
            codeWithSpace = course.get('codeWithSpace', '')
            name = course.get('name', '')
            description = course.get('description', '')

            instructor_names = []
            for section in course.get('sections', []):
                for inst in section.get('instructors', []):
                    instructor_names.append(inst.get('name', ''))
            instructors_joined = ' '.join(instructor_names)

            # Store the entire course JSON
            json_str = json.dumps(course)

            cur.execute('''
                INSERT INTO courses_fts (
                    code, codeWithSpace, name, description, 
                    instructors, json_data
                )
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (code, codeWithSpace, name, description, 
                  instructors_joined, json_str))

        conn.commit()

    conn.close()

    # Instead of storing course_data_map and course_dept_map,
    # keep a list of all courses for this (year, term):
    year_term_course_list[(year, term)] = all_courses

# Initialize a DB for each final JSON on startup
for jpath in json_files:
    init_db_for_file(jpath)

def clean_prereq(prerequisites):
    pattern = r'[A-Z]{3}\s\d{4}'
    return re.findall(pattern, prerequisites)

def format_course_code(course):
    return course[:3] + '\n' + course[3:]

def build_graph_for_all_majors(all_courses):
    """
    Return a dict: { major: nx.DiGraph }, where each graph includes edges
    based on prerequisites among courses in the same major (deptName).
    """
    # Build a quick dept map: code -> deptName
    code_dept_map = {}
    # Also store prerequisites for each course code:
    code_prereq_map = {}
    for course in all_courses:
        code = course['code']
        sections = course.get('sections', [])
        dept_name = sections[0].get("deptName", "") if sections else ""
        code_dept_map[code] = dept_name
        code_prereq_map[code] = course.get("prerequisites", "")

    # Group courses by department name (major):
    major_courses_map = {}
    for code, dept in code_dept_map.items():
        major_courses_map.setdefault(dept, []).append(code)

    # Now build a graph for each department
    graphs_for_majors = {}
    for dept, codes in major_courses_map.items():
        G = nx.DiGraph()
        # For each course in this dept, parse its prereqs, add edges
        for code in codes:
            prereq_list = clean_prereq(code_prereq_map[code])
            course_fmt = format_course_code(code.rstrip('ABCDEFGHIJKLMNOPQRSTUVWXYZ '))
            for prereq in prereq_list:
                pre_fmt = format_course_code(prereq.replace(" ", "").rstrip(' '))
                if course_fmt != pre_fmt:
                    G.add_edge(pre_fmt, course_fmt)
        graphs_for_majors[dept] = G

    return graphs_for_majors

# Build all major graphs for each (year, term) and store in major_graph_map
for (year, term), all_courses in year_term_course_list.items():
    major_graph_map[(year, term)] = build_graph_for_all_majors(all_courses)

# We can discard the raw course lists now that graphs are built
del year_term_course_list

# -------------------------------------------------------------------
# 3. Modify the /api/get_courses route to accept year and term,
#    then query the correct DB.
# -------------------------------------------------------------------
@app.route("/api/get_courses", methods=['POST'])
def get_courses():
    """
    Receives a JSON body with:
      - searchTerm: the query string
      - itemsPerPage: number of items per page
      - startFrom: offset for pagination
      - year:  '25'
      - term:  'fall', 'summer', 'spring', etc.
    Returns a JSON list of matched courses, from the correct DB.
    """
    data = request.json
    searchTerm = data.get('searchTerm', '').strip()
    itemsPerPage = data.get('itemsPerPage', 20)
    startFrom = data.get('startFrom', 0)
    year = data.get('year')
    term = data.get('term')

    if not year or not term:
        return jsonify({"error": "Missing 'year' or 'term' in request body"}), 400

    db_name = f'courses_{year}_{term}.db'

    if not searchTerm:
        return jsonify([])

    # For prefix matching in FTS
    terms = searchTerm.split()
    prefix_terms = [t + '*' for t in terms]
    fts_query = ' '.join(prefix_terms)

    conn = get_connection(db_name)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    query = '''
        SELECT
            json_data,
            bm25(courses_fts) AS rank,
            CASE 
                WHEN codeWithSpace = :exactSearch OR code = :exactSearch THEN 0 
                ELSE 1 
            END AS top_sort
        FROM courses_fts
        WHERE courses_fts MATCH :ftsQuery
        ORDER BY top_sort, rank
        LIMIT :limit
        OFFSET :offset
    '''

    rows = cur.execute(query, {
        'exactSearch': searchTerm,
        'ftsQuery': fts_query,
        'limit': itemsPerPage,
        'offset': startFrom
    }).fetchall()

    results = []
    for row in rows:
        # Each row corresponds to one specific entry
        course_json = row['json_data']
        course = json.loads(course_json)
        results.append(course)

    conn.close()
    return jsonify(results)

# -------------------------------------------------------------------
# 5. /generate_a_list (same as your original)
# -------------------------------------------------------------------
@app.route('/generate_a_list', methods=['POST'])
def generate_a_list():
    data = request.get_json()
    G = nx.DiGraph()

    selected_major = data['selectedMajorServ']
    taken_courses = data['selectedCoursesServ']
    year = data.get('year')
    term = data.get('term')

    # Format the taken courses:
    formatted_taken_courses = [
        format_course_code(course.rstrip('ABCDEFGHIJKLMNOPQRSTUVWXYZ '))
        for course in taken_courses
    ]

    # Retrieve the pre-built graph. If we don't have that major, use empty graph.
    major_graphs = major_graph_map.get((year, term), {})
    base_graph = major_graphs.get(selected_major, nx.DiGraph())
    # Make a copy so we don't mutate our global graph:
    G = base_graph.copy()

    # Now each taken course is guaranteed to be in the final node set
    # (existing logic to highlight or 'select' them):
    for course in formatted_taken_courses:
        G.add_node(course)

    # Build the final node/edge lists including 'selected' class:
    nodes = [
        {
            "data": {"id": node},
            "classes": "selected" if node in formatted_taken_courses else "not_selected"
        }
        for node in G.nodes()
    ]
    edges = [{"data": {"source": e[0], "target": e[1]}} for e in G.edges()]

    return jsonify({
        'nodes': nodes,
        'edges': edges
    })

# -------------------------------------------------------------------
# 6. Optional: run the app
# -------------------------------------------------------------------
if __name__ == '__main__':
    app.run(debug=True)