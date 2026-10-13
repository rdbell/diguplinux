"""Small synthetic files, generated locally without additional Python packages."""

import math
from pathlib import Path


def pdf(path: Path, pages: list[str]):
    page_ids = [3 + i for i in range(len(pages))]
    font_id = 3 + len(pages)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            "<< /Type /Pages /Kids [" + " ".join(f"{i} 0 R" for i in page_ids) + f"] /Count {len(pages)} >>"
        ).encode(),
    ]
    for i in range(len(pages)):
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
                f"/Contents {font_id + 1 + i} 0 R >>"
            ).encode()
        )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for text in pages:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 16 Tf 50 720 Td ({escaped}) Tj ET".encode()
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n").encode()
    )
    path.write_bytes(output)


def landscape(path: Path):
    width, height = 640, 480
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            color = (90, 150, 230)
            if y > 300 + 25 * math.sin(x / 50):
                color = (90, 150, 80)
            if y > 370 + 20 * math.sin(x / 35):
                color = (60, 120, 60)
            if (x - 490) ** 2 + (y - 110) ** 2 < 40**2:
                color = (255, 230, 150)
            if 340 <= y < 400 and 122 <= x < 138:
                color = (110, 80, 50)
            if 260 <= y < 340 and abs(x - 130) <= (y - 260) // 2:
                color = (30, 90, 40)
            pixels.extend(color)
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode() + pixels)
