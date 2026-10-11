#!/usr/bin/env python3
"""Render the profile README and its SVG panels.

Content lives in data/profile.json (edited by hand) and data/stats.json
(GitHub numbers, refreshed with --fetch). The script writes README.md and,
for every panel, assets/<name>-light.svg and assets/<name>-dark.svg; the
README picks one with <picture>, as GitHub's docs show.

GitHub shows README images as <img>, which cannot load external fonts, so
each SVG embeds a WOFF2 subset of the fonts it uses. Alt text is generated
from the same data as the SVG text, so the two cannot drift apart. Every
animation ends within five seconds (WCAG 2.2.2) and is off under
prefers-reduced-motion.

    python scripts/build.py            # render from the cached stats
    python scripts/build.py --fetch    # refresh stats from the GitHub API first
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import functools
import io
import json
import math
import os
import random
import sys
import urllib.request
from pathlib import Path

from fontTools import subset as ftsubset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
DATA = ROOT / "data"

FONTS = {
    "serif": ("IS", "InstrumentSerif-Regular.ttf"),
    "sans": ("G4", "Geist-Regular.ttf"),
    "sans-medium": ("G5", "Geist-Medium.ttf"),
    "sans-semibold": ("G6", "Geist-SemiBold.ttf"),
    "mono": ("M4", "GeistMono-Regular.ttf"),
    "mono-medium": ("M5", "GeistMono-Medium.ttf"),
}

# ink, ink2, ink3 and accent are text colors and must reach 4.5:1 on bg and soft.
THEMES = {
    "light": {
        "bg": "#f6f4ef", "soft": "#ece8df", "ink": "#161512", "ink2": "#55534c",
        "ink3": "#66635b", "line": "#e0dcd2", "line2": "#c9c4b8", "accent": "#b53a05",
    },
    "dark": {
        "bg": "#11100f", "soft": "#1a1917", "ink": "#edebe5", "ink2": "#a3a19a",
        "ink3": "#8a877f", "line": "#272624", "line2": "#3b3935", "accent": "#ff5c21",
    },
}

W = 860  # full-width panels match the profile README column
PAD = 28


# ── colors ───────────────────────────────────────────────────────────────────

def contrast(a: str, b: str) -> float:
    def lum(h: str) -> float:
        rgb = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b_ = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * r + 0.7152 * g + 0.0722 * b_
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def check_contrast() -> None:
    for name, c in THEMES.items():
        for fg in ("ink", "ink2", "ink3", "accent"):
            for bg in ("bg", "soft"):
                ratio = contrast(c[fg], c[bg])
                if ratio < 4.5:
                    raise SystemExit(f"{name}: {fg} on {bg} is {ratio:.2f}:1, below WCAG AA 4.5:1")


# ── fonts ────────────────────────────────────────────────────────────────────

@functools.cache
def font(key: str) -> TTFont:
    return TTFont(ROOT / "fonts" / FONTS[key][1], recalcTimestamp=False)


def measure(text: str, key: str, size: float, track: float = 0.0) -> float:
    f = font(key)
    cmap, hmtx, upm = f.getBestCmap(), f["hmtx"], f["head"].unitsPerEm
    units = 0
    for ch in text:
        glyph = cmap.get(ord(ch))
        if glyph is None:
            raise SystemExit(f"font {key} has no glyph for {ch!r} in {text!r}")
        units += hmtx[glyph][0]
    return units * size / upm + track * len(text)


def wrap(text: str, key: str, size: float, width: float) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and measure(trial, key, size) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    return lines + [line] if line else lines


@functools.cache
def woff2(key: str, chars: str) -> str:
    f = TTFont(ROOT / "fonts" / FONTS[key][1], recalcTimestamp=False)
    options = ftsubset.Options()
    options.flavor = "woff2"
    options.name_IDs = [1, 2]
    options.drop_tables += ["meta", "DSIG"]
    options.layout_features = ["kern", "liga", "calt", "tnum"]
    sub = ftsubset.Subsetter(options)
    sub.populate(text=chars + " ")
    sub.subset(f)
    f.flavor = "woff2"
    buf = io.BytesIO()
    f.save(buf)
    return base64.b64encode(buf.getvalue()).decode()


# ── svg document ─────────────────────────────────────────────────────────────

def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def num(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


class Svg:
    def __init__(self, w: float, h: float, theme: str, label: str):
        self.w, self.h, self.c, self.label = w, h, THEMES[theme], label
        self.body: list[str] = []
        self.css: list[str] = []
        self.glyphs: dict[str, set[str]] = {}

    def add(self, *parts: str) -> None:
        self.body.extend(parts)

    def col(self, name: str) -> str:
        return self.c.get(name, name)

    def text(self, x, y, s, key="sans", size=14, fill="ink", anchor="start",
             track=0.0, cls="", style="") -> float:
        self.glyphs.setdefault(key, set()).update(s)
        attrs = [f'x="{num(x)}"', f'y="{num(y)}"', f'font-family="{FONTS[key][0]}"',
                 f'font-size="{num(size)}"', f'fill="{self.col(fill)}"']
        if anchor != "start":
            attrs.append(f'text-anchor="{anchor}"')
        if track:
            attrs.append(f'letter-spacing="{num(track)}"')
        if cls:
            attrs.append(f'class="{cls}"')
        if style:
            attrs.append(f'style="{style}"')
        self.body.append(f"<text {' '.join(attrs)}>{esc(s)}</text>")
        return measure(s, key, size, track)

    def rect(self, x, y, w, h, fill="none", stroke="none", rx=0, extra="") -> None:
        self.add(f'<rect x="{num(x)}" y="{num(y)}" width="{num(w)}" height="{num(h)}" rx="{num(rx)}" '
                 f'fill="{self.col(fill)}" stroke="{self.col(stroke)}"{extra}/>')

    def line(self, x1, y1, x2, y2, stroke="line", width=1, extra="") -> None:
        self.add(f'<path d="M{num(x1)} {num(y1)}L{num(x2)} {num(y2)}" stroke="{self.col(stroke)}" '
                 f'stroke-width="{num(width)}"{extra}/>')

    def frame(self) -> None:
        self.rect(0.5, 0.5, self.w - 1, self.h - 1, "bg", "line", 16)

    def render(self) -> str:
        faces = "".join(
            f"@font-face{{font-family:{FONTS[k][0]};src:url(data:font/woff2;base64,"
            f"{woff2(k, ''.join(sorted(chars)))}) format('woff2')}}"
            for k, chars in sorted(self.glyphs.items())
        )
        css = (faces + "".join(self.css)
               + "@media (prefers-reduced-motion:reduce){*{animation:none!important}}")
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{num(self.w)}" height="{num(self.h)}" '
                f'viewBox="0 0 {num(self.w)} {num(self.h)}" role="img" aria-label="{esc(self.label)}" fill="none">'
                f"<title>{esc(self.label)}</title><style>{css}</style>{''.join(self.body)}</svg>\n")


# ── data ─────────────────────────────────────────────────────────────────────

class Fill(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def short(n: int) -> str:
    return f"{n / 1000:.0f}k" if n >= 10_000 else f"{n:,}"


def values(stats: dict) -> Fill:
    return Fill(
        contributions=f"{stats['contributions']:,}",
        ohmypi_stars=short(stats["ohmypi_stars"]),
        shiplog_stars=str(stats["shiplog_stars"]),
        shiplog_releases=str(stats["shiplog_releases"]),
    )


QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
  ohmypi: repository(owner: "can1357", name: "oh-my-pi") { stargazerCount }
  shiplog: repository(owner: "HabibPro1999", name: "shiplog") {
    stargazerCount
    releases { totalCount }
  }
}
"""


