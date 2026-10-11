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
import tempfile
import threading

import i18n
import paths

# Plik lezy w %APPDATA%, a nie obok programu - patrz paths.py. Katalog obok
# spakowanego .exe jest tymczasowy i znika przy zamknieciu programu.
SETTINGS_PATH = paths.data_file("settings.json")

FORMAT_VERSION = 1

# Dozwolone wartosci pola "language": oba jezyki i zyczenie "jak w systemie".
_LANGUAGE_VALUES = frozenset(i18n.LANGUAGES) | {i18n.AUTO}
_SAVE_LOCKS = {}
_SAVE_LOCKS_GUARD = threading.Lock()


def _defaults():
    return {"version": FORMAT_VERSION, "declared": [], "language": i18n.AUTO}


class Settings:
    """Czyta i zapisuje settings.json."""

    def __init__(self, path=SETTINGS_PATH):
        self._path = os.path.normcase(os.path.abspath(os.fspath(path)))
        with _SAVE_LOCKS_GUARD:
            self._save_lock = _SAVE_LOCKS.setdefault(self._path, threading.RLock())
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
        return self._set_field(
            "declared", list(dict.fromkeys(key for key in keys if isinstance(key, str)))
        )

    def set_language(self, code):
        """Zapisuje wybor jezyka. Nieznany kod laduje jako "auto"."""
        language = (
            code if isinstance(code, str) and code in _LANGUAGE_VALUES else i18n.AUTO
        )
        return self._set_field("language", language)

    def _set_field(self, field, value):
        # Okno i serwis maja osobne instancje. Odczyt plus podmiana pod jedna
        # blokada zapobiega przywroceniu starych wartosci innych pol ustawien.
        with self._save_lock:
            try:
                data = self._load(strict=True)
            except OSError:
                return False
            data[field] = value
            return self._save(data)

    # -- dysk ---------------------------------------------------------------

    def _load(self, strict=False):
        try:
            with open(self._path, "r", encoding="utf-8-sig") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return _defaults()
        except OSError:
            if strict:
                raise
            return _defaults()
        except (UnicodeError, json.JSONDecodeError):
            return _defaults()

        if not isinstance(data, dict):
            return _defaults()

        declared = data.get("declared")
        if not isinstance(declared, list):
            declared = []

        language = data.get("language")
        if not isinstance(language, str) or language not in _LANGUAGE_VALUES:
            language = i18n.AUTO

        # Przepisujemy pole po polu, zamiast oddawac wczytany slownik. Plik
        # moze byc z nowszej wersji albo poprawiany recznie; do programu wchodzi
        # tylko to, co rozumie, i w typie, ktorego sie spodziewa.
        return {
            "version": FORMAT_VERSION,
            "declared": list(dict.fromkeys(key for key in declared if isinstance(key, str))),
            "language": language,
        }

    def _save(self, data):
        """Atomowy zapis; pamiec zmienia sie dopiero po udanej podmianie pliku."""
        temporary = None
        try:
            directory = os.path.dirname(os.path.abspath(self._path))
            os.makedirs(directory, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=directory,
                prefix=os.path.basename(self._path) + ".", suffix=".tmp", delete=False,
            ) as handle:
                temporary = handle.name
                json.dump(data, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except OSError:
            return False
        finally:
            if temporary is not None:
                try:
                    os.remove(temporary)
                except OSError:
                    pass
        self._data = data
        return True
