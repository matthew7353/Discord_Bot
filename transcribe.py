import os
import whisper
import warnings
import subprocess
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv

load_dotenv()
warnings.filterwarnings("ignore", message="FP16 is not supported on CPU")

AZURE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
CONTAINER_NAME = "audio-files"
DOWNLOAD_DIR = "downloaded_recordings"

print("Ładowanie modelu Whisper (to potrwa krótką chwilę)...")
model = whisper.load_model("small")

def process_session():
    if not AZURE_CONNECTION_STRING:
        return "Błąd: Brak zmiennej środowiskowej AZURE_STORAGE_CONNECTION_STRING."

    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR)

    print("Łączenie z Azure Blob Storage...")
    try:
        blob_service_client = BlobServiceClient.from_connection_string(AZURE_CONNECTION_STRING)
        container_client = blob_service_client.get_container_client(CONTAINER_NAME)
    except Exception as e:
        return f"Błąd połączenia z Azure: {e}"

    print("Pobieranie plików .pcm z chmury...")
    blob_list = container_client.list_blobs()
    files = []

    for blob in blob_list:
        if blob.name.endswith(".pcm"):
            download_file_path = os.path.join(DOWNLOAD_DIR, blob.name)
            files.append(blob.name)
            
            if not os.path.exists(download_file_path):
                print(f"Pobieranie: {blob.name}")
                with open(download_file_path, "wb") as download_file:
                    blob_data = container_client.get_blob_client(blob).download_blob()
                    download_file.write(blob_data.readall())

    if not files:
        return "Brak plików nagrań (.pcm) w kontenerze."

    global_timeline = []
    print(f"Znaleziono {len(files)} nagrań. Rozpoczynam transkrypcję...")

    for file in files:
        parts = file.split('_')
        if len(parts) < 3: continue
            
        user_name = parts[1]
        try:
            file_start_time_ms = int(parts[2].split('.')[0])
        except ValueError:
            continue
        
        path = os.path.join(DOWNLOAD_DIR, file)
        
        # Ignorujemy pliki-wydmuszki (surowe PCM waży sporo, więc < 4KB to błąd)
        if not os.path.exists(path) or os.path.getsize(path) < 4096:
            print(f"⚠️ Pomijam {file} (plik jest pusty lub zbyt mały)")
            continue
        
        wav_path = path.replace(".pcm", ".wav")
        
        try:
            print(f"Przetwarzam: {file}...")
            # Błyskawiczna konwersja surowego PCM na standardowy WAV dla Whispera
            subprocess.run([
                'ffmpeg', '-y', '-f', 's16le', '-ar', '48000', '-ac', '2',
                '-i', path, wav_path
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            
            result = model.transcribe(wav_path, language="pl", fp16=False)
            
            # Sprzątamy plik tymczasowy .wav, zachowujemy .pcm!
            if os.path.exists(wav_path):
                os.remove(wav_path)

        except Exception as e:
            print(f"❌ Błąd przy pliku {file}, pomijam. Szczegóły: {e}")
            continue

        for segment in result['segments']:
            text = segment['text'].strip()
            if not text or len(text) < 2:
                continue
            
            segment_start_ms = segment['start'] * 1000
            absolute_time = file_start_time_ms + segment_start_ms
            
            global_timeline.append({
                "time": absolute_time,
                "user": user_name,
                "text": text
            })

    if not global_timeline:
        return "Cisza lub szumy. Nie udało się wyciągnąć słów."

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

full_text = process_session()

print("\n--- PRZYGOTOWANY DIALOG ---")
print(full_text)

with open("transcript.txt", "w", encoding="utf-8") as f:
    f.write(full_text)
print("Zapisano surową transkrypcję do pliku: transcript.txt")
