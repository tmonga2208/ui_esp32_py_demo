import os
import sys
import time
import threading
import asyncio
import subprocess
from pathlib import Path
from io import BytesIO

import pygame
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, APIC, TIT2, TPE1
from bleak import BleakScanner

# -------------------- USER CONFIG --------------------
SONGS_DIR = os.path.expanduser("./assets/mp3")   # <- the path you gave; change if needed
WIDTH, HEIGHT = 328, 360
FPS = 30
# -----------------------------------------------------

pygame.init()
pygame.mixer.init()

FONT = pygame.font.SysFont("arial", 16)
SMALL = pygame.font.SysFont("arial", 12)
SMALLER = pygame.font.SysFont("arial", 10)

# Colors
WHITE = (255, 255, 255)
LIGHT = (220, 220, 220)
BLACK = (0, 0, 0)
GRAY = (150, 150, 150)
ACCENT = (200, 200, 200)

screen = pygame.display.set_mode((WIDTH, HEIGHT))
pygame.display.set_caption("ESP32 MP3 Player")
clock = pygame.time.Clock()

# -------------------- GLOBAL STATE --------------------
state = {
    "screen": "home",   # home, songs, player, settings, wifi, bluetooth
    "wifi_list": [],
    "bt_list": [],
    "songs": [],        # list of dicts {path, title, artist, albumart_surf}
    "current_index": 0,
    "playing": False,
    "paused": False,
    "song_start_time": 0.0,   # monotonic start
    "song_length": 0.0,
    "last_scan_wifi": 0,
    "last_scan_bt": 0,
    "wifi_error": None,
    "bt_error": None
}

# -------------------- UTIL: WiFi scanning --------------------
def scan_wifi():
    """Scan for WiFi networks using nmcli (Linux/mac) or netsh (Windows).
    Returns list of SSID strings."""
    try:
        if sys.platform.startswith("linux") or sys.platform == "darwin":
            # Try nmcli first
            res = subprocess.run(["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi", "list"],
                                 capture_output=True, text=True, timeout=8)
            out = res.stdout.strip()
            networks = []
            if out:
                for line in out.splitlines():
                    parts = line.split(":")
                    ssid = parts[0].strip()
                    if ssid:
                        networks.append(ssid)
                return networks
            # fallback for macOS: try airport (common path)
            if sys.platform == "darwin":
                try:
                    airport_path = "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport"
                    res = subprocess.run([airport_path, "-s"], capture_output=True, text=True, timeout=8)
                    out = res.stdout.strip()
                    nets = []
                    for line in out.splitlines()[1:]:
                        if line.strip():
                            ssid = line.split()[0]
                            nets.append(ssid)
                    return nets
                except Exception:
                    pass
        elif sys.platform.startswith("win"):
            res = subprocess.run(["netsh", "wlan", "show", "networks", "mode=Bssid"],
                                 capture_output=True, text=True, timeout=8)
            out = res.stdout
            nets = []
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("SSID ") and ":" in line:
                    _, name = line.split(" : ", 1)
                    nets.append(name.strip())
            return nets
    except FileNotFoundError as e:
        state["wifi_error"] = f"WiFi tool not found: {e}"
    except subprocess.TimeoutExpired:
        state["wifi_error"] = "WiFi scan timed out"
    except Exception as e:
        state["wifi_error"] = f"WiFi scan failed: {e}"
    return []

# -------------------- UTIL: Bluetooth scanning (async in thread) --------------------
async def ble_discover_once(timeout=5.0):
    try:
        devices = await BleakScanner.discover(timeout=timeout)
        return [f"{d.name or d.address} ({d.address})" for d in devices]
    except Exception as e:
        raise

