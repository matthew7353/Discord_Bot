#!/usr/bin/env python3
"""
Uruchamiany automatycznie przy starcie maszyny przetwarzającej (przez systemd,
patrz orchestrator.service). Znajduje sesje czekające na przetworzenie w Azure
Blob Storage, odpala dla każdej: transcribe.py -> create_notes.py -> upload
notatek, a na koniec wyłącza (deallocate) samą siebie, żeby nie generować
kosztów gdy nic się nie dzieje.

Wymaga:
    pip install azure-storage-blob azure-identity azure-mgmt-compute requests \
                python-dotenv openai-whisper

Wymaga też, żeby ta maszyna miała włączoną System-Assigned Managed Identity
z rolą "Virtual Machine Contributor" (scope: sama na siebie / resource group),
żeby mogła się sama zdeallokować.
"""

import os
import time
import json
import shutil
import traceback
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from azure.storage.blob import BlobServiceClient
from azure.identity import ManagedIdentityCredential
from azure.mgmt.compute import ComputeManagementClient

import transcribe
import create_notes

load_dotenv()

CONTAINER_NAME = "audio-files"
IMDS_URL = "http://169.254.169.254/metadata/instance?api-version=2021-02-01"

# Po ostatniej znalezionej sesji, ile razy jeszcze sprawdzić czy nie pojawiła
# się nowa (np. bot dosłał kolejną paczkę), zanim wyłączymy maszynę.
RESCAN_ATTEMPTS = 2
RESCAN_DELAY_SECONDS = 30

# Jeśli ustawione na False, maszyna NIE wyłączy się sama po zakończeniu
# (przydatne do debugowania).
AUTO_SHUTDOWN = os.getenv("ORCHESTRATOR_AUTO_SHUTDOWN", "true").lower() == "true"

# Surowe pliki .pcm to zdecydowanie największy koszt w tym kontenerze
# (transcript.txt i notes.md to pojedyncze KB tekstu). Domyślnie kasujemy je
# z blob storage od razu po udanym przetworzeniu sesji.
CLEANUP_AUDIO_AFTER_PROCESSING = os.getenv("ORCHESTRATOR_CLEANUP_AUDIO", "true").lower() == "true"


