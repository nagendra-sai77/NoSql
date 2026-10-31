import os
import atexit
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from pymongo import MongoClient
from neo4j import GraphDatabase
from neo4j.exceptions import AuthError, ServiceUnavailable

# Load configuration (dotenv is loaded inside config.py)
from config import MONGODB_URI, NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD

app = Flask(__name__)
app.secret_key = os.getenv('FLASK_SECRET_KEY', 'supersecret')

# ------------------------------------------------------------------
# MongoDB setup
# ------------------------------------------------------------------
mongo_client = MongoClient(MONGODB_URI)
mongo_db = mongo_client.get_default_database()
students_col = mongo_db['students']
companies_col = mongo_db['companies']
applications_col = mongo_db['applications']

# ------------------------------------------------------------------
# Neo4j driver — created lazily so auth errors don't crash startup
# ------------------------------------------------------------------
neo_driver = None

def get_neo4j_driver():
    """Return the cached driver, (re-)creating it if necessary."""
    global neo_driver
    if neo_driver is None:
        neo_driver = GraphDatabase.driver(
            NEO4J_URI,
            auth=(NEO4J_USERNAME, NEO4J_PASSWORD)
        )
    return neo_driver


def test_neo4j_connection():
    """
    Run a quick RETURN 1 query to verify the connection.
    Returns (True, None) on success, (False, error_message) on failure.
    """
    try:
        driver = GraphDatabase.driver(
            NEO4J_URI,
            auth=(NEO4J_USERNAME, NEO4J_PASSWORD)
        )
        with driver.session() as session:
            session.run("RETURN 1")
        driver.close()
        print("[Neo4j] ✅  Connection OK  →  URI:", NEO4J_URI, "  User:", NEO4J_USERNAME)
        return True, None
    except AuthError as e:
        msg = (
            f"Authentication failed for user '{NEO4J_USERNAME}'. "
            "Check NEO4J_PASSWORD in your .env file. "
            f"Detail: {e}"
        )
        print("[Neo4j] ❌  Auth Error —", msg)
        return False, msg
    except ServiceUnavailable as e:
        msg = (
            f"Neo4j is not reachable at '{NEO4J_URI}'. "
            "Make sure Neo4j is running. "
            f"Detail: {e}"
        )
        print("[Neo4j] ❌  Service Unavailable —", msg)
        return False, msg
    except Exception as e:
        msg = f"Unexpected error: {e}"
        print("[Neo4j] ❌  Error —", msg)
        return False, msg


# ------------------------------------------------------------------
# Helper functions for Neo4j transactions
# ------------------------------------------------------------------
def upsert_skill(tx, skill_name: str):
    tx.run("""
    MERGE (sk:Skill {name: $name})
    RETURN sk
    """, name=skill_name)


def create_student_node(tx, student_id: str, name: str, skills: list):
    tx.run("""
    MERGE (s:Student {mongo_id: $sid, name: $name})
    RETURN s
    """, sid=student_id, name=name)
    for skill in skills:
        upsert_skill(tx, skill)
        tx.run("""
        MATCH (s:Student {mongo_id: $sid}), (sk:Skill {name: $skill})
        MERGE (s)-[:HAS_SKILL]->(sk)
        """, sid=student_id, skill=skill)


