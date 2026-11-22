"""Spotify/Spotifast lyrics overlay: click-through, always on top, Spicy Lyrics-style.

Run:   pythonw lyrics_overlay.py      (size, side, hide and quit live in the tray icon)
Check: python lyrics_overlay.py --selftest
Game must run in borderless/windowed mode; exclusive fullscreen hides overlays.

Lyrics: Better Lyrics API (Apple Music word/syllable timing, background vocals, duets),
falling back to LRCLIB (line timing; words estimated).
"""
import asyncio
import os
import subprocess
import tempfile
import zipfile
import ctypes
import json
import logging
import math
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

import requests
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import (QActionGroup, QColor, QFont, QFontMetricsF, QIcon, QImage, QLinearGradient, QRadialGradient,
                           QPainter, QPainterPath, QPixmap)
from PySide6.QtWidgets import (QApplication, QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene,
                               QHBoxLayout, QLabel, QMenu, QSlider, QMessageBox, QSystemTrayIcon, QVBoxLayout, QWidget,
                               QWidgetAction)

# ---- tweak me ----
WIDTH, HEIGHT = 760, 640  # at size 100%
EDGE_FADE = 120           # px; lines fade to fully transparent before touching the top/bottom edge
FONT, FONT_PX = "Segoe UI", 40
PAD, GAP = 24, 22         # side padding, space between lines
ANCHOR = 0.38             # current line sits this far down the window
INACTIVE_SCALE = 0.82     # other lines are drawn smaller
BG_SCALE = 0.68           # background vocals / ad-libs, relative to a main line
LETTER_POP = 0.13         # how much the letter being sung grows
WAVE = 3.0                # swell width in letters' worth of time: scales with singing speed (bigger = smoother)
WAVE_MIN = 0.12           # seconds; floor so fast words still swell smoothly
GLOW_PAD = 22             # room around each word for the bloom to spread
GLOW_LAYERS = ((14, 0.22), (5, 0.32))  # (blur radius px, strength): soft haze + faint core
INTERLUDE_MIN = 4.0       # seconds of no singing before the ... dots appear
LONG_INTERLUDE = 7.0      # gaps at least this long hide all lyrics until the next line is near
LYRIC_FADE_IN = 1.5       # seconds before the next line that it fades back in
DOT_H = 40
TOP_Y = EDGE_FADE + 20    # after a long gap, the dots/new lyrics start this far from the top
SETTINGS = Path.home() / "AppData" / "Roaming" / "LyricsOverlay" / "settings.json"  # tray choices persist here
# offset: seconds, + shows lyrics earlier. backdrop: soft dark haze behind the text for light scenes
DEFAULTS = {"scale": 1.0, "side": "right", "offset": 0.0, "backdrop": False,
            "backdrop_opacity": 0.45}  # darkness where the haze meets the screen edge, 0-1
EDGE = 40                 # window sits flush to the screen edge; this keeps text off the edge
INNER = 150               # extra room on the inner side so the backdrop can fade out before the window ends
HAZE_RES = 8              # backdrop is drawn at 1/8 resolution and blurred (cheap, and smooth anyway)

__version__ = "1.2.0"
REPO = "HayzDev/lyrics-overlay"
UA = {"User-Agent": f"lyrics-overlay/{__version__}"}
log = logging.getLogger("lyrics-overlay")
TT, TTM = "{http://www.w3.org/ns/ttml}", "{http://www.w3.org/ns/ttml#metadata}"
TAG = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")
WTAG = re.compile(r"<(\d+):(\d+(?:\.\d+)?)>")

# words: [[(start, end, syllable), ...], ...]   right: draw right-aligned   bg: background vocal
Line = namedtuple("Line", "start end words right bg")


def make_line(start, words, right=False, bg=False):
    ends = [s[1] for w in words for s in w]
    return Line(start, max(ends, default=start), words, right, bg)


def add_lines(lines, start, words, right=False, bgs=()):
    """Main line + its ad-libs. Parenthesised words count as ad-libs: right side, smaller, no parens."""
    main, ad, inside = [], [], False
    for w in words:
        text = "".join(s[2] for s in w)
        inside = inside or text.startswith("(")
        (ad if inside else main).append(w)
        inside = inside and ")" not in text
    if main or not ad:
        lines.append(make_line(start, main, right))
    for bw in (ad, *bgs):
        if bw:
            bw = [list(w) for w in bw]
            a, z, t = bw[0][0]
            bw[0][0] = (a, z, t.lstrip("("))
            a, z, t = bw[-1][-1]
            bw[-1][-1] = (a, z, t.rstrip(")"))
            lines.append(make_line(bw[0][0][0], bw, right=True, bg=True))


