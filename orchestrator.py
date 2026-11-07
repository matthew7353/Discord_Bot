#!/usr/bin/env python3
"""
Punkt wejścia kontenera (Azure Container Apps Job). Znajduje w Azure Blob
Storage sesje czekające na przetworzenie i dla każdej robi:
transkrypcja (Whisper) -> notatki (lokalna Ollama) -> upload wyników.
Kiedy nie ma już nic do zrobienia, proces się kończy, a razem z nim job.

Praca jest podzielona na dwie fazy, bo Whisper i LLM dzielą jedno GPU (T4,
16 GB VRAM) i nie zmieszczą się w nim jednocześnie:
  1. transkrypcja wszystkich oczekujących sesji (status -> "transcribed"),
  2. zwolnienie Whispera z GPU,
  3. notatki dla wszystkich sesji "transcribed" (status -> "done").

Statusy w sessions/<id>/status.json:
  recorded     - nowa sesja od bota
  processing   - transkrypcja w toku (albo przerwana - zostanie powtórzona)
  transcribed  - transkrypt zapisany w blobie, czeka na notatki
  done         - notatki gotowe
  error        - błąd (szczegóły w polu "error")

Kod wyjścia: 0 gdy wszystko się udało, 1 gdy którakolwiek sesja skończyła się
błędem albo brakuje konfiguracji (wtedy wykonanie joba będzie widoczne
w Azure jako Failed).
"""

import os
import sys
import time
import json
import shutil
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv
from azure.storage.blob import BlobServiceClient

import transcribe
import create_notes

load_dotenv()

CONTAINER_NAME = os.getenv("AZURE_STORAGE_CONTAINER", "audio-files")

# Po ostatniej znalezionej sesji, ile razy jeszcze sprawdzić czy nie pojawiła
# się nowa (np. bot dosłał kolejną paczkę), zanim job się zakończy.
RESCAN_ATTEMPTS = int(os.getenv("ORCHESTRATOR_RESCAN_ATTEMPTS", "2"))
RESCAN_DELAY_SECONDS = int(os.getenv("ORCHESTRATOR_RESCAN_DELAY_SECONDS", "30"))

# Surowe pliki .pcm to zdecydowanie największy koszt w tym kontenerze
# (transcript.txt i notes.md to pojedyncze KB tekstu). Domyślnie kasujemy je
# z blob storage od razu po udanym przetworzeniu sesji.
CLEANUP_AUDIO_AFTER_PROCESSING = os.getenv("ORCHESTRATOR_CLEANUP_AUDIO", "true").lower() == "true"

TO_TRANSCRIBE = ("recorded", "processing")
TO_SUMMARIZE = ("transcribed",)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def find_sessions(container_client, statuses):
    """Zwraca listę (session_id, status_dict) dla sesji o podanych statusach,
    posortowaną od najstarszej."""
    seen = set()
    found = []

    for blob in container_client.list_blobs(name_starts_with="sessions/"):
        parts = blob.name.split("/")
        if len(parts) < 2:
            continue
        session_id = parts[1]
        if session_id in seen:
            continue
        seen.add(session_id)

        status_client = container_client.get_blob_client(f"sessions/{session_id}/status.json")
        if not status_client.exists():
            continue
        status = json.loads(status_client.download_blob().readall())
        # "processing" oznacza sesję przerwaną w trakcie (np. replika padła
        # w połowie transkrypcji) - bezpiecznie ją powtórzyć, bo transkrypcja
        # jest idempotentna (po prostu zrobi pracę od nowa).
        if status.get("status") in statuses:
            found.append((session_id, status))

    found.sort(key=lambda x: x[1].get("created_at", ""))
    return found


def write_status(container_client, session_id, status_dict):
    status_dict["updated_at"] = now_iso()
    status_blob = container_client.get_blob_client(f"sessions/{session_id}/status.json")
    content = json.dumps(status_dict, ensure_ascii=False, indent=2).encode("utf-8")
    status_blob.upload_blob(content, overwrite=True, content_type="application/json")


def mark_error(container_client, session_id, status, e):
    print(f"❌ Błąd przy przetwarzaniu sesji {session_id}: {e}")
    traceback.print_exc()
    status["status"] = "error"
    status["error"] = str(e)
    write_status(container_client, session_id, status)


