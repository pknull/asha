<system-reminder>
RP session active. Routing directive for this turn:
- Do not voice profiled NPCs yourself: the main loop's accumulated context drifts them off-sheet.
- For any beat where a profiled NPC acts or speaks, spawn `roleplay-gm` (Task) with TRIGGER=<the user's input> plus a complete inline SCENE_STATE (location / time / present / observable / character_state + recent register-stack) so no agent reads the full session file. roleplay-gm consults character agents only for the NPCs acting this beat.
- Validator off by default: run `continuity-reviewer MODE:live_roleplay` only on key beats (a reveal or a threshold) or when the Keeper tags [validate].
- Relay roleplay-gm's prose; drive 2-3 beats; pause only at genuine decisions.
- PC-only beats (sit/sleep/wait, pure PC-internal action, [meta]): handle directly, no spawn. Drift happens at NPC voicing; do not pay the spawn where no NPC acts.
</system-reminder>
