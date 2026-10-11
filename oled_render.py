"""Renderowanie klatki 128x40 dla ekranu OLED klawiatury Apex Pro.

Dlaczego wlasna bitmapa, a nie tekstowy handler GameSense: API JSON pozwala
tylko ukladac linie jedna pod druga na cala szerokosc ekranu i nie ma zadnego
sposobu na umieszczenie tekstu w konkretnym miejscu. Zeby wpisac czas pracy
w prawy gorny rog, trzeba narysowac cala klatke samemu i wyslac ja jako
"image-data" - SDK to obsluguje.

Uklad ekranu - nazwa urzadzenia i procent sa w osobnych wierszach, bo razem
w jednej linii zajmowaly cala szerokosc i wymuszaly drobna, nieczytelna czcionke.
Rozdzielenie pozwala pokazac procent duzo wieksza czcionka:

    +----------------------------------------+
    | SLUCHAWKI                       5h30m  |   <- nazwa + czas pracy
    |  42%                             -18%  |   <- procent + ile ubylo
    | ###################                    |   <- pasek postepu
    +----------------------------------------+

Licznik ubytku stoi w pasie z duzym procentem, po jego prawej stronie, bo to
jedyne miejsce na ekranie, ktore bylo puste. Duza czcionka procentu nie musiala
sie z tego powodu zmniejszyc: "100%" w rozmiarze 22 zajmuje okolo 60 z 128
pikseli, wiec licznik miesci sie w tym, co zostawalo niewykorzystane.

Wynikiem jest lista 640 bajtow (128 * 40 / 8), gdzie kazdy bit to jeden piksel,
najstarszy bit pierwszy, 1 = piksel swiecacy. Taki format wymaga GameSense.
"""

from functools import lru_cache

from PIL import Image, ImageDraw, ImageFont

WIDTH = 128
HEIGHT = 40

# Rozmiar klatki w bajtach, ktorego wymaga GameSense: ceil(szerokosc * wysokosc / 8).
FRAME_BYTES = WIDTH * HEIGHT // 8

# Podzial ekranu na trzy pasy: nazwa z czasem pracy, duzy procent, pasek.
LABEL_AREA_TOP = 0
LABEL_AREA_HEIGHT = 11

# Miedzy procentem a paskiem zostaje jeden wolny wiersz - bez niego cyfry
# dotykaja ramki paska i zlewaja sie z nia.
VALUE_AREA_TOP = 11
VALUE_AREA_HEIGHT = 16

# Pasek jest nizszy niz tekst, bo wysokosc oddana cyfrom daje wiecej czytelnosci
# niz grubszy pasek. Ponizej ~10 pikseli ukosne kreski animacji rozpadaja sie
# jednak na kropki, wiec to jest praktyczne minimum.
BAR_TOP = 28
BAR_BOTTOM = 39

# Odstep miedzy nazwa a czasem pracy, zeby nigdy sie nie zlaly.
TEXT_GAP = 5

# Grubosc ramki paska i odstep miedzy ramka a wypelnieniem.
BAR_BORDER = 1
BAR_PADDING = 1

# Wzor animacji ladowania: ukosne paski o tej szerokosci i odstepie.
STRIPE_PERIOD = 10
STRIPE_WIDTH = 4

# Prog binaryzacji - czcionki TrueType renderuja sie z antyaliasingiem, a ekran
# jest jednobitowy. Nizszy prog daje grubsze, lepiej widoczne litery.
_THRESHOLD = 96

# Ekran ma 128 pikseli. Wiecej tekstu nie poprawi obrazu, za to mierzenie
# i przechowywanie dowolnie dlugich napisow mogloby zatrzymac renderer.
_MAX_TEXT_CHARACTERS = 256


def _load_font(names, size):
    """Zwraca pierwsza dostepna czcionke z listy, albo wbudowana awaryjna."""
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


# Tahoma i Verdana sa zaprojektowane pod male rozmiary na ekranie, wiec czytaja
# sie lepiej niz Arial po zbinaryzowaniu do jednego bitu.
# Wszedzie czcionka pogrubiona - na ekranie jednobitowym cienkie kreski gina,
# a pogrubienie jest najtanszym sposobem na poprawe czytelnosci.
_LABEL_FONT_NAMES = ("tahomabd.ttf", "verdanab.ttf", "arialbd.ttf")