def fetch(login: str) -> dict:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("--fetch needs GH_TOKEN or GITHUB_TOKEN")
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": QUERY, "variables": {"login": login}}).encode(),
        headers={"Authorization": f"bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise SystemExit(f"GitHub API error: {payload['errors']}")
    d = payload["data"]
    cal = d["user"]["contributionsCollection"]["contributionCalendar"]
    days = [day for week in cal["weeks"] for day in week["contributionDays"]]
    streak = 0
    for i, day in enumerate(reversed(days)):
        if day["contributionCount"] > 0:
            streak += 1
        elif i > 0:  # an empty "today" does not break the streak yet
            break
    return {
        "fetched_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"),
        "contributions": cal["totalContributions"],
        "weeks": [sum(day["contributionCount"] for day in week["contributionDays"]) for week in cal["weeks"]],
        "streak": streak,
        "ohmypi_stars": d["ohmypi"]["stargazerCount"],
        "shiplog_stars": d["shiplog"]["stargazerCount"],
        "shiplog_releases": d["shiplog"]["releases"]["totalCount"],
    }


# ── panels ───────────────────────────────────────────────────────────────────

def header_alt(P: dict, S: dict) -> str:
    v = values(S)
    specs = "; ".join(f"{s['value'].format_map(v)} {' '.join(s['label']).format_map(v).lower()}" for s in P["specs"])
    return (f"{P['name']}, {P['location']}. {P['headline']} {P['subline']} {specs}. "
            f"A bar chart shows weekly contributions for the last 12 months; current streak {S['streak']} days. "
            f"Status: {P['status'].lower()}.")


