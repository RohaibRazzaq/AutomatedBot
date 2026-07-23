import asyncio
import signal
import sys
import os
import json
import sqlite3
import time
import threading
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from dotenv import load_dotenv

# Load .env BEFORE importing scraper (scraper reads BROWSER_HEADLESS from env)
load_dotenv()

# ---------------------------------------------------------------------------
# Logging setup — rotating file + console (captures ALL print() output)
# ---------------------------------------------------------------------------
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")

class _TeeWriter:
    """Tee sys.stdout/stderr to both the terminal and a rotating log file.

    Every print() call across the entire application is captured with a timestamp
    and written to the log file, while still showing in the terminal.
    """

    def __init__(self, filepath, stream):
        self.filepath = filepath
        self.terminal = stream
        self.file = open(filepath, "a", encoding="utf-8")
        self._lock = threading.Lock()

    def _ensure_open(self):
        """Reopen the file if it was closed (e.g., by shutdown handler)."""
        if self.file.closed:
            self.file = open(self.filepath, "a", encoding="utf-8")

    def write(self, message):
        with self._lock:
            self.terminal.write(message)
            self.terminal.flush()
            self._ensure_open()
            if message.strip():
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                self.file.write(f"[{ts}] {message}")
            else:
                self.file.write(message)
            self.file.flush()

    def flush(self):
        self.terminal.flush()
        self._ensure_open()
        self.file.flush()


_tee_out = _TeeWriter(LOG_FILE, sys.__stdout__)
_tee_err = _TeeWriter(LOG_FILE, sys.__stderr__)
sys.stdout = _tee_out
sys.stderr = _tee_err
print(f"[Logging] Writing to {LOG_FILE}")

import discord
from discord.ext import commands
from scraper import fetch_jobs, auth_manager, calculate_content_hash

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CONFIG_FILE = "channels_config.json"
DEFAULT_EXCLUDED = [
    "general", "announcements", "rules", "off-topic",
    "bot-commands", "logs", "introductions", "voice-chat",
]
# Delay between processing each channel (seconds)
CHANNEL_DELAY = 4
# Delay between full scan cycles (seconds)
CYCLE_DELAY = 10

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------------------------
# Tracking variables
# ---------------------------------------------------------------------------
_cycles_completed = 0
_total_jobs_posted = 0
_shutting_down = False  # Flag to prevent race conditions during shutdown

# ---------------------------------------------------------------------------
# Time formatting helpers
# ---------------------------------------------------------------------------

