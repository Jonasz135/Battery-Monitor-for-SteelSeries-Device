"""Ustawienia aplikacji - zadeklarowany sprzet i jezyk interfejsu.

Program dziala w dwoch trybach:

  - **automatyczny** - odpytywane jest wszystko, co znajdzie sie na USB
    i co da sie rozpoznac po katalogu urzadzen. Nic nie trzeba ustawiac,
    ale urzadzenia wylaczonego nie da sie odroznic od nieistniejacego, bo
    w obu przypadkach dongle po prostu nie odpowiada.
  - **deklaracja** - uzytkownik wybiera z listy modele, ktore posiada. Tylko
    one sa odpytywane, a kazde z nich ma stala karte w oknie: gdy jest
    wylaczone, widnieje jako wylaczone, a nie znika.

Tryb nie jest osobnym polem, bo dwa zrodla prawdy zawsze sie rozjezdzaja.
Pusta lista zadeklarowanych urzadzen **znaczy** tryb automatyczny - nie da sie
przez to wpasc w stan "deklaracja bez urzadzen", w ktorym program nie
monitorowalby niczego i nie bylo by widac dlaczego.

Jezyk jest zapisany jako "pl", "en" albo "auto". Domyslne "auto" znaczy "pytaj
Windows przy kazdym starcie", wiec program mowi tym samym jezykiem co system,
dopoki uzytkownik sam czegos nie wybierze. Zapisanie wykrytego jezyka jako
konkretnej wartosci zamrozilo by go na zawsze przy pierwszym uruchomieniu.
"""

import json
import os

import i18n
import paths

# Plik lezy w %APPDATA%, a nie obok programu - patrz paths.py. Katalog obok
# spakowanego .exe jest tymczasowy i znika przy zamknieciu programu.
SETTINGS_PATH = paths.data_file("settings.json")

FORMAT_VERSION = 1

# Dozwolone wartosci pola "language": oba jezyki i zyczenie "jak w systemie".
_LANGUAGE_VALUES = frozenset(i18n.LANGUAGES) | {i18n.AUTO}


def _defaults():
    return {"version": FORMAT_VERSION, "declared": [], "language": i18n.AUTO}


class Settings:
    """Czyta i zapisuje settings.json."""

    def __init__(self, path=SETTINGS_PATH):
        self._path = path
        self._data = self._load()

    # -- odczyt -------------------------------------------------------------

    @property
    def declared(self):
        """Klucze zadeklarowanych urzadzen. Pusta krotka oznacza tryb automatyczny."""
        return tuple(self._data.get("declared", ()))

    @property
    def automatic(self):
        return not self.declared

    def is_declared(self, key):
        return key in self._data.get("declared", ())

    @property
    def language(self):
        """Zapisane zyczenie: "pl", "en" albo "auto"."""
        return self._data.get("language", i18n.AUTO)

    def resolved_language(self):
        """Jezyk do ustawienia teraz - "auto" rozwiniete do jezyka systemu."""
        return i18n.resolve(self.language)

    # -- zapis --------------------------------------------------------------

    def set_declared(self, keys):
        """Zapisuje liste zadeklarowanych modeli. Pusta lista wraca do trybu auto."""
        # dict.fromkeys zamiast set(), zeby kolejnosc z okna zostala zachowana -
        # inaczej karty ustawialyby sie losowo przy kazdym uruchomieniu.
        self._data["declared"] = list(dict.fromkeys(keys))
        self._save()

    def set_language(self, code):
        """Zapisuje wybor jezyka. Nieznany kod laduje jako "auto"."""
        self._data["language"] = code if code in _LANGUAGE_VALUES else i18n.AUTO
        self._save()

    # -- dysk ---------------------------------------------------------------

    def _load(self):
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return _defaults()

        if not isinstance(data, dict):
            return _defaults()

        declared = data.get("declared")
        if not isinstance(declared, list):
            declared = []

        language = data.get("language")
        if language not in _LANGUAGE_VALUES:
            language = i18n.AUTO

        # Przepisujemy pole po polu, zamiast oddawac wczytany slownik. Plik
        # moze byc z nowszej wersji albo poprawiany recznie; do programu wchodzi
        # tylko to, co rozumie, i w typie, ktorego sie spodziewa.
        return {
            "version": FORMAT_VERSION,
            "declared": [key for key in declared if isinstance(key, str)],
            "language": language,
        }

    def _save(self):
        """Zapis przez podmiane, zeby przerwanie nie zostawilo polowy pliku."""
        temporary = self._path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2)
            os.replace(temporary, self._path)
        except OSError:
            try:
                os.remove(temporary)
            except OSError:
                pass