def header(theme: str, P: dict, S: dict) -> Svg:
    v = values(S)
    g = Svg(W, 448, theme, header_alt(P, S))
    g.css.append(
        ".bar{transform-box:fill-box;transform-origin:50% 100%;animation:grow .9s cubic-bezier(.2,.8,.2,1) both}"
        "@keyframes grow{from{transform:scaleY(0)}}"
        ".rise{animation:rise .8s cubic-bezier(.2,.8,.2,1) both}"
        "@keyframes rise{from{opacity:0;transform:translateY(8px)}}"
    )
    g.frame()
    g.rect(PAD, 20, 8, 8, "accent")
    g.text(PAD + 18, 28.5, f"{P['login'].upper()} / README", "mono-medium", 11, "ink2", track=1.2)
    g.text(W - PAD, 28.5, f"{P['location'].upper()}  {P['coords']}", "mono", 11, "ink3", anchor="end", track=1.2)
    g.line(0, 48, W, 48)
    status = P["status"].upper()
    sw = measure(status, "mono", 10.5, 1.1)
    g.rect(W - PAD - sw - 34, 66, sw + 34, 24, "none", "line2", 12)
    g.add(f'<circle cx="{num(W - PAD - sw - 19)}" cy="78" r="3.5" fill="{g.col("accent")}"/>')
    g.text(W - PAD - 12, 81.8, status, "mono", 10.5, "ink2", anchor="end", track=1.1)
    size = 78
    while measure(P["name"], "serif", size) > W - 2 * PAD - sw - 60:
        size -= 2
    g.text(PAD - 3, 146, P["name"], "serif", size, "ink", cls="rise")
    g.text(PAD, 188, P["headline"], "sans-medium", 19.5, "ink", cls="rise", style="animation-delay:.1s")
    for i, line in enumerate(wrap(P["subline"], "sans", 16, 640)):
        g.text(PAD, 214 + i * 23, line, "sans", 16, "ink2", cls="rise", style=f"animation-delay:{.18 + i * .06:.2f}s")
    weeks = S["weeks"][-53:]
    top, base = 276, 330
    g.text(PAD, 266, "WEEKLY CONTRIBUTIONS · LAST 12 MONTHS", "mono", 10, "ink3", track=1.1)
    g.text(W - PAD, 266, f"STREAK {S['streak']} DAYS · UPDATED DAILY", "mono", 10, "ink3", anchor="end", track=1.1)
    step = (W - 2 * PAD) / len(weeks)
    bw = step * 0.58
    peak = max(max(weeks), 1)
    for i, count in enumerate(weeks):
        x = PAD + i * step + (step - bw) / 2
        if count == 0:
            g.rect(x, base - 1.5, bw, 1.5, "line2")
            continue
        h = 4 + (base - top - 4) * math.sqrt(count / peak)
        fill = "accent" if i == len(weeks) - 1 else "ink3"
        g.add(f'<rect class="bar" style="animation-delay:{.25 + i * .012:.3f}s" x="{num(x)}" y="{num(base - h)}" '
              f'width="{num(bw)}" height="{num(h)}" rx="1" fill="{g.col(fill)}"/>')
    g.line(0, 352, W, 352)
    cell = (W - 2 * PAD) / len(P["specs"])
    for i, spec in enumerate(P["specs"]):
        x = PAD + i * cell
        if i:
            g.line(x - 16, 368, x - 16, 428, "line")
        g.text(x, 398, spec["value"].format_map(v), "sans-semibold", 30, "ink", track=-0.6)
        for j, label in enumerate(spec["label"]):
            g.text(x, 419 + j * 15, label.format_map(v).upper(), "mono", 10, "ink2" if j == 0 else "ink3", track=0.9)
    return g


CAREER_ALT = ("Career timeline drawn as a git graph, with one branch for Value Digital Services "
              "and one for independent work. The same timeline is listed as text below the image.")


def career(theme: str, P: dict) -> Svg:
    rows = P["career"]
    row_h, top = 60, 92
    h = top + len(rows) * row_h + 8
    g = Svg(W, h, theme, CAREER_ALT)
    g.css.append(
        ".draw{stroke-dasharray:1;animation:draw 1.6s cubic-bezier(.4,0,.2,1) both}"
        "@keyframes draw{from{stroke-dashoffset:1}}"
        ".row{animation:fade .6s ease both}"
        "@keyframes fade{from{opacity:0}}"
        ".ping{transform-box:fill-box;transform-origin:center;opacity:0;"
        "animation:ping 1.6s ease-out 1.5s 2}"
        "@keyframes ping{0%{transform:scale(1);opacity:.7}100%{transform:scale(2.6);opacity:0}}"
    )
    g.frame()
    lanes = P["lanes"]
    xs = {"main": 52, "indie": 80}
    color = {"main": "ink", "indie": "accent"}
    for i, key in enumerate(("main", "indie")):
        y = 30 + i * 22
        g.add(f'<circle cx="{PAD + 5}" cy="{y - 4}" r="4.5" fill="{g.col(color[key])}"/>')
        w1 = g.text(PAD + 18, y, lanes[key]["name"], "mono-medium", 11, color[key], track=0.6)
        w2 = g.text(PAD + 18 + max(w1, 40) + 10, y, lanes[key]["label"], "sans-medium", 13, "ink")
        g.text(PAD + 18 + max(w1, 40) + 10 + w2 + 10, y, lanes[key]["since"], "mono", 11, "ink3", track=0.4)
    g.text(W - PAD, 30, "git log --graph --career", "mono", 11, "ink3", anchor="end", track=0.4)
    g.line(0, 70, W, 70)

    cy = [top + i * row_h + 8 for i in range(len(rows))]
    fork = next(i for i, r in enumerate(rows) if r.get("fork"))
    n = len(rows)
    g.add(f'<path pathLength="1" class="draw" d="M{xs["main"]} {cy[-1]}V{top - 14}" '
          f'stroke="{g.col("ink")}" stroke-width="2"/>')
    fx, fy = xs["indie"], cy[fork]
    g.add(f'<path pathLength="1" class="draw" style="animation-delay:.7s" '
          f'd="M{xs["main"]} {fy + row_h - 12}C{xs["main"]} {fy + 22} {fx} {fy + 26} {fx} {fy}V{top - 14}" '
          f'stroke="{g.col("accent")}" stroke-width="2"/>')
    for i, r in enumerate(rows):
        x, y = xs[r["lane"]], cy[i]
        delay = f"animation-delay:{.25 + (n - 1 - i) * .17:.2f}s"
        if r.get("tag"):
            d = 6
            g.add(f'<path class="row" style="{delay}" d="M{x} {y - d}L{x + d} {y}L{x} {y + d}L{x - d} {y}Z" '
                  f'fill="{g.col("bg")}" stroke="{g.col("ink")}" stroke-width="2"/>')
        else:
            g.add(f'<circle class="row" style="{delay}" cx="{x}" cy="{y}" r="6" fill="{g.col(color[r["lane"]])}" '
                  f'stroke="{g.col("bg")}" stroke-width="3"/>')
        if r.get("ref"):
            g.add(f'<circle class="ping" cx="{x}" cy="{y}" r="6" stroke="{g.col(color[r["lane"]])}" stroke-width="1.5"/>')
            rw = measure(r["ref"], "mono-medium", 10, 0.5) + 18
            g.rect(W - PAD - rw, y - 12, rw, 22, "none", color[r["lane"]], 11, f' class="row" style="{delay}"')
            g.text(W - PAD - rw / 2, y + 3.5, r["ref"], "mono-medium", 10, color[r["lane"]], anchor="middle",
                   track=0.5, cls="row", style=delay)
        g.text(112, y + 4.5, r["date"], "mono", 11, "ink3", track=0.4, cls="row", style=delay)
        g.text(190, y + 5, r["title"], "sans-semibold", 15, "ink", cls="row", style=delay)
        g.text(190, y + 26, r["text"], "sans", 13, "ink2", cls="row", style=delay)
    return g


