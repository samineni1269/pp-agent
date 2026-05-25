#!/bin/bash
# ── PP Agent Launcher ────────────────────────────────────────────────────────
# Double-click this file on macOS to start the agent web UI.
# ─────────────────────────────────────────────────────────────────────────────

cd "$(dirname "$0")"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  ⚡  PP Agent — Power Platform AI                    ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# Check .env exists
if [ ! -f ".env" ]; then
  echo "⚠️  .env file not found!"
  echo "   Run setup.command first, or copy .env.example to .env and fill it in."
  echo ""
  read -p "Press Enter to exit..."
  exit 1
fi

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "❌ Python 3 not found. Install from https://python.org"
  read -p "Press Enter to exit..."
  exit 1
fi

# Create venv if missing
if [ ! -d "venv" ]; then
  echo "🔧 Creating virtual environment..."
  python3 -m venv venv
fi

# Activate venv
source venv/bin/activate

# Install / update dependencies
echo "📦 Checking dependencies..."
pip install -r requirements.txt -q

# Open browser after 2s
(sleep 2 && open http://localhost:5005) &

# Start Flask
python3 app.py
