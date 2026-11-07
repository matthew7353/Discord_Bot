"""
Transkrypcja pojedynczej sesji D&D pobranej z Azure Blob Storage.

Ten moduł jest importowany przez orchestrator.py, ale można go też uruchomić
samodzielnie: python transcribe.py <session_id>

Zmienne środowiskowe:
  WHISPER_MODEL      - rozmiar modelu (domyślnie large-v3)
  WHISPER_CACHE_DIR  - gdzie leżą wagi modelu (w obrazie: /models/whisper)
  WHISPER_DEVICE     - "cuda" / "cpu" (domyślnie: cuda jeśli dostępna)
"""

import gc
import os
import sys
import warnings
import subprocess

warnings.filterwarnings("ignore", message="FP16 is not supported on CPU")

CONTAINER_NAME = os.getenv("AZURE_STORAGE_CONTAINER", "audio-files")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloaded_recordings")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "large-v3")
WHISPER_CACHE_DIR = os.getenv("WHISPER_CACHE_DIR") or None

_model = None
_device = None


def _pick_device():
    import torch
    wanted = os.getenv("WHISPER_DEVICE")
    if wanted:
        return wanted
    return "cuda" if torch.cuda.is_available() else "cpu"


def load_model(model_size: str = WHISPER_MODEL):
    """Ładuje model Whisper raz i trzyma w pamięci (wywołaj raz na cały proces orchestratora)."""
    global _model, _device
    if _model is None:
        import whisper
        _device = _pick_device()
        print(f"Ładowanie modelu Whisper ({model_size}) na {_device}...")
        _model = whisper.load_model(model_size, device=_device, download_root=WHISPER_CACHE_DIR)
    return _model


def unload_model():
    """Zwalnia Whispera z pamięci GPU, żeby zrobić miejsce dla LLM w Ollamie."""
    global _model
    if _model is None:
        return
    import torch
    _model = None
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    print("Whisper zwolniony z pamięci.")


def _download_blob(container_client, blob_name, local_path):
    """Pobiera blob strumieniowo prosto do pliku (bez wczytywania całości do RAM)."""
    tmp_path = local_path + ".part"
    with open(tmp_path, "wb") as f:
        container_client.get_blob_client(blob_name).download_blob(max_concurrency=4).readinto(f)
    os.replace(tmp_path, local_path)


def transcribe_session(container_client, session_id: str, model=None) -> str:
    """
    Pobiera wszystkie pliki .pcm należące do danej sesji (prefix `sessions/<session_id>/`),
    transkrybuje je i zwraca gotowy dialog jako tekst (posortowany chronologicznie,
    pogrupowany blokami wypowiedzi tej samej osoby).
    """
    if model is None:
        model = load_model()
    use_fp16 = (_device == "cuda")

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

        parts = filename.split('_')
        if len(parts) < 3:
            continue
        user_name = parts[1]
        try:
            file_start_time_ms = int(parts[2].split('.')[0])
        except ValueError:
            continue

        if (blob.size or 0) < 4096:
            print(f"⚠️ Pomijam {filename} (plik jest pusty lub zbyt mały)")
            continue

        local_path = os.path.join(session_dir, filename)
        wav_path = local_path[:-len(".pcm")] + ".wav"

        try:
            print(f"Przetwarzam: {filename}...")
            _download_blob(container_client, blob.name, local_path)

            # 48 kHz stereo z Discorda -> 16 kHz mono (tego i tak używa Whisper,
            # a plik jest 6x mniejszy).
            subprocess.run([
                'ffmpeg', '-y', '-loglevel', 'error',
                '-f', 's16le', '-ar', '48000', '-ac', '2', '-i', local_path,
                '-ar', '16000', '-ac', '1', wav_path
            ], check=True)

            result = model.transcribe(wav_path, language="pl", fp16=use_fp16)
        except Exception as e:
            print(f"❌ Błąd przy pliku {filename}, pomijam. Szczegóły: {e}")
            continue
        finally:
            # Sprzątamy od razu - dysk repliki jest mały i tymczasowy.
            for p in (local_path, wav_path):
                if os.path.exists(p):
                    os.remove(p)

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
