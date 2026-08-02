#!/usr/bin/env python3
"""
Skrypt generuje notatki z sesji D&D na podstawie transkryptu (txt, np. z Whisper AI)
i wysyła go do lokalnego modelu LLM postawionego na Ollamie.

Wymagania:
    pip install requests

Użycie:
    python notatki_z_sesji.py
"""

import requests
import sys
import re
import time
import json
import os

# ============ KONFIGURACJA - UZUPEŁNIJ ============

OLLAMA_URL = "http://localhost:11434/api/generate"
MODEL_NAME = "gemma4:31b-cloud"          # <-- WPISZ TU nazwę modelu, np. "llama3.1:70b"
TRANSCRIPT_FILE = "transcript.txt"     # <-- WPISZ TU nazwę pliku txt z transkrypcją, np. "sesja_2024_07_20.txt"
OUTPUT_FILE = "notatki_z_sesji_cloud.md"

# Ile sekund maksymalnie czekać na odpowiedź modelu dla JEDNEGO fragmentu.
# Na wolnym CPU długi prompt potrafi liczyć się bardzo długo - lepiej ustawić
# z dużym zapasem niż dostać timeout w połowie pracy.
TIMEOUT_SEKUNDY = 1800  # 30 minut

# Ile razy ponowić próbę dla fragmentu, jeśli zapytanie się nie powiedzie (timeout/błąd sieci).
LICZBA_PROB = 3

# Plik, w którym zapisywany jest postęp - jeśli skrypt padnie w trakcie,
# przy ponownym uruchomieniu wznowi pracę od miejsca, w którym skończył.
PLIK_POSTEPU = "postep_streszczen.json"

# ====================================================

# Mapowanie nicków z Discorda na postacie/role w grze.
# Whisper czasem błędnie rozpoznaje nicki (literówki), więc dodano
# kilka wariantów pisowni - w razie potrzeby dopisz kolejne.
NICK_MAP = {
    "artisdead3": "Frum Runegaser",
    "hatandhistea": "Grimmar",
    "ayuki9474": "Nyx",
    "asutosao": "Kalys",
    "sjugetti": "Mistrz Gry (MG)",
}

# Kontekst wspólny dla obu promptów - kto jest kim w transkrypcie.
KONTEKST_POSTACI = """W transkrypcie występują następujące osoby i grane przez nie postacie:
- Frum Runegaser (gracz)
- Grimmar (gracz)
- Nyx (gracz)
- Kalys (gracz)
- Mistrz Gry (prowadzący sesję)"""

# Prompt systemowy używany PRZY KAŻDYM FRAGMENCIE transkryptu.
# Zadanie: wyciągnąć fakty i wydarzenia z surowego, częściowo błędnego tekstu.
SYSTEM_PROMPT_FRAGMENT = f"""Jesteś asystentem, który sporządza robocze notatki z fragmentu sesji RPG \
(Dungeons & Dragons) na podstawie transkrypcji rozmowy głosowej z Discorda, wygenerowanej automatycznie \
przez Whisper AI.

Transkrypcja zawiera błędy rozpoznawania mowy: literówki, pomylone słowa, urwane zdania, czasem całe \
zdania bez sensu. To NIE są prawdziwe wypowiedzi graczy - to tylko niedoskonały zapis dźwięku. Twoim \
zadaniem jest odczytać z kontekstu, co NAPRAWDĘ się wydarzyło i wydarzenia, a nie próbować parafrazować \
czy "naprawiać" dosłowne, błędne sformułowania. Jeśli jakiegoś fragmentu zdania nie da się sensownie \
zrozumieć, po prostu go pomiń - nie zgaduj i nie wymyślaj szczegółów, których nie ma w tekście.

{KONTEKST_POSTACI}

Pisz WYŁĄCZNIE poprawną, płynną polszczyzną (poprawna odmiana, szyk zdania, żadnych kalek czy błędów \
gramatycznych). Nie kopiuj niezrozumiałych fragmentów transkryptu do swojej odpowiedzi w żadnej formie.

Zadanie: napisz krótkie, rzeczowe streszczenie tego, co wydarzyło się w tym fragmencie - w formie zwykłych \
akapitów prozy (NIE list punktowanych, NIE nagłówków). Uwzględnij:
- co robiła drużyna, dokąd poszła, z kim rozmawiała,
- ważne decyzje, ustalenia, obietnice,
- nowe postacie (NPC), miejsca, przedmioty, zadania, jeśli się pojawiły.

To jest tekst roboczy do dalszej obróbki, nie finalne notatki - ma być zwięzły i konkretny, bez formatowania."""

