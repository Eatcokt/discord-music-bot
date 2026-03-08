import asyncio
import discord
from discord.ext import commands
import yt_dlp
from collections import deque
import os
from dotenv import load_dotenv
import logging
logging.getLogger("yt_dlp").setLevel(logging.CRITICAL + 1)   # almost completely silent

load_dotenv()
import spotipy
from spotipy.oauth2 import SpotifyOAuth
import threading
queue_lock = threading.Lock()  # global lock for queue operations
import time
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
queues = {}           # guild_id → deque of (url, title, requester)
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

        # Better FFmpeg options to avoid short playback
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
        await play_next(ctx)  # continue

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
                        # Exact same scraper logic as Apple Music — no changes
                        await ctx.send("Spotify API blocked (403/404) → scraping page like Apple Music...")

                        from selenium import webdriver
                        from selenium.webdriver.chrome.service import Service
                        from selenium.webdriver.chrome.options import Options
                        from selenium.webdriver.common.by import By
                        from selenium.webdriver.support.ui import WebDriverWait
                        from selenium.webdriver.support import expected_conditions as EC
                        from webdriver_manager.chrome import ChromeDriverManager
                        import re
                        import time

                        try:
                            options = Options()
                            options.add_argument("--headless")
                            options.add_argument("--no-sandbox")
                            options.add_argument("--disable-dev-shm-usage")
                            options.add_argument("--window-size=1920,1080")
                            options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

                            driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
                            driver.get(query)

                            WebDriverWait(driver, 20).until(
                                EC.presence_of_element_located((By.CSS_SELECTOR, "div[role='row'], div[data-testid*='track']"))
                            )

                            last_height = driver.execute_script("return document.body.scrollHeight")
                            scroll_attempts = 0
                            while scroll_attempts < 25:
                                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                                time.sleep(2.2)
                                new_height = driver.execute_script("return document.body.scrollHeight")
                                if new_height == last_height:
                                    break
                                last_height = new_height
                                scroll_attempts += 1

                            time.sleep(4)

                            playlist_name = driver.title.split("—")[0].strip()
                            playlist_name = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", playlist_name)

                            await ctx.send(f"Playlist: **{playlist_name}** (scrolled {scroll_attempts} times)")

                            tracks = []
                            row_selectors = [
                                "div[role='row']",
                                "div[data-testid*='track']",
                                "div.TrackListRow",
                                "div[class*='Row']",
                                "div.song",
                                "li.track"
                            ]

                            rows = []
                            for sel in row_selectors:
                                rows += driver.find_elements(By.CSS_SELECTOR, sel)
                                if len(rows) > 5:
                                    break

                            if not rows:
                                rows = driver.find_elements(By.CSS_SELECTOR, "div")

                            seen = set()
                            for row in rows[:100]:
                                try:
                                    title = ""
                                    for ts in ["[data-testid*='title']", "a[href*='/track/']", "span[class*='title']", "div[class*='song-title']", "h3", "h4", "span"]:
                                        try:
                                            el = row.find_element(By.CSS_SELECTOR, ts)
                                            title = el.text.strip()
                                            if len(title) > 3:
                                                break
                                        except:
                                            pass

                                    artist = "Unknown Artist"
                                    for asel in ["[data-testid*='artist']", "a[href*='/artist/']", "span[class*='byline']", "span[class*='artist']", "div[class*='artist-name']"]:
                                        try:
                                            el = row.find_element(By.CSS_SELECTOR, asel)
                                            artist = el.text.strip()
                                            if len(artist) > 2 and "by " not in artist.lower():
                                                break
                                        except:
                                            pass

                                    if title and len(title) > 2:
                                        key = (artist.lower(), title.lower())
                                        if key in seen:
                                            continue
                                        seen.add(key)
                                        q = f"ytsearch:{artist} {title} official audio"
                                        with queue_lock:
                                            queues[guild_id].append((q, f"{artist} - {title}", ctx.author))
                                except:
                                    continue

                            driver.quit()

                            added = len(seen)
                            if added == 0:
                                await ctx.send("Scrape found 0 tracks (page may require login). Try manual search.")
                            else:
                                await ctx.send(f"✅ Extracted **{added}** unique songs from page scrape!")

                            await asyncio.sleep(4)

                            if not ctx.voice_client.is_playing() and queues[guild_id]:
                                await play_next(ctx)

                            return

                        except Exception as e:
                            if 'driver' in locals():
                                driver.quit()
                            await ctx.send(f"Scraper failed: {str(e)[:180]}")
                            return

                    else:
                        await ctx.send(f"Spotify API error: {str(se)[:200]}")
                        return

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
                except Exception as e:
                    await ctx.send(f"Album error: {str(e)[:200]}")
                if not ctx.voice_client.is_playing():
                    await play_next(ctx)
                return

            else:
                await ctx.send("Only tracks, playlists & albums supported for Spotify. Falling back to search.")

        # Apple Music handling (your current working version with lock added)
        elif "music.apple.com" in query:
            await ctx.send("🍎 Apple Music playlist detected → loading full page with browser to extract real tracks...")

            from selenium import webdriver
            from selenium.webdriver.chrome.service import Service
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support.ui import WebDriverWait
            from selenium.webdriver.support import expected_conditions as EC
            from webdriver_manager.chrome import ChromeDriverManager
            import re
            import time

            try:
                options = Options()
                options.add_argument("--headless")
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-dev-shm-usage")
                options.add_argument("--window-size=1920,1080")
                options.add_argument("--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")

                driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)
                driver.get(query)

                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "[data-testid*='track'], div[class*='Track'], div[role='row'], li.song"))
                )

                last_height = driver.execute_script("return document.body.scrollHeight")
                scroll_attempts = 0
                max_attempts = 25
                while scroll_attempts < max_attempts:
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    time.sleep(2.5)
                    new_height = driver.execute_script("return document.body.scrollHeight")
                    if new_height == last_height:
                        break
                    last_height = new_height
                    scroll_attempts += 1

                time.sleep(4)

                playlist_name = "Apple Music Playlist"
                try:
                    raw_title = driver.title
                    playlist_name = re.sub(r"\s*[-–—|]\s*Apple Music.*$", "", raw_title).strip()
                    playlist_name = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", playlist_name)
                except:
                    pass

                await ctx.send(f"Playlist: **{playlist_name}** (scrolled {scroll_attempts} times)")

                tracks = []
                row_selectors = [
                    "div[role='row']",
                    "div[data-testid*='track']",
                    "div[class*='TrackListItem']",
                    "div[class*='song']",
                    "li[class*='track']",
                    "div[class*='Row']"
                ]

                rows = []
                for sel in row_selectors:
                    rows += driver.find_elements(By.CSS_SELECTOR, sel)
                    if len(rows) > 5:
                        break

                if not rows:
                    rows = driver.find_elements(By.CSS_SELECTOR, "div")

                seen = set()
                for row in rows[:100]:
                    try:
                        title = ""
                        for ts in ["[data-testid*='title']", "a[href*='/song/']", "span[class*='title']", "div[class*='song-title']", "h3", "h4", "span"]:
                            try:
                                el = row.find_element(By.CSS_SELECTOR, ts)
                                title = el.text.strip()
                                if len(title) > 3:
                                    break
                            except:
                                pass

                        artist = "Unknown Artist"
                        for asel in ["[data-testid*='artist']", "a[href*='/artist/']", "span[class*='byline']", "span[class*='artist']", "div[class*='artist-name']"]:
                            try:
                                el = row.find_element(By.CSS_SELECTOR, asel)
                                artist = el.text.strip()
                                if len(artist) > 2 and "by " not in artist.lower():
                                    break
                            except:
                                pass

                        if title and len(title) > 2:
                            key = (artist.lower(), title.lower())
                            if key in seen:
                                continue
                            seen.add(key)
                            q = f"ytsearch:{artist} {title} official audio"
                            with queue_lock:
                                queues[guild_id].append((q, f"{artist} - {title}", ctx.author))
                    except:
                        continue

                driver.quit()

                if not seen:
                    await ctx.send("No valid tracks extracted. Falling back to playlist name search...")
                    search_term = f"ytsearch:{playlist_name} Apple Music playlist full 2026"
                    with queue_lock:
                        queues[guild_id].append((search_term, playlist_name, ctx.author))
                else:
                    added = len(seen)
                    await ctx.send(f"✅ Queued **{added}** unique songs from the playlist!")

                await asyncio.sleep(4)

                if not ctx.voice_client.is_playing() and queues[guild_id]:
                    await play_next(ctx)

                return

            except Exception as e:
                if 'driver' in locals():
                    driver.quit()
                await ctx.send(f"Scraper error: {str(e)[:180]}\nTry again or manual search.")
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
    ctx.voice_client.stop()
    await ctx.send("Skipped! ⏭️")

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

    with queue_lock:  # safe read
        q = list(queues[ctx.guild.id])  # copy to avoid race

    total = len(q)
    if total == 0:
        await ctx.send("Queue is empty.")
        return

    msg = f"**Queue:** ({total} songs)\n"
    current_length = len(msg)

    for i, (_, title, requester) in enumerate(q, 1):
        line = f"{i}. {title} | by {requester}\n"
        line_length = len(line)

        # Check if adding this line would exceed 2000 chars
        if current_length + line_length > 2000:
            remaining = total - (i - 1)
            msg += f"... + {remaining} more songs"
            break

        msg += line
        current_length += line_length

    await ctx.send(msg)



bot.run(TOKEN)