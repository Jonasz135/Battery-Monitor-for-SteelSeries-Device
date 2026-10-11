# -*- mode: python ; coding: utf-8 -*-
"""Przepis na zbudowanie BatteryMonitor.exe (PyInstaller).

Uruchomienie:  python build.py

Cztery decyzje, ktore warto rozumiec przed zmiana czegokolwiek w tym pliku.

**Jeden plik .exe (onefile).** Program ma byc czyms, co sie kopiuje i klika,
a nie katalogiem z kilkuset plikami, w ktorym trzeba szukac wlasciwego. Cena to
okolo dwie sekundy dluzszy start, bo zawartosc rozpakowuje sie do katalogu
tymczasowego. Dla programu, ktory uruchamia sie raz na sesje i potem siedzi
w zasobniku, to nie ma znaczenia.

**Bez okna konsoli (console=False).** Inaczej przy kazdym starcie razem
z Windows wyskakiwalo by czarne okno i zostawaloby otwarte na caly czas pracy.
Skutkiem ubocznym jest to, ze wyjatek przy starcie nie ma sie gdzie wypisac -
dlatego w wersji ze zrodel zostaje "python app.py", ktore konsole ma.

**Zasoby dolaczone, dane osobno.** Ikona i logo ida do wnetrza .exe i sa
czytane przez paths.resource_path(). Historia baterii i wybor sprzetu ida do
%APPDATA% - katalog tymczasowy, do ktorego rozpakowuje sie onefile, jest
usuwany przy zamknieciu programu, wiec zapis obok .exe nic by nie zachowal.

**customtkinter trzeba zebrac jawnie.** Biblioteka wczytuje motywy z plikow
.json w czasie dzialania, a PyInstaller widzi tylko importy - bez collect_all
program wystartowalby i natychmiast padl na braku pliku motywu.
"""

from PyInstaller.utils.hooks import collect_all
import os

_HERE = os.path.abspath(SPECPATH)

# Motywy i czcionki customtkinter sa wczytywane z dysku, nie importowane.
ctk_datas, ctk_binaries, ctk_hidden = collect_all("customtkinter")

analysis = Analysis(
    [os.path.join(_HERE, "app.py")],
    pathex=[_HERE],
    binaries=ctk_binaries,
    datas=[(os.path.join(_HERE, "assets"), "assets")] + ctk_datas,
    hiddenimports=[
        # pystray i PIL wybieraja backend dopiero w czasie dzialania, po
        # sprawdzeniu systemu. Statyczna analiza importow tego nie widzi.
        "pystray._win32",
        "PIL._tkinter_finder",
    ]
    + ctk_hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Nieuzywane, a ciezkie. Bez tego .exe rosnie o kilkadziesiat megabajtow.
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "pytest",
        "setuptools",
        "unittest",
    ],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    name="BatteryMonitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    # Program okienkowy: bez tego przy kazdym starcie migalaby konsola.
    console=False,
    icon=os.path.join(_HERE, "assets", "icon.ico"),
)
