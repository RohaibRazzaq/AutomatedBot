import json
import sqlite3
import os
import time
import hashlib
from datetime import datetime
from auth_manager import AuthManager

# Module-level AuthManager instance.
# main.py calls auth_manager.start() on startup and auth_manager.stop() on shutdown.
BROWSER_HEADLESS = os.getenv("BROWSER_HEADLESS", "true").lower() == "true"
auth_manager = AuthManager(headless=BROWSER_HEADLESS)

# Cache for job IDs that have been checked and found to be private (temporary, for current session)
_checked_private_job_ids = set()

# JSON file to store all skipped job IDs (persists across restarts)
SKIPPED_JOBS_FILE = 'skipped_jobs.json'


def load_skipped_job_ids():
    """Load skipped job IDs from JSON file."""
    try:
        if os.path.exists(SKIPPED_JOBS_FILE):
            with open(SKIPPED_JOBS_FILE, 'r') as f:
                data = json.load(f)
                return set(data.get('skipped_ids', []))
    except Exception as e:
        print(f"[SkippedJobs] Error loading {SKIPPED_JOBS_FILE}: {e}")
    return set()


def save_skipped_job_id(job_id):
    """Save a skipped job ID to JSON file."""
    try:
        # Load existing
        skipped_ids = load_skipped_job_ids()
        skipped_ids.add(job_id)
        
        # Save back
        with open(SKIPPED_JOBS_FILE, 'w') as f:
            json.dump({'skipped_ids': list(skipped_ids)}, f, indent=2)
    except Exception as e:
        print(f"[SkippedJobs] Error saving to {SKIPPED_JOBS_FILE}: {e}")


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
            posted INTEGER DEFAULT 0,
            content_hash TEXT
        )
    ''')
    # Add content_hash column if it doesn't exist (for existing databases)
    try:
        cursor.execute("ALTER TABLE jobs ADD COLUMN content_hash TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists
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


def calculate_content_hash(job):
    """Calculate a hash of job content to detect updates."""
    skills_str = ', '.join(job['skills'])
    content = f"{job['title']}|{job['budget']}|{job['full_description']}|{skills_str}"
    return hashlib.md5(content.encode()).hexdigest()


def save_job_to_db(job):
    """Save a single job to the SQLite database."""
    for attempt in range(3):
        try:
            conn = sqlite3.connect('jobs.db', timeout=10)
            cursor = conn.cursor()

            now = datetime.now().isoformat()
            content_hash = calculate_content_hash(job)

            cursor.execute('''
                INSERT OR REPLACE INTO jobs
                (id, title, budget, experience_level, skills, description_preview, full_description, url, publish_time, first_seen, last_seen, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT first_seen FROM jobs WHERE id=?), ?), ?, ?)
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
                now,
                content_hash
            ))
            # print("job saved in database")
            conn.commit()
            conn.close()
            return
        except sqlite3.OperationalError:
            if attempt < 2:
                time.sleep(0.5)
            else:
                print(f"[DB] Failed to save job {job['id']} after 3 retries.")


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


# ---------------------------------------------------------------------------
# Public job filter
# ---------------------------------------------------------------------------


def _is_public_job(result, job_data):
    """Check if a job listing is publicly accessible.

    Since we use the visitor (guest) API, private/invite-only jobs shouldn't
    appear. This is a safety net to catch any that slip through.

    Checks:
      - Has a ciphertext (needed for public URL access)
      - Has a valid job ID
      - Title doesn't indicate invite-only or private hiring
    """
    # Must have ciphertext for public URL (and it should be valid length)
    ciphertext = job_data.get('ciphertext')
    if not ciphertext or len(str(ciphertext).strip()) < 5:
        print(f"[Filter] Skipped job (invalid ciphertext): {result.get('title', '?')}")
        return False

    # Must have a valid job ID
    job_id = result.get('id')
    if not job_id:
        return False

    # Check for common private/invite-only indicators in title
    title = result.get('title', '').lower()
    private_indicators = [
        'private', 'invite only', 'invite-only', 'confidential',
        'by invitation', 'hidden job'
    ]
    for indicator in private_indicators:
        if indicator in title:
            print(f"[Filter] Skipped private job (title): {result.get('title', '?')}")
            return False

    return True