# Kazdy napis dostaje najwieksza czcionke, ktora zmiesci sie w dostepnym miejscu.
# Dzieki temu dluga nazwa sama sie zmniejsza, zamiast nachodzic na czas pracy,
# a krotka zostaje duza.
_LABEL_SIZES = (12, 11, 10, 9)
_VALUE_SIZES = (22, 21, 20, 19, 18, 16, 14)

# Gdy nie ma procentu (komunikat "BRAK DANYCH"), tekst dostaje oba pasy naraz.
_MESSAGE_SIZES = (22, 20, 18, 16, 14, 12, 11)

_RUNTIME_SIZES = (14, 13, 12, 11, 10)

# Licznik ubytku. Mniejszy od procentu, bo to informacja uzupelniajaca - gdyby
# byl tej samej wielkosci, dwie liczby obok siebie zlewalyby sie w jedna.
_USED_SIZES = (13, 12, 11, 10)

# Czas pracy dobiera czcionke pierwszy i ma limit szerokosci, zeby nazwie
# urzadzenia zawsze zostalo sensowne miejsce.
RUNTIME_MAX_WIDTH = 52

# Analogiczny limit dla licznika ubytku, liczony wzgledem duzego procentu.
USED_MAX_WIDTH = 40

_ALL_SIZES = sorted(
    set(_LABEL_SIZES + _VALUE_SIZES + _MESSAGE_SIZES + _RUNTIME_SIZES + _USED_SIZES)
)
FONT_BY_SIZE = {size: _load_font(_LABEL_FONT_NAMES, size) for size in _ALL_SIZES}


def _text_box(draw, text, font):
    """Zwraca (szerokosc, wysokosc, offset_x, offset_y) napisu."""
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top, left, top


def _fit_font(draw, text, available_width, sizes, available_height=None):
    """Wybiera najwieksza czcionke z listy, przy ktorej napis miesci sie w obszarze.

    Wysokosc sprawdzamy tam, gdzie napis stoi bezposrednio nad paskiem - bez tego
    duze cyfry wchodzilyby na jego ramke.
    """
    for size in sizes:
        font = FONT_BY_SIZE[size]
        width, height, _, _ = _text_box(draw, text, font)
        if width <= available_width and (available_height is None or height <= available_height):
            return font
    return FONT_BY_SIZE[sizes[-1]]


def _fit_text(draw, text, available_width, sizes, available_height):
    """Dobiera czcionke i skraca za dlugi tekst do przydzielonego obszaru."""
    font = _fit_font(draw, text, available_width, sizes, available_height)

    def fits(value):
        width, height, _, _ = _text_box(draw, value, font)
        return width <= available_width and height <= available_height

    if fits(text):
        return text, font
    if not fits("..."):
        return "", font

    # Wyszukiwanie binarne ogranicza koszt takze dla dlugich nazw z Windows.
    left, right = 0, len(text)
    while left < right:
        middle = (left + right + 1) // 2
        if fits(text[:middle] + "..."):
            left = middle
        else:
            right = middle - 1
    return text[:left] + "...", font


def _draw_centered(draw, text, font, x, area_top, area_height):
    """Rysuje napis wyrownany pionowo w podanym pasie ekranu."""
    _, height, offset_x, offset_y = _text_box(draw, text, font)
    y = area_top + (area_height - height) // 2 - offset_y
    draw.text((x - offset_x, y), text, fill=255, font=font)


def _draw_charging_stripes(canvas, box, step):
    """Wypelnia obszar ukosnymi paskami przesunietymi o numer klatki.

    Przesuniecie liczymy wprost z licznika klatek, o jeden piksel na klatke.
    Wczesniej brala sie ono z fazy 0..1 mnozonej przez okres wzoru i zaokraglanej
    w dol, co przy 8 klatkach na sekunde i okresie 10 pikseli dawalo rytm
    1-1-1-2 piksela - animacja widocznie szarpala co czwarta klatke.

    Paski rysujemy na osobnym obrazku o rozmiarze wypelnienia i wklejamy go
    dopiero na koniec - dzieki temu nie trzeba ich przycinac do ramki paska.
    """
    left, top, right, bottom = box
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return

    stripes = Image.new("L", (width, height), 0)
    stripe_draw = ImageDraw.Draw(stripes)

    offset = int(step) % STRIPE_PERIOD

    x = -height + offset
    while x < width + height:
        # Linia z dolnego-lewego do gornego-prawego daje ukos 45 stopni.
        stripe_draw.line([(x, height), (x + height, 0)], fill=255, width=STRIPE_WIDTH)
        x += STRIPE_PERIOD

    canvas.paste(stripes, (left, top))


