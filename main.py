import asyncio
import signal
import sys
import os
from dotenv import load_dotenv

# Load .env BEFORE importing scraper (scraper reads BROWSER_HEADLESS from env)
load_dotenv()

import discord
from discord.ext import commands, tasks
from scraper import fetch_jobs, auth_manager

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TARGET_CHANNEL_ID = 1526858039993569382

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

def is_job_posted(job_id):
    """Check if job was already posted by checking the database."""
    import sqlite3
    conn = sqlite3.connect('jobs.db')
    cursor = conn.cursor()
    cursor.execute('SELECT posted FROM jobs WHERE id = ?', (job_id,))
    row = cursor.fetchone()
    conn.close()
    return row and row[0] == 1

def mark_job_posted(job_id):
    """Mark a job as posted in the database."""
    import sqlite3
    conn = sqlite3.connect('jobs.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE jobs SET posted = 1 WHERE id = ?', (job_id,))
    conn.commit()
    conn.close()

def get_unposted_jobs():
    """Get all jobs from database that haven't been posted yet."""
    import sqlite3
    conn = sqlite3.connect('jobs.db')
    cursor = conn.cursor()
    cursor.execute('SELECT id, title, budget, experience_level, skills, description_preview, full_description, url FROM jobs WHERE posted = 0 OR posted IS NULL')
    rows = cursor.fetchall()
    conn.close()
    
    jobs = []
    for row in rows:
        jobs.append({
            "id": row[0],
            "title": row[1],
            "budget": row[2],
            "experience_level": row[3],
            "skills": row[4].split(', ') if row[4] else [],
            "description_preview": row[5],
            "full_description": row[6],
            "url": row[7]
        })
    return jobs

@bot.event
async def on_ready():
    print(f"--- BOT IS ONLINE ---")
    print("Starting browser for Cloudflare bypass...")
    await asyncio.to_thread(auth_manager.start)
    print("Browser ready, starting job scraper loop...")
    job_scraper_loop.start()

@tasks.loop(seconds=10)
async def job_scraper_loop():
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if not channel:
        return

    print("Checking Upwork for new jobs...")
    jobs = await asyncio.to_thread(fetch_jobs, "python")

    if jobs is None:
        print("Failed to fetch jobs. Trying again next loop.")
        return

    # Post unposted jobs from database
    unposted_jobs = get_unposted_jobs()
    for job in unposted_jobs:
        main_message = (
            f"**New Job Posted!**\n"
            f"**Title:** {job['title']}\n"
            f"**Budget:** {job['budget']}\n"
            f"**Level:** {job['experience_level']}\n"
            f"**Skills:** {', '.join(job['skills'])}\n\n"
            f"{job['description_preview']}\n"
            f"[Apply Here]({job['url']})"
        )

        message = await channel.send(main_message)
        thread = await message.create_thread(name=f"Job: {job['title'][:80]}", auto_archive_duration=60)

        thread_message = f"**Full Job Description**:\n{job['full_description']}\n\n[Apply on Upwork]({job['url']})"
        await thread.send(thread_message[:2000])

        mark_job_posted(job['id'])
        print(f"Successfully posted: {job['title']}")

def signal_handler(sig, frame):
    print("\nShutting down bot...")
    auth_manager.stop()
    sys.exit(0)

if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        bot.run(DISCORD_TOKEN)
    except KeyboardInterrupt:
        print("\nBot terminated by user")
    finally:
        auth_manager.stop()
        print("Cleanup complete")