def cleanup_session_audio(container_client, session_id):
    """Kasuje surowe .pcm z blob storage (główny koszt) i lokalne pozostałości.
    transcript.txt, notes.md i status.json zostają - to tylko tekst,
    a przydają się do debugowania/audytu."""
    deleted = 0
    prefix = f"sessions/{session_id}/"
    for blob in container_client.list_blobs(name_starts_with=prefix):
        if blob.name.endswith(".pcm"):
            try:
                container_client.delete_blob(blob.name)
                deleted += 1
            except Exception as e:
                print(f"⚠️ Nie udało się skasować {blob.name}: {e}")
    if deleted:
        print(f"🧹 Skasowano {deleted} plik(ów) .pcm z blob storage dla sesji {session_id}.")

    local_dir = os.path.join(transcribe.DOWNLOAD_DIR, session_id)
    if os.path.isdir(local_dir):
        shutil.rmtree(local_dir, ignore_errors=True)


def transcribe_phase(container_client):
    """Faza 1: transkrypcja wszystkich oczekujących sesji. Zwraca liczbę błędów."""
    errors = 0
    model = None
    attempts_left = RESCAN_ATTEMPTS

    while True:
        pending = find_sessions(container_client, TO_TRANSCRIBE)
        if not pending:
            if attempts_left <= 0:
                break
            print(f"Brak nowych sesji. Sprawdzam ponownie za {RESCAN_DELAY_SECONDS}s "
                  f"({attempts_left} próby zostały)...")
            attempts_left -= 1
            time.sleep(RESCAN_DELAY_SECONDS)
            continue

        attempts_left = RESCAN_ATTEMPTS  # reset, bo znaleźliśmy pracę
        if model is None:
            model = transcribe.load_model()

        for session_id, status in pending:
            print(f"\n=== Transkrypcja sesji {session_id} ===")
            status["status"] = "processing"
            write_status(container_client, session_id, status)
            try:
                text = transcribe.transcribe_session(container_client, session_id, model=model)
                blob = container_client.get_blob_client(f"sessions/{session_id}/transcript.txt")
                blob.upload_blob(text.encode("utf-8"), overwrite=True, content_type="text/plain")
                status["status"] = "transcribed"
                status.pop("error", None)
                write_status(container_client, session_id, status)
                print(f"=== Sesja {session_id}: transkrypt gotowy ===")
            except Exception as e:
                mark_error(container_client, session_id, status, e)
                errors += 1

    if model is not None:
        transcribe.unload_model()
    return errors


def notes_phase(container_client):
    """Faza 2: notatki dla wszystkich sesji z gotowym transkryptem. Zwraca liczbę błędów."""
    errors = 0
    sessions = find_sessions(container_client, TO_SUMMARIZE)
    if not sessions:
        return 0

    create_notes.ensure_ollama_ready()

    for session_id, status in sessions:
        print(f"\n=== Notatki dla sesji {session_id} ===")
        try:
            blob = container_client.get_blob_client(f"sessions/{session_id}/transcript.txt")
            transcript_text = blob.download_blob().readall().decode("utf-8")

            notes = create_notes.generate_notes(transcript_text, progress_prefix=f"[{session_id}] ")

            notes_blob_name = f"sessions/{session_id}/notes.md"
            container_client.get_blob_client(notes_blob_name).upload_blob(
                notes.encode("utf-8"), overwrite=True, content_type="text/markdown")

            status["status"] = "done"
            status["notes_blob"] = notes_blob_name
            status["notes_model"] = create_notes.MODEL_NAME
            status.pop("error", None)
            write_status(container_client, session_id, status)
            print(f"=== Sesja {session_id}: GOTOWE ===")

            if CLEANUP_AUDIO_AFTER_PROCESSING:
                cleanup_session_audio(container_client, session_id)
        except Exception as e:
            mark_error(container_client, session_id, status, e)
            errors += 1

    return errors


def main() -> int:
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn_str:
        print("Błąd: brak AZURE_STORAGE_CONNECTION_STRING w środowisku.")
        return 1

    service_client = BlobServiceClient.from_connection_string(conn_str)
    container_client = service_client.get_container_client(CONTAINER_NAME)

    errors = transcribe_phase(container_client)
    errors += notes_phase(container_client)

    if errors:
        print(f"\nZakończono z błędami: {errors} sesja(e) w statusie 'error'.")
        return 1
    print("\nWszystkie oczekujące sesje przetworzone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