# Card illustrations draw into a 376×104 box at (x, y). Each animation runs
# once and settles on the drawing's resting state.

def art_budgywell(g: Svg, x: float, y: float) -> None:
    g.css.append(".cmd{animation:cmd 1.4s cubic-bezier(.2,.8,.2,1) both}"
                 "@keyframes cmd{from{transform:translateX(-46px);opacity:0}}")
    g.rect(x + 16, y + 10, 50, 84, "bg", "ink2", 9)
    g.rect(x + 34, y + 15, 14, 3, "line2", rx=1.5)
    for i in range(4):
        g.rect(x + 24, y + 28 + i * 13, 22 if i % 2 else 30, 4, "ink3" if i else "accent", rx=2)
    g.text(x + 41, y + 88, "offline", "mono", 7.5, "ink3", anchor="middle")
    g.line(x + 76, y + 52, x + 214, y + 52, "line2", 1, ' stroke-dasharray="3 4"')
    g.text(x + 145, y + 40, "outbox → /sync", "mono", 9, "ink3", anchor="middle")
    g.text(x + 145, y + 72, "idempotent", "mono", 9, "ink3", anchor="middle")
    for i in range(3):
        g.add(f'<rect class="cmd" style="animation-delay:{.4 + i * .35:.2f}s" x="{x + 150 + i * 18}" y="{y + 48}" '
              f'width="8" height="8" rx="1.5" fill="{g.col("accent")}"/>')
    lx, lw = x + 224, 140
    g.rect(lx, y + 10, lw, 84, "bg", "line2", 6)
    g.text(lx + 10, y + 26, "LEDGER", "mono-medium", 8.5, "ink2", track=0.8)
    g.text(lx + lw - 10, y + 26, "DR    CR", "mono", 8.5, "ink3", anchor="end", track=0.6)
    g.line(lx, y + 33, lx + lw, y + 33, "line")
    for i, (a, b, side) in enumerate([("food", "42.000", 0), ("cash", "42.000", 1), ("rent", "650.000", 0)]):
        ry = y + 48 + i * 14
        g.text(lx + 10, ry, a, "mono", 9, "ink2")
        g.text(lx + (lw - 50 if side == 0 else lw - 10), ry, b, "mono", 9, "ink", anchor="end")
    g.text(lx + lw - 10, y + 88, "DR = CR", "mono-medium", 9, "accent", anchor="end", track=0.6)


def art_focale(g: Svg, x: float, y: float) -> None:
    g.css.append(".scan{animation:scan 1.2s ease-in-out .4s 2 alternate both}"
                 "@keyframes scan{to{transform:translateY(74px)}}")
    modules = ["REG", "ABS", "PAY", "CERT"]
    enabled = [(1, 1, 1, 0), (1, 0, 1, 1), (1, 1, 0, 1)]
    for i, row in enumerate(enabled):
        ry = y + 14 + i * 28
        g.text(x + 14, ry + 13, f"event/{i + 1:02d}", "mono", 9, "ink2")
        for j, on in enumerate(row):
            cx = x + 74 + j * 42
            g.rect(cx, ry, 36, 18, "accent" if on else "none", "accent" if on else "line2", 4)
            g.text(cx + 18, ry + 12.5, modules[j], "mono-medium", 8, "bg" if on else "ink3", anchor="middle", track=0.4)
    qx, qy, cell, n = x + 270, y + 12, 3.8, 21
    rnd = random.Random(7)
    g.rect(qx - 6, qy - 6, n * cell + 12, n * cell + 12, "bg", "line2", 6)
    cells = []
    for r in range(n):
        for c in range(n):
            if any(r0 <= r < r0 + 7 and c0 <= c < c0 + 7 for r0, c0 in ((0, 0), (0, 14), (14, 0))):
                rr, cc = (r if r < 7 else r - 14), (c if c < 7 else c - 14)
                on = rr in (0, 6) or cc in (0, 6) or (2 <= rr <= 4 and 2 <= cc <= 4)
            else:
                on = rnd.random() < 0.46
            if on:
                cells.append(f"M{num(qx + c * cell)} {num(qy + r * cell)}h{cell}v{cell}h-{cell}Z")
    g.add(f'<path d="{"".join(cells)}" fill="{g.col("ink")}"/>')
    g.add(f'<rect class="scan" x="{num(qx - 4)}" y="{qy}" width="{num(n * cell + 8)}" height="2" fill="{g.col("accent")}"/>')


