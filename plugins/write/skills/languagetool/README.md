# LanguageTool Local Skill

Grammar and style checking using your local LanguageTool server.

## Server Info
- **URL**: http://localhost:8081
- **Process**: java -jar languagetool-server.jar (PID: 8834)
- **Status**: ✓ Running

## Quick Usage

### From Claude Code
```
"Use the languagetool skill to check this text for grammar issues"
```

### Command Line
```bash
# Check text
python scripts/check_text.py "Your text here"

# Check file
python scripts/check_file.py document.txt

# Specify language
python scripts/check_text.py "Votre texte ici" fr
```

### Python Integration
```python
import requests

def check_text(text):
    response = requests.post('http://localhost:8081/v2/check', data={
        'text': text,
        'language': 'en-US'
    })
    return response.json()

result = check_text("This is a example.")
print(f"Found {len(result['matches'])} issues")
```

## Files
- `SKILL.md` - Complete documentation
- `scripts/check_text.py` - CLI text checker
- `scripts/check_file.py` - CLI file checker

## Dependencies
- Python 3.x
- requests library: `pip install requests`
- Local LanguageTool server running on port 8081

## See Also
Full documentation in `SKILL.md`
