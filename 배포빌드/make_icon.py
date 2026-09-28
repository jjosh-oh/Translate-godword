"""LiveWord 아이콘 생성 (liveword.ico) — 남색 그라데이션 + 통역 헤드셋 + 가/A.
작은 크기(16·24px)는 글자를 빼고 헤드셋만 그려 뭉개지지 않게 한다.
다시 만들려면: python make_icon.py  (Pillow 필요)"""
import os
from PIL import Image, ImageDraw, ImageFont, ImageFilter

S = 1024
WH, GOLD = (255, 255, 255, 255), (246, 196, 76, 255)
FONTS = [  # 굵은 한글 글꼴 — 있는 것 첫 번째를 쓴다
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Black.ttc", 1),
    (r"C:\Windows\Fonts\malgunbd.ttf", 0),
]

def font(sz):
    for path, idx in FONTS:
        if os.path.exists(path):
            return ImageFont.truetype(path, sz, index=idx)
    raise SystemExit("굵은 한글 글꼴을 찾지 못했습니다")

def background():
    g = Image.new("RGBA", (S, S)); p = g.load()
    c1, c2 = (52, 84, 160), (16, 30, 70)
    for y in range(S):
        for x in range(S):
            t = (x * 0.35 + y) / (1.35 * S)
            p[x, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3)) + (255,)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([48, 48, S - 48, S - 48], radius=230, fill=255)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0)); img.paste(g, (0, 0), mask)
    hl = Image.new("RGBA", (S, S), (255, 255, 255, 0))
    ImageDraw.Draw(hl).ellipse([-200, -760, S + 200, 440], fill=(255, 255, 255, 34))
    hl = hl.filter(ImageFilter.GaussianBlur(90))
    return Image.alpha_composite(img, Image.composite(hl, Image.new("RGBA", (S, S)), mask))

def with_shadow(img, layer):
    a = layer.split()[3].point(lambda v: v * 70 // 255)
    sh = Image.new("RGBA", (S, S), (0, 0, 0, 0)); sh.putalpha(a)
    sh = sh.filter(ImageFilter.GaussianBlur(26))
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0)); out.paste(sh, (0, 18), sh)
    return Image.alpha_composite(Image.alpha_composite(img, out), layer)

def draw(small=False):
    img = background()
    L = Image.new("RGBA", (S, S), (0, 0, 0, 0)); d = ImageDraw.Draw(L)
    cx, cy, r = 512, (520 if small else 560), (320 if small else 300)
    band, cup = (90 if small else 62), (100 if small else 78)
    d.arc([cx - r, cy - r, cx + r, cy + r], start=180, end=360, fill=WH, width=band)
    for sx in (-1, 1):
        x = cx + sx * r
        d.rounded_rectangle([x - cup, cy - 40, x + cup, cy + 230], radius=70, fill=GOLD)
    if not small:  # 마이크 붐
        d.line([(cx - r + 40, cy + 190), (cx - r + 60, cy + 300), (cx - 120, cy + 320)],
               fill=WH, width=34, joint="curve")
        d.ellipse([cx - 150, cy + 285, cx - 80, cy + 355], fill=WH)
    img = with_shadow(img, L)
    if not small:
        d = ImageDraw.Draw(img)
        d.text((cx - 78, cy + 40), "가", font=font(170), fill=WH, anchor="mm")
        d.text((cx + 92, cy + 40), "A", font=font(185), fill=GOLD, anchor="mm")
    return img

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    big, small = draw(), draw(small=True)
    frames = [big.resize((s, s), Image.LANCZOS) for s in (256, 128, 64, 48, 32)]
    frames += [small.resize((s, s), Image.LANCZOS) for s in (24, 16)]
    frames[0].save(os.path.join(here, "liveword.ico"), format="ICO",
                   sizes=[f.size for f in frames], append_images=frames[1:])
    big.resize((512, 512), Image.LANCZOS).save(os.path.join(here, "liveword.png"))
    print("ok")
