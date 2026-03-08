import asyncio
import discord
from discord.ext import commands
import yt_dlp
from collections import deque
import os
from dotenv import load_dotenv
import logging
import threading
import time
import re

logging.getLogger("yt_dlp").setLevel(logging.CRITICAL + 1)   # almost completely silent

load_dotenv()

import spotipy
from spotipy.oauth2 import SpotifyOAuth

queue_lock = threading.Lock()  # global lock for queue operations

REDIRECT_URI = "http://127.0.0.1:8888/callback"

sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=os.getenv("SPOTIFY_CLIENT_ID"),
    client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
    redirect_uri=REDIRECT_URI,
    scope="playlist-read-private playlist-read-collaborative",
    cache_path=".cache-spotify-bot",
    open_browser=True
))

TOKEN = os.getenv("DISCORD_TOKEN")          # put token in .env file
PREFIX = "/"                                # or use slash commands later

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# Queue per guild
queues = {}           # guild_id → deque of (url/query, title, requester)
now_playing = {}      # guild_id → current title

ydl_opts = {
    'format': 'bestaudio/best',
    'noplaylist': False,          # allow playlists
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'extract_flat': False,
}

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")


# ────────────────────────────────────────────────
# Unified scraper for both Spotify fallback & Apple Music
# ────────────────────────────────────────────────

