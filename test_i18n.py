"""Testy tlumaczen i zapisu wybranego jezyka.

Uruchomienie:  python test_i18n.py

Bez pytest, bez sprzetu i bez otwierania okna. Sprawdzamy to, czego nie widac
przy zwyklym klikaniu po interfejsie: czy oba jezyki maja te same klucze, czy
napisy na ekran OLED zmiescily sie w ASCII i czy parametry w zdaniach zgadzaja
sie miedzy jezykami. Kazdy z tych bledow objawia sie dopiero u uzytkownika,
ktory przelaczyl jezyk - i to czesto w jednym, rzadko odwiedzanym miejscu okna.
"""

import os
import re
import shutil
import sys
import tempfile

import i18n
import settings as app_settings

_failures = []


def check(condition, description, detail=""):
    if condition:
        print(f"  OK   {description}")
    else:
        print(f"  BLAD {description}" + (f" -> {detail}" if detail else ""))
        _failures.append(description)


class Workspace:
    def __init__(self):
        self.directory = tempfile.mkdtemp(prefix="i18n_test_")

    def path(self, name="settings.json"):
        return os.path.join(self.directory, name)

    def cleanup(self):
        shutil.rmtree(self.directory, ignore_errors=True)


def restoring_language(function):
    """Przywraca jezyk po tescie, zeby testy nie zaleznialy od kolejnosci."""

    def wrapper(*arguments):
        original = i18n.language()
        try:
            return function(*arguments)
        finally:
            i18n.set_language(original)

    return wrapper


# ---------------------------------------------------------------------------
# 1. KOMPLETNOSC SLOWNIKOW
# ---------------------------------------------------------------------------


def test_both_languages_have_same_keys():
    print("\n1a. Oba jezyki maja te same klucze")
    reference = i18n.keys(i18n.POLISH)

    for code in i18n.LANGUAGES:
        missing = reference - i18n.keys(code)
        extra = i18n.keys(code) - reference
        check(not missing, f"{code}: nie brakuje zadnego napisu", f"{sorted(missing)}")
        check(not extra, f"{code}: nie ma napisow bez odpowiednika", f"{sorted(extra)}")

    check(len(reference) > 0, "slownik nie jest pusty")


def test_no_empty_translations():
    print("\n1b. Zaden napis nie jest pusty")
    for code in i18n.LANGUAGES:
        i18n.set_language(code)
        blank = [key for key in i18n.keys(code) if not i18n.t(key).strip()]
        check(not blank, f"{code}: kazdy klucz ma tresc", f"{sorted(blank)}")


test_no_empty_translations = restoring_language(test_no_empty_translations)


def test_parameters_match_between_languages():
    print("\n1c. Zdania z parametrami maja te same pola w kazdym jezyku")

    # Napis w jednym jezyku moze przestawic kolejnosc slow, ale nie moze zgubic
    # pola ani wprowadzic nowego - t() dostaje dokladnie te parametry, ktore
    # przewidziano przy wywolaniu, wiec literowka w {adress} pokazalaby
    # uzytkownikowi surowy wzorzec zamiast adresu serwera.
    def fields(code, key):
        i18n.set_language(code)
        return set(re.findall(r"\{(\w+)\}", i18n.t(key)))

    mismatched = []
    for key in sorted(i18n.keys(i18n.POLISH)):
        expected = fields(i18n.POLISH, key)
        for code in i18n.LANGUAGES:
            if fields(code, key) != expected:
                mismatched.append(f"{key} ({code})")

    check(not mismatched, "te same parametry w obu jezykach", f"{mismatched}")


test_parameters_match_between_languages = restoring_language(
    test_parameters_match_between_languages
)


# ---------------------------------------------------------------------------
# 2. OGRANICZENIA EKRANU OLED
# ---------------------------------------------------------------------------


