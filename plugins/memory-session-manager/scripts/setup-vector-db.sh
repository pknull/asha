#!/bin/bash
# Setup script for memory-session-manager vector DB dependencies

set -e

echo "🔧 Memory Session Manager - Vector DB Setup"
echo "==========================================="
echo ""

# Check if Python is available
if ! command -v python3 &> /dev/null; then
    echo "❌ Error: Python 3 is not installed"
    echo "   Please install Python 3.8+ and try again"
    exit 1
fi

# Check Python version
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "✓ Found Python $PYTHON_VERSION"

# Check if pip is available
if ! python3 -m pip --version &> /dev/null; then
    echo "❌ Error: pip is not installed"
    echo "   Install with: python3 -m ensurepip --default-pip"
    exit 1
fi

echo "✓ Found pip"
echo ""

# Get the plugin directory
PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Check if requirements.txt exists
if [ ! -f "$PLUGIN_DIR/requirements.txt" ]; then
    echo "❌ Error: requirements.txt not found in plugin directory"
    exit 1
fi

echo "📦 Installing vector DB dependencies..."
echo "   (mem0ai, qdrant-client)"
echo ""

# Install requirements
python3 -m pip install -r "$PLUGIN_DIR/requirements.txt"

echo ""
echo "✅ Vector DB dependencies installed successfully!"
echo ""
echo "📝 Next steps:"
echo "   1. Create vector DB directory in your project: mkdir -p Memory/vector_db"
echo "   2. Ingest Memory files: .venv/bin/python tools/ingest_memory.py"
echo "   3. Search Memory: .venv/bin/python tools/mem0_helper.py search 'query'"
echo ""