def render_frame(
    label_text, percent=None, runtime_text="", charging=False, step=0, used_text=""
):
    """Rysuje klatke i zwraca ja jako liste 640 bajtow dla GameSense.

    label_text   - nazwa urzadzenia w lewym gornym rogu, np. "SLUCHAWKI"
    percent      - poziom baterii 0-100; None oznacza brak danych i wtedy
                   label_text dostaje cala gorna czesc ekranu
    runtime_text - tekst w prawym gornym rogu, np. "5h30m" (moze byc pusty)
    charging     - gdy True, procent dostaje znak "+", a pasek animowane paski
    step         - numer klatki animacji; ignorowany gdy charging jest False
    used_text    - ile ubylo od naladowania, np. "-18%", w pasie z procentem
                   po jego prawej stronie (moze byc pusty)
    """
    normalized_percent = None if percent is None else max(0, min(100, int(percent)))
    label_text = label_text[:_MAX_TEXT_CHARACTERS]
    runtime_text = runtime_text[:_MAX_TEXT_CHARACTERS]
    used_text = used_text[:_MAX_TEXT_CHARACTERS]
    # Tylko faza wzoru wplywa na obraz. Numer klatki rozladowywania nie ma
    # znaczenia, a ladowanie powtarza sie co STRIPE_PERIOD klatek.
    phase = int(step) % STRIPE_PERIOD if charging and normalized_percent is not None else 0
    return list(_render_frame_cached(
        label_text, normalized_percent, runtime_text, bool(charging), phase, used_text
    ))


@lru_cache(maxsize=128)
def _render_frame_cached(label_text, percent, runtime_text, charging, step, used_text):
    # Rysujemy w odcieniach szarosci, bo czcionki maja antyaliasing, a dopiero
    # na koniec binaryzujemy calosc jednym progiem.
    canvas = Image.new("L", (WIDTH, HEIGHT), 0)
    draw = ImageDraw.Draw(canvas)

    if percent is None:
        # Komunikat bez danych - nie ma czego pokazywac w drugim wierszu,
        # wiec tekst dostaje oba pasy i moze byc odpowiednio wiekszy.
        message_height = LABEL_AREA_HEIGHT + VALUE_AREA_HEIGHT
        label_text, font = _fit_text(draw, label_text, WIDTH, _MESSAGE_SIZES, message_height)
        _draw_centered(draw, label_text, font, 0, LABEL_AREA_TOP, message_height)
        percent = 0
    else:
        percent = max(0, min(100, int(percent)))

        # Czas pracy trafia w prawy gorny rog, wiec jego szerokosc trzeba znac
        # przed dobraniem czcionki nazwy urzadzenia.
        runtime_width = 0
        if runtime_text:
            runtime_text, runtime_font = _fit_text(
                draw, runtime_text, RUNTIME_MAX_WIDTH, _RUNTIME_SIZES, LABEL_AREA_HEIGHT
            )
            runtime_width = _text_box(draw, runtime_text, runtime_font)[0]
            _draw_centered(
                draw,
                runtime_text,
                runtime_font,
                WIDTH - runtime_width - 1,
                LABEL_AREA_TOP,
                LABEL_AREA_HEIGHT,
            )

        available = WIDTH - runtime_width - (TEXT_GAP if runtime_width else 0)
        label_text, label_font = _fit_text(
            draw, label_text, available, _LABEL_SIZES, LABEL_AREA_HEIGHT
        )
        _draw_centered(draw, label_text, label_font, 0, LABEL_AREA_TOP, LABEL_AREA_HEIGHT)

        # Licznik ubytku idzie pierwszy, bo zabiera miejsce duzemu procentowi.
        # W praktyce nie zmusza go do zmniejszenia czcionki: procent zajmuje
        # najwyzej okolo 60 pikseli, a licznik miesci sie w reszcie.
        used_width = 0
        if used_text:
            used_text, used_font = _fit_text(
                draw, used_text, USED_MAX_WIDTH, _USED_SIZES, VALUE_AREA_HEIGHT
            )
            used_width = _text_box(draw, used_text, used_font)[0]
            _draw_centered(
                draw,
                used_text,
                used_font,
                WIDTH - used_width - 1,
                VALUE_AREA_TOP,
                VALUE_AREA_HEIGHT,
            )

        value_available = WIDTH - used_width - (TEXT_GAP if used_width else 0)
        value_text = f"+{percent}%" if charging else f"{percent}%"
        value_text, value_font = _fit_text(
            draw, value_text, value_available, _VALUE_SIZES, VALUE_AREA_HEIGHT
        )
        _draw_centered(draw, value_text, value_font, 0, VALUE_AREA_TOP, VALUE_AREA_HEIGHT)

    draw.rectangle(
        [0, BAR_TOP, WIDTH - 1, BAR_BOTTOM],
        outline=255,
        width=BAR_BORDER,
    )

    inner_left = BAR_BORDER + BAR_PADDING
    inner_top = BAR_TOP + BAR_BORDER + BAR_PADDING
    inner_right = WIDTH - 1 - BAR_BORDER - BAR_PADDING
    inner_bottom = BAR_BOTTOM - BAR_BORDER - BAR_PADDING

    inner_width = inner_right - inner_left + 1
    fill_width = round(inner_width * percent / 100)

    if fill_width > 0:
        fill_box = (inner_left, inner_top, inner_left + fill_width, inner_bottom + 1)
        if charging:
            _draw_charging_stripes(canvas, fill_box, step)
        else:
            draw.rectangle(
                [fill_box[0], fill_box[1], fill_box[2] - 1, fill_box[3] - 1],
                fill=255,
            )

    mono = canvas.point(lambda value: 255 if value >= _THRESHOLD else 0).convert("1")
    return tuple(mono.tobytes())