def add_interludes(lines):
    """Empty lines dropped; gaps with no singing become dot lines (Line with no words)."""
    out, end = [], 0.0
    for ln in lines:
        if not ln.words:
            continue
        if not ln.bg and ln.start - end >= INTERLUDE_MIN:
            out.append(Line(end, ln.start, [], False, False))
        out.append(ln)
        end = max(end, ln.end)
    return out


def smooth(t):
    return t * t * (3 - 2 * t)


def estimate(t, nxt, text):
    # ponytail: line-only timing, so the line's time is spread over its words by length.
    toks = text.split()
    dur = min((nxt - t) * 0.95, 0.09 * len(text) + 0.5)
    weight = sum(len(w) + 1 for w in toks) or 1
    words, c = [], t
    for w in toks:
        d = dur * (len(w) + 1) / weight
        words.append([(c, c + d, w)])
        c += d
    return words


def secs(m, s):
    return int(m) * 60 + float(s)


def parse_lrc(lrc, total):
    rows = []
    for row in lrc.splitlines():
        text = TAG.sub("", row).strip()
        rows += [(secs(m, s), text) for m, s in TAG.findall(row)]
    rows.sort(key=lambda r: r[0])
    lines = []
    for i, (t, text) in enumerate(rows):
        nxt = rows[i + 1][0] if i + 1 < len(rows) else max(total, t + 4)
        if WTAG.search(text):  # enhanced LRC with real word timings
            parts = WTAG.split(text)  # [pre, m, s, word, m, s, word, ...]
            stamps = [secs(parts[j], parts[j + 1]) for j in range(1, len(parts), 3)]
            texts = [parts[j + 2] for j in range(1, len(parts), 3)]
            ends = stamps[1:] + [min(nxt, stamps[-1] + 2)]
            words = [[(a, b, w.strip())] for a, b, w in zip(stamps, ends, texts) if w.strip()]
        else:
            words = estimate(t, nxt, text)
        add_lines(lines, t, words)
    return lines


def tsec(v):
    s = 0.0
    for part in v.rstrip("s").split(":"):
        s = s * 60 + float(part)
    return s


def group_words(spans):
    """Timed spans -> words. Spans with no whitespace between them are syllables of one word."""
    words, cur = [], []
    for sp in spans:
        if sp.get("begin") is None or not (sp.text or "").strip():
            continue
        cur.append((tsec(sp.get("begin")), tsec(sp.get("end")), sp.text.strip()))
        if sp.tail and sp.tail[:1].isspace():
            words.append(cur)
            cur = []
    return words + [cur] if cur else words


def parse_ttml(ttml):
    lines, agents = [], []
    for p in ET.fromstring(ttml).iter(TT + "p"):
        agent = p.get(TTM + "agent", "v1")
        if agent not in agents:
            agents.append(agent)
        right = agents.index(agent) % 2 == 1  # second singer goes on the right
        spans = [c for c in p if c.tag == TT + "span"]
        main = [c for c in spans if c.get(TTM + "role") != "x-bg"]
        start = tsec(p.get("begin"))
        words = group_words(main) if spans else estimate(start, tsec(p.get("end")), "".join(p.itertext()))
        bgs = [group_words([c for c in b if c.tag == TT + "span"])
               for b in spans if b.get(TTM + "role") == "x-bg"]
        add_lines(lines, start, words, right, bgs)
    return lines


def fetch_lyrics(title, artist, album, duration):
    # Better Lyrics: Apple Music syllable timing; only songs already in its cache, and the cache is keyed
    # on exact text, so also try fewer artists ("A, B" -> "A"; split on ", " is ambiguous for names like
    # "Tyler, The Creator", hence every prefix) and the title without "(feat. ...)".
    parts = artist.split(", ")
    artists = [", ".join(parts[:n]) for n in range(len(parts), 0, -1)]
    titles = dict.fromkeys([title, re.sub(r"\s*[(\[](feat|ft|with)\b[^)\]]*[)\]]", "", title, flags=re.I)])
    for t in titles:
        for a in artists:
            try:
                r = requests.get("https://lyrics-api.boidu.dev/getLyrics", headers=UA, timeout=8,
                                 params=dict(s=t, a=a, d=round(duration)))
                if r.ok and r.json().get("ttml"):
                    lines = parse_ttml(r.json()["ttml"])
                    if lines:
                        return add_interludes(lines)
            except (requests.RequestException, ValueError, ET.ParseError, TypeError, AttributeError) as e:
                log.warning("better lyrics: %s", e)
    try:  # LRCLIB: line timing
        api = "https://lrclib.net/api/"
        r = requests.get(api + "get", headers=UA, timeout=8, params=dict(
            track_name=title, artist_name=artist, album_name=album, duration=round(duration)))
        lrc = r.json().get("syncedLyrics") if r.ok else None
        if not lrc:
            r = requests.get(api + "search", headers=UA, timeout=8,
                             params=dict(track_name=title, artist_name=artist))
            hits = [h for h in r.json() if h.get("syncedLyrics")] if r.ok else []
            hits.sort(key=lambda h: abs((h.get("duration") or 0) - duration))
            lrc = hits[0]["syncedLyrics"] if hits else None
        return add_interludes(parse_lrc(lrc, duration)) if lrc else None
    except (requests.RequestException, ValueError):
        return None