def _filter_private_jobs_via_browser(clean_jobs):
    """Check job URLs using the browser session to bypass Cloudflare.

    Uses JavaScript fetch() to get raw HTML and checks for private listing
    indicators. This is faster than navigating the browser to each page.
    """
    if not clean_jobs:
        return clean_jobs

    js_check_url = """
    var callback = arguments[arguments.length - 1];
    var url = arguments[0];
    var charLimit = arguments[1] || 5000;  // Default 5000, can be overridden
    var timeoutMs = arguments[2] || 8000;  // Default 8s, can be overridden

    var controller = new AbortController();
    var timeoutId = setTimeout(function() { controller.abort(); }, timeoutMs);

    fetch(url, {
        credentials: 'include',
        headers: { 'accept': 'text/html,*/*' },
        signal: controller.signal
    }).then(function(r) {
        clearTimeout(timeoutId);
        if (!r.ok) {
            // Strict mode: HTTP errors (403, 404) mean job is not accessible
            callback(JSON.stringify({public: false, reason: 'http_error', status: r.status}));
            return '';
        }
        var reader = r.body.getReader();
        var decoder = new TextDecoder();
        var collected = '';

        function readChunk() {
            return reader.read().then(function(result) {
                if (result.done || collected.length > charLimit) {
                    return collected;
                }
                collected += decoder.decode(result.value, {stream: true});
                return readChunk();
            });
        }
        return readChunk();
    }).then(function(text) {
        if (!text) return;
        
        // Remove script and style tags to get to actual content faster
        var cleaned = text.replace(/<script[^>]*>[\s\S]*?<\/script>/gi, '')
                         .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, '');
        
        var lower = cleaned.toLowerCase();
        
        var isPrivateListing = (
            lower.indexOf('private listing') !== -1 ||
            lower.indexOf('this job is private') !== -1 ||
            lower.indexOf('this job is a private listing') !== -1 ||
            lower.indexOf('unavailable-reason') !== -1
        );
        
        var requiresLogin = (
            lower.indexOf('sign in to apply') !== -1 ||
            lower.indexOf('log in to apply') !== -1 ||
            lower.indexOf('sign in to view') !== -1 ||
            lower.indexOf('log in to view') !== -1
        );
        
        var isPublic = !isPrivateListing && !requiresLogin;
        
        callback(JSON.stringify({
            public: isPublic,
            requiresLogin: requiresLogin,
            isPrivateListing: isPrivateListing
        }));
    }).catch(function(e) {
        clearTimeout(timeoutId);
        // Strict mode: on error, assume private (don't let private jobs slip through)
        callback(JSON.stringify({public: false, reason: 'fetch_error', error: e.name === 'AbortError' ? 'timeout' : e.message}));
    });
    """

    public_jobs = []
    skipped = 0

    # Pre-check: verify browser session is still valid
    try:
        auth_manager.sb.driver.set_script_timeout(5)
        auth_manager.sb.driver.execute_script("return 1;")
    except Exception as e:
        error_short = str(e).split('\n')[0][:80]
        print(f"[Filter] Browser unavailable, skipping URL checks: {error_short}")
        return []

    for job in clean_jobs:
        job_id = job.get('id', '')
        url = job.get('url', '')
        
        # Skip if already checked and found private
        if job_id in _checked_private_job_ids:
            skipped += 1
            print(f"[Filter] Skipped job (cached private): {job['title']}")
            continue
        
        if not url:
            # Strict mode: skip jobs without URLs (can't verify if public)
            skipped += 1
            print(f"[Filter] Skipped job (no URL): {job['title']}")
            continue

        try:
            auth_manager.sb.driver.set_script_timeout(12)
            # First try: 5000 chars, 8s timeout (fast)
            result = auth_manager.sb.driver.execute_async_script(js_check_url, url, 5000, 8000)
            data = json.loads(result)

            # Retry once if timeout (public jobs might be slow)
            if data.get('reason') == 'fetch_error' and data.get('error') == 'timeout':
                print(f"[Filter] Timeout checking {job['title']}, retrying with 10000 chars & 15s timeout...")
                time.sleep(1)  # Brief pause before retry
                # Second try: 10000 chars, 15s timeout (more thorough)
                result = auth_manager.sb.driver.execute_async_script(js_check_url, url, 10000, 15000)
                data = json.loads(result)

            if not data.get('public'):
                skipped += 1
                reason = data.get('reason', 'not_public')
                
                # Save ALL skipped jobs to JSON file (including 403/404) for quick lookup
                if job_id:
                    save_skipped_job_id(job_id)
                    _checked_private_job_ids.add(job_id)
                
                if reason == 'http_error':
                    print(f"[Filter] Skipped job (HTTP {data.get('status')}): {job['title']}")
                elif reason == 'fetch_error':
                    print(f"[Filter] Skipped job (fetch error: {data.get('error')}): {job['title']}")
                else:
                    reasons = []
                    if data.get('isPrivateListing'):
                        reasons.append('private listing')
                    if data.get('requiresLogin'):
                        reasons.append('requires login')
                    print(f"[Filter] Skipped job ({', '.join(reasons)}): {job['title']}")
                continue

            public_jobs.append(job)
            
        except Exception as e:
            skipped += 1
            error_short = str(e).split('\n')[0][:100]
            print(f"[Filter] Skipped job (check failed): {job['title']} — {error_short}")

    if skipped > 0:
        print(f"[Filter] Browser check skipped {skipped} private job(s).")

    return public_jobs


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
                'sort': 'RECENCY',
                'highlight': True,
                'paging': {
                    'offset': 0,
                    'count': 15,  # Increased from 7 to get more jobs
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
    skipped_ids = load_skipped_job_ids()  # Load from JSON file
    candidate_jobs = []
    skipped_private = 0

    for result in job_results:
        job_data = result.get('jobTile', {}).get('job', {})
        job_id = result.get('id')
        
        # Skip if already in skipped jobs JSON (checked before)
        if job_id in skipped_ids:
            skipped_private += 1
            continue

        # Phase 1: Quick heuristic filter (ciphertext, title checks)
        if not _is_public_job(result, job_data):
            skipped_private += 1
            continue

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
        candidate_jobs.append(clean_job)

    if skipped_private > 0:
        print(f"[Filter] Phase 1 skipped {skipped_private} job(s) via heuristic check.")

    # Phase 2: Browser-based private job detection
    # Uses the authenticated browser to fetch each job page and check for
    # "private listing" text. This bypasses Cloudflare since the browser
    # already has valid cookies.
    clean_jobs = _filter_private_jobs_via_browser(candidate_jobs)

    # Save passing jobs to DB
    for clean_job in clean_jobs:
        if clean_job['id'] not in existing_ids:
            save_job_to_db(clean_job)
            print(f"[DB] Saved new job: {clean_job['title']}")

    return clean_jobs


init_db()
