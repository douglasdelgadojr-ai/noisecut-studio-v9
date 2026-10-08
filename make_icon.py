"""Generate the NoiseCut Studio application icons. Requires Pillow."""
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / 'assets'
SIZE = 512
SVG_ICONS = {
    'media': '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8.5" cy="9" r="1.5"/><path d="m21 15-5-5L5 20"/>',
    'audio': '<path d="M2 10v4m4-8v12m4-16v20m4-15v10m4-13v16m4-11v6"/>',
    'text': '<path d="M4 7V4h16v3M9 20h6m-3-16v16"/>',
    'stickers': '<path d="M20 14V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h8"/><path d="M14 20v-4a2 2 0 0 1 2-2h4m0 0-6 6"/>',
    'filters': '<path d="M4 7h16M4 12h16M4 17h16"/><circle cx="9" cy="7" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="11" cy="17" r="2"/>',
    'effects': '<path d="m12 3 1.9 5.8L20 11l-6.1 2.2L12 19l-1.9-5.8L4 11l6.1-2.2L12 3Z"/><path d="m19 14 1.1 2.2L22 17l-1.9.8L19 20l-1.1-2.2L16 17l1.9-.8L19 14Z"/>',
    'transitions': '<path d="M4 7h12l-3-3m3 3-3 3M20 17H8l3 3m-3-3 3-3"/>',
    'ai': '<path d="m12 3 1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3Z"/><path d="m19 15 .9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15Z"/>',
    'settings': '<path d="M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z"/><path d="m19.4 15 .1.1 1.4 1.1-1.4 2.4-1.7-.6a8 8 0 0 1-1.5.9l-.3 1.8h-2.8l-.3-1.8a8 8 0 0 1-1.5-.9l-1.7.6-1.4-2.4 1.4-1.1a7 7 0 0 1 0-1.8l-1.4-1.1 1.4-2.4 1.7.6a8 8 0 0 1 1.5-.9l.3-1.8h2.8l.3 1.8a8 8 0 0 1 1.5.9l1.7-.6 1.4 2.4-1.4 1.1a7 7 0 0 1 0 1.8Z"/>',
}


def make_icon():
    ASSETS.mkdir(parents=True, exist_ok=True)
    image = Image.new('RGBA', (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((16, 16, 496, 496), radius=104, fill='#1e3a8a')

    center_y = 256
    bar_width = 27
    gap = 22
    heights = [72, 130, 190, 112, 250, 170, 218, 126, 78]
    total_width = len(heights) * bar_width + (len(heights) - 1) * gap
    left = (SIZE - total_width) // 2
    for index, height in enumerate(heights):
        x = left + index * (bar_width + gap)
        top = center_y - height // 2
        bottom = center_y + height // 2
        draw.rounded_rectangle((x, top, x + bar_width, bottom), radius=bar_width // 2, fill='#ffffff')

    # A clean diagonal cut through the sound wave.
    draw.line((184, 336, 330, 190), fill='#7dd3fc', width=24)
    r = 12
    for x, y in ((184, 336), (330, 190)):
        draw.ellipse((x - r, y - r, x + r, y + r), fill='#7dd3fc')

    png = ASSETS / 'icon.png'
    ico = ASSETS / 'icon.ico'
    image.save(png, format='PNG', optimize=True)
    image.save(ico, format='ICO', sizes=[(n, n) for n in (16, 32, 48, 64, 128, 256)])
    icons_dir = ASSETS / 'icons'
    icons_dir.mkdir(parents=True, exist_ok=True)
    for name, paths in SVG_ICONS.items():
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" '
               'fill="none" stroke="#8b93a5" stroke-width="1.8" stroke-linecap="round" '
               'stroke-linejoin="round">' + paths + '</svg>')
        (icons_dir / f'{name}.svg').write_text(svg, encoding='utf-8')
    print(f'Icons generated: {png} and {ico}')


if __name__ == '__main__':
    make_icon()