async def scrape_playlist_page(ctx, url: str, service: str = "Unknown"):
    """
    Unified browser-based playlist scraper.
    Preserves nearly identical logic from both original scrapers.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
    from webdriver_manager.chrome import ChromeDriverManager

    guild_id = ctx.guild.id

    try:
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

        driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
        driver.get(url)

        print(f"[{service.upper()} SCRAPER] Waiting for track elements...")
        WebDriverWait(driver, 25).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div[role='row'], div[data-testid*='track'], div.TrackListRow, div.song"))
        )

        print(f"[{service.upper()} SCRAPER] Scrolling...")
        last_height = driver.execute_script("return document.body.scrollHeight")
        scroll_attempts = 0
        while scroll_attempts < 25:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(2.2 if service.lower() == "spotify" else 2.5)
            new_height = driver.execute_script("return document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height
            scroll_attempts += 1

        time.sleep(4)
        print(f"[{service.upper()} SCRAPER] Scrolled {scroll_attempts} times")

        # Playlist name heuristic
        playlist_name = "Playlist"
        try:
            raw_title = driver.title
            if service.lower() == "spotify":
                playlist_name = raw_title.split("—")[0].strip()
            else:  # Apple Music
                playlist_name = re.sub(r"\s*[-–—|]\s*Apple Music.*$", "", raw_title).strip()
            playlist_name = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", playlist_name)
        except:
            pass

        await ctx.send(f"**{service}**: {playlist_name} (scrolled {scroll_attempts} times)")

        # Row collection (same multi-selector approach)
        row_selectors = [
            "div[role='row']",
            "div[data-testid*='track']",
            "div.TrackListRow",
            "div[class*='Row']",
            "div.song",
            "li.track",
            "div.track-item"
        ]

        rows = []
        for sel in row_selectors:
            found = driver.find_elements(By.CSS_SELECTOR, sel)
            rows += found
            print(f"[{service.upper()} SCRAPER] Selector '{sel}' found {len(found)} rows")
            if len(rows) > 10:
                break

        if len(rows) < 5:
            print(f"[{service.upper()} SCRAPER] No specific rows → using broad div fallback")
            rows = driver.find_elements(By.CSS_SELECTOR, "div")

        # Track extraction (very close to original logic)
        seen = set()
        added = 0

        for row in rows[:100]:
            try:
                title = ""
                title_selectors = [
                    "a[href*='/track/'], a[href*='/song/']",
                    "span[data-testid*='title'], span.title",
                    "div.title, div.song-name",
                    "h3, h4, span"
                ]
                for ts in title_selectors:
                    try:
                        el = row.find_element(By.CSS_SELECTOR, ts)
                        title = el.text.strip()
                        if len(title) > 3:
                            break
                    except:
                        pass

                artist = "Unknown Artist"

                if title:
                    # Quick same-row artist fallback
                    try:
                        artist_el = row.find_element(By.CSS_SELECTOR, "a[href*='/artist/']")
                        artist = artist_el.text.strip()
                    except:
                        pass

                    # Complex sibling search (original heuristic)
                    if artist == "Unknown Artist":
                        try:
                            title_el = None
                            for ts in ["a[href*='/track/']", "span[data-testid*='title']", "div.title", "div.song-name", "span.title"]:
                                try:
                                    el = row.find_element(By.CSS_SELECTOR, ts)
                                    if el.text.strip() == title:
                                        title_el = el
                                        break
                                except:
                                    pass

                            if title_el:
                                following = title_el.find_elements(By.XPATH, "./following::*[position() <= 5]")
                                for sib in following:
                                    sib_text = sib.text.strip()
                                    if len(sib_text) > 2:
                                        if " by " in sib_text.lower():
                                            artist = sib_text.split(" by ", 1)[-1].strip()
                                            break
                                        if sib.get_attribute("href") and "/artist/" in sib.get_attribute("href"):
                                            artist = sib_text
                                            break
                                        if "feat." not in sib_text.lower() and not sib_text.isdigit() and len(sib_text) < 40:
                                            artist = sib_text.split("E\n", 1)[-1].strip()
                                            break
                        except:
                            pass
                if artist == "Unknown Artist":
                    artist = title
                if title and len(title) > 2 and artist != "Unknown Artist":
                    key = (artist.lower(), title.lower())
                    if key in seen:
                        continue
                    seen.add(key)

                    q = f"ytsearch:{artist} {title} official audio"
                    with queue_lock:
                        queues[guild_id].append((q, f"{artist} - {title}", ctx.author))
                    added += 1
                    print(f"[{service.upper()} SCRAPER] Added: {artist} - {title}")

            except:
                continue

        driver.quit()

        if added == 0:
            await ctx.send(f"{service} scrape found **0** tracks (page changed / login required?)")
        else:
            await ctx.send(f"Extracted and queued **{added}** unique tracks from {service} page scrape!")

        if added > 0 and not ctx.voice_client.is_playing() and guild_id in queues and queues[guild_id]:
            await play_next(ctx)

    except Exception as e:
        if 'driver' in locals():
            driver.quit()
        print(f"[{service.upper()} SCRAPER ERROR] {str(e)}")
        await ctx.send(f"{service} scraper failed: {str(e)[:180]}")


async def play_next(ctx):
    guild_id = ctx.guild.id
    if guild_id not in queues or not queues[guild_id]:
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
        return

    item = queues[guild_id].popleft()
    query_or_url, title, requester = item
    now_playing[guild_id] = title

    try:
        url = query_or_url

        # If it's a search string (not starting with http), resolve it
        if not query_or_url.lower().startswith(('http://', 'https://')):
            ydl = yt_dlp.YoutubeDL(ydl_opts)
            info = ydl.extract_info(query_or_url, download=False)
            if 'entries' in info and info['entries']:
                entry = info['entries'][0]  # best match
                url = entry['url']
                title = entry.get('title', title)  # update title
                now_playing[guild_id] = title
            else:
                raise Exception("No search results found")

        source = discord.FFmpegPCMAudio(
            url,
            before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -reconnect_at_eof 1",
            options="-vn"
        )

        def after_play(error):
            if error:
                print(f"Playback error for '{title}': {error}")
            asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)

        ctx.voice_client.play(source, after=after_play)

        requester_mention = requester.mention if requester else "someone"
        await ctx.send(f"**Now playing:** {title} (by {requester_mention})")

    except Exception as e:
        print(f"Failed to play '{title}': {e}")
        await ctx.send(f"Couldn't play **{title}**: {str(e)[:150]}... skipping")
        await play_next(ctx)


@bot.command(name="play", aliases=["p"])
async def play(ctx, *, query: str = None):
    if not query:
        await ctx.send("Give a song name, YouTube URL, or Spotify/Apple Music link!\nExamples:\n`/play bad guy`\n`/play https://open.spotify.com/track/...`")
        return

    if not ctx.author.voice:
        await ctx.send("Join a voice channel first!")
        return

    voice_channel = ctx.author.voice.channel

    if not ctx.voice_client:
        await voice_channel.connect()
    elif ctx.voice_client.channel != voice_channel:
        await ctx.send("I'm already in a different voice channel!")
        return

    guild_id = ctx.guild.id
    if guild_id not in queues:
        queues[guild_id] = deque()

    await ctx.send(f"Processing `{query}`...")

    try:
        search_term = query.strip()

        # Spotify handling
        if "spotify.com" in query or query.startswith("spotify:"):
            if "/track/" in query or query.startswith("spotify:track:"):
                track_id = query.split("/")[-1].split("?")[0].split(":")[-1]
                track = sp.track(track_id)
                artist = track['artists'][0]['name']
                title = track['name']
                search_term = f"ytsearch:{artist} {title} official audio"
                await ctx.send(f"Spotify track → **{artist} - {title}**")

                with queue_lock:
                    queues[guild_id].append((search_term, f"{artist} - {title}", ctx.author))

                if not ctx.voice_client.is_playing():
                    await play_next(ctx)

            elif "/playlist/" in query or query.startswith("spotify:playlist:"):
                pl_id = query.split("/")[-1].split("?")[0].split(":")[-1]
                try:
                    # Fast API path
                    playlist_info = sp.playlist(pl_id)
                    playlist_name = playlist_info.get('name', 'Unknown Playlist')
                    await ctx.send(f"Spotify playlist: **{playlist_name}**")

                    results = sp.playlist_items(pl_id, limit=50)
                    added_count = 0

                    while results:
                        for item in results['items']:
                            track = item.get('track')
                            if not track or track.get('is_local'):
                                continue
                            art = track['artists'][0]['name'] if track['artists'] else 'Unknown'
                            ttl = track['name']
                            q = f"ytsearch:{art} {ttl} official audio"
                            with queue_lock:
                                queues[guild_id].append((q, f"{art} - {ttl}", ctx.author))
                            added_count += 1

                        if results.get('next'):
                            results = sp.next(results)
                        else:
                            break

                    await ctx.send(f"Queued **{added_count}** tracks from Spotify playlist.")
                    if added_count > 0 and not ctx.voice_client.is_playing():
                        await play_next(ctx)
                    return

                except spotipy.exceptions.SpotifyException as se:
                    if se.http_status in (403, 404):
                        print("[SCRAPER] Starting page scrape for:", query)
                        await scrape_playlist_page(ctx, query, service="Spotify")
                        return
                    else:
                        raise

            elif "/album/" in query or query.startswith("spotify:album:"):
                alb_id = query.split("/")[-1].split("?")[0].split(":")[-1]
                try:
                    alb = sp.album(alb_id)
                    album_name = alb['name']
                    results = sp.album_tracks(alb_id)
                    added_count = 0
                    for tr in results['items']:
                        art = tr['artists'][0]['name'] if tr['artists'] else 'Unknown'
                        ttl = tr['name']
                        q = f"ytsearch:{art} {ttl}"
                        with queue_lock:
                            queues[guild_id].append((q, f"{art} - {ttl} ({album_name})", ctx.author))
                        added_count += 1
                    await ctx.send(f"Queued **{added_count}** tracks from Spotify album **{album_name}**.")
                    if not ctx.voice_client.is_playing():
                        await play_next(ctx)
                except Exception as e:
                    await ctx.send(f"Album error: {str(e)[:200]}")
                return

            else:
                await ctx.send("Only tracks, playlists & albums supported for Spotify. Falling back to search.")

        # Apple Music handling
        elif "music.apple.com" in query:
            await ctx.send("🍎 Apple Music playlist detected → scraping page...")
            await scrape_playlist_page(ctx, query, service="Apple Music")
            return

        # Normal YouTube / search / direct URL handling
        ydl = yt_dlp.YoutubeDL(ydl_opts)
        info = ydl.extract_info(search_term, download=False)

        if 'entries' in info:
            count = 0
            for entry in info['entries'][:50]:
                if entry and entry.get('url'):
                    with queue_lock:
                        queues[guild_id].append((entry['url'], entry.get('title', 'Unknown'), ctx.author))
                    count += 1
            await ctx.send(f"Added **{count}** tracks from playlist.")
        else:
            url = info['url']
            title = info.get('title', search_term)
            with queue_lock:
                queues[guild_id].append((url, title, ctx.author))
            await ctx.send(f"Added **{title}**")

        if not ctx.voice_client.is_playing():
            await play_next(ctx)

    except Exception as e:
        err = str(e)
        if "DRM" in err.upper():
            await ctx.send("❌ DRM protected link (Spotify?). Try song name manually instead.")
        else:
            await ctx.send(f"Error: {err[:350]}")


@bot.command(name="skip")
async def skip(ctx, count: int = 1):
    """
    Skip the current song (or multiple songs).
    Usage:
    /skip          → skip 1 song (current one)
    /skip 3        → skip current song + next 2 songs
    """
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("Nothing is playing right now!")
        return

    if count < 1:
        await ctx.send("Please specify a number greater than 0.")
        return

    # Stop current song (this triggers after_play → play_next)
    ctx.voice_client.stop()

    # Skip additional songs (pop them from queue without playing)
    skipped = 0
    for _ in range(count - 1):  # -1 because we already stopped the current one
        with queue_lock:
            if queues.get(ctx.guild.id) and queues[ctx.guild.id]:
                queues[ctx.guild.id].popleft()
                skipped += 1
            else:
                break

    total_skipped = 1 + skipped  # include the current one we stopped

    if skipped > 0:
        await ctx.send(f"Skipped **{total_skipped}** songs ⏭️")
    else:
        await ctx.send("Skipped the current song ⏭️")

    # If queue is now empty, disconnect
    if not queues.get(ctx.guild.id):
        await ctx.voice_client.disconnect()
        await ctx.send("Queue empty — disconnected 👋")
    if not ctx.voice_client or not ctx.voice_client.is_playing():
        await ctx.send("Nothing is playing!")
        return


@bot.command(name="stop")
async def stop(ctx):
    if ctx.voice_client:
        queues[ctx.guild.id].clear()
        ctx.voice_client.stop()
        await ctx.voice_client.disconnect()
        await ctx.send("Stopped & disconnected 👋")
    else:
        await ctx.send("Not in voice channel.")


@bot.command(name="queue", aliases=["q"])
async def show_queue(ctx):
    if ctx.guild.id not in queues or not queues[ctx.guild.id]:
        await ctx.send("Queue is empty.")
        return

    with queue_lock:
        q = list(queues[ctx.guild.id])

    total = len(q)
    if total == 0:
        await ctx.send("Queue is empty.")
        return

    msg = f"**Queue:** ({total} songs)\n"
    current_length = len(msg)

    for i, (_, title, requester) in enumerate(q, 1):
        line = f"{i}. {title} | by {requester}\n"
        line_length = len(line)

        if current_length + line_length + len(f"... + {total - (i - 1)} more songs") > 2000:
            remaining = total - (i - 1)
            msg += f"... + {remaining} more songs"
            break

        msg += line
        current_length += line_length

    await ctx.send(msg)


if __name__ == "__main__":
    bot.run(TOKEN)