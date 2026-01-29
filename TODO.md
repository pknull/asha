# asha-marketplace TODO

Tracking enhancements and issues for marketplace plugins.

---

## write plugin

### ai-detector: Check API credits before scanning

**Status**: Open
**Created**: 2026-01-27
**Priority**: Medium

**Problem**: The ai-detector agent calls GPTZero without checking remaining API credits. Risk of exhausting tokens unexpectedly on large scan runs.

**Desired behavior**: Before running detection, check available credits and warn user if low. Abort if insufficient.

**Investigation needed**:
- [ ] Check if GPTZero API has a credits/usage endpoint (not documented publicly)
- [ ] Contact GPTZero support or check developer dashboard
- [ ] If no API endpoint exists, consider tracking usage locally (count calls, estimate tokens)

**Implementation options**:
1. API endpoint (ideal) — query balance, abort if below threshold
2. Local tracking (fallback) — maintain count in config, user sets limit
3. Warning only — estimate token cost before scan, confirm with user

**Files affected**: `plugins/write/agents/ai-detector.md`

---