# Prompt systemowy używany RAZ, przy składaniu wszystkich streszczeń fragmentów w finalne notatki.
# Zadanie: synteza i redakcja spójnego dokumentu, a nie wyciąganie faktów z surowego tekstu.
SYSTEM_PROMPT_FINALNE = f"""Jesteś redaktorem, który na podstawie chronologicznych, roboczych streszczeń \
fragmentów jednej sesji RPG (Dungeons & Dragons) przygotowuje finalne, czyste notatki z całej sesji.

{KONTEKST_POSTACI}

Dostajesz serię streszczeń kolejnych fragmentów tej samej sesji. Streszczenia mogą się częściowo \
powtarzać lub zachodzić na siebie (bo fragmenty transkryptu też się częściowo pokrywały) - Twoim zadaniem \
jest scalić je w jeden spójny, logiczny dokument, a nie wypisać je jedno po drugim.

Pisz WYŁĄCZNIE poprawną, płynną polszczyzną - zadbaj o poprawną gramatykę, odmianę, szyk zdania. Unikaj \
urwanych, niegramatycznych zdań - jeśli któreś ze streszczeń zawiera fragment, który nie trzyma się kupy, \
po prostu go pomiń zamiast przepisywać dosłownie.

Przygotuj notatki w formacie Markdown, dokładnie w tej strukturze (użyj każdego nagłówka DOKŁADNIE RAZ):

## Podsumowanie sesji
Spójna, chronologiczna narracja w formie normalnej prozy (kilka akapitów) - co drużyna robiła, gdzie była, \
z kim rozmawiała, jakie napotkała wyzwania. Nie dziel tego na dodatkowe podpunkty czy podtytuły.

## Kluczowe informacje na przyszłość
Lista punktowana najważniejszych faktów, NPC-ów, miejsc, przedmiotów, zadań, obietnic czy tajemnic, które \
warto pamiętać na kolejnych sesjach. Każdy punkt to jedno konkretne, zwięzłe zdanie - bez powtórzeń.

## Otwarte wątki / co dalej
Lista punktowana tego, dokąd drużyna zmierza lub co planuje zrobić dalej, jeśli to wynika ze streszczeń.

Nie twórz żadnych innych sekcji ani nie powtarzaj tych samych informacji w kilku miejscach."""


def wczytaj_transkrypt(sciezka: str) -> str:
    try:
        with open(sciezka, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        print(f"Błąd: nie znaleziono pliku '{sciezka}' w bieżącym folderze.")
        sys.exit(1)
    except UnicodeDecodeError:
        # awaryjnie próbujemy innego kodowania
        with open(sciezka, "r", encoding="cp1250") as f:
            return f.read()


def podmien_nicki(tekst: str) -> str:
    """Zamienia nicki z Discorda na imiona postaci, uwzględniając różną wielkość liter."""
    for nick, postac in NICK_MAP.items():
        tekst = re.sub(re.escape(nick), postac, tekst, flags=re.IGNORECASE)
    return tekst


def podziel_na_fragmenty(tekst: str, max_znakow: int = 12000) -> list[str]:
    """
    Dzieli długi transkrypt na fragmenty, żeby zmieścić się w kontekście modelu.
    Dzieli po liniach, żeby nie przecinać wypowiedzi w połowie.
    """
    linie = tekst.split("\n")
    fragmenty = []
    biezacy = []
    dlugosc = 0

    for linia in linie:
        if dlugosc + len(linia) > max_znakow and biezacy:
            fragmenty.append("\n".join(biezacy))
            biezacy = []
            dlugosc = 0
        biezacy.append(linia)
        dlugosc += len(linia)

    if biezacy:
        fragmenty.append("\n".join(biezacy))

    return fragmenty


def zapytaj_ollame(prompt: str, system: str = "") -> str:
    payload = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "system": system,
        "stream": False,
    }

    for proba in range(1, LICZBA_PROB + 1):
        try:
            odpowiedz = requests.post(OLLAMA_URL, json=payload, timeout=TIMEOUT_SEKUNDY)
            odpowiedz.raise_for_status()
            return odpowiedz.json().get("response", "").strip()

        except requests.exceptions.ReadTimeout:
            print(f"    [uwaga] Timeout po {TIMEOUT_SEKUNDY}s (próba {proba}/{LICZBA_PROB}).")
        except requests.exceptions.ConnectionError:
            print(f"    [uwaga] Nie udało się połączyć z Ollamą (próba {proba}/{LICZBA_PROB}). "
                  f"Sprawdź czy serwer działa: `systemctl status ollama`.")
        except requests.exceptions.HTTPError as e:
            print(f"    [uwaga] Błąd HTTP od Ollamy (próba {proba}/{LICZBA_PROB}): {e}")

        if proba < LICZBA_PROB:
            czekaj = 15 * proba
            print(f"    Czekam {czekaj}s i próbuję ponownie...")
            time.sleep(czekaj)

    print("Błąd: nie udało się uzyskać odpowiedzi od Ollamy po kilku próbach. Przerywam.")
    print("Dotychczasowy postęp jest zapisany w pliku "
          f"'{PLIK_POSTEPU}' - uruchom skrypt ponownie, żeby wznowić od tego miejsca.")
    sys.exit(1)


