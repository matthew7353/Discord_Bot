#!/usr/bin/env python3
"""
Generuje notatki z sesji D&D na podstawie tekstu transkryptu, przez lokalny
model LLM na Ollamie.

Można importować (generate_notes) z orchestrator.py, albo uruchomić samodzielnie:
    python create_notes.py <plik_z_transkryptem.txt> [plik_wyjsciowy.md]
"""

import requests
import sys
import re
import time
import os

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
MODEL_NAME = os.getenv("OLLAMA_MODEL_NAME", "gemma4:31b-cloud")

TIMEOUT_SEKUNDY = 1800
LICZBA_PROB = 3
MAX_ZNAKOW_FRAGMENT = 12000

NICK_MAP = {
    "artisdead3": "Frum Runegaser",
    "hatandhistea": "Grimmar",
    "ayuki9474": "Nyx",
    "asutosao": "Kalys",
    "sjugetti": "Mistrz Gry (MG)",
}

KONTEKST_POSTACI = """W transkrypcie występują następujące osoby i grane przez nie postacie:
- Frum Runegaser (gracz)
- Grimmar (gracz)
- Nyx (gracz)
- Kalys (gracz)
- Mistrz Gry (prowadzący sesję)"""

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


def podmien_nicki(tekst: str) -> str:
    for nick, postac in NICK_MAP.items():
        tekst = re.sub(re.escape(nick), postac, tekst, flags=re.IGNORECASE)
    return tekst


def podziel_na_fragmenty(tekst: str, max_znakow: int = MAX_ZNAKOW_FRAGMENT) -> list[str]:
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
    payload = {"model": MODEL_NAME, "prompt": prompt, "system": system, "stream": False}

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

    raise RuntimeError("Nie udało się uzyskać odpowiedzi od Ollamy po kilku próbach.")


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


def generate_notes(transcript_text: str, progress_prefix: str = "") -> str:
    """Bierze surowy tekst transkryptu i zwraca finalne notatki w Markdown."""
    tekst = podmien_nicki(transcript_text)
    fragmenty = podziel_na_fragmenty(tekst)
    print(f"{progress_prefix}Transkrypt podzielono na {len(fragmenty)} fragment(ów).")

    streszczenia = []
    for i, fragment in enumerate(fragmenty, start=1):
        print(f"{progress_prefix}  -> przetwarzanie fragmentu {i}/{len(fragmenty)}...")
        streszczenia.append(podsumuj_fragment(fragment, i, len(fragmenty)))

    print(f"{progress_prefix}Łączenie streszczeń w finalne notatki...")
    return stworz_finalne_notatki(streszczenia)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Użycie: python create_notes.py <plik_z_transkryptem.txt> [plik_wyjsciowy.md]")
        sys.exit(1)

    transcript_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else "notatki_z_sesji.md"

    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript_text = f.read()

    notes = generate_notes(transcript_text)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(notes)

    print(f"\nGotowe! Notatki zapisano do pliku '{output_path}'.")
