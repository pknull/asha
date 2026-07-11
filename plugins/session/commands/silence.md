---
name: session-silence
description: "Toggle silence mode to disable Memory logging"
argument-hint: "Optional: 'on' or 'off' to set explicitly"
allowed-tools: ["Bash"]
---

# Silence Mode Toggle

Controls the silence marker (`Work/markers/silence`) that disables transcript synthesis and Memory persistence.

Additional context: $ARGUMENTS

## Behavior

**When silence mode is ENABLED**:
- Explicit and automatic synthesis are skipped
- Clean session exit does not launch automatic save
- Marker persists across sessions until explicitly disabled

**When silence mode is DISABLED**:
- Manual transcript synthesis resumes on every supported harness
- Claude clean-exit automatic save resumes

## Usage

**Toggle current state** (if on → off, if off → on):
```bash
if [[ -f "Work/markers/silence" ]]; then
    rm Work/markers/silence
    echo "🔊 Silence mode DISABLED - Memory logging active"
else
    mkdir -p Work/markers
    touch Work/markers/silence
    echo "🔇 Silence mode ENABLED - Memory logging disabled"
fi
```

**Explicit enable** (if argument is "on"):
```bash
mkdir -p Work/markers
touch Work/markers/silence
echo "🔇 Silence mode ENABLED - Memory logging disabled"
```

**Explicit disable** (if argument is "off"):
```bash
rm -f Work/markers/silence
echo "🔊 Silence mode DISABLED - Memory logging active"
```

**Check current status**:
```bash
if [[ -f "Work/markers/silence" ]]; then
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

- Silence marker persists until explicitly disabled (`/session:silence off`)
- Use for experimental sessions, debugging, or when Memory logging unwanted
- Related marker: `Work/markers/rp-active` (RP mode, disables session watching only, auto-removed at session-end)