def test_oled_strings_are_ascii():
    print("\n2a. Napisy na ekran OLED sa czystym ASCII")

    # Czcionka jest binaryzowana do jednego bitu i nie gwarantuje diakrytykow -
    # "SŁUCHAWKI" wyszloby na ekranie jako "S UCHAWKI" albo z kwadratem.
    check(len(i18n.OLED_KEYS) > 0, "sa klucze przeznaczone na ekran")

    for code in i18n.LANGUAGES:
        i18n.set_language(code)
        offenders = [key for key in i18n.OLED_KEYS if not i18n.t(key).isascii()]
        check(not offenders, f"{code}: bez znakow poza ASCII", f"{sorted(offenders)}")


test_oled_strings_are_ascii = restoring_language(test_oled_strings_are_ascii)


def test_oled_labels_stay_short():
    print("\n2b. Etykiety na ekran nie rozsadzaja ukladu")

    # Renderer sam zmniejsza czcionke, ale przy bardzo dlugiej nazwie zabraklo by
    # miejsca na czas pracy w prawym gornym rogu. Najdluzsza obecna etykieta,
    # "KLAWIATURA", ma 10 znakow.
    limit = 12
    for code in i18n.LANGUAGES:
        i18n.set_language(code)
        for token in ("MYSZ", "SLUCHAWKI", "KLAWIATURA"):
            label = i18n.oled_label(token)
            check(
                len(label) <= limit,
                f"{code}: {token} miesci sie w {limit} znakach",
                f"{label} ({len(label)})",
            )


test_oled_labels_stay_short = restoring_language(test_oled_labels_stay_short)


def test_labels_are_translated_but_tokens_are_not():
    print("\n2c. Token etykiety nie zmienia sie razem z jezykiem")

    # device_catalog trzyma "MYSZ" jako identyfikator kategorii i monitor_service
    # odsiewa po nim podwojne odczyty. Gdyby token szedl przez tlumaczenie,
    # mysz odczytana po Bluetooth przestalaby byc rozpoznawana jako ta sama,
    # ktora juz odpowiedziala przez HID.
    i18n.set_language(i18n.ENGLISH)
    check(i18n.oled_label("MYSZ") == "MOUSE", "po angielsku etykieta to MOUSE")
    check(i18n.window_label("SLUCHAWKI") == "HEADSET", "sluchawki jako HEADSET")

    i18n.set_language(i18n.POLISH)
    check(i18n.oled_label("MYSZ") == "MYSZ", "po polsku bez zmian")
    check(
        i18n.window_label("SLUCHAWKI") == "SŁUCHAWKI",
        "w oknie polskie znaki sa dozwolone",
    )
    check(i18n.oled_label("COS INNEGO") == "COS INNEGO", "nieznany token wraca bez zmian")


test_labels_are_translated_but_tokens_are_not = restoring_language(
    test_labels_are_translated_but_tokens_are_not
)


# ---------------------------------------------------------------------------
# 3. POBIERANIE NAPISOW
# ---------------------------------------------------------------------------


def test_parameters_are_substituted():
    print("\n3a. Parametry wchodza do napisu")
    i18n.set_language(i18n.POLISH)
    text = i18n.t("service.connected", address="127.0.0.1:52123")

    check("127.0.0.1:52123" in text, "adres wstawiony", text)
    check("{" not in text, "we wyniku nie zostal wzorzec", text)


test_parameters_are_substituted = restoring_language(test_parameters_are_substituted)


def test_missing_key_returns_itself():
    print("\n3b. Nieznany klucz nie wywraca programu")

    # Brakujace tlumaczenie ma byc widoczne, ale nie ma prawa zatrzymac monitora -
    # to napis, nie logika.
    check(i18n.t("nie.ma.takiego.klucza") == "nie.ma.takiego.klucza", "klucz wraca jako napis")
    check(i18n.t("card.charging", nadmiarowy=1) != "", "nadmiarowy parametr jest pomijany")

    # Brak wymaganego parametru zostawia wzorzec, zamiast podniesc KeyError
    # w watku serwisu.
    check("{" in i18n.t("service.connected"), "brak parametru zostawia wzorzec")