class Player:
    """Spotify state read from Windows media controls (no Spotify login needed)."""

    def __init__(self):
        self.key, self.lines, self.status = None, [], ""
        self.pos, self.stamp, self.playing = 0.0, time.monotonic(), False

    def position(self):
        return self.pos + (time.monotonic() - self.stamp if self.playing else 0.0)

    def sync(self, pos, playing):
        if playing != self.playing or abs(pos - self.position()) > 0.35:  # avoid jitter
            self.pos, self.stamp = pos, time.monotonic()
        self.playing = playing


async def poll(p):
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status)
    mgr = await MediaManager.request_async()
    cache, missing_since = {}, None  # lyrics per track, so a metadata blip never refetches
    while True:
        try:
            ss = [s for s in mgr.get_sessions() if "spoti" in s.source_app_user_model_id.lower()]
            # several sessions (e.g. Spotify + Spotifast): prefer whichever is playing, so we don't flip-flop
            s = next((s for s in ss if s.get_playback_info().playback_status == Status.PLAYING), ss[0] if ss else None)
            if not s:
                missing_since = missing_since or time.monotonic()
                if p.key and time.monotonic() - missing_since > 5:  # ride out brief session dropouts
                    log.info("player gone, clearing")
                    p.key, p.lines, p.status, p.playing = None, [], "", False
            else:
                missing_since = None
                props = await s.try_get_media_properties_async()
                tl, info = s.get_timeline_properties(), s.get_playback_info()
                dur = tl.end_time.total_seconds()
                key = (props.title, props.artist, props.album_title)
                if key != p.key and props.title:
                    log.info("track %r (was %r) from %s", key, p.key, s.source_app_user_model_id)
                    p.key, p.status = key, f"{props.title} — {props.artist}"
                    if key not in cache:
                        p.lines = []
                        cache[key] = await asyncio.to_thread(fetch_lyrics, *key, dur) or []
                        log.info("lyrics: %d lines", len(cache[key]))
                    if p.key == key:
                        p.lines = cache[key]
                        if not p.lines:
                            p.status += "   ·   no synced lyrics"
                    continue  # re-read the timeline after the fetch
                playing = info.playback_status == Status.PLAYING
                pos = tl.position.total_seconds()
                if playing:
                    pos += (datetime.now(timezone.utc) - tl.last_updated_time).total_seconds()
                p.sync(pos, playing)
        except Exception:  # player closing mid-call etc.; keep polling
            log.exception("poll")
        await asyncio.sleep(0.25)


def blur(img, radius):
    scene = QGraphicsScene()
    item = QGraphicsPixmapItem(QPixmap.fromImage(img))
    fx = QGraphicsBlurEffect()
    fx.setBlurRadius(radius)
    fx.setBlurHints(QGraphicsBlurEffect.QualityHint)
    item.setGraphicsEffect(fx)
    scene.addItem(item)
    out = QImage(img.size(), QImage.Format_ARGB32_Premultiplied)
    out.fill(Qt.transparent)
    qp = QPainter(out)
    rect = QRectF(img.rect())
    scene.render(qp, rect, rect)
    qp.end()
    return out


def clamp01(v):
    return min(1.0, max(0.0, v))


