# Virtual machine serie D

## Installing dependecies for transcription

### Installing system dependecies

```
sudo apt update && sudo apt install ffmpeg -y
```

## Installing Python dependecies

```
pip install azure-storage-blob
pip install python-dotenv
pip install openai-whisper
```

## Installing dependecies for making notes

### Installing Python dependencies

```
pip install requests
```

### Installing Ollama

```
curl -fsSL https://ollama.com/install.sh | sh
```

Additionally you need to run command *ollama run ***model**.
This will locally install model of llm you want to use to make notes.


