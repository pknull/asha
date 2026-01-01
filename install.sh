#!/bin/bash
# Asha Install Script - Sets up Asha in a project
# Run from project root: ./asha/install.sh
#
# Options:
#   --yes, -y      Accept all defaults (non-interactive)
#   --minimal      Skip optional features (Vector DB)
#   --full         Install everything without prompts

set -euo pipefail

# =============================================================================
# Configuration
# =============================================================================

# Color output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Modes
INTERACTIVE=true
INSTALL_VECTORDB=true
AUTO_YES=false

# =============================================================================
# Helper Functions
# =============================================================================

warn() { echo -e "${YELLOW}⚠  $1${NC}"; }
success() { echo -e "${GREEN}✓  $1${NC}"; }
error() { echo -e "${RED}✗  $1${NC}"; }
info() { echo -e "${BLUE}ℹ  $1${NC}"; }
header() { echo -e "\n${BOLD}$1${NC}"; }

# Prompt user for yes/no, with default
# Usage: ask "Question?" Y  -> default yes
#        ask "Question?" N  -> default no
ask() {
    local prompt="$1"
    local default="${2:-Y}"

    if [[ "$AUTO_YES" == true ]]; then
        [[ "$default" =~ ^[Yy] ]] && return 0 || return 1
    fi

    if [[ "$INTERACTIVE" == false ]]; then
        [[ "$default" =~ ^[Yy] ]] && return 0 || return 1
    fi

    local yn_hint="[Y/n]"
    [[ "$default" =~ ^[Nn] ]] && yn_hint="[y/N]"

    read -p "$(echo -e "${BOLD}$prompt${NC} $yn_hint ") " response
    response="${response:-$default}"

    [[ "$response" =~ ^[Yy] ]] && return 0 || return 1
}

