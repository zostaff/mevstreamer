"""ASCII-art SVG generator for the mev-streamer README.

    pip install pyfiglet pillow numpy
    python scripts/readme_art.py            # writes assets/*.svg

Every picture is a character grid; each cell has a char and a colour. Block glyphs
(█ ▀ ▄ ░ ▒ ▓) are drawn as rects so they tile without gaps in any font; everything
else is <text> with explicit per-char x so columns align whatever font GitHub's
viewer falls back to.
"""
import math, os, sys
from PIL import Image, ImageDraw, ImageFilter
import pyfiglet

OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets")
CW, LH, FS = 8.0, 16.0, 13.4
PAD = 22

P = dict(
    bg0="#140a2a", bg1="#07040f", edge="#2a1850",
    white="#f1eaff", text="#d9ccff", dim="#8a7bb5", faint="#4a3d73",
    purple="#9945FF", violet="#b78cff", green="#14F195", lime="#7dffb8",
    cyan="#00C2FF", pink="#ff4fd8", red="#ff4d6d", yellow="#ffd166", twitch="#a970ff",
)


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Grid:
    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.c = [[(" ", None)] * cols for _ in range(rows)]

    def put(self, r, c, s, col):
        for i, ch in enumerate(s):
            if 0 <= r < self.rows and 0 <= c + i < self.cols:
                self.c[r][c + i] = (ch, col)

    def spans(self, r, c, parts):
        """parts = [(text, colour), ...] written left to right."""
        for s, col in parts:
            self.put(r, c, s, col)
            c += len(s)
        return c

    def box(self, r, c, h, w, col, title=None, tcol=None, style="round"):
        tl, tr, bl, br, hz, vt = {"round": "╭╮╰╯─│", "double": "╔╗╚╝═║", "sq": "┌┐└┘─│"}[style]
        self.put(r, c, tl + hz * (w - 2) + tr, col)
        for i in range(1, h - 1):
            self.put(r + i, c, vt, col)
            self.put(r + i, c + w - 1, vt, col)
        self.put(r + h - 1, c, bl + hz * (w - 2) + br, col)
        if title:
            self.put(r, c + 2, f" {title} ", tcol or col)

    def center(self, r, s, col, c0=0, w=None):
        w = w or self.cols
        self.put(r, c0 + (w - len(s)) // 2, s, col)


BLOCKS = {"█": (0, 1, 1), "▀": (0, 0.5, 1), "▄": (0.5, 1, 1), "░": (0, 1, .28), "▒": (0, 1, .5), "▓": (0, 1, .75)}


def render(g, name, glow=True, bg=True):
    W, H = g.cols * CW + 2 * PAD, g.rows * LH + 2 * PAD
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" viewBox="0 0 {W:.0f} {H:.0f}">',
         '<defs>',
         f'<radialGradient id="bg" cx="0.5" cy="0" r="1.15"><stop offset="0" stop-color="{P["bg0"]}"/><stop offset="1" stop-color="{P["bg1"]}"/></radialGradient>',
         '<filter id="glow" x="-5%" y="-20%" width="110%" height="140%"><feGaussianBlur stdDeviation="1.6" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>',
         '<pattern id="scan" width="4" height="3" patternUnits="userSpaceOnUse"><rect width="4" height="1" fill="#000" fill-opacity="0.22"/></pattern>',
         '</defs>']
    if bg:
        o.append(f'<rect x="0.5" y="0.5" width="{W-1:.0f}" height="{H-1:.0f}" rx="14" fill="url(#bg)" stroke="{P["edge"]}"/>')
    o.append(f'<g font-family="Menlo,Consolas,\'DejaVu Sans Mono\',\'Liberation Mono\',\'Courier New\',monospace" '
             f'font-size="{FS}" font-weight="700" xml:space="preserve"{" filter=\"url(#glow)\"" if glow else ""}>')
    for r in range(g.rows):
        y = PAD + r * LH
        run, rc, xs = "", None, []

        def flush():
            if run.strip():
                o.append(f'<tspan x="{" ".join(f"{x:.1f}" for x in xs)}" fill="{rc}">{esc(run)}</tspan>')

        o.append(f'<text y="{y + LH * 0.76:.1f}">')
        for c in range(g.cols):
            ch, col = g.c[r][c]
            x = PAD + c * CW
            if ch in BLOCKS:
                flush(); run, xs = "", []
                a, b, op = BLOCKS[ch]
                o.append(f'</text><rect x="{x:.1f}" y="{y + a * LH:.1f}" width="{CW + .3:.1f}" height="{(b - a) * LH + .3:.1f}" '
                         f'fill="{col}"{f" fill-opacity=\"{op}\"" if op < 1 else ""}/><text y="{y + LH * 0.76:.1f}">')
                continue
            if ch == " " or col != rc:
                flush(); run, xs = "", []
                rc = col
            if ch != " ":
                run += ch; xs.append(x)
        flush()
        o.append('</text>')
    o.append('</g>')
    if bg:
        o.append(f'<rect x="0.5" y="0.5" width="{W-1:.0f}" height="{H-1:.0f}" rx="14" fill="url(#scan)" pointer-events="none"/>')
    o.append('</svg>')
    svg = "".join(o).replace('<text y="' , '\n<text y="')
    # drop empty text elements left by rect splitting
    import re
    svg = re.sub(r'<text y="[\d.]+"></text>', "", svg)
    with open(os.path.join(OUT, name), "w") as f:
        f.write(svg)
    print(name, f"{W:.0f}x{H:.0f}", f"{len(svg)//1024}KB")