def get_instance_metadata():
    """Pyta Azure Instance Metadata Service kim jesteśmy (nazwa VM, resource group, subskrypcja)."""
    resp = requests.get(IMDS_URL, headers={"Metadata": "true"}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    compute = data["compute"]
    return {
        "vm_name": compute["name"],
        "resource_group": compute["resourceGroupName"],
        "subscription_id": compute["subscriptionId"],
    }


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def find_pending_sessions(container_client):
    """Zwraca listę session_id do przetworzenia: status 'recorded' (nowe) albo
    'processing' (przerwane w poprzednim, ubitym uruchomieniu)."""
    seen = set()
    pending = []

    for blob in container_client.list_blobs(name_starts_with="sessions/"):
        parts = blob.name.split("/")
        if len(parts) < 2:
            continue
        session_id = parts[1]
        if session_id in seen:
            continue
        seen.add(session_id)

        status_blob_name = f"sessions/{session_id}/status.json"
        status_client = container_client.get_blob_client(status_blob_name)
        if not status_client.exists():
            continue
        status = json.loads(status_client.download_blob().readall())
        # "processing" oznacza sesję przerwaną w trakcie (np. VM padła w
        # połowie transkrypcji) — bezpiecznie ją powtórzyć, bo transcribe.py
        # i create_notes.py są idempotentne (po prostu zrobią pracę od nowa).
        if status.get("status") in ("recorded", "processing"):
            pending.append((session_id, status))

    pending.sort(key=lambda x: x[1].get("created_at", ""))
    return pending


def write_status(container_client, session_id, status_dict):
    status_blob = container_client.get_blob_client(f"sessions/{session_id}/status.json")
    content = json.dumps(status_dict, ensure_ascii=False, indent=2).encode("utf-8")
    status_blob.upload_blob(content, overwrite=True, content_type="application/json")


def cleanup_session_audio(container_client, session_id):
    """Kasuje surowe .pcm z blob storage (główny koszt) i lokalne pobrane kopie
    (dysk VM). transcript.txt, notes.md i status.json zostają - to tylko tekst,
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
        print(f"🧹 Skasowano lokalny katalog {local_dir}.")


def process_session(container_client, session_id, status, model):
    print(f"\n=== Przetwarzam sesję {session_id} ===")
    status["status"] = "processing"
    status["updated_at"] = now_iso()
    write_status(container_client, session_id, status)

    try:
        transcript_text = transcribe.transcribe_session(container_client, session_id, model=model)

        # Zapisz surowy transkrypt też do blobu, przydatne do debugowania.
        transcript_blob = container_client.get_blob_client(f"sessions/{session_id}/transcript.txt")
        transcript_blob.upload_blob(transcript_text.encode("utf-8"), overwrite=True, content_type="text/plain")

        notes = create_notes.generate_notes(transcript_text, progress_prefix=f"[{session_id}] ")

        notes_blob_name = f"sessions/{session_id}/notes.md"
        notes_blob = container_client.get_blob_client(notes_blob_name)
        notes_blob.upload_blob(notes.encode("utf-8"), overwrite=True, content_type="text/markdown")

        status["status"] = "done"
        status["notes_blob"] = notes_blob_name
        status["updated_at"] = now_iso()
        write_status(container_client, session_id, status)
        print(f"=== Sesja {session_id}: GOTOWE ===")

        if CLEANUP_AUDIO_AFTER_PROCESSING:
            cleanup_session_audio(container_client, session_id)

    except Exception as e:
        print(f"❌ Błąd przy przetwarzaniu sesji {session_id}: {e}")
        traceback.print_exc()
        status["status"] = "error"
        status["error"] = str(e)
        status["updated_at"] = now_iso()
        write_status(container_client, session_id, status)


def deallocate_self():
    if not AUTO_SHUTDOWN:
        print("ORCHESTRATOR_AUTO_SHUTDOWN=false — pomijam wyłączenie maszyny.")
        return

    try:
        meta = get_instance_metadata()
        credential = ManagedIdentityCredential()
        compute_client = ComputeManagementClient(credential, meta["subscription_id"])
        print(f"🛑 Deallokuję maszynę {meta['vm_name']} (rg: {meta['resource_group']})...")
        compute_client.virtual_machines.begin_deallocate(meta["resource_group"], meta["vm_name"])
    except Exception as e:
        print(f"⚠️ Nie udało się zdeallokować maszyny automatycznie: {e}")
        print("Sprawdź czy System-Assigned Managed Identity jest włączona i ma rolę "
              "'Virtual Machine Contributor'.")


def main():
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn_str:
        print("Błąd: brak AZURE_STORAGE_CONNECTION_STRING w środowisku.")
        return

    service_client = BlobServiceClient.from_connection_string(conn_str)
    container_client = service_client.get_container_client(CONTAINER_NAME)

    model = transcribe.load_model()

    processed_any = False
    attempts_left = RESCAN_ATTEMPTS

    while True:
        pending = find_pending_sessions(container_client)

        if not pending:
            if attempts_left <= 0:
                break
            print(f"Brak nowych sesji. Sprawdzam ponownie za {RESCAN_DELAY_SECONDS}s "
                  f"({attempts_left} próby zostały)...")
            attempts_left -= 1
            time.sleep(RESCAN_DELAY_SECONDS)
            continue

        attempts_left = RESCAN_ATTEMPTS  # reset, bo znaleźliśmy pracę
        for session_id, status in pending:
            process_session(container_client, session_id, status, model)
            processed_any = True

    if processed_any:
        print("\nWszystkie oczekujące sesje przetworzone.")
    else:
        print("\nNie było żadnych sesji do przetworzenia.")

    deallocate_self()


if __name__ == "__main__":
    main()