# Prompt user for choice selection
# Usage: choose "Question?" "Option 1" "Option 2" "Option 3"
# Returns: selected option number (1-indexed) in $CHOICE
choose() {
    local prompt="$1"
    shift
    local options=("$@")

    if [[ "$INTERACTIVE" == false ]] || [[ "$AUTO_YES" == true ]]; then
        CHOICE=1
        return
    fi

    echo -e "\n${BOLD}$prompt${NC}"
    local i=1
    for opt in "${options[@]}"; do
        echo "  $i) $opt"
        ((i++))
    done

    while true; do
        read -p "$(echo -e "${BOLD}Select [1-${#options[@]}]:${NC} ") " response
        if [[ "$response" =~ ^[0-9]+$ ]] && (( response >= 1 && response <= ${#options[@]} )); then
            CHOICE="$response"
            return
        fi
        echo "Invalid selection. Please enter a number between 1 and ${#options[@]}."
    done
}

# =============================================================================
# Parse Arguments
# =============================================================================

while [[ $# -gt 0 ]]; do
    case "$1" in
        --yes|-y)
            AUTO_YES=true
            shift
            ;;
        --minimal)
            INTERACTIVE=false
            INSTALL_VECTORDB=false
            shift
            ;;
        --full)
            INTERACTIVE=false
            AUTO_YES=true
            INSTALL_VECTORDB=true
            shift
            ;;
        --help|-h)
            echo "Asha Install Script"
            echo ""
            echo "Usage: ./install.sh [options]"
            echo ""
            echo "Options:"
            echo "  --yes, -y    Accept all defaults (non-interactive)"
            echo "  --minimal    Skip optional features (Vector DB)"
            echo "  --full       Install everything without prompts"
            echo "  --help, -h   Show this help message"
            exit 0
            ;;
        *)
            error "Unknown option: $1"
            exit 1
            ;;
    esac
done

# =============================================================================
# Detect Project Root
# =============================================================================

if [[ -d "asha" ]]; then
    PROJECT_ROOT="$(pwd)"
elif [[ -d "../asha" ]]; then
    PROJECT_ROOT="$(cd .. && pwd)"
else
    error "Run from project root (where asha/ directory is located)"
    exit 1
fi

ASHA_DIR="$PROJECT_ROOT/asha"

# =============================================================================
# Welcome Banner
# =============================================================================

clear 2>/dev/null || true
echo -e "${BOLD}"
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║                     Asha Installation                         ║"
echo "║               Cognitive Scaffold Framework                    ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"
echo "Project: $PROJECT_ROOT"
echo ""

# =============================================================================
# Step 1: Create Directory Structure (always runs)
# =============================================================================

header "Step 1: Creating directory structure"

mkdir -p "$PROJECT_ROOT/Memory/sessions/archive"
mkdir -p "$PROJECT_ROOT/Memory/reasoning_bank"
mkdir -p "$PROJECT_ROOT/Memory/vector_db"
mkdir -p "$PROJECT_ROOT/Work/markers"
mkdir -p "$PROJECT_ROOT/.claude/hooks"
mkdir -p "$PROJECT_ROOT/.claude/commands"

success "Directory structure created"

# =============================================================================
# Step 2: Check Python Availability
# =============================================================================

header "Step 2: Checking Python environment"

SYSTEM_PYTHON=""
PYTHON_VERSION=""

if command -v python3 >/dev/null 2>&1; then
    SYSTEM_PYTHON="python3"
    PYTHON_VERSION=$($SYSTEM_PYTHON --version 2>&1)
    success "Found: $PYTHON_VERSION"
elif command -v python >/dev/null 2>&1; then
    SYSTEM_PYTHON="python"
    PYTHON_VERSION=$($SYSTEM_PYTHON --version 2>&1)
    success "Found: $PYTHON_VERSION"
else
    error "Python not found"
    echo ""
    echo "Python 3.8+ is required. Install with:"
    echo "  Ubuntu/Debian: sudo apt install python3 python3-venv"
    echo "  macOS:         brew install python3"
    echo "  Fedora:        sudo dnf install python3"
    exit 1
fi

# =============================================================================
# Step 3: Setup Python Virtual Environment
# =============================================================================

header "Step 3: Python virtual environment"

VENV_DIR="$ASHA_DIR/.venv"
PYTHON_CMD="$SYSTEM_PYTHON"
PIP_CMD=""

if [[ -d "$VENV_DIR" ]]; then
    info "Existing virtual environment found at: $VENV_DIR"
    PYTHON_CMD="$VENV_DIR/bin/python"
    PIP_CMD="$VENV_DIR/bin/pip"
    success "Using existing virtual environment"
else
    echo ""
    echo "A Python virtual environment is needed for dependencies."
    echo "Location: $VENV_DIR"
    echo ""

    if ask "Create Python virtual environment?" Y; then
        echo ""
        if $SYSTEM_PYTHON -m venv "$VENV_DIR" 2>&1; then
            PYTHON_CMD="$VENV_DIR/bin/python"
            PIP_CMD="$VENV_DIR/bin/pip"
            success "Virtual environment created"
        else
            warn "Could not create virtual environment"
            echo ""
            echo "You may need to install python3-venv:"
            echo "  sudo apt install python3-venv"
            echo ""
            if ! ask "Continue without virtual environment?" N; then
                echo "Installation cancelled."
                exit 1
            fi
        fi
    else
        info "Skipping virtual environment setup"
        echo "  Dependencies will need manual installation."
    fi
fi

# =============================================================================
# Step 4: Install Python Dependencies
# =============================================================================

header "Step 4: Python dependencies"

DEPS_INSTALLED=false

if [[ -n "$PIP_CMD" && -f "$PIP_CMD" ]]; then
    echo ""
    echo "Required packages:"
    echo "  • chromadb  - Vector database for semantic search"
    echo "  • requests  - HTTP client for Ollama API"
    echo ""

    if ask "Install Python dependencies?" Y; then
        echo ""
        echo "Installing packages (this may take a moment)..."
        if "$PIP_CMD" install -r "$ASHA_DIR/requirements.txt" 2>&1 | while read line; do
            # Show progress dots
            echo -n "."
        done; then
            echo ""
            success "Python dependencies installed"
            DEPS_INSTALLED=true
        else
            echo ""
            warn "Some packages failed to install"
            echo "  Run manually: $PIP_CMD install -r $ASHA_DIR/requirements.txt"
        fi
    else
        info "Skipping dependency installation"
    fi
else
    warn "No virtual environment available"
    echo "  Install dependencies manually: pip install chromadb requests"
fi

# =============================================================================
# Step 5: Check/Install Ollama (Optional)
# =============================================================================

header "Step 5: Ollama (optional - for semantic search)"

OLLAMA_READY=false

if command -v ollama >/dev/null 2>&1; then
    success "Ollama is installed"

    if pgrep -x "ollama" >/dev/null 2>&1 || curl -s http://localhost:11434/api/version >/dev/null 2>&1; then
        success "Ollama is running"
        OLLAMA_READY=true

        # Check for embedding model
        if ollama list 2>/dev/null | grep -q "nomic-embed-text"; then
            success "Embedding model (nomic-embed-text) available"
        else
            echo ""
            if ask "Download embedding model (nomic-embed-text, ~274MB)?" Y; then
                echo "Downloading model..."
                if ollama pull nomic-embed-text 2>&1; then
                    success "Embedding model downloaded"
                else
                    warn "Could not download model"
                    echo "  Run manually: ollama pull nomic-embed-text"
                fi
            fi
        fi
    else
        warn "Ollama installed but not running"
        echo "  Start with: ollama serve"
    fi
else
    info "Ollama not installed"
    echo ""
    echo "Ollama provides local embeddings for semantic search."
    echo "Without it, Vector DB features will be unavailable."
    echo ""
    echo "Install from: https://ollama.ai"
    echo ""

    if [[ "$INTERACTIVE" == true ]] && [[ "$AUTO_YES" == false ]]; then
        if ask "Open Ollama download page in browser?" N; then
            if command -v xdg-open >/dev/null 2>&1; then
                xdg-open "https://ollama.ai" 2>/dev/null &
            elif command -v open >/dev/null 2>&1; then
                open "https://ollama.ai" 2>/dev/null &
            fi
        fi
    fi
fi

# =============================================================================
# Step 6: Install Hooks (Platform Bridges)
# =============================================================================

header "Step 6: Installing hooks"

# Create directories
mkdir -p "$PROJECT_ROOT/.claude/hooks"
mkdir -p "$PROJECT_ROOT/.opencode/plugin"

# Cleanup old symlinks from previous installations
for old_link in "$PROJECT_ROOT/.claude/hooks/post-tool-use" \
                "$PROJECT_ROOT/.claude/hooks/session-end" \
                "$PROJECT_ROOT/.claude/hooks/common.sh" \
                "$PROJECT_ROOT/.claude/hooks/user-prompt-submit" \
                "$PROJECT_ROOT/.claude/hooks/violation-checker"; do
    if [[ -L "$old_link" ]]; then
        rm "$old_link"
        info "Removed old symlink: $(basename "$old_link")"
    elif [[ -f "$old_link" ]]; then
        # Regular file from previous installation - backup and remove
        mv "$old_link" "$old_link.backup.$(date +%Y%m%d%H%M%S)"
        info "Backed up: $(basename "$old_link")"
    fi
done

# Claude Code bridge: Copy hooks.json
CLAUDE_HOOKS="$PROJECT_ROOT/.claude/hooks/hooks.json"
if [[ -f "$CLAUDE_HOOKS" ]]; then
    cp "$CLAUDE_HOOKS" "$CLAUDE_HOOKS.backup.$(date +%Y%m%d%H%M%S)"
    warn "Existing hooks.json backed up"
fi
cp "$ASHA_DIR/bridges/claude.json" "$CLAUDE_HOOKS"
success "Claude Code hooks configured (.claude/hooks/hooks.json)"

# OpenCode bridge: Copy plugin
OPENCODE_PLUGIN="$PROJECT_ROOT/.opencode/plugin/asha-hooks.ts"
if [[ -f "$OPENCODE_PLUGIN" ]]; then
    cp "$OPENCODE_PLUGIN" "$OPENCODE_PLUGIN.backup.$(date +%Y%m%d%H%M%S)"
    warn "Existing asha-hooks.ts backed up"
fi
cp "$ASHA_DIR/bridges/opencode.ts" "$OPENCODE_PLUGIN"
success "OpenCode hooks configured (.opencode/plugin/asha-hooks.ts)"

# Ensure hook scripts are executable
chmod +x "$ASHA_DIR/hooks/"* 2>/dev/null || true

success "Hooks installed for both platforms"

# =============================================================================
# Step 7: Install Commands (Both Platforms)
# =============================================================================

header "Step 7: Installing commands"

# Claude Code commands (symlinks)
for cmd in "$ASHA_DIR/commands/"*.md; do
    if [[ -f "$cmd" ]]; then
        cmd_name=$(basename "$cmd")
        target="$PROJECT_ROOT/.claude/commands/$cmd_name"
        [[ -L "$target" ]] && rm "$target"
        ln -sf "$ASHA_DIR/commands/$cmd_name" "$target"
        echo "  → Claude: $cmd_name"
    fi
done

# OpenCode commands (copies - format is compatible but we strip Claude-specific frontmatter)
mkdir -p "$PROJECT_ROOT/.opencode/command"
for cmd in "$ASHA_DIR/commands/"*.md; do
    if [[ -f "$cmd" ]]; then
        cmd_name=$(basename "$cmd")
        target="$PROJECT_ROOT/.opencode/command/$cmd_name"

        # Remove existing file/symlink before copying (handles symlink-to-source issue)
        [[ -e "$target" || -L "$target" ]] && rm "$target"

        # Copy and strip Claude-specific frontmatter fields
        # OpenCode ignores unknown fields, so we can just copy directly
        cp "$cmd" "$target"
        echo "  → OpenCode: $cmd_name"
    fi
done

success "Commands installed for both platforms"

# =============================================================================
# Step 8: Set Permissions
# =============================================================================

header "Step 8: Setting permissions"

chmod +x "$ASHA_DIR/tools/"* 2>/dev/null || true
chmod +x "$ASHA_DIR/hooks/"* 2>/dev/null || true
success "Tool permissions set"

# =============================================================================
# Step 9: Copy Templates
# =============================================================================

header "Step 9: Memory templates"

for tmpl in "$ASHA_DIR/templates/"*.md; do
    if [[ -f "$tmpl" ]]; then
        filename=$(basename "$tmpl")
        target="$PROJECT_ROOT/Memory/$filename"
        if [[ ! -f "$target" ]]; then
            cp "$tmpl" "$target"
            echo "  → Created Memory/$filename"
        else
            echo "  → Memory/$filename (exists, skipped)"
        fi
    fi
done

success "Memory files ready"

# =============================================================================
# Step 10: Generate CLAUDE.md
# =============================================================================

header "Step 10: Generating CLAUDE.md"

CLAUDE_MD="$PROJECT_ROOT/CLAUDE.md"
CLAUDE_TEMPLATE="$ASHA_DIR/templates/CLAUDE.md"

if [[ ! -f "$CLAUDE_MD" ]]; then
    if [[ -f "$CLAUDE_TEMPLATE" ]]; then
        cp "$CLAUDE_TEMPLATE" "$CLAUDE_MD"
        success "CLAUDE.md created (Claude Code auto-context)"
    else
        warn "CLAUDE.md template not found"
    fi
else
    echo "  → CLAUDE.md (exists, skipped)"
fi

# =============================================================================
# Step 11: Initialize Databases
# =============================================================================

header "Step 11: Initializing databases"

# ReasoningBank
if [[ -f "$PYTHON_CMD" ]] || command -v "$PYTHON_CMD" >/dev/null 2>&1; then
    if $PYTHON_CMD "$ASHA_DIR/tools/reasoning_bank.py" stats >/dev/null 2>&1; then
        success "ReasoningBank initialized"
    else
        warn "ReasoningBank initialization failed"
    fi
else
    warn "Skipping ReasoningBank (Python not configured)"
fi

# Vector DB check
VECTORDB_READY=false
if [[ -f "$PYTHON_CMD" ]] || command -v "$PYTHON_CMD" >/dev/null 2>&1; then
    if $PYTHON_CMD "$ASHA_DIR/tools/memory_index.py" check >/dev/null 2>&1; then
        success "Vector DB ready"
        VECTORDB_READY=true
    else
        info "Vector DB dependencies not satisfied"
    fi
fi

# =============================================================================
# Installation Complete
# =============================================================================

echo ""
echo -e "${BOLD}"
echo "╔═══════════════════════════════════════════════════════════════╗"
echo "║                  Installation Complete                        ║"
echo "╚═══════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

echo ""
echo -e "${BOLD}System Status:${NC}"
echo ""
echo -e "  Memory Bank     ${GREEN}✓ Ready${NC}  (Memory/*.md)"
echo -e "  ReasoningBank   ${GREEN}✓ Ready${NC}  (Memory/reasoning_bank/)"

if [[ "$VECTORDB_READY" == true ]]; then
    echo -e "  Vector DB       ${GREEN}✓ Ready${NC}  (needs indexing)"
elif [[ "$DEPS_INSTALLED" == true ]]; then
    echo -e "  Vector DB       ${YELLOW}⚠ Partial${NC} (Ollama not running)"
else
    echo -e "  Vector DB       ${YELLOW}⚠ Unavailable${NC} (missing dependencies)"
fi

echo ""
echo -e "${BOLD}Getting Started:${NC}"
echo ""
echo "  Asha is ready! Start a Claude Code session and it will automatically"
echo "  read CLAUDE.md for context. The Memory Bank files need your input:"
echo ""
echo -e "  ${BOLD}1. Define your project${NC} (required)"
echo "     → Edit Memory/projectbrief.md with your project scope"
echo "     → Edit Memory/activeContext.md with current status"
echo ""
echo -e "  ${BOLD}2. Configure voice${NC} (optional)"
echo "     → Edit Memory/communicationStyle.md for persona/tone"
echo ""

if [[ "$VECTORDB_READY" == true ]]; then
    echo -e "  ${BOLD}3. Enable semantic search${NC}"
    echo "     → Run /index to index your project files"
    echo ""
fi

echo -e "${BOLD}Key Commands:${NC}"
echo ""
echo "  /save     Save session context, update Memory, commit changes"
echo "  /index    Index files for semantic search"
echo ""
echo -e "${BOLD}Session Workflow:${NC}"
echo ""
echo "  1. Claude reads CLAUDE.md and Memory/activeContext.md automatically"
echo "  2. Work on your project - operations are logged via hooks"
echo "  3. Run /save before ending to persist learnings"
echo ""
echo -e "For full documentation: ${BLUE}@asha/CORE.md${NC}"
echo ""