def wczytaj_postep() -> list[str]:
    if os.path.exists(PLIK_POSTEPU):
        with open(PLIK_POSTEPU, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def zapisz_postep(streszczenia: list[str]) -> None:
    with open(PLIK_POSTEPU, "w", encoding="utf-8") as f:
        json.dump(streszczenia, f, ensure_ascii=False, indent=2)


def podsumuj_fragment(fragment: str, numer: int, ile_lacznie: int) -> str:
    prompt = (
        f"To jest fragment {numer}/{ile_lacznie} transkryptu sesji D&D (transkrypt podzielony jest "
        f"na części ze względu na długość). Streść zwięźle, ale konkretnie, co się w nim wydarzyło "
        f"oraz wypisz wszelkie ważne informacje, decyzje, imiona NPC-ów, przedmioty i zadania. "
        f"Nie formatuj jeszcze finalnych notatek - to tylko surowe streszczenie robocze do dalszej obróbki.\n\n"
        f"Fragment transkryptu:\n{fragment}"
    )
    return zapytaj_ollame(prompt, system=SYSTEM_PROMPT_FRAGMENT)


def stworz_finalne_notatki(streszczenia: list[str]) -> str:
    polaczone = "\n\n---\n\n".join(
        f"[Streszczenie części {i+1}]\n{s}" for i, s in enumerate(streszczenia)
    )
    prompt = (
        "Poniżej znajdują się kolejne, chronologiczne streszczenia fragmentów jednej sesji D&D. "
        "Na ich podstawie stwórz spójne, finalne notatki z całej sesji zgodnie z formatem opisanym "
        "w instrukcjach systemowych (Podsumowanie sesji, Kluczowe informacje na przyszłość, "
        "Otwarte wątki / co dalej). Połącz informacje z poszczególnych części w jedną spójną całość, "
        "usuń powtórzenia.\n\n"
        f"{polaczone}"
    )
    return zapytaj_ollame(prompt, system=SYSTEM_PROMPT_FINALNE)


def main():
    if not MODEL_NAME:
        print("Błąd: uzupełnij zmienną MODEL_NAME na początku skryptu (nazwa modelu z `ollama list`).")
        sys.exit(1)
    if not TRANSCRIPT_FILE:
        print("Błąd: uzupełnij zmienną TRANSCRIPT_FILE na początku skryptu (nazwa pliku txt z transkryptem).")
        sys.exit(1)

    print(f"Wczytuję transkrypt z pliku '{TRANSCRIPT_FILE}'...")
    surowy_tekst = wczytaj_transkrypt(TRANSCRIPT_FILE)
    tekst = podmien_nicki(surowy_tekst)

    fragmenty = podziel_na_fragmenty(tekst)
    print(f"Transkrypt podzielono na {len(fragmenty)} fragment(ów).")

    streszczenia = wczytaj_postep()
    if streszczenia:
        print(f"Znaleziono zapisany postęp: {len(streszczenia)}/{len(fragmenty)} fragmentów już przetworzonych. "
              f"Wznawiam od fragmentu {len(streszczenia) + 1}.")

    for i in range(len(streszczenia) + 1, len(fragmenty) + 1):
        print(f"  -> przetwarzanie fragmentu {i}/{len(fragmenty)}...")
        fragment = fragmenty[i - 1]
        streszczenia.append(podsumuj_fragment(fragment, i, len(fragmenty)))
        zapisz_postep(streszczenia)

    print("Łączenie streszczeń w finalne notatki...")
    finalne_notatki = stworz_finalne_notatki(streszczenia)

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(finalne_notatki)

    if os.path.exists(PLIK_POSTEPU):
        os.remove(PLIK_POSTEPU)

    print(f"\nGotowe! Notatki zapisano do pliku '{OUTPUT_FILE}'.")


if __name__ == "__main__":
    main()
