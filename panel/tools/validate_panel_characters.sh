#!/bin/bash
# Validate Panel character files against template standards
# Usage: ./validate_panel_characters.sh
# Run from panel plugin directory or provide CHAR_DIR override

set -euo pipefail

# Detect script directory and locate character files
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(dirname "$SCRIPT_DIR")"
CHAR_DIR="${CHAR_DIR:-$PLUGIN_DIR/docs/characters}"
EXIT_CODE=0

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Panel Character File Validation"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Character Directory: $CHAR_DIR"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

# Process each character file (exclude template and archived files)
for file in "$CHAR_DIR"/The*.md "$CHAR_DIR"/Asha.md; do
  [ -f "$file" ] || continue

  filename=$(basename "$file")
  echo "📄 Validating: $filename"

  # Check 1: Capability Requirements section exists
  if ! grep -q "^## Capability Requirements" "$file"; then
    echo "  ❌ ERROR: Missing '## Capability Requirements' section"
    EXIT_CODE=1
  else
    echo "  ✅ Capability Requirements section found"

    # Check 2: Section length (extract section, count lines)
    section_lines=$(sed -n '/^## Capability Requirements/,/^##/p' "$file" | sed '$d' | wc -l)

    if [ "$section_lines" -gt 30 ]; then
      echo "  ❌ ERROR: Capability Requirements section exceeds 30 lines (found $section_lines)"
      EXIT_CODE=1
    elif [ "$section_lines" -gt 25 ]; then
      echo "  ⚠️  WARNING: Capability Requirements section exceeds 25 lines (found $section_lines, acceptable if ≤30)"
    else
      echo "  ✅ Section length acceptable ($section_lines lines)"
    fi
  fi

  # Check 3: No prohibited WIREFRAME sections
  if grep -Eq "^## .*(EXPECTED OUTPUT|^## .*FLOW|^## .*WIREFRAME)" "$file"; then
    echo "  ❌ ERROR: Prohibited WIREFRAME sections detected (W-I-R-E-F-R-A-M-E structure)"
    grep -En "^## .*(EXPECTED OUTPUT|FLOW|WIREFRAME)" "$file" || true
    EXIT_CODE=1
  else
    echo "  ✅ No WIREFRAME structure detected"
  fi

  # Check 4: No static agent assignments (anti-pattern)
  if grep -Eq "Tier 1 Defaults|Tier 2.*Specialized|Automatic Deployment" "$file" | grep -v "template\|TEMPLATE\|example\|EXAMPLE" ; then
    echo "  ⚠️  WARNING: Possible static agent assignment language detected (should use dynamic assignment)"
    grep -n "Tier 1 Defaults\|Tier 2.*Specialized\|Automatic Deployment" "$file" | grep -v "template\|TEMPLATE\|example\|EXAMPLE" || true
  else
    echo "  ✅ No static assignment anti-patterns detected"
  fi

  # Check 5: Has required sections
  required_sections=("## Nature" "## Manifestation" "## Analytical Approach" "## Role in Panel Sessions" "## Voice Examples")
  missing_sections=()

  for section in "${required_sections[@]}"; do
    if ! grep -q "^$section" "$file"; then
      missing_sections+=("$section")
    fi
  done

  if [ ${#missing_sections[@]} -gt 0 ]; then
    echo "  ⚠️  WARNING: Missing recommended sections: ${missing_sections[*]}"
  else
    echo "  ✅ All recommended sections present"
  fi

  echo "  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo
done

# Summary
echo
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [ $EXIT_CODE -eq 0 ]; then
  echo "  ✅ VALIDATION PASSED - All character files compliant"
else
  echo "  ❌ VALIDATION FAILED - Fix errors above"
fi
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo

exit $EXIT_CODE
