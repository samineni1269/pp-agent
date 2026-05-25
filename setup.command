#!/bin/bash
# ── PP Agent Setup ────────────────────────────────────────────────────────────
# Double-click once to install dependencies and create .env from template.
# ─────────────────────────────────────────────────────────────────────────────

cd "$(dirname "$0")"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  ⚡  PP Agent Setup                                  ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# Create .env if not present
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "✅ Created .env from template."
  echo "   → Open .env in a text editor and fill in your credentials."
  echo ""
else
  echo "ℹ️  .env already exists — skipping creation."
fi

# Create venv and install dependencies
echo "🔧 Creating virtual environment..."
python3 -m venv venv
source venv/bin/activate
echo "📦 Installing Python dependencies..."
pip install -r requirements.txt

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║  ✅  Setup complete!                                  ║"
echo "║                                                      ║"
echo "║  Next steps:                                         ║"
echo "║  1. Edit .env with your credentials                  ║"
echo "║  2. Double-click launch.command to start             ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
read -p "Press Enter to close..."