def frame_to_image(data):
    """Zamienia bajty klatki z powrotem na obrazek - do podgladu na monitorze."""
    payload = bytes(data)
    if len(payload) != FRAME_BYTES:
        raise ValueError(f"OLED frame must contain exactly {FRAME_BYTES} bytes")
    return Image.frombytes("1", (WIDTH, HEIGHT), payload)


# Reprezentatywne stany ekranu, uzywane do wygenerowania podgladu.
# Kolejnosc pol: nazwa, procent, czas pracy, ladowanie, klatka, ubytek.
PREVIEW_STATES = (
    ("MYSZ", 75, "~60h", False, 0, "-25%"),
    ("SLUCHAWKI", 42, "9h", False, 0, "-58%"),
    ("MYSZ", 75, "LAD", True, 0, ""),
    ("MYSZ", 75, "LAD", True, 5, ""),
    ("SLUCHAWKI", 8, "<1h", False, 0, "-92%"),
    # Pelna bateria zaraz po odlaczeniu kabla: ubytku jeszcze nie ma, wiec
    # licznik jest pusty i procent moze zajac cala szerokosc.
    ("MYSZ", 100, "18h", False, 0, ""),
    ("BRAK DANYCH", None, "", False, 0, ""),
)


def save_preview(path, scale=3):
    """Sklada wszystkie stany ekranu w jeden obrazek PNG.

    Powiekszamy metoda NEAREST, zeby pojedyncze piksele byly widoczne, i dodajemy
    szara linie miedzy klatkami, zeby bylo jasne, gdzie konczy sie ekran.
    """
    frames = [frame_to_image(render_frame(*state)) for state in PREVIEW_STATES]

    separator = 2
    total_height = len(frames) * (HEIGHT + separator) - separator
    sheet = Image.new("L", (WIDTH, total_height), 64)

    for index, frame in enumerate(frames):
        sheet.paste(frame.convert("L"), (0, index * (HEIGHT + separator)))

    sheet.resize((WIDTH * scale, total_height * scale), Image.NEAREST).save(path)
    return path


if __name__ == "__main__":
    # python oled_render.py - odswieza podglad ukladu ekranu bez ruszania OLED.
    print("Zapisano", save_preview("preview.png"))
