import json
import sqlite3
import os
from datetime import datetime
from auth_manager import AuthManager

# Module-level AuthManager instance.
# main.py calls auth_manager.start() on startup and auth_manager.stop() on shutdown.
BROWSER_HEADLESS = os.getenv("BROWSER_HEADLESS", "true").lower() == "true"
auth_manager = AuthManager(headless=BROWSER_HEADLESS)


def init_db():
    """Initialize the SQLite database and create tables if they don't exist."""
    conn = sqlite3.connect('jobs.db')
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            title TEXT,
            budget TEXT,
            experience_level TEXT,
            skills TEXT,
            description_preview TEXT,
            full_description TEXT,
            url TEXT,
            publish_time TEXT,
            first_seen TEXT,
            last_seen TEXT,
            posted INTEGER DEFAULT 0
        )
    ''')
    conn.commit()
    conn.close()


def load_existing_job_ids():
    """Load existing job IDs from database."""
    job_ids = set()
    try:
        conn = sqlite3.connect('jobs.db')
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM jobs')
        rows = cursor.fetchall()
        for row in rows:
            job_ids.add(row[0])
        conn.close()
    except Exception:
        pass
    return job_ids


def save_job_to_db(job):
    """Save a single job to the SQLite database."""
    conn = sqlite3.connect('jobs.db')
    cursor = conn.cursor()

    now = datetime.now().isoformat()

    cursor.execute('''
        INSERT OR REPLACE INTO jobs
        (id, title, budget, experience_level, skills, description_preview, full_description, url, publish_time, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT first_seen FROM jobs WHERE id=?), ?), ?)
    ''', (
        job['id'],
        job['title'],
        job['budget'],
        job['experience_level'],
        ', '.join(job['skills']),
        job['description_preview'],
        job['full_description'],
        job['url'],
        job.get('publish_time'),
        job['id'],
        now,
        now
    ))

    conn.commit()
    conn.close()


# The GraphQL query string used to search for jobs on Upwork.
GRAPHQL_QUERY = """
  query VisitorJobSearch($requestVariables: VisitorJobSearchV1Request!) {
    search {
      universalSearchNuxt {
        visitorJobSearchV1(request: $requestVariables) {
          paging {
            total
            offset
            count
          }

    facets {
      jobType
    {
      key
      value
    }

      workload
    {
      key
      value
    }

      clientHires
    {
      key
      value
    }

      durationV3
    {
      key
      value
    }

      amount
    {
      key
      value
    }

      contractorTier
    {
      key
      value
    }

      contractToHire
    {
      key
      value
    }


    }

          results {
            id
            title
            description
            relevanceEncoded
            ontologySkills {
              uid
              parentSkillUid
              prefLabel
              prettyName: prefLabel
              freeText
              highlighted
            }


            jobTile {
              job {
                id
                ciphertext: cipherText
                jobType
                weeklyRetainerBudget
                hourlyBudgetMax
                hourlyBudgetMin
                hourlyEngagementType
                contractorTier
                sourcingTimestamp
                createTime
                publishTime

                hourlyEngagementDuration {
                  rid
                  label
                  weeks
                  mtime
                  ctime
                }
                fixedPriceAmount {
                  isoCurrencyCode
                  amount
                }
                fixedPriceEngagementDuration {
                  id
                  rid
                  label
                  weeks
                  ctime
                  mtime
                }
              }
            }
          }
        }
      }
    }
  }
"""


def fetch_jobs(keyword):
    """Fetches job listings from Upwork based on a keyword.

    Uses the AuthManager browser session to execute the GraphQL query
    from within a real Chrome browser, bypassing Cloudflare dynamically
    on whatever network the bot is connected to.
    """
    json_data = {
        'query': GRAPHQL_QUERY,
        'variables': {
            'requestVariables': {
                'userQuery': keyword,
                'sort': 'relevance+desc',
                'highlight': True,
                'paging': {
                    'offset': 0,
                    'count': 10,
                },
            },
        },
    }

    # Execute the GraphQL query via the browser (bypasses Cloudflare,
    # corporate proxies, and SSL inspection automatically)
    raw_data = auth_manager.execute_graphql(json_data)

    if raw_data is None:
        print("Failed to fetch jobs from Upwork.")
        return None

    # Check for error response from the browser fetch
    if isinstance(raw_data, dict) and 'error' in raw_data:
        print(f"GraphQL error: {raw_data['error']}")
        return None

    try:
        job_results = raw_data['data']['search']['universalSearchNuxt']['visitorJobSearchV1']['results']
    except KeyError as e:
        print(f"Error navigating JSON structure. Missing key: {e}")
        print(f"Response keys: {list(raw_data.keys()) if isinstance(raw_data, dict) else type(raw_data)}")
        if isinstance(raw_data, dict) and 'message' in raw_data:
            print(f"API message: {raw_data['message']}")
        if isinstance(raw_data, dict) and 'errors' in raw_data:
            print(f"GraphQL errors: {raw_data['errors']}")
        return None
    except TypeError as e:
        print(f"Unexpected response format: {e}")
        print(f"Raw response: {str(raw_data)[:300]}")
        return None

    existing_ids = load_existing_job_ids()
    clean_jobs = []

    for result in job_results:
        job_data = result.get('jobTile', {}).get('job', {})

        skills = [skill.get('prettyName') for skill in result.get('ontologySkills', [])]

        budget_str = "Not Specified"
        job_type = job_data.get('jobType')

        raw_title = result.get('title', '')
        clean_title = raw_title.replace("H^", "").replace("^H", "")

        raw_desc = result.get('description', '')
        clean_desc = raw_desc.replace("H^", "").replace("^H", "")

        ciphertext = job_data.get('ciphertext', '')

        if job_type == "HOURLY":
            min_rate = job_data.get('hourlyBudgetMin')
            max_rate = job_data.get('hourlyBudgetMax')
            if min_rate and max_rate:
                budget_str = f"${min_rate} - ${max_rate}/hr"
            elif min_rate:
                budget_str = f"${min_rate}/hr"
        elif job_type == "FIXED":
            fixed_amount = job_data.get('fixedPriceAmount', {}).get('amount') if job_data.get('fixedPriceAmount') else None
            if fixed_amount:
                budget_str = f"${fixed_amount} (Fixed)"

        clean_job = {
            "id": result.get('id'),
            "title": clean_title,
            "description_preview": clean_desc[:300] + "...",
            "full_description": clean_desc,
            "budget": budget_str,
            "experience_level": job_data.get('contractorTier'),
            "publish_time": job_data.get('publishTime'),
            "skills": skills,
            "url": f"https://www.upwork.com/jobs/{ciphertext}"
        }
        clean_jobs.append(clean_job)

        if clean_job['id'] not in existing_ids:
            save_job_to_db(clean_job)
            print(f"[DB] Saved new job: {clean_title}")

    return clean_jobs


init_db()