def format_relative_time(publish_time_str):
    """Convert an Upwork publishTime string to a relative 'X mins ago' string."""
    if not publish_time_str:
        return "Unknown"

    try:
        pt = publish_time_str
        # Handle Unix timestamp (milliseconds or seconds) as int or string
        if isinstance(pt, (int, float)) or (isinstance(pt, str) and pt.isdigit()):
            ts = float(pt)
            if ts > 1e12:  # milliseconds
                ts /= 1000
            pub_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            # ISO 8601 string — strip trailing Z and parse
            pt_str = str(pt).replace("Z", "+00:00")
            pub_dt = datetime.fromisoformat(pt_str)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        diff = now - pub_dt
        total_seconds = diff.total_seconds()

        if total_seconds < 0:
            return "Just now"
        mins = int(total_seconds // 60)
        hours = int(total_seconds // 3600)
        # Round to nearest day (5.5+ days = 6 days)
        days = round(total_seconds / 86400)

        if mins < 1:
            return "Just now"
        elif mins < 60:
            return f"{mins} min{'s' if mins != 1 else ''} ago"
        elif hours < 24:
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        else:
            return f"{days} day{'s' if days != 1 else ''} ago"
    except Exception:
        return str(publish_time_str)


# ---------------------------------------------------------------------------
# Config file management
# ---------------------------------------------------------------------------

def load_channels_config():
    """Load channel config from JSON file, creating defaults if missing."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"tracked": {}, "excluded": list(DEFAULT_EXCLUDED)}


def save_channels_config(config):
    """Save channel config to JSON file."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def scan_and_update_channels():
    """Scan all text channels in every guild the bot is in and update config.

    - Adds new channels to tracking
    - Removes channels that no longer exist in Discord (deleted while bot was offline)
    """
    config = load_channels_config()
    tracked = config.setdefault("tracked", {})
    excluded = config.get("excluded", list(DEFAULT_EXCLUDED))

    # Collect all current channel IDs and names from Discord
    current_channels = {}  # name -> id
    for guild in bot.guilds:
        for channel in guild.text_channels:
            if channel.name not in excluded:
                current_channels[channel.name] = channel.id

    # Remove tracked channels that no longer exist in Discord
    removed = []
    for name, cid in list(tracked.items()):
        if cid not in current_channels.values():
            del tracked[name]
            removed.append(name)
            print(f"[Config] Removed deleted channel: #{name}")

    # Add new channels
    added = []
    for name, cid in current_channels.items():
        if name not in tracked:
            tracked[name] = cid
            added.append(name)
            print(f"[Config] Added new channel: #{name} ({cid})")

    config["excluded"] = excluded
    save_channels_config(config)
    return config


# ---------------------------------------------------------------------------
# Database functions
# ---------------------------------------------------------------------------

def is_job_posted(job_id, job=None):
    """Check if a job was already posted to any channel.

    If job is provided, also checks if the job content has changed (updated).
    If the content hash doesn't match, resets the posted flag and returns False.
    """
    for attempt in range(3):
        try:
            conn = sqlite3.connect("jobs.db", timeout=10)
            cursor = conn.cursor()
            cursor.execute("SELECT posted, content_hash FROM jobs WHERE id = ?", (job_id,))
            row = cursor.fetchone()
            conn.close()

            if not row or row[0] != 1:
                return False

            # Job was posted, but check if it has been updated
            if job is not None:
                stored_hash = row[1]
                current_hash = calculate_content_hash(job)
                if stored_hash != current_hash:
                    # Job was updated, reset posted flag
                    print(f"[Update] Job {job_id} content changed, will repost.")
                    mark_job_posted(job_id, reset=True)
                    return False

            return True
        except sqlite3.OperationalError:
            if attempt < 2:
                time.sleep(0.5)
            else:
                print(f"[DB] Failed to check job {job_id} after 3 retries, assuming not posted.")
                return False


def mark_job_posted(job_id, reset=False):
    """Mark a job as posted (or reset if updated)."""
    for attempt in range(3):
        try:
            conn = sqlite3.connect("jobs.db", timeout=10)
            cursor = conn.cursor()
            if reset:
                cursor.execute("UPDATE jobs SET posted = 0 WHERE id = ?", (job_id,))
            else:
                cursor.execute("UPDATE jobs SET posted = 1 WHERE id = ?", (job_id,))
            conn.commit()
            conn.close()
            return
        except sqlite3.OperationalError:
            if attempt < 2:
                time.sleep(0.5)
            else:
                print(f"[DB] Failed to mark job {job_id} as posted after 3 retries.")


# ---------------------------------------------------------------------------
# Discord event handlers
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"--- BOT IS ONLINE ---")
    print("Starting browser for Cloudflare bypass...")
    await asyncio.to_thread(auth_manager.start)
    print("Browser ready.")

    print("Scanning Discord channels...")
    config = scan_and_update_channels()
    tracked = config.get("tracked", {})
    excluded = config.get("excluded", [])
    print(f"Tracking {len(tracked)} channel(s): {', '.join(tracked.keys())}")
    print(f"Excluded: {', '.join(excluded)}")

    bot.loop.create_task(job_scraper_task())


@bot.event
async def on_guild_channel_create(channel):
    """Auto-track new text channels."""
    if isinstance(channel, discord.TextChannel):
        config = load_channels_config()
        excluded = config.get("excluded", [])
        if channel.name not in excluded:
            tracked = config.setdefault("tracked", {})
            tracked[channel.name] = channel.id
            save_channels_config(config)
            print(f"[Config] New channel tracked: #{channel.name} ({channel.id})")


@bot.event
async def on_guild_channel_delete(channel):
    """Remove deleted channels from tracking."""
    if isinstance(channel, discord.TextChannel):
        config = load_channels_config()
        tracked = config.get("tracked", {})
        if channel.name in tracked:
            del tracked[channel.name]
            save_channels_config(config)
            print(f"[Config] Channel removed: #{channel.name}")


@bot.event
async def on_guild_channel_update(before, after):
    """Update config when a channel is renamed."""
    if isinstance(after, discord.TextChannel) and before.name != after.name:
        config = load_channels_config()
        tracked = config.setdefault("tracked", {})
        excluded = config.get("excluded", [])

        # Remove old name
        if before.name in tracked:
            del tracked[before.name]

        # Add new name (unless excluded)
        if after.name not in excluded:
            tracked[after.name] = after.id
            print(f"[Config] Channel renamed: #{before.name} -> #{after.name}")

        save_channels_config(config)


# ---------------------------------------------------------------------------
# Bot commands
# ---------------------------------------------------------------------------

@bot.command(name="rescan")
async def rescan_channels(ctx):
    """Manually trigger a channel rescan."""
    config = scan_and_update_channels()
    tracked = config.get("tracked", {})
    await ctx.send(f"Rescanned! Tracking {len(tracked)} channel(s): {', '.join(tracked.keys())}")


@bot.command(name="channels")
async def list_channels(ctx):
    """List all tracked channels."""
    config = load_channels_config()
    tracked = config.get("tracked", {})
    excluded = config.get("excluded", [])
    lines = [f"**Tracked ({len(tracked)}):**"]
    for name, cid in tracked.items():
        lines.append(f"  #{name} -> {cid}")
    lines.append(f"\n**Excluded:** {', '.join(excluded)}")
    await ctx.send("\n".join(lines))


@bot.command(name="exclude")
async def exclude_channel(ctx, channel_name: str):
    """Exclude a channel from job tracking. Usage: !exclude general"""
    config = load_channels_config()
    excluded = config.setdefault("excluded", [])
    if channel_name not in excluded:
        excluded.append(channel_name)
        tracked = config.get("tracked", {})
        if channel_name in tracked:
            del tracked[channel_name]
        save_channels_config(config)
        await ctx.send(f"Excluded #{channel_name} from tracking.")
    else:
        await ctx.send(f"#{channel_name} is already excluded.")


@bot.command(name="include")
async def include_channel(ctx, channel_name: str):
    """Re-include a previously excluded channel. Usage: !include general"""
    config = load_channels_config()
    excluded = config.get("excluded", [])
    if channel_name in excluded:
        excluded.remove(channel_name)
        save_channels_config(config)
        await ctx.send(f"Re-included #{channel_name}. Run !rescan to start tracking it.")
    else:
        await ctx.send(f"#{channel_name} is not in the exclusion list.")


@bot.command(name="add")
async def add_channel(ctx, channel_name: str):
    """Create a new channel and start tracking it. Usage: !add next"""
    config = load_channels_config()
    tracked = config.setdefault("tracked", {})
    category = discord.utils.get(ctx.guild.categories, name="Text Channels")
    if not category:
        await ctx.send("Category 'Text Channels' not found. Please create it manually.")
        return

    if channel_name in tracked:
        await ctx.send(f"#{channel_name} is already being tracked.")
        return

    # Create the channel in the current guild
    try:
        new_channel = await ctx.guild.create_text_channel(name=channel_name, category=category)  # Places it inside this category
    except discord.Forbidden:
        await ctx.send(f"I don't have permission to create channels in this server.")
        return
    except Exception as e:
        await ctx.send(f"Failed to create channel: {e}")
        return

    # Remove from excluded list if present
    excluded = config.get("excluded", [])
    if channel_name in excluded:
        excluded.remove(channel_name)

    tracked[channel_name] = new_channel.id
    save_channels_config(config)
    await ctx.send(f"Created #{channel_name} ({new_channel.id}) and added to tracking.")


@bot.command(name="delete")
async def delete_channel(ctx, channel_name: str):
    """Delete a channel from Discord and stop tracking it. Usage: !delete next"""
    config = load_channels_config()
    tracked = config.get("tracked", {})

    # Find the channel by name in the guild
    channel = discord.utils.get(ctx.guild.text_channels, name=channel_name)

    if not channel:
        await ctx.send(f"Channel #{channel_name} not found in this server.")
        return

    # Delete the channel from Discord
    try:
        await channel.delete()
    except discord.Forbidden:
        await ctx.send(f"I don't have permission to delete channels in this server.")
        return
    except Exception as e:
        await ctx.send(f"Failed to delete channel: {e}")
        return

    # Remove from tracking config
    if channel_name in tracked:
        del tracked[channel_name]
        save_channels_config(config)
        await ctx.send(f"Deleted #{channel_name} and removed from tracking.")
    else:
        await ctx.send(f"Deleted #{channel_name} (was not being tracked).")


# ---------------------------------------------------------------------------
# Multi-channel scraper loop
# ---------------------------------------------------------------------------

async def job_scraper_task():
    """Continuously scan Upwork for each tracked channel's keyword.

    For each channel:
      1. Call fetch_jobs(keyword) where keyword = channel name (hyphens -> spaces)
      2. Post any jobs not yet posted to that specific channel
      3. Wait CHANNEL_DELAY seconds before the next channel

    After all channels are processed, wait CYCLE_DELAY seconds and repeat.
    """
    global _cycles_completed, _total_jobs_posted
    await bot.wait_until_ready()
    while not bot.is_closed() and not _shutting_down:
        config = load_channels_config()
        tracked = config.get("tracked", {})
        excluded = config.get("excluded", [])

        for channel_name, channel_id in list(tracked.items()):
            # Check shutdown flag before each channel
            if _shutting_down:
                print("[Scraper] Shutdown requested, stopping...")
                return

            if channel_name in excluded:
                continue

            try:
                channel = bot.get_channel(channel_id)
                if not channel:
                    continue

                # Convert Discord hyphenated names to spaces for search
                # e.g. "machine-learning" -> "machine learning"
                keyword = channel_name.replace("-", " ")

                print(f"Checking Upwork for '{keyword}' jobs...")
                jobs = await asyncio.to_thread(fetch_jobs, keyword)

                if jobs is None:
                    print(f"  Failed to fetch jobs for '{keyword}'.")
                elif len(jobs) == 0:
                    print(f"  No jobs found for '{keyword}'.")
                else:
                    posted_count = 0
                    for job in jobs:
                        if is_job_posted(job["id"], job):
                            continue

                        detected_time = datetime.now().strftime("%H:%M")
                        posted_ago = format_relative_time(job.get("publish_time"))

                        skills_str = ', '.join(job['skills'])
                        # Build message with all fields
                        main_message = (
                            f"**New Job Posted!**\n"
                            f"**Title:** {job['title']}\n"
                            f"**Budget:** {job['budget']}\n"
                            f"**Level:** {job['experience_level']}\n"
                            f"**Posted:** {posted_ago}\n"
                            f"**Detected:** {detected_time}\n"
                            f"**Skills:** {skills_str}"
                        )

                        # Simple hard cap at 1900 chars — bulletproof
                        if len(main_message) > 1900:
                            main_message = main_message[:1897] + "..."

                        message = await channel.send(main_message)
                        thread = await message.create_thread(
                            name=f"Job: {job['title'][:80]}",
                            auto_archive_duration=60,
                        )
                        apply_link = f"\n\n[Apply on Upwork]({job['url']})"
                        header = "**Full Job Description**:\n"
                        # Reserve space for header and apply link, truncate description
                        max_desc_len = 1900 - len(header) - len(apply_link)
                        description = job['full_description']
                        if len(description) > max_desc_len:
                            description = description[:max_desc_len] + "..."
                        thread_message = header + description + apply_link

                        await thread.send(thread_message)

                        mark_job_posted(job["id"])
                        posted_count += 1
                        _total_jobs_posted += 1
                        print(f"  Posted to #{channel_name}: {job['title']}")

                        # Rate-limit: wait between Discord messages to avoid 429
                        await asyncio.sleep(1)

                    if posted_count == 0:
                        print(f"  No new jobs for #{channel_name} (all already posted).")
                    else:
                        print(f"  Posted {posted_count} new job(s) to #{channel_name}.")
            except Exception as e:
                print(f"[ERROR] Channel #{channel_name} failed: {e}")
                if 'main_message' in locals():
                    print(f"[DEBUG] main_message length: {len(main_message)}")
                if 'thread_message' in locals():
                    print(f"[DEBUG] thread_message length: {len(thread_message)}")
                print(f"[ERROR] Continuing to next channel...")

            # Break between channels to avoid hammering Upwork
            await asyncio.sleep(CHANNEL_DELAY)

        print(f"--- Cycle complete ({_cycles_completed + 1} done). Waiting {CYCLE_DELAY}s before next cycle... ---")
        _cycles_completed += 1
        await asyncio.sleep(CYCLE_DELAY)


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

def signal_handler(sig, frame):
    global _shutting_down
    print("\nShutting down bot...")

    # Set flag first to stop the scraper loop
    _shutting_down = True

    # Give the scraper loop time to finish current operation
    time.sleep(2)

    # Now close the browser
    auth_manager.stop()

    # Close log files
    try:
        _tee_out.file.close()
    except:
        pass
    try:
        _tee_err.file.close()
    except:
        pass

    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGINT, signal_handler)

    try:
        bot.run(DISCORD_TOKEN)
    except KeyboardInterrupt:
        print("\nBot terminated by user")
    finally:
        _shutting_down = True
        time.sleep(1)
        try:
            auth_manager.stop()
        except:
            pass
        try:
            _tee_out.file.close()
        except:
            pass
        try:
            _tee_err.file.close()
        except:
            pass
        print("Cleanup complete")
