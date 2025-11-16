---
description: "Toggle silence mode to disable Memory logging"
argument-hint: "Optional: 'on' or 'off' to set explicitly"
allowed-tools: ["Bash"]
---

# Silence Mode Toggle

Controls the silence marker (`Memory/markers/silence`) that disables all Memory logging and session capture.

Additional context: $ARGUMENTS

## Behavior

**When silence mode is ENABLED**:
- Session watching disabled (no operations logged to current-session.md)
- All Memory Bank hooks skip execution
- Marker file automatically removed at session-end

**When silence mode is DISABLED**:
- Normal session capture resumes
- Operations logged to Memory/sessions/current-session.md
- Memory Bank hooks execute normally

## Usage

**Toggle current state** (if on → off, if off → on):
```bash
if [[ -f "Memory/markers/silence" ]]; then
    rm Memory/markers/silence
    echo "🔊 Silence mode DISABLED - Memory logging active"
else
    mkdir -p Memory/markers
    touch Memory/markers/silence
    echo "🔇 Silence mode ENABLED - Memory logging disabled"
fi
```

**Explicit enable** (if argument is "on"):
```bash
mkdir -p Memory/markers
touch Memory/markers/silence
echo "🔇 Silence mode ENABLED - Memory logging disabled"
```

**Explicit disable** (if argument is "off"):
```bash
rm -f Memory/markers/silence
echo "🔊 Silence mode DISABLED - Memory logging active"
```

**Check current status**:
```bash
if [[ -f "Memory/markers/silence" ]]; then
    echo "Current status: 🔇 ENABLED (Memory logging disabled)"
else
    echo "Current status: 🔊 DISABLED (Memory logging active)"
fi
```

## Implementation

Determine action based on $ARGUMENTS:

- **No arguments or "toggle"**: Toggle current state
- **"on" or "enable"**: Explicitly enable silence mode
- **"off" or "disable"**: Explicitly disable silence mode
- **"status"**: Show current state only

Execute appropriate bash commands above based on the argument.

## Notes

- Silence marker automatically removed at session-end (hook cleanup)
- Use for experimental sessions, debugging, or when Memory logging unwanted
- Related marker: `Memory/markers/rp-active` (RP mode, disables session watching only)