def test_language_selection():
    print("\n3c. Wybor jezyka")
    check(i18n.set_language(i18n.ENGLISH) == i18n.ENGLISH, "angielski ustawiony")
    check(i18n.language() == i18n.ENGLISH, "i zapamietany")

    # "auto" i wszystko nieznane rozwija sie do jezyka systemu, zeby recznie
    # zepsuty plik ustawien nie zostawil programu bez napisow.
    check(i18n.set_language(i18n.AUTO) == i18n.detect(), "auto to jezyk systemu")
    check(i18n.set_language("klingonski") == i18n.detect(), "nieznany kod tez")
    check(i18n.detect() in i18n.LANGUAGES, "wykryty jezyk jest obslugiwany")

    codes = [code for code, _ in i18n.available()]
    check(codes == list(i18n.LANGUAGES), "przelacznik pokazuje wszystkie jezyki")
    check(
        all(name for _, name in i18n.available()),
        "kazdy jezyk ma podpis do przelacznika",
    )


test_language_selection = restoring_language(test_language_selection)


# ---------------------------------------------------------------------------
# 4. ZAPIS WYBORU
# ---------------------------------------------------------------------------


def test_language_defaults_to_auto(workspace):
    print("\n4a. Bez pliku ustawien jezyk idzie za systemem")
    config = app_settings.Settings(workspace.path())

    check(config.language == i18n.AUTO, "zapisane zyczenie to auto", config.language)
    check(config.resolved_language() == i18n.detect(), "rozwiniete do jezyka systemu")


def test_language_round_trip(workspace):
    print("\n4b. Wybor jezyka przetrwa restart")
    config = app_settings.Settings(workspace.path())
    config.set_language(i18n.ENGLISH)

    reopened = app_settings.Settings(workspace.path())
    check(reopened.language == i18n.ENGLISH, "angielski wczytany z pliku")
    check(reopened.resolved_language() == i18n.ENGLISH, "i nie podmieniony na systemowy")

    # Wybor jezyka nie moze przestawic deklaracji sprzetu - oba pola siedza
    # w jednym pliku i zapisuja sie tym samym kodem.
    check(reopened.declared == (), "deklaracja sprzetu nietknieta")


def test_language_and_devices_coexist(workspace):
    print("\n4c. Jezyk i lista sprzetu nie nadpisuja sie wzajemnie")
    config = app_settings.Settings(workspace.path())
    config.set_declared(["aerox_5_wireless"])
    config.set_language(i18n.ENGLISH)
    config.set_declared(["aerox_5_wireless", "arctis_nova_7"])

    reopened = app_settings.Settings(workspace.path())
    check(reopened.language == i18n.ENGLISH, "jezyk przetrwal zapis deklaracji")
    check(len(reopened.declared) == 2, "deklaracja przetrwala zapis jezyka")


def test_junk_language_falls_back(workspace):
    print("\n4d. Obca wartosc w pliku wraca do auto")
    config = app_settings.Settings(workspace.path())
    config.set_language("nie_ma_takiego_jezyka")

    reopened = app_settings.Settings(workspace.path())
    check(reopened.language == i18n.AUTO, "zapisane jako auto", reopened.language)
    check(reopened.resolved_language() in i18n.LANGUAGES, "i daje dzialajacy jezyk")


def main():
    plain_tests = [
        test_both_languages_have_same_keys,
        test_no_empty_translations,
        test_parameters_match_between_languages,
        test_oled_strings_are_ascii,
        test_oled_labels_stay_short,
        test_labels_are_translated_but_tokens_are_not,
        test_parameters_are_substituted,
        test_missing_key_returns_itself,
        test_language_selection,
    ]
    settings_tests = [
        test_language_defaults_to_auto,
        test_language_round_trip,
        test_language_and_devices_coexist,
        test_junk_language_falls_back,
    ]

    for test in plain_tests:
        test()

    for test in settings_tests:
        workspace = Workspace()
        try:
            test(workspace)
        finally:
            workspace.cleanup()

    print()
    if _failures:
        print(f"NIEPOWODZENIA: {len(_failures)}")
        for description in _failures:
            print(f"  - {description}")
        return 1

    print(f"Wszystkie sprawdzenia przeszly ({len(plain_tests) + len(settings_tests)} testow).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
