#!/usr/bin/env bash
# ==============================================================================
# PowerOf3 Trading - Production Deployment Script for GCloud Linux VM
# ==============================================================================

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

echo "📂 Deploying PowerOf3 Trading in: $(pwd)"

# 1. Sync latest code from Git (Hard reset preserves git-ignored .env & config.yaml)
if [ -d ".git" ]; then
    CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo 'main')"
    echo "🔄 Pulling latest changes from branch: ${CURRENT_BRANCH}..."
    git fetch origin "${CURRENT_BRANCH}" || true
    git reset --hard "origin/${CURRENT_BRANCH}" || true
fi

# Ensure dynamic server configurations exist
if [ ! -f "config/config.yaml" ] && [ -f "config/config.yaml.example" ]; then
    echo "⚙️ Creating config/config.yaml from template..."
    cp config/config.yaml.example config/config.yaml
fi

if [ ! -f ".env" ] && [ -f "config/.env.example" ]; then
    echo "🔑 Creating .env from template..."
    cp config/.env.example .env
fi

# 2. Python Environment Setup
if [ -d ".venv" ]; then
    VENV_PATH=".venv"
elif [ -d "venv" ]; then
    VENV_PATH="venv"
else
    echo "🐍 Creating virtual environment (.venv)..."
    python3 -m venv .venv
    VENV_PATH=".venv"
fi

echo "🔌 Activating virtual environment (${VENV_PATH})..."
source "${VENV_PATH}/bin/activate"

echo "📦 Installing core dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

echo "📦 Installing Kotak Neo SDK from GitHub..."
pip install git+https://github.com/Kotak-Neo/kotak-neo-python.git

# Ensure data directories exist
mkdir -p data/cache data/sessions logs

# 3. Process Management via Tmux
echo "🛑 Stopping existing tmux session 'powerof3' if active..."
tmux kill-session -t powerof3 2>/dev/null || true

echo "🚀 Starting PowerOf3 Trading Daemon in Tmux session 'powerof3'..."

# Window 1: TRADING ENGINE
tmux new-session -d -s powerof3 -n 'engine' "${VENV_PATH}/bin/python3 main.py start"

# Window 2: INTERACTIVE DASHBOARD / CLI
tmux new-window -t powerof3 -n 'dashboard'
tmux send-keys -t powerof3:dashboard "cd ${PROJECT_DIR} && source ${VENV_PATH}/bin/activate" C-m

echo "====================================================="
echo "✅ PowerOf3 Trading Deployed Successfully!"
echo "====================================================="
echo "👀 To view live terminal dashboard:  tmux attach -t powerof3"
echo "🚪 To detach safely from view:      Press Ctrl+B, then D"
echo "📊 Run dashboard snapshot:           ./.venv/bin/python3 main.py dashboard"
echo "🛒 Place manual discretionary order: ./.venv/bin/python3 main.py order"
echo "🔔 Test Telegram alert:              ./.venv/bin/python3 main.py test-alert"
echo "====================================================="
