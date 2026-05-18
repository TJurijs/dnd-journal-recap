#!/usr/bin/env bash
set -euo pipefail

DIR="/opt/dnd-recap-bot"
if [ ! -d "$DIR" ]; then
    git clone https://github.com/TJurijs/dnd-journal-recap.git "$DIR"
fi

cd "$DIR"
cp -n .env.example .env || true
echo "Edit .env with your DISCORD_BOT_TOKEN and GEMINI_API_KEY, then run: docker compose up -d"
