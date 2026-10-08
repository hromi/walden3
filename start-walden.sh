#!/usr/bin/env bash
# SUPERSEDED: Walden now runs as systemd user services (deploy/systemd/, see README section 6).
# Do not run this while walden.service / walden-ollama.service are active: both use port 11435.
# Starts the Walden deployment in tmux session "walden":
#   window 0 "ollama" - dedicated Ollama (port 11435) serving granite4:small-h (and any other model Walden asks for)
#   window 1 "bot"    - Walden Matrix bot
# The system-wide Ollama on :11434 is shared with other apps and is left alone.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs

OLLAMA_BIN=$HOME/.local/ollama
OLLAMA_ENV="PATH=$OLLAMA_BIN/bin:\$PATH CUDA_VISIBLE_DEVICES=0 OLLAMA_HOST=127.0.0.1:11435 OLLAMA_MODELS=$HOME/.ollama/models OLLAMA_KEEP_ALIVE=24h OLLAMA_MAX_QUEUE=16"

tmux has-session -t walden 2>/dev/null || tmux new-session -d -s walden -n ollama
tmux list-windows -t walden -F '#W' | grep -qx ollama || tmux new-window -t walden -n ollama

tmux send-keys -t walden:ollama "$OLLAMA_ENV ollama serve 2>&1 | tee -a $PWD/logs/ollama.log" Enter
until curl -sf -m2 http://127.0.0.1:11435/api/version >/dev/null; do sleep 1; done

tmux list-windows -t walden -F '#W' | grep -qx bot || tmux new-window -t walden -n bot
tmux send-keys -t walden:bot "cd $PWD && set -a && . ./.env && set +a && .venv/bin/walden -c config.yaml matrix 2>&1 | tee -a logs/walden.log" Enter

echo "walden session started: tmux attach -t walden"