def lerp_hex(a, b, t):
    a, b = [int(a[i:i + 2], 16) for i in (1, 3, 5)], [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def grad(stops, t):
    t = max(0, min(1, t))
    n = len(stops) - 1
    i = min(int(t * n), n - 1)
    return lerp_hex(stops[i], stops[i + 1], t * n - i)


def figlet(g, r, c0, text, font, stops, shadow=None):
    lines = pyfiglet.figlet_format(text, font=font, width=400).rstrip("\n").split("\n")
    w = max(len(l) for l in lines)
    for i, line in enumerate(lines):
        for j, ch in enumerate(line):
            if ch == " ":
                continue
            col = grad(stops, j / max(1, w - 1))
            if ch not in BLOCKS and shadow:
                col = shadow
            g.put(r + i, c0 + j, ch, col)
    return w, len(lines)


# ---------------------------------------------------------------- streamer cam
RAMP = " .'`^,:;Il!i><~+_-?][}{1)(|\\/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$"


def streamer_image(w, h):
    """Procedural facecam (head and shoulders): headset, boom mic, hoodie, lit by the
    monitor. Drawn at the real cell aspect (8x16 px per char), downsampled to w x h."""
    S = 3
    W, H = w * 8 * S, h * 16 * S
    im = Image.new("L", (W, H), 0)
    d = ImageDraw.Draw(im)
    # back wall LED strip + a poster frame
    d.rectangle([0, H * .06, W, H * .09], fill=90)
    d.rectangle([W * .04, H * .20, W * .17, H * .55], outline=60, width=S * 3)
    d.rectangle([W * .83, H * .20, W * .96, H * .45], outline=60, width=S * 3)
    cx = W * .5
    # shoulders / hoodie
    d.rounded_rectangle([cx - W * .42, H * .80, cx + W * .42, H * 1.5], radius=W * .2, fill=105)
    d.polygon([(cx - W * .10, H * .79), (cx + W * .10, H * .79), (cx, H * .97)], fill=40)
    for sx in (-1, 1):
        d.line([(cx + sx * W * .07, H * .84), (cx + sx * W * .08, H * 1.0)], fill=230, width=S * 4)
    # neck
    d.rectangle([cx - W * .07, H * .62, cx + W * .07, H * .84], fill=120)
    # head
    hw, hh, hy = W * .17, H * .30, H * .44
    d.ellipse([cx - hw, hy - hh, cx + hw, hy + hh], fill=205)
    # hair
    d.chord([cx - hw * 1.04, hy - hh * 1.10, cx + hw * 1.04, hy + hh * .10], 180, 360, fill=50)
    d.polygon([(cx - hw * 1.0, hy - hh * .1), (cx - hw * .2, hy - hh * .55), (cx + hw * .9, hy - hh * .25), (cx + hw, hy - hh * .1)], fill=50)
    # brows, eyes, nose, mouth
    for sx in (-1, 1):
        ex = cx + sx * hw * .40
        d.line([(ex - hw * .22, hy + hh * .02), (ex + hw * .22, hy - hh * .03)], fill=30, width=S * 5)
        d.ellipse([ex - hw * .16, hy + hh * .12, ex + hw * .16, hy + hh * .30], fill=20)
        d.ellipse([ex - hw * .05, hy + hh * .16, ex + hw * .07, hy + hh * .24], fill=255)
    d.line([(cx, hy + hh * .25), (cx - hw * .08, hy + hh * .50), (cx + hw * .06, hy + hh * .52)], fill=110, width=S * 3)
    d.chord([cx - hw * .42, hy + hh * .55, cx + hw * .42, hy + hh * .85], 0, 180, fill=25)
    d.rectangle([cx - hw * .30, hy + hh * .62, cx + hw * .30, hy + hh * .66], fill=245)  # teeth
    # headset band + cups
    d.arc([cx - hw * 1.28, hy - hh * 1.30, cx + hw * 1.28, hy + hh * .85], 195, 345, fill=255, width=S * 8)
    for sx in (-1, 1):
        x = cx + sx * hw * 1.12
        d.rounded_rectangle([x - hw * .30, hy - hh * .25, x + hw * .30, hy + hh * .45], radius=hw * .14, fill=250)
        d.rounded_rectangle([x - hw * .17, hy - hh * .12, x + hw * .17, hy + hh * .32], radius=hw * .08, fill=140)
    # boom mic
    d.line([(cx - hw * 1.12, hy + hh * .35), (cx - hw * .95, hy + hh * .90), (cx - hw * .45, hy + hh * .78)], fill=240, width=S * 6)
    d.ellipse([cx - hw * .62, hy + hh * .62, cx - hw * .28, hy + hh * .94], fill=255)
    # monitor light falloff
    from PIL import ImageChops, ImageOps
    light = Image.eval(Image.radial_gradient("L").resize((int(W * 1.3), int(H * 1.5))), lambda v: 255 - v)
    canvas = Image.new("L", (W, H), 0)
    canvas.paste(light, (int(cx - W * .65), int(hy - H * .75)))
    im = ImageChops.multiply(im, Image.eval(canvas, lambda v: 175 + v * 80 // 255))
    im = im.filter(ImageFilter.GaussianBlur(S * 0.8))
    im = ImageOps.autocontrast(im, cutoff=1)
    return im


RAMP2 = " .,:;-=+*o#%@"


def ascii_cam(g, r0, c0, w, h):
    """Edge-aware ASCII: contours get / \\ | _ by gradient direction, fills a ramp."""
    import numpy as np
    big = streamer_image(w, h)
    a = np.asarray(big, dtype=np.float32) / 255
    gy, gx = np.gradient(a)
    cw_, ch_ = a.shape[1] // w, a.shape[0] // h
    def cells(m):
        return m[:ch_ * h, :cw_ * w].reshape(h, ch_, w, cw_).mean(axis=(1, 3))
    L, GX, GY = cells(a), cells(gx), cells(gy)
    M = np.hypot(GX, GY)
    thr = np.percentile(M, 80)
    ramp = " .:-=+*#%@"
    for y in range(h):
        for x in range(w):
            v = float(L[y, x])
            if M[y, x] > thr and M[y, x] > 0.004:
                ang = (np.degrees(np.arctan2(GY[y, x], GX[y, x] * 2)) + 180) % 180  # cells are 2:1
                ch = "|" if ang < 22.5 or ang >= 157.5 else ("\\" if ang < 67.5 else ("_" if ang < 112.5 else "/"))
                col = grad([P["violet"], P["pink"], P["white"]], min(1, .3 + v))
            else:
                if v < 0.08:
                    continue
                ch = ramp[min(len(ramp) - 1, max(1, int(v ** .9 * (len(ramp) - 1) + .5)))]
                col = grad([P["faint"], P["purple"], P["violet"], P["pink"], P["white"]], min(1, v * 1.05))
            if y < h * .14 and v > .2:
                col = grad([P["cyan"], P["pink"], P["green"]], x / w)
            g.put(r0 + y, c0 + x, ch, col)


# ---------------------------------------------------------------- hero
def hero():
    g = Grid(152, 41)
    stops = [P["purple"], P["pink"], P["cyan"], P["green"]]
    w, h = 102, 6
    figlet(g, 1, (152 - w) // 2, "MEV-STREAMER", "ansi_shadow", stops, shadow=P["faint"])
    g.center(8, "> sees the token on stream . hears the call . lands the buy before chat does_", P["dim"])
    g.spans(9, 34, [("[ ", P["faint"]), ("OCR 118ms", P["violet"]), (" ]──[ ", P["faint"]), ("WHISPER 398ms", P["cyan"]),
                    (" ]──[ ", P["faint"]), ("QUOTE 157ms", P["green"]), (" ]──[ ", P["faint"]), ("SENDER ║ JITO ║ RPC", P["pink"]), (" ]", P["faint"])])

    top, H = 11, 29
    # ---- screen 1: the stream
    c, W = 1, 48
    g.box(top, c, H, W, P["twitch"], style="round")
    g.spans(top, c + 2, [(" ", None), ("◉ LIVE", P["red"]), ("  kick.com/degen_andy  ", P["text"]), ("4.2k ", P["dim"])])
    ascii_cam(g, top + 1, c + 1, W - 2, 21)
    # lower third overlay
    g.put(top + 19, c + 1, " " * 22, None)
    g.spans(top + 19, c + 2, [("$JIMOTHY ", P["white"]), ("MC $1.2M ", P["green"]), ("▲38%", P["lime"])])
    # chat
    g.put(top + 22, c + 1, "─" * (W - 2), P["faint"])
    g.spans(top + 22, c + 2, [(" chat ", P["dim"])])
    chat = [("anon_44", P["cyan"], "CA???"), ("degen_andy", P["pink"], "aping jimothy rn, sending it"),
            ("solboi", P["yellow"], "LFG $JIMOTHY"), ("anon_91", P["lime"], "what mc?"),
            ("degen_andy", P["pink"], "1.2 mil, holders 30k")]
    for i, (u, col, msg) in enumerate(chat):
        g.spans(top + 23 + i, c + 2, [(u, col), (": ", P["faint"]), (msg, P["text"])])

    # arrows
    for cc in (50, 101):
        g.put(top + 13, cc, "══▶", P["green"])
        g.put(top + 14, cc - 0, "OCR" if cc == 50 else "IPC", P["faint"])

    # ---- screen 2: what the bot sees
    c, W = 53, 48
    g.box(top, c, H, W, P["cyan"], style="round")
    g.spans(top, c + 2, [(" vision ", P["cyan"]), ("+ audio ", P["cyan"]), (" frame 1280x720 ", P["dim"])])
    g.box(top + 2, c + 2, 3, 13, P["green"], style="sq")
    g.put(top + 3, c + 4, "JIMOTHY", P["white"])
    g.put(top + 2, c + 15, "◀ title", P["green"])
    g.box(top + 2, c + 25, 3, 11, P["green"], style="sq")
    g.put(top + 3, c + 27, "$1.20M", P["white"])
    g.put(top + 5, c + 27, "MARKET CAP", P["dim"])
    g.box(top + 2, c + 36, 3, 10, P["violet"], style="sq")
    g.put(top + 3, c + 38, "30,484", P["text"])
    g.put(top + 5, c + 38, "HOLDERS", P["dim"])
    # candles
    import random
    random.seed(7)
    base, cols_ = 9.0, list(range(c + 3, c + W - 3, 2))
    price = 0.0
    chart_top, chart_h = top + 7, 10
    vals = []
    for i, x in enumerate(cols_):
        o_ = price
        price += random.uniform(-0.55, 0.85) + (0.25 if i > len(cols_) * 0.6 else 0)
        vals.append((x, o_, price, max(o_, price) + random.uniform(0, .6), min(o_, price) - random.uniform(0, .6)))
    lo = min(v[4] for v in vals); hi = max(v[3] for v in vals)
    for x, o_, cl, h_, l_ in vals:
        f = lambda v: chart_top + chart_h - 1 - round((v - lo) / (hi - lo) * (chart_h - 1))
        col = P["green"] if cl >= o_ else P["red"]
        for y in range(f(h_), f(l_) + 1):
            g.put(y, x, "│", col)
        for y in range(f(max(o_, cl)), f(min(o_, cl)) + 1):
            g.put(y, x, "█", col)
    g.put(chart_top + chart_h, c + 2, "└" + "─" * (W - 6), P["faint"])
    g.spans(top + 18, c + 3, [("OCR ", P["violet"]), ("title=JIMOTHY mc=$1.20M holders=30484", P["text"])])
    g.spans(top + 19, c + 3, [("ASR ", P["cyan"]), ('"aping jimothy rn, sending it"', P["text"])])
    g.spans(top + 20, c + 3, [("KWS ", P["pink"]), ("buy-phrase ", P["text"]), ("+ ticker", P["text"])])
    g.put(top + 22, c + 2, "─" * (W - 4), P["faint"])
    g.spans(top + 23, c + 3, [("resolve ", P["dim"]), ("18 tokens named JIMOTHY", P["text"])])
    g.spans(top + 24, c + 3, [("        ", None), ("mc within ±25% → ", P["dim"]), ("1 match", P["green"])])
    g.spans(top + 25, c + 3, [("score   ", P["dim"]), ("████████████████", P["green"]), ("░░░░", P["faint"]), (" 0.85", P["white"])])
    g.spans(top + 26, c + 3, [("        ", None), ("threshold 0.70 ", P["dim"]), ("→ BUY", P["lime"])])

    # ---- screen 3: executor terminal
    c, W = 104, 48
    g.box(top, c, H, W, P["green"], style="round")
    g.spans(top, c + 2, [(" ", None), ("●", P["red"]), ("●", P["yellow"]), ("●", P["green"]), ("  executor — zsh ", P["dim"])])
    L = [
        [("$ ", P["faint"]), ("./scripts/run.sh --stream", P["text"])],
        [("  kick.com/degen_andy", P["text"])],
        [],
        [("17:42:08.114 ", P["faint"]), ("INFO ", P["cyan"]), ("identified", P["text"])],
        [("  $JIMOTHY ", P["white"]), ("@ MC $1.20M", P["dim"])],
        [("  → Ge87…pump ", P["violet"]), ("(1 of 18) 682ms", P["dim"])],
        [("17:42:08.120 ", P["faint"]), ("INFO ", P["cyan"]), ("signal Buy", P["text"])],
        [("  conf=0.85 src=both lag=1.9s", P["dim"])],
        [("17:42:08.277 ", P["faint"]), ("INFO ", P["cyan"]), ("venue ", P["text"]), ("PumpSwap", P["cyan"])],
        [("  quote 0.1 SOL → 81.2M tokens", P["dim"])],
        [("  fan-out ", P["dim"]), ("sender ║ jito ║ rpc", P["pink"])],
        [("17:42:08.931 ", P["faint"]), ("OK   ", P["green"]), ("BUY LANDED", P["green"])],
        [("  $JIMOTHY confirmed in 654ms", P["text"])],
        [("  cost 0.10204 SOL (real Δ)", P["dim"])],
        [],
        [("monitor ", P["dim"]), ("TP 2.0x  SL 0.6x  trail 25%", P["text"])],
        [("  value ", P["dim"]), ("▁▂▂▃▅▄▆▇", P["green"]), (" 1.34x ", P["lime"]), ("peak 1.41x", P["dim"])],
        [],
        [("17:44:51.402 ", P["faint"]), ("OK   ", P["green"]), ("CLOSED", P["green"])],
        [("  $JIMOTHY [TrailingStop]", P["text"])],
        [("  PnL ", P["dim"]), ("+0.0331 SOL", P["lime"]), (" held 163s", P["dim"])],
        [],
        [("$ ", P["faint"]), ("█", P["green"])],
    ]
    for i, parts in enumerate(L):
        g.spans(top + 2 + i, c + 2, parts)

    g.center(top + H + 0, "", None)
    g.spans(top + H, 2, [("illustration · ", P["faint"]), ("numbers from real mainnet runs, trade outcome is an example — see Disclaimer", P["faint"])])
    render(g, "hero.svg")


# ---------------------------------------------------------------- small pieces
def divider():
    g = Grid(150, 1)
    for i in range(150):
        t = 1 - abs(i - 75) / 75
        g.put(0, i, "─", grad([P["bg1"], P["purple"], P["green"]], t))
    g.put(0, 68, "╾┤ ◉ LIVE ├╼", P["pink"])
    render(g, "divider.svg", bg=False)


def section(n, title, sub):
    lines = pyfiglet.figlet_format(title, font="ansi_regular", width=400).rstrip("\n").split("\n")
    lines = [l for l in lines if l.strip()]
    g = Grid(150, len(lines) + 1)
    num = f"{n:02d}"
    w = max(len(l) for l in lines)
    g.put(0, 0, "┌─", P["faint"]); g.put(len(lines), 0, "└─", P["faint"])
    for i in range(1, len(lines)):
        g.put(i, 0, "│", P["faint"])
    g.put(len(lines) // 2, 2, f"{num}", P["green"])
    for i, line in enumerate(lines):
        for j, ch in enumerate(line):
            if ch != " ":
                g.put(i, 6 + j, ch, grad([P["purple"], P["pink"], P["cyan"]], j / max(1, w)))
    dots = 150 - (8 + w) - len(sub) - 2
    g.put(len(lines) // 2, 8 + w, "·" * max(0, dots), P["faint"])
    g.put(len(lines) // 2, 150 - len(sub), sub, P["dim"])
    render(g, f"section-{num}.svg", bg=False)


def flow():
    g = Grid(150, 21)
    nodes = [
        (1, 2, "STREAM", ["twitch · kick", "youtube live"], P["red"]),
        (1, 26, "CAPTURE", ["ffmpeg 3 fps", "pcm 16k · 3s"], P["violet"]),
        (0, 51, "VISION", ["ocr · mint · $tkr", "mc · holders"], P["purple"]),
        (6, 51, "AUDIO", ["whisper + vad", "keyword spotter"], P["cyan"]),
        (3, 77, "SIGNAL", ["score ≥ 0.70", "guards · cooldown"], P["pink"]),
        (3, 102, "EXECUTOR", ["one RPC trip", "quote · build"], P["green"]),
    ]
    for r, c, t, sub, col in nodes:
        g.box(r, c, 5, 21, col, t, P["white"])
        for i, s in enumerate(sub):
            g.put(r + 1 + i, c + 2, s, P["text"])
        g.put(r + 3, c + 2, "", None)
    g.put(3, 23, "──▶", P["faint"])
    g.put(2, 47, "─┬─▶", P["faint"]); g.put(3, 48, "│", P["faint"]); g.put(4, 48, "│", P["faint"])
    g.put(5, 48, "│", P["faint"]); g.put(6, 48, "│", P["faint"]); g.put(7, 48, "│", P["faint"]); g.put(8, 48, "└─▶", P["faint"])
    g.put(2, 72, "─┐", P["faint"]); g.put(3, 73, "│", P["faint"]); g.put(4, 73, "├─▶", P["faint"])
    g.put(8, 72, "─┘", P["faint"]); g.put(7, 73, "│", P["faint"]); g.put(6, 73, "│", P["faint"]); g.put(5, 73, "│", P["faint"])
    g.put(5, 98, "──▶", P["green"])
    g.put(5, 75, "", None)
    g.spans(10, 51, [("╰─ no address? ", P["dim"]), ("$TICKER + mcap", P["yellow"]), (" → resolver", P["dim"])])
    # executor fan
    g.put(8, 112, "│", P["faint"]); g.put(9, 112, "▼", P["faint"])
    g.box(10, 102, 4, 21, P["green"], "curve done?", P["white"])
    g.put(11, 104, "no  → pump.fun v3", P["text"]); g.put(12, 104, "yes → PumpSwap v2", P["cyan"])
    g.put(14, 112, "│", P["faint"]); g.put(15, 112, "▼", P["faint"])
    g.box(16, 96, 3, 32, P["pink"], style="double")
    g.put(17, 99, "HELIUS SENDER ║ JITO ║ RPC", P["pink"])
    g.put(17, 92, "◀──", P["faint"])
    g.box(16, 52, 3, 40, P["yellow"], style="sq")
    g.put(17, 55, "MONITOR  TP · SL · TRAIL · TIMEOUT", P["yellow"])
    g.put(17, 48, "◀──", P["faint"])
    g.box(16, 14, 3, 33, P["violet"], style="sq")
    g.put(17, 17, "JOURNAL  positions · trades", P["violet"])
    g.spans(20, 2, [("one signed tx → every relay at once · lands at most once · \"expired\" only past lastValidBlockHeight", P["dim"])])
    render(g, "flow.svg")


def terminal():
    g = Grid(150, 24)
    g.box(0, 0, 24, 150, P["edge"], style="round")
    g.spans(0, 2, [(" ", None), ("●", P["red"]), (" ", None), ("●", P["yellow"]), (" ", None), ("●", P["green"]), ("   mev-streamer — dry-run — zsh ", P["dim"])])
    L = [
        [("$ ", P["faint"]), ("./scripts/dry-run.sh --stream \"kick.com/degen_andy\"", P["white"])],
        [],
        [("09:56:07.879 ", P["faint"]), ("INFO ", P["cyan"]), ("relays       [Sender, Jito] | jito [\"https://mainnet.block-engine.jito.wtf\"]", P["text"])],
        [("09:56:07.879 ", P["faint"]), ("INFO ", P["cyan"]), ("exits        TP 2.00x | SL 0.60x | trail 25% | timeout 120s | slip 15% → 40% urgent | poll 400ms", P["text"])],
        [("09:56:07.879 ", P["faint"]), ("INFO ", P["cyan"]), ("risk         max 3 positions | session loss cap 0.3000 SOL | 5 losses in a row | reserve 0.0100 SOL", P["text"])],
        [("09:56:08.639 ", P["faint"]), ("INFO ", P["cyan"]), ("pump fees    protocol 95 bps, creator 30 bps, buyback 50% of protocol", P["text"])],
        [("09:56:08.640 ", P["faint"]), ("OK   ", P["green"]), ("listening on /tmp/mev-streamer.sock", P["text"])],
        [],
        [("[vision] ", P["purple"]), ("Apple Vision · 118ms/frame   ", P["text"]), ("[audio] ", P["cyan"]), ("faster-whisper base.en · int8 · 398ms/chunk", P["text"])],
        [("[vision] ", P["purple"]), ("metrics  ", P["dim"]), ("title=JIMOTHY  market_cap=$1.20M  holders=30484", P["white"])],
        [("[signal] ", P["pink"]), ("identified $JIMOTHY at MC $1.20M → Ge87…pump (unique of 18, 682ms)", P["text"])],
        [("[audio]  ", P["cyan"]), ("\"aping jimothy right now\"  ", P["white"]), ("→ buy-phrase, ticker", P["dim"])],
        [("[signal] ", P["pink"]), ("BUY SIGNAL 0.85  ", P["lime"]), ("visual+audio · mint 0.40 · ticker 0.20 · phrase 0.30 · both 0.20", P["dim"])],
        [],
        [("09:57:41.220 ", P["faint"]), ("INFO ", P["cyan"]), ("signal Buy $JIMOTHY Ge87…pump conf=0.85 src=both lag=1874ms", P["text"])],
        [("09:57:41.377 ", P["faint"]), ("INFO ", P["cyan"]), ("PumpSwap pool 7xQd…m2Pa · quote 0.1 SOL → 81,204,310 tokens · fee tier 25 bps", P["text"])],
        [("09:57:41.512 ", P["faint"]), ("OK   ", P["green"]), ("simulation OK — 82,604 / 100,000 CU, receives 81,204,310 tokens", P["green"])],
        [("09:57:41.513 ", P["faint"]), ("INFO ", P["cyan"]), ("paper position opened · monitor every 400ms", P["text"])],
        [],
        [("══════ Session stats (DRY RUN) ══════", P["white"])],
        [("Trades: 13  Wins: 0  Losses: 13   ", P["text"]), ("Total PnL: -0.0345 SOL", P["red"]), ("   ← random-token baseline = fees", P["dim"])],
        [("$ ", P["faint"]), ("█", P["green"])],
    ]
    for i, parts in enumerate(L):
        g.spans(1 + i, 3, parts)
    render(g, "terminal.svg")


def scoring():
    g = Grid(150, 15)
    rows = [
        ("mint on screen, ≥2 frames", "VISION", 0.50, P["purple"]),
        ("mint identified by ticker + market cap", "VISION", 0.40, P["violet"]),
        ("$TICKER on screen", "VISION", 0.20, P["violet"]),
        ("trading platform recognised", "VISION", 0.15, P["violet"]),
        ("streamer on Buy button / bought", "VISION", 0.40, P["purple"]),
        ("buy-phrase in speech", "AUDIO", 0.30, P["cyan"]),
        ("ticker in speech", "AUDIO", 0.15, P["cyan"]),
        ("both channels agree on one token", "BOTH", 0.20, P["green"]),
        ("ticker without mint (copycat risk)", "PENALTY", -0.15, P["red"]),
    ]
    g.spans(0, 2, [("SIGNAL SCORE", P["white"]), ("  sum clipped to 1.0 · execute at ", P["dim"]), ("0.70", P["green"])])
    for i, (name, ch, w, col) in enumerate(rows):
        r = 2 + i
        g.put(r, 2, name, P["text"])
        g.put(r, 44, ch, col)
        n = round(abs(w) * 80)
        g.put(r, 54, ("█" * n) if w > 0 else ("▓" * n), col)
        g.put(r, 54 + n + 1, f"{w:+.2f}", P["white"] if w > 0 else P["red"])
    g.spans(12, 2, [("mint + platform + buy-phrase = ", P["dim"]), ("0.95 → BUY", P["lime"]),
                    ("      only buy-phrase + ticker in speech = ", P["dim"]), ("0.30 → no", P["red"])])
    g.put(13, 2, "\"let's go\", \"I'm in\" are not buy-phrases · negations and rug/scam/stay away kill the signal", P["faint"])
    render(g, "scoring.svg")


def latency():
    g = Grid(150, 15)
    rows = [
        ("frame wait (3 fps)", "≤333 ms", 333, P["faint"]),
        ("OCR · Apple Vision avg", "118 ms", 118, P["green"]),
        ("OCR · Apple Vision p95", "179 ms", 179, P["green"]),
        ("OCR · EasyOCR on MPS", "748 ms", 748, P["yellow"]),
        ("Whisper base.en, 3 s chunk", "398 ms", 398, P["cyan"]),
        ("identify by ticker + MC (bg)", "~700 ms", 700, P["violet"]),
        ("score + IPC", "<1 ms", 1, P["green"]),
        ("state + quote + build (1 RPC trip)", "~157 ms", 157, P["green"]),
        ("relay accept · RPC", "115 ms", 115, P["pink"]),
        ("relay accept · Sender/Jito (EU home)", "0.5–0.9 s", 900, P["pink"]),
    ]
    g.spans(0, 2, [("LATENCY", P["white"]), ("  measured 2026-11-09 · Apple M1 · public RPC · scale: 1 block = 12 ms", P["dim"])])
    for i, (name, lab, ms, col) in enumerate(rows):
        r = 2 + i
        g.put(r, 2, name, P["text"])
        n = max(1, round(ms / 12))
        g.put(r, 40, "█" * n, col)
        g.put(r, 40 + n + 1, lab, P["white"])
    g.put(13, 2, "slots are 200 ms since Oct 2026 · blockhash lives ~30 s, refreshed every 2 s, never signed older than 10 s", P["faint"])
    render(g, "latency.svg")


def risk():
    g = Grid(150, 9)
    cells = [("TAKE-PROFIT", "2.0x value", P["green"]), ("STOP-LOSS", "0.6x value", P["red"]),
             ("TRAILING", "-25% from peak", P["yellow"]), ("TIMEOUT", "120 s", P["cyan"]),
             ("SESSION LOSS", "3 x BUY_SOL", P["red"]), ("LOSS STREAK", "5 in a row", P["pink"]),
             ("SOL RESERVE", "0.01 SOL", P["violet"]), ("SIGNAL AGE", "<= 6 s", P["purple"])]
    for i, (t, v, col) in enumerate(cells):
        r, c = (i // 4) * 4, 1 + (i % 4) * 37
        g.box(r, c, 4, 36, col, t, P["white"])
        g.put(r + 1, c + 3, v, col)
        g.put(r + 2, c + 3, ["sell on real value", "with fees", "arms at 1.2x", "then sell all",
                             "buys stop", "buys stop", "kept for exits", "else dropped"][i], P["dim"])
    render(g, "risk.svg")


def tree():
    g = Grid(150, 24)
    T = [
        ("mev-streamer/", "", P["white"]),
        ("├── ingestion/", "module 1 · streamlink / yt-dlp + ffmpeg, watchdog", P["red"]),
        ("├── vision/", "module 2 · screen", P["purple"]),
        ("│   ├── ocr.py", "Apple Vision / EasyOCR / Tesseract, ROI", None),
        ("│   ├── detector.py", "solana addresses, tickers, near-addresses", None),
        ("│   ├── mint_repair.py", "fix OCR typos in a mint + on-chain check", None),
        ("│   ├── metrics.py", "market cap / liquidity / holders from the frame", None),
        ("│   └── ui_recognizer.py", "platform + streamer action", None),
        ("├── audio/", "module 3 · faster-whisper + keyword spotter", P["cyan"]),
        ("├── signal/", "module 4 · orchestrator, aggregator, resolver, ipc", P["pink"]),
        ("├── executor/src/", "module 5 · rust", P["green"]),
        ("│   ├── main.rs", "buy/sell, on-chain reconcile, risk, restore", None),
        ("│   ├── bonding.rs", "curve / global / fee config + v3 quotes", None),
        ("│   ├── pumpswap.rs", "PumpSwap v2: pool, configs, quotes, wrap/unwrap", None),
        ("│   ├── tx_builder.rs", "v3 buy/sell, compute budget, tips, close ATA", None),
        ("│   ├── sender.rs", "fan-out sender ║ jito ║ rpc, block-height landing", None),
        ("│   ├── position.rs", "TP / SL / trailing / timeout, venue, migration", None),
        ("│   ├── journal.rs", "positions.json + trades.jsonl", None),
        ("│   ├── selfcheck.rs", "--selfcheck · --selfcheck-amm · --inspect · --probe-relays", None),
        ("│   └── rpc · pda · config · ipc · jito · types", "", None),
        ("├── scripts/", "build · monitor · dry-run · run · check · smoke · unit checks", P["yellow"]),
        ("└── assets/", "this artwork", P["dim"]),
    ]
    for i, (a, b, col) in enumerate(T):
        r = 1 + i
        k = a.rfind("─ ") + 2 if "─ " in a else 0
        g.put(r, 3, a[:k], P["faint"])
        g.put(r, 3 + k, a[k:], col or P["text"])
        if b:
            g.put(r, 3 + len(a) + 1, "." * (52 - len(a)), P["faint"])
            g.put(r, 58, b, P["dim"] if col is None else col)
    render(g, "tree.svg")


def can_cannot():
    g = Grid(150, 13)
    can = ["read mint, $TICKER, platform, action; repair OCR typos", "identify a token with no address (ticker + mc)",
           "hear trade calls and tickers", "trade the curve (v3) and PumpSwap (v2), exact fees",
           "one tx to Sender, Jito multi-region and RPC", "manage positions on real value, paper too",
           "survive restarts, never double-buy on a fake timeout", "stop itself on session limits",
           "self-check against mainnet after pump.fun upgrades"]
    cant = ["curves quoted in non-SOL mints (xStock chains)", "cashback coins and cashback pools",
            "buys that eat the rest of the curve", "beat the stream platform's own delay",
            "tell original from copycat by ticker alone", "understand sarcasm (explicit negations: yes)",
            "rug-check dev wallet / bundles / holders"]
    g.box(0, 0, 12, 74, P["green"], "CAN", P["green"])
    g.box(0, 76, 12, 74, P["red"], "CANNOT", P["red"])
    for i, s in enumerate(can):
        g.spans(1 + i, 2, [("[+] ", P["green"]), (s, P["text"])])
    for i, s in enumerate(cant):
        g.spans(1 + i, 78, [("[-] ", P["red"]), (s, P["text"])])
    render(g, "can-cannot.svg")


hero(); divider(); flow(); terminal(); scoring(); latency(); risk(); tree(); can_cannot()
for i, (t, s) in enumerate([("QUICK START", "three commands, then paper"), ("HOW IT WORKS", "stream -> signal -> on-chain"),
                            ("SIGNAL", "visual + audio scoring"), ("NO ADDRESS", "ticker + market cap"),
                            ("LATENCY", "measured, not promised"), ("PUMP VENUES", "curve v3 + pumpswap v2"),
                            ("RISK", "exits and session limits"), ("CONFIG", ".env reference"),
                            ("INTERNALS", "tree, ipc, debugging"), ("LIMITS", "can / cannot")], 1):
    section(i, t, s)