def art_shiplog(g: Svg, x: float, y: float) -> None:
    g.css.append(".cur{animation:cur .8s steps(1) 5}@keyframes cur{50%{opacity:0}}")
    tx, tw = x + 8, 190
    g.rect(tx, y + 6, tw, 92, "bg", "line2", 7)
    for i in range(3):
        g.add(f'<circle cx="{tx + 12 + i * 10}" cy="{y + 16}" r="2.6" stroke="{g.col("ink3")}"/>')
    g.line(tx, y + 25, tx + tw, y + 25, "line")
    w = g.text(tx + 10, y + 41, "$ ", "mono", 9.5, "accent")
    w2 = g.text(tx + 10 + w, y + 41, "shiplog", "mono", 9.5, "ink")
    g.rect(tx + 10 + w + w2 + 3, y + 33, 5.5, 10, "ink", extra=' class="cur"')
    g.rect(tx + 6, y + 48, tw - 12, 14, "soft", rx=3)
    for i, (agent, count) in enumerate([("claude-code", "12"), ("codex", "4"), ("aider", "2")]):
        ry = y + 58.5 + i * 14
        if i == 0:
            g.text(tx + 10, ry, "›", "mono", 9.5, "accent")
        g.text(tx + 22, ry, agent, "mono", 9.5, "ink" if i == 0 else "ink2")
        g.text(tx + tw - 10, ry, f"{count} sessions", "mono", 9.5, "ink3", anchor="end")
    g.line(x + 208, y + 52, x + 240, y + 52, "ink3")
    g.add(f'<path d="M{x + 236} {y + 48}L{x + 241} {y + 52}L{x + 236} {y + 56}" stroke="{g.col("ink3")}"/>')
    dx, dw = x + 250, 118
    g.rect(dx, y + 6, dw, 92, "bg", "line2", 7)
    g.text(dx + dw - 8, y + 19, "session.html", "mono", 7.5, "ink3", anchor="end")
    g.rect(dx + 8, y + 26, 54, 5, "ink2", rx=2.5)
    for i, (w_, c) in enumerate([(92, "line2"), (70, "line2"), (98, "accent"), (60, "line2"), (84, "line2")]):
        indent = 12 if c == "accent" else 0
        g.rect(dx + 8 + indent, y + 40 + i * 11, w_ - indent, 5, c, rx=2.5)


def art_liquid(g: Svg, x: float, y: float, uid: str) -> None:
    travel, k = 170, 1.45  # lens travel in px; magnification inside the glass
    g.css.append(f".lens{{animation:lens 3.2s cubic-bezier(.45,0,.55,1) .3s both}}"
                 f"@keyframes lens{{from{{transform:translateX(-{travel}px)}}}}"
                 f".refr{{animation:refr 3.2s cubic-bezier(.45,0,.55,1) .3s both}}"
                 f"@keyframes refr{{from{{transform:translateX({num(k * travel)}px)}}}}")
    w, h = 376, 104
    g.add(f'<clipPath id="panel{uid}"><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8"/></clipPath>')

    def stripes(color: str) -> str:
        out = []
        for i in range(-60, 90):
            sx = x + i * 9
            out.append(f"M{num(sx)} {y - 20}C{num(sx + 14)} {y + 30} {num(sx - 14)} {y + 74} {num(sx)} {y + h + 20}")
        return f'<path d="{"".join(out)}" stroke="{color}" stroke-width="1.6"/>'

    g.add(f'<g clip-path="url(#panel{uid})">{stripes(g.col("line2"))}</g>')
    lx, ly, lw, lh = x + 204, y + 24, 140, 56  # resting position, right side
    cx = lx + lw / 2
    g.add(f'<g clip-path="url(#panel{uid})"><g class="lens">'
          f'<clipPath id="glass{uid}"><rect x="{lx}" y="{ly}" width="{lw}" height="{lh}" rx="{lh / 2}"/></clipPath>'
          f'<rect x="{lx}" y="{ly}" width="{lw}" height="{lh}" rx="{lh / 2}" fill="{g.col("bg")}"/>'
          f'<g clip-path="url(#glass{uid})"><g class="refr">'
          f'<g transform="translate({num(cx)} {num(y + h / 2)}) scale({k}) translate({num(-cx)} {num(-(y + h / 2))})">'
          f'{stripes(g.col("ink3"))}</g></g></g>'
          f'<rect x="{lx + .5}" y="{ly + .5}" width="{lw - 1}" height="{lh - 1}" rx="{lh / 2}" stroke="{g.col("ink2")}"/>'
          f'<path d="M{lx + 22} {ly + 7}H{lx + lw - 22}" stroke="{g.col("bg")}" stroke-width="2" stroke-linecap="round"/>'
          f'</g></g>')
    g.rect(x + 8, y + h - 22, 104, 16, "bg", rx=4)
    g.text(x + 14, y + h - 10.5, "ImageFilter.shader", "mono", 8.5, "ink2")


