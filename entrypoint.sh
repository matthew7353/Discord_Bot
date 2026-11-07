#!/usr/bin/env bash
# Startuje lokalny serwer Ollamy w tle, uruchamia orchestrator i kończy kontener
# z jego kodem wyjścia (od tego zależy, czy wykonanie joba jest Succeeded/Failed).
set -uo pipefail

ollama serve > >(sed -u 's/^/[ollama] /') 2>&1 &
OLLAMA_PID=$!

python -u /app/orchestrator.py "$@" &
APP_PID=$!

# Azure wysyła SIGTERM przy przekroczeniu timeoutu / ręcznym zatrzymaniu.
trap 'kill -TERM "$APP_PID" 2>/dev/null' TERM INT

wait "$APP_PID"
CODE=$?
# wait zwraca wcześniej, jeśli przerwał go sygnał - poczekaj na faktyczny koniec.
if kill -0 "$APP_PID" 2>/dev/null; then
    wait "$APP_PID"
    CODE=$?
fi

kill -TERM "$OLLAMA_PID" 2>/dev/null
wait "$OLLAMA_PID" 2>/dev/null
exit "$CODE"
