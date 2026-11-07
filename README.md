# DnD notes – Azure Container Apps Job (GPU)

Kontener, który pobiera nagrania sesji z Azure Blob Storage, robi transkrypcję
(Whisper `large-v3`) i notatki (lokalny LLM w Ollamie), zapisuje wyniki z
powrotem do blobu i kończy pracę. Przeznaczony do uruchamiania jako
**Azure Container Apps Job** na serverless GPU **T4** (`Consumption-GPU-NC8as-T4`,
8 vCPU / 56 GiB RAM / 16 GB VRAM).

Kolejność pracy w jednym uruchomieniu:

1. transkrypcja wszystkich sesji o statusie `recorded` / `processing` → `transcribed`,
2. zwolnienie Whispera z GPU,
3. notatki dla sesji `transcribed` → `done` (LLM może częściowo działać na CPU,
   jeśli model nie mieści się w 16 GB VRAM).

## Budowanie i wysyłka do Docker Hub

```bash
docker login
# model LLM pobierany przy starcie joba (mniejszy obraz, łatwo zmienić model)
docker build -t <user>/dnd-notes:0.1 .
# albo model LLM wbudowany w obraz (+~20 GB)
docker build --build-arg OLLAMA_MODEL=gemma4:31b -t <user>/dnd-notes:0.1-gemma31b .
docker push <user>/dnd-notes:0.1
```

## Zmienne środowiskowe

| Zmienna | Domyślnie | Opis |
|---|---|---|
| `AZURE_STORAGE_CONNECTION_STRING` | – (wymagana) | connection string do Storage Account |
| `AZURE_STORAGE_CONTAINER` | `audio-files` | kontener z sesjami |
| `OLLAMA_MODEL_NAME` | `gemma4:31b` | model LLM (np. `gemma4:31b-it-q8_0` – lepsza jakość, 34 GB) |
| `OLLAMA_NUM_CTX` | `16384` | okno kontekstu LLM |
| `OLLAMA_TEMPERATURE` | `0.3` | niżej = mniej zmyślania |
| `OLLAMA_THINK` | – | `true` włącza tryb rozumowania (jeśli model go ma) |
| `WHISPER_MODEL` | `large-v3` | ustawiany przy budowaniu (`--build-arg WHISPER_MODEL=...`) |
| `ORCHESTRATOR_CLEANUP_AUDIO` | `true` | kasuj `.pcm` po udanym przetworzeniu |

## Wdrożenie jako Container Apps Job

Środowisko musi być w trybie **WorkloadProfiles** (CLI na subskrypcji studenckiej
zakłada domyślnie środowisko *Express*, które nie obsługuje jobów ani GPU),
w regionie z serverless GPU (np. `swedencentral`), z profilem
`Consumption-GPU-NC8as-T4`.

```bash
az containerapp job create -g <rg> -n dnd-notes-job \
  --environment <env> --workload-profile-name gput4 \
  --trigger-type Manual --parallelism 1 --replica-completion-count 1 \
  --replica-timeout 14400 --replica-retry-limit 0 \
  --cpu 8 --memory 56Gi \
  --image docker.io/<user>/dnd-notes:0.1 \
  --registry-server docker.io --registry-username <user> --registry-password <docker-hub-token> \
  --secrets storage-conn="<connection string>" \
  --env-vars AZURE_STORAGE_CONNECTION_STRING=secretref:storage-conn

az containerapp job start -g <rg> -n dnd-notes-job
az containerapp job execution list -g <rg> -n dnd-notes-job -o table
az containerapp job logs show -g <rg> -n dnd-notes-job --container dnd-notes-job --follow
```

## Uruchomienie lokalne (bez Dockera)

```bash
sudo apt install ffmpeg
pip install -r requirements.txt
ollama serve &            # lokalna Ollama
cp .env.example .env      # uzupełnij connection string
python orchestrator.py
```