class Overlay(QWidget):
    def __init__(self, player):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.p = player
        try:
            self.cfg = {**DEFAULTS, **json.loads(SETTINGS.read_text())}
        except (OSError, ValueError):
            self.cfg = dict(DEFAULTS)
        self.place()
        self.qfont = QFont(FONT)
        self.qfont.setPixelSize(FONT_PX)
        self.qfont.setWeight(QFont.Bold)
        self.fm = QFontMetricsF(self.qfont)
        self.lines, self.layout, self.scroll = None, [], 0.0
        self.glows = {}  # letter -> pre-blurred bloom image
        self.last = time.monotonic()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update)
        self.timer.start(16)
        self.reveal = 0.0  # 0 = song title showing, 1 = lyrics showing; eased between

    def place(self):
        s = self.cfg["scale"]
        w, h = int((WIDTH + EDGE + INNER) * s), int(HEIGHT * s)
        g = QApplication.primaryScreen().availableGeometry()
        self.resize(w, h)
        self.move(g.x() if self.cfg["side"] == "left" else g.x() + g.width() - w, g.center().y() - h // 2)

    def set(self, **kw):
        self.cfg.update(kw)
        self.place()
        try:
            SETTINGS.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS.write_text(json.dumps(self.cfg))
        except OSError:
            pass

    def relayout(self, lines):
        self.lines, self.layout, self.scroll, self.pinned = lines, [], 0.0, False
        self.reveal = 0.0  # new lyrics fade in
        sp, lh, asc = self.fm.horizontalAdvance(" "), self.fm.height(), self.fm.ascent()
        maxw = WIDTH - 2 * PAD
        for ln in lines:
            if not ln.words:  # interlude dots
                self.layout.append([ln, [], DOT_H, 0.0, 0.0, (-10.0, 90.0)])
                continue
            items, rows, x = [], [0.0], 0.0
            for word in ln.words:
                text = "".join(s[2] for s in word)
                wd = self.fm.horizontalAdvance(text)
                if x and x + wd > maxw:
                    rows.append(0.0)
                    x = 0.0
                path = QPainterPath()
                path.addText(0, 0, self.qfont, text)
                chars, prefix = [], ""
                for a, b, syl in word:
                    for k, ch in enumerate(syl):  # each letter gets an equal slice of its syllable
                        cp = QPainterPath()
                        cp.addText(0, 0, self.qfont, ch)
                        chars.append((cp, self.fm.horizontalAdvance(prefix), self.fm.horizontalAdvance(ch),
                                      a + (b - a) * k / len(syl), a + (b - a) * (k + 1) / len(syl)))
                        prefix += ch
                items.append([text, path, x, len(rows) - 1, wd, chars, word[0][0], word[-1][1]])
                x += wd + sp
                rows[-1] = x - sp
            for it in items:  # right-align rows of right-side lines, row index -> baseline y
                if ln.right:
                    it[2] += maxw - rows[it[3]]
                it[3] = it[3] * lh + asc
            ext = (min(it[2] for it in items), max(it[2] + it[4] for it in items))  # x extent, for the backdrop
            self.layout.append([ln, items, len(rows) * lh, INACTIVE_SCALE, 0.0, ext])  # + anim scale/alpha

    def paintEvent(self, _):
        now = time.monotonic()
        dt = min(now - self.last, 0.1)
        k = 1 - math.exp(-dt * 9)  # frame-rate independent smoothing
        self.last = now
        qp = QPainter(self)
        qp.setRenderHint(QPainter.Antialiasing)
        qp.scale(self.cfg["scale"], self.cfg["scale"])
        lines = self.p.lines
        if lines is not self.lines:
            self.relayout(lines)
        qp.translate(EDGE if self.cfg["side"] == "left" else INNER, 0)
        self.reveal += ((1.0 if lines else 0.0) - self.reveal) * (1 - math.exp(-dt * 3.5))
        shown = smooth(clamp01(self.reveal))
        haze = []  # (x0, y0, x1, y1, strength) behind whatever text is visible
        if self.p.status and shown < 0.99:
            haze.append((PAD, HEIGHT * ANCHOR - 26, PAD + 20 + 9 * len(self.p.status), HEIGHT * ANCHOR + 8,
                         1 - shown))
        if not lines:
            self.draw_haze(qp, haze)
        if self.p.status and shown < 0.99:  # title fades out (drifting up) as the lyrics fade in
            f = QFont(FONT)
            f.setPixelSize(20)
            qp.setFont(f)
            qp.setPen(QColor(255, 255, 255, int(140 * (1 - shown))))
            qp.drawText(QPointF(PAD, HEIGHT * ANCHOR - 12 * shown), self.p.status)
        if not lines:
            return
        pos = self.p.position() + self.cfg["offset"]
        anchor = max((i for i, st in enumerate(self.layout) if not st[0].bg and st[0].start <= pos), default=-1)
        # latest long instrumental gap that has started (it shapes scrolling until the lyrics catch up)
        li = max((i for i, st in enumerate(self.layout) if not st[0].words and st[0].start <= pos
                  and st[0].end - st[0].start >= LONG_INTERLUDE), default=-1)

        ys, ons, y, veil = [], [], 0.0, 0.0
        for i, st in enumerate(self.layout):
            ln = st[0]
            if not ln.words:  # dots: grow in for the gap, collapse away after
                on = ln.start <= pos < ln.end
                if on and ln.end - ln.start >= LONG_INTERLUDE:  # long gap: lyrics fade out, then back in
                    veil = smooth(clamp01(pos - ln.start)) * smooth(clamp01((ln.end - pos) / LYRIC_FADE_IN))
                st[3] += ((1.0 if on else 0.0) - st[3]) * k
                st[4] += ((1.0 if on else 0.0) - st[4]) * k
            else:
                # several lines can be live at once (overlaps, ad-libs); the anchor stays lit until the next
                on = ln.start <= pos and (pos < ln.end + 0.5 or i == anchor)
                d = abs(i - max(anchor, 0))
                # only the newest line is full size: the previous one shrinks the moment the next starts
                # (it can still finish its highlight), so heights change once, together
                big = i == anchor or (ln.bg and on)
                st[3] += ((1.0 if big else INACTIVE_SCALE) - st[3]) * k
                target = 1.0 if on else max(0.1, min(0.5, 0.62 - 0.12 * d))
                if i < li and self.pinned:  # lyrics from before a long gap stay hidden
                    target = 0.0
                st[4] += (target - st[4]) * k
            ys.append(y)
            ons.append(on)
            next_bg = i + 1 < len(self.layout) and self.layout[i + 1][0].bg
            gap = (GAP * 0.25 if next_bg else GAP) * (st[3] if not ln.words else 1)
            y += st[2] * st[3] * (BG_SCALE if ln.bg else 1) + gap
        target = ys[max(anchor, 0)]
        self.pinned = False
        if li >= 0:
            gap = self.layout[li][0]
            top = HEIGHT * ANCHOR + ys[li] - TOP_Y  # scroll that puts the dots at the top
            if pos < gap.end:  # during the gap: dots centered, easing to the top as the lyrics approach
                mid = HEIGHT * ANCHOR + ys[li] + DOT_H / 2 - HEIGHT / 2
                target = mid + (top - mid) * smooth(clamp01(1 - (gap.end - pos) / LYRIC_FADE_IN))
                self.pinned = True
            elif top > target:  # after: lyrics play down from the top until they reach the anchor
                target = top
                self.pinned = True
        self.scroll += (target - self.scroll) * k

        maxw = WIDTH - 2 * PAD
        visible = []
        for i, (ln, items, h, s, a, (x0, x1)) in enumerate(self.layout):
            s *= BG_SCALE if ln.bg else 1
            top = HEIGHT * ANCHOR + ys[i] - self.scroll
            if top > HEIGHT or top + h * s < 0:
                continue
            top += 16 * (1 - shown)  # lyrics rise into place as they fade in
            # fade by the line's own top/bottom (+ room for lift and glow), so nothing is ever clipped
            alpha = a * shown * smooth(clamp01(min(top - 12, HEIGHT - (top + h * s) - 12) / EDGE_FADE))
            if ln.words:
                alpha *= 1 - veil
                top += 14 * veil  # hidden lyrics sit slightly low, so they rise as they fade back in
            sx = (lambda x: PAD + maxw + (x - maxw) * s) if ln.right else (lambda x: PAD + x * s)
            haze.append((sx(x0), top, sx(x1), top + h * s, alpha ** 0.6))
            visible.append((i, ln, items, h, s, top, alpha))
        self.draw_haze(qp, haze)

        for i, ln, items, h, s, top, alpha in visible:
            qp.save()
            if not ln.words:
                qp.translate(PAD, top + h * s / 2)
                qp.scale(s, s)
                self.draw_dots(qp, ln, pos, alpha)
                qp.restore()
                continue
            if ln.right:  # scale toward the right edge
                qp.translate(PAD + maxw, top)
                qp.scale(s, s)
                qp.translate(-maxw, 0)
            else:
                qp.translate(PAD, top)
                qp.scale(s, s)
            for it in items:
                self.draw_word(qp, it, pos, alpha, ons[i])
            qp.restore()

    def draw_haze(self, qp, rects):
        """Optional dark backdrop shaped like the visible lyrics: a soft rounded patch behind each line,
        weighted by how visible the line is, blurred together so it reads as a dull haze, not boxes."""
        if not (self.cfg["backdrop"] and rects):
            return
        x_off = EDGE if self.cfg["side"] == "left" else INNER  # haze image covers the whole window
        full_w = WIDTH + EDGE + INNER
        img = QImage(full_w // HAZE_RES, HEIGHT // HAZE_RES, QImage.Format_ARGB32_Premultiplied)
        img.fill(Qt.transparent)
        m = QPainter(img)
        m.setRenderHint(QPainter.Antialiasing)
        m.setPen(Qt.NoPen)
        m.scale(1 / HAZE_RES, 1 / HAZE_RES)
        m.translate(x_off, 0)
        for x0, y0, x1, y1, strength in rects:
            m.setBrush(QColor(0, 0, 0, int(255 * min(1.0, self.cfg["backdrop_opacity"] * 1.4) * clamp01(strength))))
            m.drawRoundedRect(QRectF(x0 - 50, y0 - 24, x1 - x0 + 100, y1 - y0 + 48), 48, 48)  # overlap -> merge
        m.end()
        qp.save()
        qp.translate(-x_off, 0)
        qp.setRenderHint(QPainter.SmoothPixmapTransform)
        qp.drawImage(QRectF(0, 0, full_w, HEIGHT), blur(blur(img, 9), 9))  # twice: no visible edges
        qp.restore()

    def draw_dots(self, qp, ln, pos, alpha):
        q = clamp01((pos - ln.start) / max(ln.end - ln.start, 0.01))
        breathe = 1 + 0.1 * math.sin(pos * 2.6)
        qp.setPen(Qt.NoPen)
        for j in range(3):  # each dot fills over a third of the gap
            f = smooth(clamp01(q * 3 - j))
            c, r = QPointF(10 + j * 32, 0), 7.5 * breathe * (0.85 + 0.2 * f)
            if f > 0:  # faint halo
                g = QRadialGradient(c, r * 2.8)
                g.setColorAt(0, QColor(255, 255, 255, int(60 * f * alpha)))
                g.setColorAt(1, QColor(255, 255, 255, 0))
                qp.setBrush(g)
                qp.drawEllipse(c, r * 2.8, r * 2.8)
            qp.setBrush(QColor(255, 255, 255, int(255 * alpha * (0.3 + 0.7 * f))))
            qp.drawEllipse(c, r, r)

    def make_glow(self, path, wd):
        size = (int(wd) + 2 * GLOW_PAD, int(self.fm.height()) + 2 * GLOW_PAD)
        src = QImage(*size, QImage.Format_ARGB32_Premultiplied)
        src.fill(Qt.transparent)
        qp = QPainter(src)
        qp.setRenderHint(QPainter.Antialiasing)
        qp.fillPath(path.translated(GLOW_PAD, self.fm.ascent() + GLOW_PAD), Qt.white)
        qp.end()
        out = QImage(*size, QImage.Format_ARGB32_Premultiplied)
        out.fill(Qt.transparent)
        qp = QPainter(out)
        for radius, strength in GLOW_LAYERS:
            qp.setOpacity(strength)
            qp.drawImage(0, 0, blur(src, radius))
        qp.end()
        return out

    def draw_word(self, qp, it, pos, alpha, on):
        text, path, x, y, wd, chars, t0, t1 = it
        hi = QColor(255, 255, 255, int(255 * alpha))
        lo = QColor(255, 255, 255, int(255 * alpha * 0.4))
        shadow = QColor(0, 0, 0, int(70 * alpha))  # readability on bright scenes
        if not on or not chars:
            qp.fillPath(path.translated(x, y + 2), shadow)
            qp.fillPath(path.translated(x, y), hi)
            return

        # bloom: ramps in as the word starts, holds while sung, fades 0.6s after
        glow = 0.0
        if pos >= t0:
            glow = min(1.0, (pos - t0) / 0.12) if pos < t1 else max(0.0, 1 - (pos - t1) / 0.6)

        for (cp, cx, cw, a, b), ch in zip(chars, text):
            q = clamp01((pos - a) / max(b - a, 0.01))
            dt = pos - (a + b) / 2  # smooth swell that rolls across letters: short rise, longer settle
            width = max(WAVE_MIN, WAVE * (b - a))  # slow letters -> long, slow wave; fast -> tight
            bump = math.exp(-(dt / (width if dt > 0 else width * 0.5)) ** 2)
            qp.save()
            qp.translate(x + cx + cw / 2, y - 2 * bump - glow)
            sc = 1 + LETTER_POP * bump
            qp.scale(sc, sc)
            qp.translate(-cw / 2, 0)
            qp.fillPath(cp.translated(0, 2), shadow)
            if glow > 0 and q > 0:  # per-letter bloom, inside the letter's transform so it moves with it
                if ch not in self.glows:
                    self.glows[ch] = self.make_glow(cp, cw)
                img = self.glows[ch]
                qp.setOpacity(glow * alpha * smooth(min(1.0, q * 1.5)))  # lights up as the sweep reaches it
                qp.drawImage(QRectF(-GLOW_PAD, -self.fm.ascent() - GLOW_PAD, img.width(), img.height()), img)
                qp.setOpacity(1.0)
            if q <= 0:
                brush = lo
            elif q >= 1:
                brush = hi
            else:  # soft edge sweeping through the letter
                f = max(4.0, cw * 0.8)
                e = -f / 2 + q * (cw + f)
                brush = QLinearGradient(e - f / 2, 0, e + f / 2, 0)
                brush.setColorAt(0, hi)
                brush.setColorAt(1, lo)
            qp.fillPath(cp, brush)
            qp.restore()


def selftest():
    plain = parse_lrc("[00:01.00]hello big world\n[00:05.50]\n[00:06.00]x", 10)
    assert [ln.start for ln in plain] == [1.0, 5.5, 6.0]
    w = plain[0].words
    assert [x[0][2] for x in w] == ["hello", "big", "world"] and w[0][0][0] == 1.0
    assert all(a[0][1] == b[0][0] for a, b in zip(w, w[1:])) and w[-1][0][1] <= 5.5
    assert plain[1].words == []
    enh = parse_lrc("[00:02.00]<00:02.00>Hi <00:02.50>there", 10)
    assert enh[0].words == [[(2.0, 2.5, "Hi")], [(2.5, 4.5, "there")]]
    ttml = ('<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata"><body><div>'
            '<p begin="1:01.5" end="1:04" ttm:agent="v1"><span begin="1:01.5" end="1:02">First</span> '
            '<span ttm:role="x-bg"><span begin="1:02" end="1:02.5">(Hap</span><span begin="1:02.5" '
            'end="1:03">py)</span></span></p>'
            '<p begin="65" end="66" ttm:agent="v2"><span begin="65" end="66">Yo</span></p></div></body></tt>')
    t = parse_ttml(ttml)
    assert t[0] == Line(61.5, 62.0, [[(61.5, 62.0, "First")]], False, False)
    assert t[1] == Line(62.0, 63.0, [[(62.0, 62.5, "Hap"), (62.5, 63.0, "py")]], True, True)
    assert t[2].right and not t[2].bg
    ad = parse_lrc("[00:01.00]<00:01.00>yup <00:01.50>(I'm <00:02.00>thirsty!)", 10)
    assert [(ln.bg, [w[0][2] for w in ln.words]) for ln in ad] == [(False, ["yup"]), (True, ["I'm", "thirsty!"])]
    gaps = add_interludes([make_line(5.0, [[(5.0, 6.0, "a")]]), make_line(6.5, []),
                           make_line(12.0, [[(12.0, 13.0, "b")]]), make_line(13.0, [[(13.0, 14.0, "c")]], True, True)])
    assert [(ln.start, ln.end, bool(ln.words)) for ln in gaps] == [
        (0.0, 5.0, False), (5.0, 6.0, True), (6.0, 12.0, False), (12.0, 13.0, True), (13.0, 14.0, True)]
    assert version_tuple("v1.10.0") > version_tuple("1.9.2") > version_tuple("1.9")
    print("ok")


def tray_icon(n=64):
    pm = QPixmap(n, n)
    pm.fill(Qt.transparent)
    qp = QPainter(pm)
    qp.setRenderHint(QPainter.Antialiasing)
    qp.scale(n / 64, n / 64)
    qp.setBrush(QColor(30, 215, 96))
    qp.setPen(Qt.NoPen)
    qp.drawEllipse(2, 2, 60, 60)
    f = QFont(FONT)
    f.setPixelSize(44)
    f.setWeight(QFont.Bold)
    qp.setFont(f)
    qp.setPen(Qt.white)
    qp.drawText(QRectF(0, 0, 64, 64), Qt.AlignCenter, "♪")
    qp.end()
    return QIcon(pm)


def choice_menu(menu, title, w, key, options):
    sub, grp = menu.addMenu(title), QActionGroup(menu)
    for label, value in options:
        act = sub.addAction(label)
        act.setCheckable(True)
        act.setChecked(w.cfg[key] == value)
        grp.addAction(act)
        act.triggered.connect(lambda _=False, v=value: w.set(**{key: v}))


def slider_action(menu, w, key, lo, hi, scale, text, left="", right=""):
    """Tray-menu slider bound to a setting: applied live while dragging, saved on release."""
    box = QWidget()
    col = QVBoxLayout(box)
    col.setContentsMargins(12, 6, 12, 6)
    label = QLabel()
    slider = QSlider(Qt.Horizontal)
    slider.setRange(lo, hi)
    slider.setMinimumWidth(220)
    row = QHBoxLayout()
    row.addWidget(QLabel(left))
    row.addWidget(slider)
    row.addWidget(QLabel(right))
    col.addWidget(label)
    col.addLayout(row)
    slider.valueChanged.connect(lambda v: label.setText(text(v / scale)))
    slider.valueChanged.connect(lambda v: w.cfg.update({key: v / scale}))
    slider.sliderReleased.connect(lambda: w.set(**{key: slider.value() / scale}))
    slider.setValue(round(w.cfg[key] * scale))
    label.setText(text(slider.value() / scale))
    act = QWidgetAction(menu)
    act.setDefaultWidget(box)
    return act


def check_for_updates():
    """Ask GitHub for the newest release; if newer and confirmed, download it and run its installer
    (which closes this copy, replaces the files and starts the new version; settings are kept)."""
    try:
        r = requests.get(f"https://api.github.com/repos/{REPO}/releases/latest", headers=UA, timeout=10)
        r.raise_for_status()
        rel = r.json()
        latest = rel["tag_name"].lstrip("v")
        asset = next(a["browser_download_url"] for a in rel["assets"] if a["name"].endswith(".zip"))
    except (requests.RequestException, ValueError, KeyError, StopIteration) as e:
        log.warning("update check: %s", e)
        QMessageBox.warning(None, "Lyrics Overlay", f"Couldn't check for updates.\n\n{e}")
        return
    if version_tuple(latest) <= version_tuple(__version__):
        QMessageBox.information(None, "Lyrics Overlay", f"You're on the latest version ({__version__}).")
        return
    if not getattr(sys, "frozen", False):
        QMessageBox.information(None, "Lyrics Overlay", f"Version {latest} is out. You're running from "
                                f"source, so update with git pull instead.")
        return
    ask = QMessageBox.question(None, "Lyrics Overlay", f"Version {latest} is available (you have {__version__})."
                               "\n\nDownload and install it now? The overlay will restart.")
    if ask != QMessageBox.Yes:
        return
    try:
        tmp = Path(tempfile.mkdtemp(prefix="lyrics-overlay-update-"))
        zpath = tmp / "setup.zip"
        with requests.get(asset, headers=UA, timeout=60, stream=True) as d:
            d.raise_for_status()
            with open(zpath, "wb") as fh:
                for chunk in d.iter_content(1 << 16):
                    fh.write(chunk)
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmp / "setup")
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                          "-File", str(tmp / "setup" / "install.ps1")],
                         creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        log.info("updating to %s", latest)
    except (requests.RequestException, OSError, zipfile.BadZipFile) as e:
        log.exception("update failed")
        QMessageBox.warning(None, "Lyrics Overlay", f"Update failed.\n\n{e}")


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def main():
    # one instance only: a second launch exits instead of stacking another overlay
    ctypes.windll.kernel32.CreateMutexW(None, False, "lyrics-overlay-single-instance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)  # log next to settings, fresh each launch
    logging.basicConfig(filename=SETTINGS.with_name("overlay.log"), filemode="w", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    app = QApplication(sys.argv)
    p = Player()
    threading.Thread(target=lambda: asyncio.run(poll(p)), daemon=True).start()
    w = Overlay(p)
    w.show()
    tray = QSystemTrayIcon(tray_icon(), app)
    menu = QMenu()
    choice_menu(menu, "Size", w, "scale",
                (("Tiny (50%)", 0.5), ("Small (75%)", 0.75), ("Normal (100%)", 1.0),
                 ("Large (125%)", 1.25), ("Huge (150%)", 1.5)))
    choice_menu(menu, "Side", w, "side", (("Left", "left"), ("Right", "right")))
    dark = menu.addAction("Dark backdrop (for light scenes)")
    dark.setCheckable(True)
    dark.setChecked(w.cfg["backdrop"])
    dark.toggled.connect(lambda on: w.set(backdrop=on))
    menu.addSeparator()
    menu.addAction(slider_action(menu, w, "offset", -200, 200, 100, lambda v: f"Lyrics timing: {v:+.2f}s",
                                 "later", "earlier"))
    menu.addAction(slider_action(menu, w, "backdrop_opacity", 5, 90, 100,
                                 lambda v: f"Backdrop darkness: {v:.0%}", "light", "dark"))
    menu.addSeparator()
    menu.addAction("Hide / show lyrics", lambda: w.setVisible(not w.isVisible()))
    menu.addAction(f"Check for updates…  (v{__version__})", check_for_updates)
    menu.addSeparator()
    menu.addAction("Quit lyrics overlay", app.quit)
    tray.setContextMenu(menu)
    tray.setToolTip("Lyrics overlay")
    tray.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    elif "--version" in sys.argv:
        print(__version__)
    elif "--make-icon" in sys.argv:  # build step: writes icon.ico for the exe / Start menu
        app = QApplication([])
        tray_icon(256).pixmap(256, 256).save(sys.argv[-1])
    else:
        main()