def bt_scan_background():
    """Runs in a thread and periodically updates state['bt_list']"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    while True:
        try:
            devices = loop.run_until_complete(ble_discover_once(timeout=5.0))
            state["bt_list"] = devices
            state["bt_error"] = None
        except Exception as e:
            state["bt_error"] = f"Bluetooth scan failed: {e}"
            state["bt_list"] = []
        state["last_scan_bt"] = time.time()
        time.sleep(6)

# start BT scanning thread
bt_thread = threading.Thread(target=bt_scan_background, daemon=True)
bt_thread.start()

# -------------------- UTIL: MP3 loading & metadata --------------------
def load_songs_from_folder(path):
    songs = []
    p = Path(path)
    if not p.exists() or not p.is_dir():
        print(f"Songs folder not found: {path}")
        return songs
    for file in sorted(p.iterdir()):
        if file.suffix.lower() in (".mp3", ".wav", ".ogg", ".flac"):
            meta = {"path": str(file), "title": file.stem, "artist": "", "albumart_surf": None, "length": 0.0}
            try:
                if file.suffix.lower() == ".mp3":
                    audio = MP3(str(file))
                    meta["length"] = audio.info.length if audio.info else 0.0
                    try:
                        id3 = ID3(str(file))
                        if "TIT2" in id3:
                            meta["title"] = id3["TIT2"].text[0]
                        if "TPE1" in id3:
                            meta["artist"] = id3["TPE1"].text[0]
                        # APIC = album art
                        apic_frames = [f for f in id3.values() if isinstance(f, APIC)]
                        if apic_frames:
                            apic = apic_frames[0]
                            img_data = apic.data
                            # create pygame surface from bytes
                            try:
                                import io
                                image_file = BytesIO(img_data)
                                album_surf = pygame.image.load(image_file).convert_alpha()
                                # scale to square
                                album_surf = pygame.transform.smoothscale(album_surf, (140,140))
                                meta["albumart_surf"] = album_surf
                            except Exception:
                                meta["albumart_surf"] = None
                    except Exception:
                        # no ID3 or can't parse
                        pass
                else:
                    # For non-mp3, attempt to get length via pygame mixer Sound
                    try:
                        snd = pygame.mixer.Sound(str(file))
                        meta["length"] = snd.get_length()
                    except Exception:
                        pass
            except Exception as e:
                print("Error reading metadata:", e)
            songs.append(meta)
    return songs

# load songs now
state["songs"] = load_songs_from_folder(SONGS_DIR)

# -------------------- AUDIO CONTROL --------------------
def play_song(index):
    if not state["songs"]:
        return
    index = index % len(state["songs"])
    song = state["songs"][index]
    try:
        pygame.mixer.music.load(song["path"])
        pygame.mixer.music.play()
        state["current_index"] = index
        state["playing"] = True
        state["paused"] = False
        state["song_start_time"] = time.monotonic()
        state["song_length"] = song.get("length") or 0.0
    except Exception as e:
        print("Failed to play:", e)

def toggle_play_pause():
    if not state["playing"]:
        play_song(state["current_index"])
    else:
        if state["paused"]:
            pygame.mixer.music.unpause()
            state["paused"] = False
            state["song_start_time"] = time.monotonic() - get_playback_position()
        else:
            pygame.mixer.music.pause()
            state["paused"] = True

def stop_playback():
    pygame.mixer.music.stop()
    state["playing"] = False
    state["paused"] = False

def next_song():
    idx = (state["current_index"] + 1) % max(1, len(state["songs"]))
    play_song(idx)

def prev_song():
    idx = (state["current_index"] - 1) % max(1, len(state["songs"]))
    play_song(idx)

def get_playback_position():
    """
    Return seconds played for current song based on mixer position.
    pygame.mixer.music.get_pos() returns ms since play (or -1)
    """
    pos_ms = pygame.mixer.music.get_pos()
    if pos_ms == -1:
        # maybe stopped, fallback to time difference
        if state["playing"] and not state["paused"]:
            return time.monotonic() - state["song_start_time"]
        return 0.0
    return max(0.0, pos_ms / 1000.0)

# -------------------- UI: Components --------------------
def draw_header():
    t = FONT.render("12:30", True, WHITE)
    screen.blit(t, (10, 6))
    # status icons placeholder (wifi/bluetooth)
    pygame.draw.circle(screen, LIGHT, (WIDTH-20, 12), 5)

def draw_button(rect, text, small=False):
    pygame.draw.rect(screen, LIGHT, rect, border_radius=8)
    txt = SMALL.render(text, True, BLACK) if small else FONT.render(text, True, BLACK)
    screen.blit(txt, (rect.x + 8, rect.y + (rect.height - txt.get_height()) // 2))

def draw_list_item(y, text):
    rect = pygame.Rect(20, y, WIDTH - 40, 40)
    pygame.draw.rect(screen, LIGHT, rect, border_radius=8)
    txt = SMALL.render(text, True, BLACK)
    screen.blit(txt, (rect.x + 10, rect.y + 10))
    return rect

# bottom nav
def draw_bottom_nav():
    nav_h = 48
    nav_rect = pygame.Rect(0, HEIGHT - nav_h, WIDTH, nav_h)
    pygame.draw.rect(screen, BLACK, nav_rect)
    names = [("home", "Home"), ("songs", "Songs"), ("settings", "Settings")]
    w = WIDTH // len(names)
    for i, (key, label) in enumerate(names):
        x = i * w
        rect = pygame.Rect(x, HEIGHT - nav_h, w, nav_h)
        txt = SMALL.render(label, True, WHITE if state["screen"]==key else GRAY)
        screen.blit(txt, (rect.x + (w - txt.get_width())//2, rect.y + 12))

# -------------------- PYGAME SCREENS --------------------
def screen_home(events):
    draw_header()
    # icons
    s_rect = pygame.Rect(50, 80, 60, 60)
    pygame.draw.rect(screen, LIGHT, s_rect, border_radius=10)
    stxt = SMALL.render("Settings", True, BLACK)
    screen.blit(stxt, (s_rect.x + 4, s_rect.y + s_rect.height + 6))

    m_rect = pygame.Rect(160, 80, 60, 60)
    pygame.draw.rect(screen, LIGHT, m_rect, border_radius=10)
    mtxt = SMALL.render("Songs", True, BLACK)
    screen.blit(mtxt, (m_rect.x + 10, m_rect.y + m_rect.height + 6))

    for e in events:
        if e.type == pygame.MOUSEBUTTONDOWN:
            if s_rect.collidepoint(e.pos):
                state["screen"] = "settings"
            if m_rect.collidepoint(e.pos):
                state["screen"] = "songs"

def screen_settings(events):
    draw_header()
    draw_button(pygame.Rect(20, 40, WIDTH - 40, 40), "Wifi Settings")
    draw_button(pygame.Rect(20, 100, WIDTH - 40, 40), "Bluetooth Settings")
    for e in events:
        if e.type == pygame.MOUSEBUTTONDOWN:
            if pygame.Rect(20, 40, WIDTH - 40, 40).collidepoint(e.pos):
                state["screen"] = "wifi"
            if pygame.Rect(20, 100, WIDTH - 40, 40).collidepoint(e.pos):
                state["screen"] = "bluetooth"

def screen_wifi(events):
    draw_header()
    pygame.draw.rect(screen, LIGHT, pygame.Rect(20, 40, WIDTH-40, 40), border_radius=8)
    txt = SMALL.render("Wifi Settings", True, BLACK)
    screen.blit(txt, (30, 50))

    y = 100
    if state["wifi_error"]:
        err = SMALL.render("Error: " + str(state["wifi_error"]), True, (255,100,100))
        screen.blit(err, (20, y))
    else:
        # if older than 6s, rescan
        if time.time() - state["last_scan_wifi"] > 6:
            try:
                state["wifi_list"] = scan_wifi()
                state["wifi_error"] = None
            except Exception as e:
                state["wifi_error"] = str(e)
        if state["wifi_list"]:
            for ssid in state["wifi_list"][:6]:
                draw_list_item(y, ssid)
                y += 50
        else:
            info = SMALL.render("No networks found yet. (Ensure nmcli/netsh available)", True, GRAY)
            screen.blit(info, (20, y))

def screen_bluetooth(events):
    draw_header()
    pygame.draw.rect(screen, LIGHT, pygame.Rect(20, 40, WIDTH-40, 40), border_radius=8)
    txt = SMALL.render("Bluetooth Settings", True, BLACK)
    screen.blit(txt, (30, 50))

    y = 100
    if state["bt_error"]:
        err = SMALL.render("Error: " + str(state["bt_error"]), True, (255,100,100))
        screen.blit(err, (20, y))
    else:
        if time.time() - state["last_scan_bt"] > 6:
            # scanning runs in background thread; we just show latest
            pass
        if state["bt_list"]:
            for dev in state["bt_list"][:6]:
                draw_list_item(y, dev)
                y += 50
        else:
            info = SMALL.render("No devices found yet. (Ensure Bluetooth is on)", True, GRAY)
            screen.blit(info, (20, y))

def screen_songs(events):
    draw_header()
    y = 40
    if not state["songs"]:
        info = SMALL.render(f"No songs found in {SONGS_DIR}", True, (255,100,100))
        screen.blit(info, (20, 60))
    else:
        for i, s in enumerate(state["songs"]):
            r = draw_list_item(40 + i*50, f"{s['title']}  —  {s.get('artist','')}")
            for e in events:
                if e.type == pygame.MOUSEBUTTONDOWN and r.collidepoint(e.pos):
                    play_song(i)
                    state["screen"] = "player"

def draw_player_controls():
    # Prev
    pygame.draw.polygon(screen, WHITE, [(80, 300), (92, 292), (92, 308)])
    # Play / Pause button circle
    pygame.draw.circle(screen, WHITE, (WIDTH//2, 300), 24)
    # play triangle or pause bars
    if pygame.mixer.music.get_busy() and not state["paused"]:
        # draw pause
        pygame.draw.rect(screen, BLACK, (WIDTH//2 - 7, 292, 5, 16))
        pygame.draw.rect(screen, BLACK, (WIDTH//2 + 2, 292, 5, 16))
    else:
        # draw play triangle
        pygame.draw.polygon(screen, BLACK, [(WIDTH//2 - 6, 292), (WIDTH//2 + 10, 300), (WIDTH//2 - 6, 308)])
    # Next
    pygame.draw.polygon(screen, WHITE, [(WIDTH-80, 292), (WIDTH-92, 300), (WIDTH-80, 308)])

def screen_player(events):
    draw_header()
    # album art
    cur = state["songs"][state["current_index"]] if state["songs"] else None
    if cur:
        if cur.get("albumart_surf"):
            album = cur["albumart_surf"]
        else:
            album = pygame.Surface((140, 140))
            album.fill(LIGHT)
        screen.blit(album, (94, 60))

        # title / artist
        title = FONT.render(cur.get("title", "Unknown"), True, WHITE)
        artist = SMALL.render(cur.get("artist", ""), True, GRAY)
        screen.blit(title, (WIDTH//2 - title.get_width()//2, 220))
        screen.blit(artist, (WIDTH//2 - artist.get_width()//2, 245))

        # playback progress
        total = state.get("song_length") or cur.get("length") or 0.0
        pos = get_playback_position()
        # clamp
        if total > 0 and pos > total + 0.3:
            # track ended -> Auto next
            next_song()
            pos = 0
            total = state.get("song_length") or cur.get("length") or 0.0

        bar_rect = pygame.Rect(20, 270, WIDTH - 40, 6)
        pygame.draw.rect(screen, GRAY, bar_rect, border_radius=3)
        if total > 0:
            frac = min(1.0, pos / total)
            fill_rect = pygame.Rect(bar_rect.x, bar_rect.y, int(bar_rect.width * frac), bar_rect.height)
            pygame.draw.rect(screen, WHITE, fill_rect, border_radius=3)

        # time text
        tcur = time.strftime("%M:%S", time.gmtime(int(pos)))
        ttot = time.strftime("%M:%S", time.gmtime(int(total)))
        time_txt = SMALL.render(f"{tcur} / {ttot}", True, LIGHT)
        screen.blit(time_txt, (WIDTH//2 - time_txt.get_width()//2, 280))

        # controls
        draw_player_controls()

    else:
        info = SMALL.render("No song loaded", True, GRAY)
        screen.blit(info, (20, 80))

    # handle clicks on controls
    for e in events:
        if e.type == pygame.MOUSEBUTTONDOWN:
            mx,my = e.pos
            # prev area
            if 60 < mx < 110 and 280 < my < 320:
                prev_song()
            # center play area
            if WIDTH//2 - 30 < mx < WIDTH//2 + 30 and 272 < my < 328:
                toggle_play_pause()
            # next area
            if WIDTH-110 < mx < WIDTH-60 and 280 < my < 320:
                next_song()

# -------------------- MAIN LOOP --------------------
def main_loop():
    running = True
    while running:
        events = pygame.event.get()
        for e in events:
            if e.type == pygame.QUIT:
                running = False
            if e.type == pygame.MOUSEBUTTONDOWN:
                # bottom nav
                if e.pos[1] > HEIGHT - 48:
                    w = WIDTH // 3
                    idx = e.pos[0] // w
                    if idx == 0:
                        state["screen"] = "home"
                    elif idx == 1:
                        state["screen"] = "songs"
                    else:
                        state["screen"] = "settings"

        screen.fill(BLACK)

        if state["screen"] == "home":
            screen_home(events)
        elif state["screen"] == "settings":
            screen_settings(events)
        elif state["screen"] == "wifi":
            screen_wifi(events)
        elif state["screen"] == "bluetooth":
            screen_bluetooth(events)
        elif state["screen"] == "songs":
            screen_songs(events)
        elif state["screen"] == "player":
            screen_player(events)
        else:
            screen_home(events)

        draw_bottom_nav()

        pygame.display.flip()
        clock.tick(FPS)

    pygame.quit()
    sys.exit()

if __name__ == "__main__":
    print("Loaded songs:", len(state["songs"]))
    main_loop()