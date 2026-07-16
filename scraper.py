import requests
import json
import sqlite3
from datetime import datetime


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


def fetch_jobs(keyword):
    """Fetches job listings from Upwork based on a keyword."""
    
    cookies = {
        'visitor_id': '162.120.188.76.1768507087772000',
        '_vwo_uuid_v2': 'D369268CCEE4D969C8599E43E05303DB5|7c13ab87bc234a1078a093785aca4e95',
        '_vwo_uuid': 'J3CC83414A7F4364806F2DD531CD42819',
        'x-spec-id': '153e39e9-f813-4a6e-baba-42250c48b214',
        'spt': 'ea882552-7cd9-4ca4-a58c-deb8fa2cbfde',
        'OptanonAlertBoxClosed': '2026-01-15T20:04:38.190Z',
        'enabled_ff': '!CI12577UniversalSearch,!Fluid,!MP16400Air3Migration,!SSINavUser,!TranscendUIOn,!i18nGA,CI17409DarkModeUI,CmpLibOn,JPAir3,OTBnrOn,SSINavUserBpa,TONB2256Air3Migration,WP658TranscendOn,i18nOn',
        'cookie_prefix': '',
        'cookie_domain': '.upwork.com',
        '__cflb': '02DiuEXPXZVk436fJfSVuuwDqLqkhavJc2DBxsZThYSzX',
        'tkbl_session_id': '0ca271de-6a45-449b-ae16-d62e50732357',
        '_gcl_au': '1.1.1461495331.1784022000',
        'country_code': 'PK',
        '_vwo_ds': '3%241784095194%3A61.69300826%3A%3A%3A%3A%3A1784095194%3A1784095194%3A1',
        '_vis_opt_s': '1%7C',
        '_vis_opt_test_cookie': '1',
        'XSRF-TOKEN': '6jEs5aZK6jKQh1216bP1JpBmH4IlsDIj',
        'umq': '1920',
        'asct_vt': 'oauth2v2_int_4e54590fb29d71936196de1c83b57746',
        'g_state': '{"i_l":0,"i_ll":1784196988042,"i_b":"ZjDNADx34H8X0w5Bd65u7LjD280uaUsTnmyO8WkukuA","i_e":{"enable_itp_optimization":24},"i_et":1784196988042}',
        'cf_chl_rc_ni': '1',
        'UniversalSearchNuxt_vt': 'oauth2v2_int_6e51f65c546484324547d14fd5ca68cc',
        'OptanonConsent': 'consentId=d90d0b6c-131f-4e85-9217-a73f86a1d290&datestamp=Thu+Jul+16+2026+19%3A35%3A07+GMT%2B0500+(Pakistan+Standard+Time)&version=202512.1.0&isAnonUser=1&isGpcEnabled=0&browserGpcFlag=0&isIABGlobal=false&identifierType=Cookie+Unique+Id&hosts=&interactionCount=2&landingPath=NotLandingPage&iType=undefined&groups=C0001%3A1%2CC0002%3A1%2CC0003%3A1%2CC0004%3A1&AwaitingReconsent=false&intType=&crTime=1784021997781&geolocation=PK%3BPB',
        'AWSALBTGCORS': '23uYWtD+INmXBwtuzr/kg1S0/84s3aw3IetJyzRLH6Mi22rnKVEwFoQX3VdFKT/lDEzVWpovYzfc0w3Emxuw78UQ3rC3nM+mfGLrbAjqfFjTCNd+FDaLSVb/MbCbmoHM/ZX1fiPdgjmaZhcd4Hw5ZWf+EOaWGEa8BJsiuxfNXFkp',
        'cf_clearance': 'R_rH9k6TKZlAmC062YNQLygPDIUN43dt8Cogx30i5bQ-1784212523-1.2.1.1-LplMw609hTuo5DTdM7PEz5fLCxP2hZy2sJqa5mPsbnB.1_76ti8Zr14Zzavzmv4D8BRfmJq85am9vAM0V6AKO7oP_Gm3oQ9wz9T7rAKOjUpvZuNxdcxArchH4ouWAfNR1VMHFbDKvINVW.quoYvaFz8Y0VglB9XhE9iDvqP6MhChKPrZJizqfN.GE6SSEyKOdmjKOi3WWcBq86L0nD7JrwDlqygGEJ1FdSr4xNStJInx5EMuBO9GksZZCsejLN0Wey3Uenj19M6gZuwkhA.BNPnUXdmrAhE3QUJmxBqht.VK33qYK2cWjzP.TZia11Gq7uoB1sEnqYbxwWFBtBv8APumRrPYFuneTddA97zRi.bn2FzqLJTfKBoZiaQD1bR5.ngYF7Hhy56DKSZ1XpiosPzONCBSBO2wDWI36DlO1mIuaClF4O6GG2BVR.37KJOU',
        '_cfuvid': '6bieGcwyM8mCbG9O3Uj97jUDD14xSWKLksR4xZRbpPk-1784212522.7635875-1.0.1.1-ji.t2dBDUipVXZAiSFlzIdvCfP5uYEknBWZ7.LV2keQ',
        '__cf_bm': '_wKsPCeY0Uc4EenFK.Bawj5oN4rkFiNKe359NO4.pZs-1784212523.3379512-1.0.1.1-rOljBYr7FeOJA1hnBYLtFyLq.au4dUU4Z9wU0RsgSzHtCtamzfp2MVTICuwS7X828wm6sLMK6kQXO6BS.fJ3e7_IHODaDBq0LruktjZbF6dBLfE6WqHXR0mr52VhDUQW',
        '_upw_ses.5831': '*',
        'forterToken': '5360fd3034e24abca2a40a4ffbb1c20a_1784212507810_96_UAS9_23ck',
        'AWSALBTG': 'IsKPbb3JXj/kRzAnAaL53hYn7P9+HONz3FW5taBENMbbQi+L/4UYtPoPH3s4E6Ggsit75cu7+ED3cb5cC2n5UQ6ClJ1aaBeSfFAFnxhq4ZpYniJLREEX4KXxLGSqVgetDgfWW3ZHM5TXADz21omSiMyQT7oWT6kUdDQjUhJOU9ah',
        'AWSALB': '8faMxaZ6s5PnhsPEa0YBp+w4o++0z9OmZ1wq0pax1dFrSjAzi/qHBGvbW6CQMPYDhf0FP62SiDH9E23v+ct3zLfIG51na+8tnCcGT2ryFpIiCjERB7ZghNdijUqV',
        'AWSALBCORS': '8faMxaZ6s5PnhsPEa0YBp+w4o++0z9OmZ1wq0pax1dFrSjAzi/qHBGvbW6CQMPYDhf0FP62SiDH9E23v+ct3zLfIG51na+8tnCcGT2ryFpIiCjERB7ZghNdijUqV',
        'usnGlobalParams': '%7B%22isAutosuggest%22%3A1%2C%22autosuggestion%22%3A%7B%22isCustom%22%3Atrue%2C%22label%22%3A%22python%22%7D%7D',
        '_upw_id.5831': '0906daac-b874-4491-b287-bf728155141a.1768507138.17.1784212514.1784196989.4d404278-4a87-4ab9-8a8e-c7c10c558152.c8607e3a-228a-4751-92ee-8c1fc2e57c00.7f1462b0-a34d-45bb-b18e-8ceb1c576f6f.1784212507988.18',
    }

    headers = {
        'accept': '*/*',
        'accept-language': 'en-US,en;q=0.9',
        'authorization': 'Bearer oauth2v2_int_6e51f65c546484324547d14fd5ca68cc',
        'content-type': 'application/json',
        'origin': 'https://www.upwork.com',
        'priority': 'u=1, i',
        'referer': 'https://www.upwork.com/nx/search/jobs/?q=python',
        'sec-ch-ua': '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
        'sec-ch-ua-arch': '"x86"',
        'sec-ch-ua-bitness': '"64"',
        'sec-ch-ua-full-version': '"150.0.7871.115"',
        'sec-ch-ua-full-version-list': '"Not;A=Brand";v="8.0.0.0", "Chromium";v="150.0.7871.115", "Google Chrome";v="150.0.7871.115"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-model': '""',
        'sec-ch-ua-platform': '"Windows"',
        'sec-ch-ua-platform-version': '"19.0.0"',
        'sec-ch-viewport-width': '1920',
        'sec-fetch-dest': 'empty',
        'sec-fetch-mode': 'cors',
        'sec-fetch-site': 'same-origin',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36',
        'x-upwork-accept-language': 'en-US',
    }

    params = {
        'alias': 'visitorJobSearch',
    }

    json_data = {
        'query': '\n  query VisitorJobSearch($requestVariables: VisitorJobSearchV1Request!) {\n    search {\n      universalSearchNuxt {\n        visitorJobSearchV1(request: $requestVariables) {\n          paging {\n            total\n            offset\n            count\n          }\n          \n    facets {\n      jobType \n    {\n      key\n      value\n    }\n  \n      workload \n    {\n      key\n      value\n    }\n  \n      clientHires \n    {\n      key\n      value\n    }\n  \n      durationV3 \n    {\n      key\n      value\n    }\n  \n      amount \n    {\n      key\n      value\n    }\n  \n      contractorTier \n    {\n      key\n      value\n    }\n  \n      contractToHire \n    {\n      key\n      value\n    }\n  \n      \n    }\n  \n          results {\n            id\n            title\n            description\n            relevanceEncoded\n            ontologySkills {\n              uid\n              parentSkillUid\n              prefLabel\n              prettyName: prefLabel\n              freeText\n              highlighted\n            }\n            \n            \n            jobTile {\n              job {\n                id\n                ciphertext: cipherText\n                jobType\n                weeklyRetainerBudget\n                hourlyBudgetMax\n                hourlyBudgetMin\n                hourlyEngagementType\n                contractorTier\n                sourcingTimestamp\n                createTime\n                publishTime\n                \n                hourlyEngagementDuration {\n                  rid\n                  label\n                  weeks\n                  mtime\n                  ctime\n                }\n                fixedPriceAmount {\n                  isoCurrencyCode\n                  amount\n                }\n                fixedPriceEngagementDuration {\n                  id\n                  rid\n                  label\n                  weeks\n                  ctime\n                  mtime\n                }\n              }\n            }\n          }\n        }\n      }\n    }\n  }\n  ',
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

    try:
        response = requests.post(
            'https://www.upwork.com/api/graphql/v1',
            params=params,
            cookies=cookies,
            headers=headers,
            json=json_data,
        )

        print(response.status_code)
        if response.status_code == 200:
            raw_data = response.json()
            
            try:
                job_results = raw_data['data']['search']['universalSearchNuxt']['visitorJobSearchV1']['results']
            except KeyError as e:
                print(f"Error navigating JSON structure. Missing key: {e}")
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
                    
            return clean_jobs
        else:
            print("Blocked! Received status code:", response.status_code)
            return None
            
    except Exception as e:
        print("An error occurred while fetching jobs:", str(e))
        return None


init_db()