def art_ohmypi(g: Svg, x: float, y: float) -> None:
    g.css.append(".hit{animation:hit .5s ease 1s both}@keyframes hit{from{fill:transparent}}"
                 ".hitt{animation:hitt .5s ease 1s both}@keyframes hitt{from{fill:var(--from)}}")
    tools = ["read", "bash", "edit", "search", "grep", "find", "lsp", "fetch",
             "web", "python", "browser", "task", "ssh", "notebook", "mcp:jira", "mcp:figma"]
    essential = {"read", "bash", "edit", "search"}
    found = {"lsp", "mcp:figma"}
    g.text(x + 8, y + 15, 'search_tool_bm25("rename symbol")', "mono", 9, "ink2")
    cols, cw, ch = 4, 56, 15
    for i, t in enumerate(tools):
        cx, cy = x + 8 + (i % cols) * (cw + 5), y + 24 + (i // cols) * (ch + 4)
        if t in essential:
            g.rect(cx, cy, cw, ch, "ink", rx=4)
            g.text(cx + cw / 2, cy + 10.5, t, "mono", 8, "bg", anchor="middle")
        elif t in found:
            g.rect(cx, cy, cw, ch, "accent", "accent", 4, ' class="hit"')
            g.text(cx + cw / 2, cy + 10.5, t, "mono", 8, "bg", anchor="middle", cls="hitt",
                   style=f"--from:{g.col('accent')}")
        else:
            g.rect(cx, cy, cw, ch, "none", "line2", 4, ' stroke-dasharray="2 2"')
            g.text(cx + cw / 2, cy + 10.5, t, "mono", 8, "ink3", anchor="middle")
    bx = x + 262
    g.text(bx, y + 56, "−11k", "sans-semibold", 36, "ink", track=-1)
    g.text(bx + 2, y + 74, "TOKENS IN THE", "mono", 8.5, "ink2", track=0.8)
    g.text(bx + 2, y + 86, "FIRST PROMPT", "mono", 8.5, "ink2", track=0.8)


def art_fatoura(g: Svg, x: float, y: float) -> None:
    g.css.append(".tot{animation:tot .6s ease 1.2s both}@keyframes tot{from{opacity:0}}")
    for i, (label, value) in enumerate([("profile", "saved"), ("clients", "12"), ("services", "7")]):
        ry = y + 10 + i * 30
        g.rect(x + 8, ry, 132, 24, "bg", "line2", 5)
        g.text(x + 18, ry + 15.5, label, "mono", 9, "ink2")
        g.text(x + 130, ry + 15.5, value, "mono", 9, "ink", anchor="end")
    g.line(x + 150, y + 52, x + 186, y + 52, "ink3")
    g.add(f'<path d="M{x + 182} {y + 48}L{x + 187} {y + 52}L{x + 182} {y + 56}" stroke="{g.col("ink3")}"/>')
    ix, iw = x + 198, 170
    g.rect(ix, y + 4, iw, 96, "bg", "line2", 4)
    g.text(ix + 12, y + 21, "FACTURE", "mono-medium", 9, "ink", track=1.2)
    g.text(ix + iw - 12, y + 21, "FAC-000042", "mono", 8.5, "ink3", anchor="end")
    g.line(ix + 12, y + 28, ix + iw - 12, y + 28, "line")
    for i, (a, b) in enumerate([("P-00001  design", "4 500,000"), ("P-00002  build", "9 250,000")]):
        g.text(ix + 12, y + 42 + i * 13, a, "mono", 8, "ink2")
        g.text(ix + iw - 12, y + 42 + i * 13, b, "mono", 8, "ink2", anchor="end")
    g.line(ix + 12, y + 72, ix + iw - 12, y + 72, "line")
    g.text(ix + 12, y + 88, "TOTAL", "mono", 8.5, "ink3", track=0.8)
    g.text(ix + iw - 12, y + 89, "13 750,000 DT", "mono-medium", 10.5, "accent", anchor="end", cls="tot")


ART = {"budgywell": art_budgywell, "focale": art_focale, "shiplog": art_shiplog,
       "ohmypi": art_ohmypi, "fatoura": art_fatoura}
BADGE = {"private": "PRIVATE CODE", "public": "VIEW CODE ↗", "merged": "MERGED PR ↗"}


def card_alt(p: dict, S: dict) -> str:
    v = values(S)
    alt = (f"{p['name']}: {p['tagline']}. {p['kicker']}. {p['text'].format_map(v)} "
           f"Stack: {p['stack'].replace(' · ', ', ')}. {p['meta'].format_map(v).replace(' · ', ', ')}.")
    if p.get("where"):
        alt += f" Opens {p['where']}."
    else:
        alt += " The code is private."
    return alt


def card(theme: str, p: dict, S: dict) -> Svg:
    v = values(S)
    cw, ch = 420, 356
    text = p["text"].format_map(v)
    g = Svg(cw, ch, theme, card_alt(p, S))
    g.frame()
    g.text(22, 30, p["kicker"].upper(), "mono", 10, "ink2", track=1)
    g.text(cw - 22, 30, BADGE[p["visibility"]], "mono-medium", 10,
           "ink3" if p["visibility"] == "private" else "accent", anchor="end", track=1)
    ax, ay = 22, 46
    g.rect(ax, ay, 376, 104, "soft", rx=8)
    if p["id"] == "liquid":
        art_liquid(g, ax, ay, f"-{theme}")
    else:
        ART[p["id"]](g, ax, ay)
    g.text(21, 196, p["name"], "serif", 36, "ink")
    g.text(22, 220, p["tagline"], "sans-medium", 14.5, "ink")
    lines = wrap(text, "sans", 12.75, 376)
    if len(lines) > 3:
        raise SystemExit(f"card {p['id']}: description needs {len(lines)} lines; the card fits 3")
    for i, line in enumerate(lines):
        g.text(22, 245 + i * 18.5, line, "sans", 12.75, "ink2")
    g.line(22, 304, cw - 22, 304, "line")
    g.text(22, 324, p["stack"].upper(), "mono", 9.5, "ink2", track=0.6)
    g.text(22, 341, p["meta"].format_map(v).upper(), "mono", 9.5, "ink3", track=0.6)
    return g


def ship_alt(P: dict) -> str:
    steps = " ".join(f"{s['n']} {s['name']}: {' '.join(s['text'])}" for s in P["ship"])
    return (f"Diagram of the loop behind every feature, in five steps. {steps} "
            f"After review, a fix loop returns to build until the gate is green. "
            f"A human signs off at steps 01 and 05. {P['ship_tools']}.")


def ship(theme: str, P: dict) -> Svg:
    stages = P["ship"]
    g = Svg(W, 286, theme, ship_alt(P))
    col = (W - 2 * PAD) / len(stages)
    ty = 112
    first, last = PAD + col / 2 - 50, PAD + col * (len(stages) - 0.5) - 50
    g.css.append(f".tok{{animation:tok 3.6s cubic-bezier(.45,0,.55,1) .5s both}}"
                 f"@keyframes tok{{from{{transform:translateX(-{num(last - first)}px)}}}}"
                 ".loop{stroke-dasharray:4 5;animation:loop 1.2s linear 3}"
                 "@keyframes loop{to{stroke-dashoffset:-18}}")
    g.frame()
    g.text(PAD, 32, "THE LOOP BEHIND EVERY FEATURE", "mono-medium", 10.5, "ink2", track=1.1)
    g.text(W - PAD, 32, "A HUMAN SIGNS OFF AT 01 AND 05", "mono", 10.5, "ink3", anchor="end", track=1.1)
    g.line(0, 50, W, 50)
    g.line(first + 18, ty, last - 18, ty, "line2", 1.5)
    rx3, rx4 = PAD + col * 2.5 - 50, PAD + col * 3.5 - 50
    g.add(f'<path class="loop" d="M{num(rx4)} {ty - 20}C{num(rx4 - 20)} {ty - 52} {num(rx3 + 20)} {ty - 52} '
          f'{num(rx3 + 6)} {ty - 22}" stroke="{g.col("accent")}" stroke-width="1.5"/>')
    g.add(f'<path d="M{num(rx3 + 1)} {ty - 29}L{num(rx3 + 5)} {ty - 21}L{num(rx3 + 13)} {ty - 25}" '
          f'stroke="{g.col("accent")}" stroke-width="1.5"/>')
    g.text((rx3 + rx4) / 2, ty - 50, "fix, re-review, re-gate", "mono", 9.5, "accent", anchor="middle")
    for i, s in enumerate(stages):
        cx = PAD + col * (i + 0.5) - 50
        human = i in (0, len(stages) - 1)
        g.add(f'<circle cx="{num(cx)}" cy="{ty}" r="17" fill="{g.col("bg")}" '
              f'stroke="{g.col("ink" if human else "line2")}" stroke-width="1.5"/>')
        g.text(cx, ty + 4, s["n"], "mono-medium", 11, "ink", anchor="middle")
        if human:
            g.text(cx, ty - 28, "HUMAN", "mono", 8.5, "ink3", anchor="middle", track=1)
        g.text(cx - 17, ty + 50, s["name"], "sans-semibold", 17, "ink")
        for j, line in enumerate(s["text"]):
            g.text(cx - 17, ty + 72 + j * 18, line, "sans", 12.75, "ink2")
    # the token comes to rest on the last stage: shipped
    g.add(f'<circle class="tok" cx="{num(last)}" cy="{ty - 17}" r="4" fill="{g.col("accent")}"/>')
    g.line(0, 240, W, 240)
    g.text(PAD, 267, P["ship_tools"], "mono", 10.5, "ink2", track=0.4)
    return g


def langs_alt(P: dict) -> str:
    items = ", ".join(f"{name} {pct:g}%" for name, pct in P["languages"]["items"])
    return f"Bar chart. {P['languages']['note']}: {items}."


def langs(theme: str, P: dict) -> Svg:
    data = P["languages"]
    g = Svg(W, 124, theme, langs_alt(P))
    g.css.append(".seg{transform-box:fill-box;transform-origin:0 50%;animation:seg 1s cubic-bezier(.2,.8,.2,1) both}"
                 "@keyframes seg{from{transform:scaleX(0)}}")
    g.frame()
    g.text(PAD, 32, data["note"].upper(), "mono", 10, "ink2", track=0.9)
    bar_y = 46
    shades = ["accent", "ink", "ink2", "ink3", "line2", "line2", "line2", "line2", "line"]
    x, total, gap = PAD, W - 2 * PAD, 2
    lx, ly = PAD, bar_y + 38
    for i, (name, pct) in enumerate(data["items"]):
        w = max(total * pct / 100 - gap, 2)
        g.add(f'<rect class="seg" style="animation-delay:{.1 + i * .06:.2f}s" x="{num(x)}" y="{bar_y}" '
              f'width="{num(w)}" height="12" rx="2" fill="{g.col(shades[i])}"/>')
        x += w + gap
        label = f"{name} {pct:g}%"
        lw = measure(label, "mono", 10.5, 0.3) + 30
        if lx + lw > W - PAD:
            lx, ly = PAD, ly + 20
        g.rect(lx, ly - 8.5, 9, 9, shades[i], "line2" if shades[i] == "line" else "none", 1.5)
        g.text(lx + 15, ly, label, "mono", 10.5, "ink2", track=0.3)
        lx += lw
    return g


def pill(theme: str, link: dict) -> Svg:
    label, value = link["label"].upper(), link["value"]
    lw = measure(label, "mono-medium", 10, 1)
    vw = measure(value + "  ↗", "sans-medium", 14)
    w = 20 + lw + 12 + vw + 18
    g = Svg(w, 40, theme, link["alt"])
    g.rect(0.5, 0.5, w - 1, 39, "bg", "line2", 20)
    g.text(20, 24, label, "mono-medium", 10, "accent", track=1)
    g.text(20 + lw + 12, 25, value + "  ↗", "sans-medium", 14, "ink")
    return g


# ── readme ───────────────────────────────────────────────────────────────────

def picture(name: str, alt: str, width: str = "", height: str = "") -> str:
    size = (f' width="{width}"' if width else "") + (f' height="{height}"' if height else "")
    return ("<picture>"
            f'<source media="(prefers-color-scheme: dark)" srcset="assets/{name}-dark.svg">'
            f'<source media="(prefers-color-scheme: light)" srcset="assets/{name}-light.svg">'
            f'<img alt="{esc(alt)}" src="assets/{name}-light.svg"{size}>'
            "</picture>")


def readme(P: dict, S: dict) -> str:
    v = values(S)
    out = ["<!-- Generated by scripts/build.py from data/profile.json. Edit those, not this file. -->", ""]
    out += [f"<h1>{picture('header', header_alt(P, S), width='100%')}</h1>", ""]
    out += ["<p>"] + [f'<a href="{link["href"]}">{picture("link-" + link["id"], link["alt"], height="40")}</a>'
                      for link in P["links"]] + ["</p>", ""]
    out += [p + "\n" for p in P["intro"]]

    out += ["## Experience", "", picture("career", CAREER_ALT, width="100%"), ""]
    out += ["<details>", "<summary>Career timeline as text</summary>", ""]
    for key in ("main", "indie"):
        lane = P["lanes"][key]
        out += [f"**{lane['label']}** ({lane['since']})", ""]
        out += [f"- {r['date']}: **{r['title']}**. {r['text']}" for r in P["career"] if r["lane"] == key]
        out.append("")
    out += ["</details>", ""]

    out += ["## Selected work", ""]
    cards = P["projects"]
    for i in range(0, len(cards), 2):
        out.append("<p>")
        for p in cards[i:i + 2]:
            img = picture(f"card-{p['id']}", card_alt(p, S), width="49%")
            out.append(f'<a href="{p["href"]}">{img}</a>' if p.get("href") else img)
        out += ["</p>", ""]
    out += ["<details>", "<summary>Also shipped: private client work</summary>", "",
            "| Project | What it is | Stack |", "| --- | --- | --- |"]
    out += [f"| **{name}** | {what} | {stack} |" for name, what, stack in P["also"]]
    out += ["", "</details>", ""]

    out += ["## How I ship", "", picture("ship", ship_alt(P), width="100%"), "", P["ship_note"], ""]

    out += ["## Open source", "", "| Project | What it does | Stack |", "| --- | --- | --- |"]
    out += [f"| [{name}]({href}) | {what.format_map(v)} | {stack} |" for name, href, what, stack in P["oss"]]
    out += [""]

    out += ["## Stack", "", "| Area | Tools |", "| --- | --- |"]
    out += [f"| {area} | {tools} |" for area, tools in P["stack"]]
    out += ["", picture("languages", langs_alt(P), width="100%"), ""]

    email = next(l for l in P["links"] if l["id"] == "email")
    linkedin = next(l for l in P["links"] if l["id"] == "linkedin")
    out += ["## Contact", "",
            f"{P['status']}. Write to me at [{email['value']}]({email['href']}) "
            f"or send a message on [LinkedIn ({linkedin['value']})]({linkedin['href']}).", ""]
    out += ["---", "",
            f"<sub>The panels on this page are rendered by [scripts/build.py](scripts/build.py) from "
            f"[data/profile.json](data/profile.json), and a GitHub Actions workflow refreshes the numbers "
            f"every day. Data as of {S['fetched_at']}.</sub>", ""]
    return "\n".join(out)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fetch", action="store_true", help="refresh data/stats.json from the GitHub API")
    args = ap.parse_args()
    check_contrast()

    P = json.loads((DATA / "profile.json").read_text())
    stats_path = DATA / "stats.json"
    S = json.loads(stats_path.read_text()) if stats_path.exists() else None
    if args.fetch:
        fresh = fetch(P["login"])
        # A token that cannot see private contributions returns a much smaller
        # total. Keep the cached numbers rather than publish a wrong one.
        if S and fresh["contributions"] < 0.6 * S["contributions"]:
            print(f"warning: fetched {fresh['contributions']} contributions, cached {S['contributions']}; "
                  "keeping the cached stats", file=sys.stderr)
        else:
            S = fresh
            stats_path.write_text(json.dumps(S, indent=2) + "\n")
    if S is None:
        raise SystemExit("data/stats.json is missing; run with --fetch")

    panels = {
        "header": lambda t: header(t, P, S),
        "career": lambda t: career(t, P),
        "ship": lambda t: ship(t, P),
        "languages": lambda t: langs(t, P),
    }
    for p in P["projects"]:
        panels[f"card-{p['id']}"] = lambda t, p=p: card(t, p, S)
    for link in P["links"]:
        panels[f"link-{link['id']}"] = lambda t, link=link: pill(t, link)

    ASSETS.mkdir(exist_ok=True)
    wanted = {f"{name}-{theme}.svg" for name in panels for theme in THEMES}
    for stale in ASSETS.glob("*.svg"):
        if stale.name not in wanted:
            stale.unlink()
    for name, build in panels.items():
        for theme in THEMES:
            (ASSETS / f"{name}-{theme}.svg").write_text(build(theme).render())
    (ROOT / "README.md").write_text(readme(P, S))
    print(f"wrote README.md and {len(wanted)} SVGs to {ASSETS.relative_to(ROOT)}/")


if __name__ == "__main__":
    main()
