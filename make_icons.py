"""Przygotowuje logo i ikone aplikacji z pliku zrodlowego.

Uruchomienie:  python make_icons.py

Z jednego rysunku zrodlowego (assets/logo_source.png) powstaja:
  - assets/logo.png  - 256x256, do naglowka okna,
  - assets/icon.ico  - ikona wielorozmiarowa dla okna, paska zadan i zasobnika.

Najmniejsze rozmiary dostaja uproszczony rysunek: przy 16 pikselach cienka
kreska symbolizujaca ekran OLED zlewa sie z ramka baterii w nieczytelna plame,
wiec dla 16-24 px zostaje sama bateria. Pillow pozwala wlozyc do pliku ICO
osobna grafike dla kazdego rozmiaru przez append_images.
"""

import os

from PIL import Image, ImageChops, ImageDraw

_HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(_HERE, "assets")

SOURCE = os.path.join(ASSETS, "logo_source.png")
LOGO_PNG = os.path.join(ASSETS, "logo.png")
ICON_ICO = os.path.join(ASSETS, "icon.ico")

# Rozmiary, ktore Windows wybiera zaleznie od miejsca uzycia ikony.
ICO_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)

# Do tego rozmiaru uzywamy uproszczonego rysunku (sama bateria).
COMPACT_UP_TO = 24

# Jak duzy margines zostawic wokol rysunku - mniejszy przy malych ikonach,
# zeby glif byl jak najwiekszy.
MARGIN_FULL = 0.08
MARGIN_COMPACT = 0.04

# Minimalne nasycenie koloru, przy ktorym piksel uznajemy za tresc rysunku.
_SATURATION_THRESHOLD = 40


def _background_color(image):
    """Kolor tla rysunku, probkowany z gornej krawedzi miedzy zaokragleniami."""
    return image.getpixel((image.width // 2, 4))


def _content_mask(image):
    """Maska pikseli nalezacych do rysunku, a nie do tla.

    Kluczujemy po nasyceniu koloru, a nie po odleglosci od koloru tla. Rysunek
    to nasycony pomaranczowy na neutralnej szarosci, natomiast zaokraglone naroza
    zrodla sa czarne - przy porownywaniu z tlem liczylyby sie jako tresc
    i ciasne obciecie nic by nie dawalo.
    """
    red, green, blue = image.convert("RGB").split()
    brightest = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    darkest = ImageChops.darker(ImageChops.darker(red, green), blue)
    saturation = ImageChops.subtract(brightest, darkest)
    return saturation.point(lambda value: 255 if value > _SATURATION_THRESHOLD else 0, mode="1")


def _row_bands(mask):
    """Zwraca zakresy wierszy z trescia, rozdzielone pustymi pasami.

    Rysunek zrodlowy sklada sie z dwoch elementow jeden nad drugim: kreski
    ekranu OLED i baterii. Rozdzielenie ich pozwala uzyc samej baterii
    w najmniejszych ikonach.
    """
    filled = [
        mask.crop((0, y, mask.width, y + 1)).getbbox() is not None for y in range(mask.height)
    ]

    bands = []
    start = None
    for y, has_content in enumerate(filled):
        if has_content and start is None:
            start = y
        elif not has_content and start is not None:
            bands.append((start, y - 1))
            start = None
    if start is not None:
        bands.append((start, mask.height - 1))
    return bands


def _crop_to_content(image, mask, row_range=None):
    """Wycina fragment rysunku ciasno wokol jego tresci."""
    if row_range is not None:
        top, bottom = row_range
        mask = mask.crop((0, top, mask.width, bottom + 1))
        image = image.crop((0, top, image.width, bottom + 1))

    box = mask.getbbox()
    return image.crop(box) if box else image


def _compose(content, size, margin, background):
    """Wkleja rysunek wysrodkowany na zaokraglonym kwadracie w kolorze tla."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # Zaokraglone naroza zostaja przezroczyste, wiec ikona dobrze wyglada
    # zarowno na jasnym, jak i ciemnym pasku zadan.
    radius = max(2, round(size * 0.18))
    shape = Image.new("L", (size, size), 0)
    ImageDraw.Draw(shape).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    canvas.paste(Image.new("RGBA", (size, size), tuple(background[:3]) + (255,)), (0, 0), shape)

    inner = max(1, round(size * (1 - 2 * margin)))
    scale = min(inner / content.width, inner / content.height)
    width = max(1, round(content.width * scale))
    height = max(1, round(content.height * scale))
    resized = content.resize((width, height), Image.LANCZOS).convert("RGBA")

    canvas.paste(resized, ((size - width) // 2, (size - height) // 2), resized)
    return canvas


def build():
    source = Image.open(SOURCE).convert("RGB")
    background = _background_color(source)

    mask = _content_mask(source)
    bands = _row_bands(mask)

    full = _crop_to_content(source, mask)

    # Bateria to najwyzszy element rysunku - bierzemy pas o najwiekszej wysokosci.
    if len(bands) > 1:
        tallest = max(bands, key=lambda band: band[1] - band[0])
        compact = _crop_to_content(source, mask, tallest)
    else:
        compact = full

    layers = []
    for size in ICO_SIZES:
        if size <= COMPACT_UP_TO:
            layers.append(_compose(compact, size, MARGIN_COMPACT, background))
        else:
            layers.append(_compose(full, size, MARGIN_FULL, background))

    logo = _compose(full, 256, MARGIN_FULL, background)
    logo.save(LOGO_PNG)

    # Pierwsza warstwa jest zapisywana normalnie, pozostale przez append_images.
    layers[-1].save(
        ICON_ICO,
        format="ICO",
        sizes=[(size, size) for size in ICO_SIZES],
        append_images=layers[:-1],
    )

    return bands, len(layers)


if __name__ == "__main__":
    found_bands, layer_count = build()
    print(f"Pasy tresci w zrodle: {found_bands}")
    print(f"Zapisano {LOGO_PNG}")
    print(f"Zapisano {ICON_ICO} ({layer_count} warstw: {', '.join(map(str, ICO_SIZES))})")
