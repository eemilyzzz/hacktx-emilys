"""Create the fictional, image-only OCR demo fixture."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

root = Path(__file__).resolve().parents[1]
image = Image.new("RGB", (1100, 1200), "#f5eee1")
draw = ImageDraw.Draw(image)
font_path = Path("C:/Windows/Fonts/arial.ttf")
if not font_path.exists():
    font_path = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")


def font(size):
    return ImageFont.truetype(str(font_path), size) if font_path.exists() else ImageFont.load_default(size=size)


draw.rectangle((0, 0, 1100, 28), fill="#256b56")
draw.text((80, 90), "PHOTO CLUB", font=font(36), fill="#256b56")
draw.text((80, 200), "Night photography", font=font(76), fill="#202d27")
draw.text((80, 300), "walk", font=font(100), fill="#202d27")
draw.line((80, 470, 1020, 470), fill="#256b56", width=3)
draw.text((80, 550), "November 12, 2026", font=font(65), fill="#202d27")
draw.text((80, 660), "7:00 PM - 9:00 PM", font=font(60), fill="#202d27")
draw.text((80, 800), "Location: Campus fountain", font=font(49), fill="#202d27")
draw.text((80, 920), "Bring a camera. Beginners welcome.", font=font(39), fill="#202d27")
draw.text((80, 1100), "FICTIONAL EVENT / OCR DEMO", font=font(29), fill="#256b56")
image.save(root / "examples" / "demo_flyer.png")
