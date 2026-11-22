"""Renders the README images into docs/ using original demo lyrics (no real songs, no game art).

    python tools/screenshots.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import lyrics_overlay as L  # noqa: E402
from PIL import Image  # noqa: E402
from PySide6.QtCore import QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QRadialGradient  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

DOCS = ROOT / "docs"


def line(start, end, text, agent="v1", bg=None):
    """Spread a line's time over its words; '-' joins syllables, '~' marks a held (3x) syllable."""
    sylls, weights = [], []
    for word in text.split():
        parts = word.split("-")
        for i, part in enumerate(parts):
            held = part.endswith("~")
            part = part.rstrip("~")
            sylls.append((part, i == len(parts) - 1))
            weights.append(len(part) * (3 if held else 1) + 1)
    t, spans = start, []
    for (part, last), w in zip(sylls, weights):
        d = (end - start) * w / sum(weights)
        spans.append(f'<span begin="{t:.3f}" end="{t + d:.3f}">{part}</span>' + (" " if last else ""))
        t += d
    extra = ""
    if bg:
        b0, b1, btext = bg
        extra = f'<span ttm:role="x-bg">{line(b0, b1, btext)[1]}</span>'
    return agent, "".join(spans) + extra, start, end


DEMO = [
    line(1.0, 3.6, "Neon rivers running through the night"),
    line(3.8, 6.4, "Every heartbeat echoes like a drum", bg=(5.6, 6.9, "(like a drum)")),
    line(7.0, 10.6, "Stay with me to-night~~"),
    line(10.9, 13.2, "I'll be right here waiting", agent="v2"),
    line(13.4, 15.6, "Till the city lights are gone"),
    line(24.0, 26.6, "Morning comes too soon again"),
]


def demo_ttml():
    ps = "".join(f'<p begin="{a:.3f}" end="{b:.3f}" ttm:agent="{ag}">{body}</p>' for ag, body, a, b in DEMO)
    return ('<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttm="http://www.w3.org/ns/ttml#metadata">'
            f"<body><div>{ps}</div></body></tt>")


def scene(w, h, light=False):
    """Abstract stand-in for a game: soft color blobs on a gradient."""
    img = QImage(w, h, QImage.Format_ARGB32)
    qp = QPainter(img)
    qp.setRenderHint(QPainter.Antialiasing)
    g = QLinearGradient(0, 0, w, h)
    stops = ((0, (236, 232, 222)), (0.6, (196, 214, 232)), (1, (250, 248, 244))) if light else \
            ((0, (24, 18, 48)), (0.55, (72, 24, 64)), (1, (16, 44, 70)))
    for at, rgb in stops:
        g.setColorAt(at, QColor(*rgb))
    qp.fillRect(img.rect(), g)
    blobs = ((0.2, 0.3, (255, 245, 220)), (0.75, 0.7, (170, 200, 240))) if light else \
            ((0.25, 0.25, (220, 60, 110)), (0.8, 0.75, (40, 160, 200)), (0.6, 0.15, (120, 60, 220)))
    for fx, fy, rgb in blobs:
        r = QRadialGradient(QPointF(w * fx, h * fy), h * 0.7)
        r.setColorAt(0, QColor(*rgb, 150))
        r.setColorAt(1, QColor(*rgb, 0))
        qp.fillRect(img.rect(), r)
    qp.end()
    return img


class Rig:
    def __init__(self, **cfg):
        self.p = L.Player()
        self.p.lines = L.add_interludes(L.parse_ttml(demo_ttml()))
        self.p.status = "Neon Rivers — Demo Artist"
        self.w = L.Overlay(self.p)
        self.w.cfg = dict(L.DEFAULTS, **cfg)
        self.w.place()  # size the window from these settings, not the user's saved ones
        self.t = None

    def frame(self, t, fps=60):
        """Advance playback to t in real-time steps so all easing looks as it would live."""
        start = t - 3 if self.t is None else self.t
        steps = max(1, round((t - start) * fps))
        for i in range(steps):
            self.p.pos = start + (t - start) * (i + 1) / steps
            self.w.last -= 1 / fps
            self.w.grab()
        self.t = t
        img = self.w.grab().toImage()
        img.setDevicePixelRatio(1.0)  # use real pixels; otherwise high-DPI screens shrink it when composited
        return img


def composite(overlay, light=False, side="right", pad=360):
    w, h = overlay.width() + pad, overlay.height()
    img = scene(w, h, light)
    qp = QPainter(img)
    qp.drawImage(w - overlay.width() if side == "right" else 0, 0, overlay)
    qp.end()
    return img


def to_pil(img):
    img = img.convertToFormat(QImage.Format_RGBA8888)
    return Image.frombuffer("RGBA", (img.width(), img.height()), bytes(img.constBits()), "raw", "RGBA", 0, 1)


def main():
    app = QApplication([])  # noqa: F841
    DOCS.mkdir(exist_ok=True)

    rig = Rig()
    composite(rig.frame(5.95)).save(str(DOCS / "hero.png"))            # word sweep + ad-lib
    composite(Rig().frame(9.3)).save(str(DOCS / "held-note.png"))      # slow held syllable
    composite(Rig().frame(19.0)).save(str(DOCS / "interlude.png"))     # long instrumental: dots only

    a = composite(Rig(backdrop=False).frame(5.95), light=True)
    b = composite(Rig(backdrop=True).frame(5.95), light=True)
    both = QImage(a.width(), a.height() * 2 + 12, QImage.Format_ARGB32)
    both.fill(Qt.transparent)
    qp = QPainter(both)
    qp.drawImage(0, 0, a)
    qp.drawImage(0, a.height() + 12, b)
    qp.end()
    both.save(str(DOCS / "backdrop.png"))

    rig = Rig()  # tray menu, built exactly as the app builds it
    menu = L.QMenu()
    L.choice_menu(menu, "Size", rig.w, "scale", (("Normal (100%)", 1.0),))
    L.choice_menu(menu, "Side", rig.w, "side", (("Right", "right"),))
    dark = menu.addAction("Dark backdrop (for light scenes)")
    dark.setCheckable(True)
    dark.setChecked(True)
    menu.addSeparator()
    menu.addAction(L.slider_action(menu, rig.w, "offset", -200, 200, 100, lambda v: f"Lyrics timing: {v:+.2f}s",
                                   "later", "earlier"))
    menu.addAction(L.slider_action(menu, rig.w, "backdrop_opacity", 5, 90, 100,
                                   lambda v: f"Backdrop darkness: {v:.0%}", "light", "dark"))
    menu.addSeparator()
    menu.addAction("Hide / show lyrics")
    menu.addAction(f"Check for updates…  (v{L.__version__})")
    menu.addSeparator()
    menu.addAction("Quit lyrics overlay")
    menu.adjustSize()
    menu.grab().save(str(DOCS / "tray-menu.png"))

    rig, frames = Rig(), []
    t = 3.6
    while t < 10.8:
        img = to_pil(composite(rig.frame(t))).convert("RGB")
        frames.append(img.resize((720, round(720 * img.height / img.width)), Image.LANCZOS))
        t += 1 / 20
    frames[0].save(DOCS / "demo.gif", save_all=True, append_images=frames[1:], duration=50, loop=0,
                   optimize=True)
    print("wrote", *sorted(p.name for p in DOCS.iterdir()))


if __name__ == "__main__":
    main()
