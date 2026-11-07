"""
Transkrypcja pojedynczej sesji D&D pobranej z Azure Blob Storage.

Ten moduł jest importowany przez orchestrator.py, ale można go też uruchomić
samodzielnie: python transcribe.py <session_id>
"""

import os
import sys
import warnings
import subprocess

warnings.filterwarnings("ignore", message="FP16 is not supported on CPU")

CONTAINER_NAME = "audio-files"
DOWNLOAD_DIR = "downloaded_recordings"

_model = None


def load_model(model_size: str = "medium"):
    """Ładuje model Whisper raz i trzyma w pamięci (wywołaj raz na cały proces orchestratora)."""
    global _model
    if _model is None:
        import whisper
        print(f"Ładowanie modelu Whisper ({model_size})...")
        _model = whisper.load_model(model_size)
    return _model


def transcribe_session(container_client, session_id: str, model=None) -> str:
    """
    Pobiera wszystkie pliki .pcm należące do danej sesji (prefix `sessions/<session_id>/`),
    transkrybuje je i zwraca gotowy dialog jako tekst (posortowany chronologicznie,
    pogrupowany blokami wypowiedzi tej samej osoby).
    """
    if model is None:
        model = load_model()

    session_dir = os.path.join(DOWNLOAD_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)

    prefix = f"sessions/{session_id}/"
    blobs = [b for b in container_client.list_blobs(name_starts_with=prefix) if b.name.endswith(".pcm")]

    if not blobs:
        raise RuntimeError(f"Brak plików .pcm dla sesji {session_id} (prefix {prefix})")

    print(f"Sesja {session_id}: znaleziono {len(blobs)} nagrań. Pobieram i transkrybuję...")

    global_timeline = []

    for blob in blobs:
        filename = os.path.basename(blob.name)
        local_path = os.path.join(session_dir, filename)

        if not os.path.exists(local_path):
            print(f"Pobieranie: {blob.name}")
            blob_data = container_client.get_blob_client(blob).download_blob()
            with open(local_path, "wb") as f:
                f.write(blob_data.readall())

        parts = filename.split('_')
        if len(parts) < 3:
            continue
        user_name = parts[1]
        try:
            file_start_time_ms = int(parts[2].split('.')[0])
        except ValueError:
            continue

        if not os.path.exists(local_path) or os.path.getsize(local_path) < 4096:
            print(f"⚠️ Pomijam {filename} (plik jest pusty lub zbyt mały)")
            continue

        wav_path = local_path.replace(".pcm", ".wav")

        try:
            print(f"Przetwarzam: {filename}...")
            subprocess.run([
                'ffmpeg', '-y', '-f', 's16le', '-ar', '48000', '-ac', '2',
                '-i', local_path, wav_path
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

            result = model.transcribe(wav_path, language="pl", fp16=False)

            if os.path.exists(wav_path):
                os.remove(wav_path)
        except Exception as e:
            print(f"❌ Błąd przy pliku {filename}, pomijam. Szczegóły: {e}")
            continue

        for segment in result['segments']:
            text = segment['text'].strip()
            if not text or len(text) < 2:
                continue
            absolute_time = file_start_time_ms + segment['start'] * 1000
            global_timeline.append({"time": absolute_time, "user": user_name, "text": text})

    if not global_timeline:
        raise RuntimeError(f"Sesja {session_id}: cisza lub szumy, nie udało się wyciągnąć słów.")

    global_timeline.sort(key=lambda x: x["time"])

    transcript = []
    last_user = None
    current_block = ""

    for item in global_timeline:
        if item["user"] == last_user:
            current_block += " " + item["text"]
        else:
            if last_user:
                transcript.append(f"[{last_user}]: {current_block}")
            last_user = item["user"]
            current_block = item["text"]

    if last_user and current_block:
        transcript.append(f"[{last_user}]: {current_block}")

    return "\n".join(transcript)


if __name__ == "__main__":
    from dotenv import load_dotenv
    from azure.storage.blob import BlobServiceClient

    load_dotenv()

    if len(sys.argv) < 2:
        print("Użycie: python transcribe.py <session_id>")
        sys.exit(1)

    session_id = sys.argv[1]
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not conn_str:
        print("Błąd: brak AZURE_STORAGE_CONNECTION_STRING")
        sys.exit(1)

    service_client = BlobServiceClient.from_connection_string(conn_str)
    container_client = service_client.get_container_client(CONTAINER_NAME)

    text = transcribe_session(container_client, session_id)
    out_path = f"transcript_{session_id}.txt"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"\nZapisano: {out_path}")
