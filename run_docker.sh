#!/usr/bin/env bash
set -e

# Change directory to gateway script location
CDIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$CDIR"

echo "🚀 Launching Wan2GP Gateway via Docker Compose..."
docker compose up -d --build

echo "✅ Gateway container is running!"
echo "📡 Gateway API: http://localhost:50080"
echo "⚙️  Settings UI: http://localhost:50080/"
