#!/usr/bin/env python3
"""
Generate the purple flame app icon: an SVG (vector source + favicon) plus a
PNG and a multi-size Windows .ico rasterised from the same flame path.

Run:  py -3 assets/make_flame_icon.py
Outputs (into this assets/ folder): flame.svg, flame.png, flame@512.png, flame.ico
"""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))

OUTER = (124, 58, 237, 255)   # #7C3AED  violet-600
INNER = (167, 139, 250, 255)  # #A78BFA  violet-400
CORE  = (237, 233, 254, 255)  # #EDE9FE  violet-100

# Flame silhouette as 4 cubic beziers (P0, C1, C2, P3) in a 0..1 box (y down).
SEGMENTS = [
    ((0.50, 0.04), (0.63, 0.17), (0.80, 0.31), (0.80, 0.55)),
    ((0.80, 0.55), (0.80, 0.77), (0.66, 0.93), (0.50, 0.96)),
    ((0.50, 0.96), (0.34, 0.93), (0.20, 0.77), (0.20, 0.55)),
    ((0.20, 0.55), (0.20, 0.31), (0.37, 0.17), (0.50, 0.04)),
]
ANCHOR = (0.50, 0.66)                       # inner flames shrink toward here
LAYERS = [(1.00, OUTER), (0.64, INNER), (0.32, CORE)]


def _bez(p0, c1, c2, p3, n=48):
    out = []
    for i in range(n + 1):
        t = i / n
        m = 1 - t
        x = m*m*m*p0[0] + 3*m*m*t*c1[0] + 3*m*t*t*c2[0] + t*t*t*p3[0]
        y = m*m*m*p0[1] + 3*m*m*t*c1[1] + 3*m*t*t*c2[1] + t*t*t*p3[1]
        out.append((x, y))
    return out


def _scale(p, s):
    ax, ay = ANCHOR
    return (ax + (p[0] - ax) * s, ay + (p[1] - ay) * s)


def _outline(scale):
    pts = []
    for (p0, c1, c2, p3) in SEGMENTS:
        pts.extend(_bez(_scale(p0, scale), _scale(c1, scale),
                        _scale(c2, scale), _scale(p3, scale)))
    return pts


def render(size):
    ss = 8
    big = size * ss
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    for scale, col in LAYERS:
        draw.polygon([(x * big, y * big) for (x, y) in _outline(scale)], fill=col)
    return img.resize((size, size), Image.LANCZOS)


def _svg_d(scale):
    start = _scale(SEGMENTS[0][0], scale)
    d = f"M {start[0]*100:.2f} {start[1]*100:.2f} "
    for (p0, c1, c2, p3) in SEGMENTS:
        a, b, c = _scale(c1, scale), _scale(c2, scale), _scale(p3, scale)
        d += (f"C {a[0]*100:.2f} {a[1]*100:.2f} {b[0]*100:.2f} {b[1]*100:.2f} "
              f"{c[0]*100:.2f} {c[1]*100:.2f} ")
    return d + "Z"


def write_svg(path):
    hexc = lambda c: "#%02x%02x%02x" % (c[0], c[1], c[2])
    body = "\n".join(f'  <path d="{_svg_d(s)}" fill="{hexc(c)}"/>' for s, c in LAYERS)
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" '
           'width="256" height="256">\n' + body + "\n</svg>\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)


def main():
    render(256).save(os.path.join(HERE, "flame.png"))
    render(512).save(os.path.join(HERE, "flame@512.png"))
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    render(256).save(os.path.join(HERE, "flame.ico"), format="ICO", sizes=sizes)
    write_svg(os.path.join(HERE, "flame.svg"))
    print("wrote flame.svg, flame.png, flame@512.png, flame.ico to", HERE)


if __name__ == "__main__":
    main()
