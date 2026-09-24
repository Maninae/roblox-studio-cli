"""Render the README banner: the Roblox Studio icon drawn in Luau source text, beside a zsh session.

The icon's two interlocking pieces (the 2025 Studio icon, traced from its published SVG polygons)
are rasterized into a monospace grid. Fully covered cells take the next character of a Luau
snippet; partially covered edge cells take a dimmed density-ramp glyph, which anti-aliases the
tilted edges.

    python3 assets/banner/make_banner.py   ->  assets/banner/banner.png  (2560x1440, 16:9)

Needs Pillow and Playwright (with Chromium). JetBrains Mono is fetched once into ~/.cache.
"""
import base64
import html
import re
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

BANNER_DIR = Path(__file__).resolve().parent
OUTPUT_PNG = BANNER_DIR / "banner.png"
FONT_CACHE_DIR = Path.home() / ".cache" / "banner-fonts"
FONT_WEIGHTS = (400, 600, 800)

# The two pieces of the 2025 Studio icon, in its 512x512 viewBox.
ICON_PIECE_POLYGONS = [
    [(399.9, 294.6), (296.2, 266.8), (285.4, 307), (101.4, 257.7), (73.6, 361.3), (361.3, 438.4), (389.1, 334.8)],
    [(438.4, 150.7), (150.7, 73.6), (112.1, 217.4), (215.8, 245.2), (226.6, 205), (410.6, 254.3)],
]
ICON_BOUNDS = (73.6, 73.6, 438.4, 438.4)
ART_COLUMNS = 76
ART_FONT_PX = 12
CELL_WIDTH_OVER_HEIGHT = 0.5  # monospace glyph 0.6em wide in a 1.2em line
SUPERSAMPLE = 8
SOLID_COVERAGE = 0.85
DENSITY_RAMP = " .:-=+*#%@"
LUAU_FILL = (
    "local Players=game:GetService('Players') for _,part in workspace:GetDescendants() do "
    "if part:IsA('BasePart') then part.Anchored=true end end print(#workspace:GetChildren()) "
    "task.wait() local camera=workspace.CurrentCamera camera.CFrame=CFrame.new(0,10,-20) "
)

TERMINAL_SESSION = [
    ("command", "roblox-studio doctor"),
    ("output", "CONNECTED (28 tools, 1 instance)"),
    ("command", "roblox-studio luau 'return game.Name'"),
    ("output", "Place1"),
    ("command", "roblox-studio screenshot --out viewport.png"),
    ("cursor", ""),
]


def ensure_jetbrains_mono_fonts() -> dict[int, Path]:
    """Download the JetBrains Mono TTFs (OFL) from Google Fonts once, keyed by weight."""
    FONT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    font_paths = {}
    for weight in FONT_WEIGHTS:
        font_path = FONT_CACHE_DIR / f"JetBrainsMono-{weight}.ttf"
        if not font_path.exists():
            css = urllib.request.urlopen(f"https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@{weight}").read().decode()
            ttf_url = re.search(r"https://[^)]+\.ttf", css).group(0)
            font_path.write_bytes(urllib.request.urlopen(ttf_url).read())
        font_paths[weight] = font_path
    return font_paths


def icon_coverage_grid(columns: int) -> list[list[float]]:
    """Fraction of each monospace cell covered by the icon, via supersampled polygon fill."""
    x0, y0, x1, y1 = ICON_BOUNDS
    rows = round(columns * CELL_WIDTH_OVER_HEIGHT * (y1 - y0) / (x1 - x0))
    mask_width, mask_height = columns * SUPERSAMPLE, rows * SUPERSAMPLE
    mask = Image.new("L", (mask_width, mask_height), 0)
    draw = ImageDraw.Draw(mask)
    for polygon in ICON_PIECE_POLYGONS:
        draw.polygon([((x - x0) / (x1 - x0) * mask_width, (y - y0) / (y1 - y0) * mask_height) for x, y in polygon], fill=255)
    cells = mask.resize((columns, rows), Image.BOX)
    return [[cells.getpixel((c, r)) / 255 for c in range(columns)] for r in range(rows)]


def icon_art_html(columns: int) -> str:
    """The icon as HTML text: Luau source in solid cells, dimmed ramp glyphs on the edges."""
    art_lines, fill_index = [], 0
    for row in icon_coverage_grid(columns):
        cells = []
        for coverage in row:
            if coverage > SOLID_COVERAGE:
                character = LUAU_FILL[fill_index % len(LUAU_FILL)]
                fill_index += 1
                cells.append(html.escape("·" if character == " " else character))
                continue
            ramp_character = DENSITY_RAMP[round(coverage * (len(DENSITY_RAMP) - 1))]
            cells.append(" " if ramp_character == " " else f'<span class="edge">{html.escape(ramp_character)}</span>')
        art_lines.append("".join(cells).rstrip())
    return "\n".join(art_lines)


def terminal_session_html() -> str:
    """The zsh session beside the icon."""
    lines = []
    for kind, text in TERMINAL_SESSION:
        if kind == "command":
            lines.append(f'<div><span class="prompt">❯</span> {html.escape(text)}</div>')
        elif kind == "output":
            lines.append(f'<div class="output">{html.escape(text)}</div>')
        else:
            lines.append('<div><span class="prompt">❯</span> █</div>')
    return "\n".join(lines)


def banner_page_html(font_paths: dict[int, Path]) -> str:
    """Full 1280x720 page; rendered at 2x."""
    font_faces = "".join(
        f"@font-face{{font-family:JBM;src:url(data:font/ttf;base64,{base64.b64encode(path.read_bytes()).decode()});font-weight:{weight}}}"
        for weight, path in font_paths.items()
    )
    return f"""<!doctype html><html><head><style>{font_faces}
:root{{--bg:#0a0a0b;--ink:#ecebe8;--dim:#7c7b78;--accent:#4db8ff}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{width:1280px;height:720px;background:var(--bg);color:var(--ink);font-family:JBM,monospace;display:flex;align-items:center;gap:64px;padding:0 80px;overflow:hidden}}
pre.art{{font-family:JBM,monospace;font-size:{ART_FONT_PX}px;line-height:1.2;font-weight:600}}
.edge{{color:#6f6e6b}}
.side{{display:flex;flex-direction:column;gap:34px}}
h1{{font-size:40px;font-weight:800;letter-spacing:-1px}}
.tagline{{font-size:17px;color:var(--dim);margin-top:10px;line-height:1.5}}
.terminal{{font-size:15px;line-height:1.85}}
.prompt{{color:var(--accent)}} .output{{color:var(--dim)}}
</style></head><body>
<pre class="art">{icon_art_html(ART_COLUMNS)}</pre>
<div class="side">
  <div><h1>roblox-studio</h1><div class="tagline">Run Luau and capture the viewport<br>in a live Roblox Studio, from a shell.</div></div>
  <div class="terminal">{terminal_session_html()}</div>
</div></body></html>"""


def render_banner() -> None:
    """Load the page in headless Chromium and screenshot it."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 720}, device_scale_factor=2)
        page.set_content(banner_page_html(ensure_jetbrains_mono_fonts()))
        page.evaluate("Promise.all(['400','600','800'].map(w => document.fonts.load(w + ' 12px JBM')))")
        page.screenshot(path=str(OUTPUT_PNG))
        browser.close()
    print(f"wrote {OUTPUT_PNG}")


if __name__ == "__main__":
    render_banner()