def create_company_node(tx, company_id: str, name: str, required_skills: list):
    tx.run("""
    MERGE (c:Company {mongo_id: $cid, name: $name})
    RETURN c
    """, cid=company_id, name=name)
    for skill in required_skills:
        upsert_skill(tx, skill)
        tx.run("""
        MATCH (c:Company {mongo_id: $cid}), (sk:Skill {name: $skill})
        MERGE (c)-[:REQUIRES_SKILL]->(sk)
        """, cid=company_id, skill=skill)


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------
@app.route('/')
def home():
    # Live counts from MongoDB
    student_count  = students_col.count_documents({})
    company_count  = companies_col.count_documents({})
    app_count      = applications_col.count_documents({})

    # Count unique skills across all students
    pipeline = [{'$unwind': '$skills'}, {'$group': {'_id': '$skills'}}]
    skill_count = len(list(students_col.aggregate(pipeline)))

    # Count skill matches from Neo4j (graceful fallback to 0)
    match_count = 0
    try:
        driver = get_neo4j_driver()
        with driver.session() as session:
            cypher = (
                "MATCH (s:Student)-[:HAS_SKILL]->(sk:Skill)<-[:REQUIRES_SKILL]-(c:Company) "
                "WHERE s.cgpa >= c.min_cgpa "
                "RETURN count(*) AS total"
            )
            result = session.run(cypher)
            record = result.single()
            if record:
                match_count = record['total']
    except Exception:
        pass

    recent_students  = list(students_col.find({}, {'_id':0}).sort('_id', -1).limit(5))
    recent_companies = list(companies_col.find({}, {'_id':0}).sort('_id', -1).limit(5))

    return render_template(
        'home.html',
        stats={
            'students': student_count,
            'companies': company_count,
            'matches': match_count,
            'skills': skill_count,
        },
        recent_students=recent_students,
        recent_companies=recent_companies,
    )


@app.route('/add_student', methods=['GET', 'POST'])
def add_student():
    if request.method == 'POST':
        name = request.form['name'].strip()
        roll_no = request.form['roll_no'].strip()
        cgpa = float(request.form['cgpa'])
        dept = request.form['dept'].strip()
        skills_raw = request.form['skills'].strip()
        skills = [s.strip() for s in skills_raw.split(',') if s.strip()]

        # Insert into MongoDB
        student_doc = {
            'name': name, 'roll_no': roll_no,
            'cgpa': cgpa, 'dept': dept, 'skills': skills
        }
        result = students_col.insert_one(student_doc)
        student_id = str(result.inserted_id)

        # Insert into Neo4j
        try:
            driver = get_neo4j_driver()
            with driver.session() as session:
                session.write_transaction(create_student_node, student_id, name, skills)
        except (AuthError, ServiceUnavailable) as e:
            flash(f'Student saved to MongoDB but Neo4j sync failed: {e}. Visit /setup to fix credentials.', 'warning')
            return redirect(url_for('home'))

        flash('Student added successfully!', 'success')
        return redirect(url_for('home'))
    return render_template('add_student.html')


@app.route('/add_company', methods=['GET', 'POST'])
def add_company():
    if request.method == 'POST':
        name = request.form['name'].strip()
        role = request.form['role'].strip()
        min_cgpa = float(request.form['min_cgpa'])
        skills_raw = request.form['required_skills'].strip()
        required_skills = [s.strip() for s in skills_raw.split(',') if s.strip()]

        # Insert into MongoDB
        company_doc = {
            'name': name, 'role': role,
            'min_cgpa': min_cgpa, 'required_skills': required_skills
        }
        result = companies_col.insert_one(company_doc)
        company_id = str(result.inserted_id)

        # Insert into Neo4j
        try:
            driver = get_neo4j_driver()
            with driver.session() as session:
                session.write_transaction(create_company_node, company_id, name, required_skills)
        except (AuthError, ServiceUnavailable) as e:
            flash(f'Company saved to MongoDB but Neo4j sync failed: {e}. Visit /setup to fix credentials.', 'warning')
            return redirect(url_for('home'))

        flash('Company added successfully!', 'success')
        return redirect(url_for('home'))
    return render_template('add_company.html')


@app.route('/students')
def students_list():
    students = list(students_col.find({}, {'_id': 0}))
    return render_template('students.html', students=students)


@app.route('/companies')
def companies_list():
    companies = list(companies_col.find({}, {'_id': 0}))
    return render_template('companies.html', companies=companies)


@app.route('/applications')
def applications_list():
    applications = list(applications_col.find({}, {'_id': 0}))
    return render_template('applications.html', applications=applications)


