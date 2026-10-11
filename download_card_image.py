#!/usr/bin/env python3
"""
Download Yu-Gi-Oh card images from cardcluster.de, process them,
and optionally build a printable 9-up A4 PDF from a text file list.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image


API_BASE = "https://cardcluster.de/api/cards"
LANGUAGE_HEADERS = {
    "de": "de-DE",
    "en": "en-GB",
}
PRINT_DPI = 300
A4_MM = (210, 297)
CARD_MM = (59, 86)
CARDS_PER_ROW = 3
CARDS_PER_COLUMN = 3


class CardLookupError(RuntimeError):
    pass


@dataclass
class CardImageInfo:
    slug: str
    name: str
    image_base: str


def build_request_headers(language: str) -> dict[str, str]:
    return {
        "accept-language": LANGUAGE_HEADERS[language],
        "accept-card-language": language,
        "accept-game": "ygo",
        "standalone": "false",
        "user-agent": "cardcluster-image-downloader/1.0",
    }


def slugify_card_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.lower()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9]+", "-", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    if not normalized:
        raise CardLookupError("Could not derive a slug from the provided card name.")
    return normalized


def mm_to_px(mm: float, dpi: int = PRINT_DPI) -> int:
    return round(mm / 25.4 * dpi)


def fetch_card_data(session: requests.Session, slug: str, language: str) -> dict:
    response = session.get(
        f"{API_BASE}/{slug}",
        headers=build_request_headers(language),
        timeout=30,
    )
    if response.status_code == 404:
        raise CardLookupError(
            f"Card not found for slug '{slug}'. "
            "If cardcluster uses a different slug, pass it via --slug."
        )
    response.raise_for_status()
    payload = response.json()
    if "data" not in payload or not payload["data"]:
        raise CardLookupError(f"Card payload for slug '{slug}' did not contain card data.")
    return payload["data"]


def resolve_card(
    session: requests.Session,
    name: str,
    explicit_slug: str | None,
    language: str,
) -> CardImageInfo:
    slug = explicit_slug or slugify_card_name(name)
    data = fetch_card_data(session, slug, language)
    image_base = data.get("image")
    if not image_base:
        raise CardLookupError(f"Card '{data.get('name') or slug}' did not contain an image URL.")
    return CardImageInfo(slug=data["slug"], name=data.get("name") or name, image_base=image_base)


def candidate_image_urls(image_base: str) -> list[str]:
    return [f"{image_base}{extension}" for extension in (".webp", ".png", ".jpg", ".jpeg")]


def download_image(session: requests.Session, image_base: str, language: str) -> Image.Image:
    last_error: Exception | None = None
    headers = build_request_headers(language)
    for url in candidate_image_urls(image_base):
        try:
            response = session.get(url, headers=headers, timeout=60)
            if response.status_code == 404:
                continue
            response.raise_for_status()
            image = Image.open(BytesIO(response.content))
            return image.convert("RGB")
        except Exception as exc:  # pragma: no cover
            last_error = exc
    raise CardLookupError(f"Could not download an image from base URL '{image_base}'.") from last_error


def crop_scan_border(img: Image.Image) -> Image.Image:
    width, height = img.size
    crop_box = (
        int(width * 0.031),
        int(height * 0.022),
        width - int(width * 0.031),
        height - int(height * 0.022),
    )
    return img.crop(crop_box)


def default_output_path(slug: str, output_dir: Path, fmt: str) -> Path:
    return output_dir / f"{slug}.{fmt}"


def save_image(img: Image.Image, output_path: Path, fmt: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    save_kwargs = {"quality": 95} if fmt in {"jpg", "webp"} else {}
    image_format = "JPEG" if fmt == "jpg" else fmt.upper()
    img.save(output_path, format=image_format, **save_kwargs)


def read_card_names(input_file: Path) -> list[str]:
    names: list[str] = []
    for line in input_file.read_text(encoding="utf-8").splitlines():
        card_name = line.strip()
        if not card_name or card_name.startswith("#"):
            continue
        names.append(card_name)
    return names


def resolve_output_paths(
    session: requests.Session,
    card_names: list[str],
    output_dir: Path,
    fmt: str,
    language: str,
) -> tuple[list[tuple[str, CardImageInfo, Path]], list[str]]:
    resolved: list[tuple[str, CardImageInfo, Path]] = []
    issues: list[str] = []

    for card_name in card_names:
        try:
            card = resolve_card(session, card_name, None, language)
        except (requests.RequestException, CardLookupError) as exc:
            issues.append(f"{card_name}: {exc}")
            continue
        resolved.append((card_name, card, default_output_path(card.slug, output_dir, fmt)))

    return resolved, issues


def ensure_card_image(
    session: requests.Session,
    card: CardImageInfo,
    output_path: Path,
    fmt: str,
    language: str,
) -> None:
    if output_path.exists():
        return
    image = download_image(session, card.image_base, language)
    image = crop_scan_border(image)
    save_image(image, output_path, fmt)


def create_pdf(image_paths: list[Path], pdf_path: Path) -> None:
    a4_width = mm_to_px(A4_MM[0])
    a4_height = mm_to_px(A4_MM[1])
    card_width = mm_to_px(CARD_MM[0])
    card_height = mm_to_px(CARD_MM[1])
    grid_width = card_width * CARDS_PER_ROW
    grid_height = card_height * CARDS_PER_COLUMN
    margin_x = (a4_width - grid_width) // 2
    margin_y = (a4_height - grid_height) // 2

    pages: list[Image.Image] = []
    cards_per_page = CARDS_PER_ROW * CARDS_PER_COLUMN

    for page_index in range(math.ceil(len(image_paths) / cards_per_page)):
        page = Image.new("RGB", (a4_width, a4_height), "white")
        chunk = image_paths[page_index * cards_per_page:(page_index + 1) * cards_per_page]
        for idx, image_path in enumerate(chunk):
            with Image.open(image_path) as card_image:
                card = card_image.convert("RGB").resize((card_width, card_height), Image.Resampling.LANCZOS)
            row = idx // CARDS_PER_ROW
            col = idx % CARDS_PER_ROW
            x = margin_x + col * card_width
            y = margin_y + row * card_height
            page.paste(card, (x, y))
        pages.append(page)

    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(
        pdf_path,
        save_all=True,
        append_images=pages[1:],
        resolution=PRINT_DPI,
    )


def process_single_card(args: argparse.Namespace) -> int:
    session = requests.Session()
    try:
        card = resolve_card(session, args.card_name, args.slug, args.language)
        image = download_image(session, card.image_base, args.language)
        image = crop_scan_border(image)
        output_path = args.output or default_output_path(card.slug, args.output_dir, args.format)
        save_image(image, output_path, args.format)
    except (requests.RequestException, CardLookupError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(output_path.resolve())
    return 0


def process_batch(args: argparse.Namespace) -> int:
    session = requests.Session()
    card_names = read_card_names(args.input_file)
    if not card_names:
        print("Error: input file did not contain any card names.", file=sys.stderr)
        return 1

    resolved_cards, issues = resolve_output_paths(
        session,
        card_names,
        args.output_dir,
        args.format,
        args.language,
    )

    if issues:
        print("The following card names could not be resolved:", file=sys.stderr)
        for issue in issues:
            print(f"- {issue}", file=sys.stderr)
        return 1

    downloaded = 0
    skipped = 0
    output_paths: list[Path] = []

    try:
        for _, card, output_path in resolved_cards:
            if output_path.exists():
                skipped += 1
            else:
                ensure_card_image(session, card, output_path, args.format, args.language)
                downloaded += 1
            output_paths.append(output_path)
    except (requests.RequestException, CardLookupError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    pdf_path = args.pdf or args.output_dir / f"{args.input_file.stem}.pdf"
    create_pdf(output_paths, pdf_path)

    print(f"Downloaded: {downloaded}")
    print(f"Skipped existing: {skipped}")
    print(f"PDF: {pdf_path.resolve()}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download and clean card images from cardcluster.de."
    )
    parser.add_argument(
        "card_name",
        nargs="?",
        help="Single card name. Omit when using --input-file.",
    )
    parser.add_argument(
        "--input-file",
        type=Path,
        help="Text file with one card name per line.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Explicit output file path for single-card mode.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Directory for downloaded card images.",
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        help="Output PDF path for batch mode. Defaults to <input-file-stem>.pdf in output-dir.",
    )
    parser.add_argument(
        "--slug",
        help="Override the derived cardcluster slug in single-card mode.",
    )
    parser.add_argument(
        "--format",
        choices=("png", "jpg", "webp"),
        default="png",
        help="Output image format.",
    )
    parser.add_argument(
        "--language",
        choices=("de", "en"),
        default="de",
        help="Card language to request from cardcluster. Defaults to German.",
    )
    args = parser.parse_args()

    if bool(args.card_name) == bool(args.input_file):
        parser.error("Provide exactly one of card_name or --input-file.")
    if args.output and args.input_file:
        parser.error("--output is only supported in single-card mode.")
    if args.slug and args.input_file:
        parser.error("--slug is only supported in single-card mode.")

    return args


def main() -> int:
    args = parse_args()
    if args.input_file:
        return process_batch(args)
    return process_single_card(args)


if __name__ == "__main__":
    raise SystemExit(main())
