import sqlite3

DB_NAME = "jobs.db"

def init_db():
    """Creates the database and table if they don't exist yet."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Create a simple table that just stores the unique Upwork Job ID
    c.execute('''CREATE TABLE IF NOT EXISTS posted_jobs (id TEXT PRIMARY KEY)''')
    conn.commit()
    conn.close()

def is_job_seen(job_id):
    """Checks if a job ID already exists in the database."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id FROM posted_jobs WHERE id=?", (job_id,))
    result = c.fetchone()
    conn.close()
    return result is not None

def mark_job_seen(job_id):
    """Saves a new job ID to the database permanently."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO posted_jobs (id) VALUES (?)", (job_id,))
    conn.commit()
    conn.close()