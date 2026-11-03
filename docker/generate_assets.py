from pathlib import Path

from PIL import Image, ImageDraw


def main():
    image = Image.new("RGBA", (288, 320), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((41, 287, 247, 308), fill=(39, 58, 46, 22))
    draw.rounded_rectangle((56, 32, 232, 293), radius=20, fill="#69736d")
    draw.rounded_rectangle((50, 25, 224, 286), radius=18, fill="#c0c6c0")
    draw.rounded_rectangle((57, 32, 217, 278), radius=14, fill="#a4aea7")
    for row in range(28):
        for column in range(17):
            left = 66 + column * 8
            top = 72 + row * 7
            draw.ellipse((left, top, left + 2, top + 2), fill="#758279")
    draw.rounded_rectangle((92, 48, 181, 62), radius=4, fill="#34483c")
    for column in range(9):
        draw.rectangle((103 + column * 7, 53, 105 + column * 7, 56), fill="#accc89")
    draw.line((86, 270, 188, 270), fill="#dae0d9", width=2)
    target = Path(__file__).resolve().parents[1] / "src/spk_platform/static/speaker.png"
    image.save(target, optimize=True)


if __name__ == "__main__":
    main()