@app.route('/match')
def match():
    cypher = (
        "MATCH (s:Student)-[:HAS_SKILL]->(sk:Skill)<-[:REQUIRES_SKILL]-(c:Company) "
        "WHERE s.cgpa >= c.min_cgpa "
        "RETURN s.name AS student, c.name AS company, c.role AS role, collect(sk.name) AS skills"
    )
    try:
        driver = get_neo4j_driver()
        with driver.session() as session:
            result = session.run(cypher)
            matches = [
                {
                    'student': r['student'],
                    'company': r['company'],
                    'role': r['role'],
                    'skills': r['skills'],
                }
                for r in result
            ]
    except (AuthError, ServiceUnavailable) as e:
        flash(f'Neo4j error: {e}. Visit /setup to fix credentials.', 'danger')
        return render_template('match.html', matches=[], neo4j_error=str(e))
    return render_template('match.html', matches=matches)


# ------------------------------------------------------------------
# /setup  — connection tester & credential updater
# ------------------------------------------------------------------
@app.route('/setup', methods=['GET', 'POST'])
def setup():
    status = None
    error_msg = None

    if request.method == 'POST':
        action = request.form.get('action', 'test')
        uri = request.form.get('neo4j_uri', '').strip()
        username = request.form.get('neo4j_username', '').strip()
        password = request.form.get('neo4j_password', '').strip()

        # Test the provided credentials
        try:
            test_driver = GraphDatabase.driver(uri, auth=(username, password))
            with test_driver.session() as session:
                session.run("RETURN 1")
            test_driver.close()
            status = 'success'

            if action == 'save':
                # Write / update .env file
                env_path = os.path.join(os.path.dirname(__file__), '.env')
                env_lines = []
                keys_written = set()

                if os.path.exists(env_path):
                    with open(env_path, 'r') as f:
                        for line in f:
                            key = line.split('=')[0].strip()
                            if key == 'NEO4J_URI':
                                env_lines.append(f'NEO4J_URI={uri}\n')
                                keys_written.add('NEO4J_URI')
                            elif key == 'NEO4J_USERNAME':
                                env_lines.append(f'NEO4J_USERNAME={username}\n')
                                keys_written.add('NEO4J_USERNAME')
                            elif key == 'NEO4J_PASSWORD':
                                env_lines.append(f'NEO4J_PASSWORD={password}\n')
                                keys_written.add('NEO4J_PASSWORD')
                            else:
                                env_lines.append(line)

                # Append any missing keys
                if 'NEO4J_URI' not in keys_written:
                    env_lines.append(f'NEO4J_URI={uri}\n')
                if 'NEO4J_USERNAME' not in keys_written:
                    env_lines.append(f'NEO4J_USERNAME={username}\n')
                if 'NEO4J_PASSWORD' not in keys_written:
                    env_lines.append(f'NEO4J_PASSWORD={password}\n')

                with open(env_path, 'w') as f:
                    f.writelines(env_lines)

                # Hot-reload env vars and reset driver
                global neo_driver, NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD
                if neo_driver:
                    neo_driver.close()
                    neo_driver = None
                NEO4J_URI = uri
                NEO4J_USERNAME = username
                NEO4J_PASSWORD = password

                flash('✅ Credentials saved to .env and connection updated!', 'success')

        except AuthError as e:
            status = 'failed'
            error_msg = f'Authentication failed — wrong username or password. Detail: {e}'
        except ServiceUnavailable as e:
            status = 'failed'
            error_msg = f'Neo4j not reachable at {uri} — is the server running? Detail: {e}'
        except Exception as e:
            status = 'failed'
            error_msg = f'Unexpected error: {e}'

    # Always show current connection status on GET
    current_ok, current_err = test_neo4j_connection()
    return render_template(
        'setup.html',
        current_uri=NEO4J_URI,
        current_username=NEO4J_USERNAME,
        current_ok=current_ok,
        current_err=current_err,
        status=status,
        error_msg=error_msg,
    )


# ------------------------------------------------------------------
# Graceful shutdown
# ------------------------------------------------------------------
def close_neo4j_driver():
    if neo_driver is not None:
        neo_driver.close()

atexit.register(close_neo4j_driver)

# ------------------------------------------------------------------
# Startup connection test
# ------------------------------------------------------------------
if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("  College Placement Management System")
    print("=" * 60)
    test_neo4j_connection()
    print("=" * 60 + "\n")
    debug = os.getenv('FLASK_DEBUG', '0') == '1'
    app.run(host='0.0.0.0', port=5001, debug=debug)
