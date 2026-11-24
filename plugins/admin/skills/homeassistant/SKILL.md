---
name: admin-homeassistant
description: Query and control a self-hosted Home Assistant through its REST API. Use when the Keeper asks what is on, what state a light, speaker, TV, thermostat, sensor or room is in, what changed recently, why a device is unavailable, or to turn something on or off, set a scene, or play or pause media. Reads run freely; anything that changes a device needs the Keeper's word. Requires HOMEASSISTANT_URL and HOMEASSISTANT_TOKEN from the Asha secrets wrapper.
metadata:
  triggers: "what lights are on | is the X on / what is the thermostat set to | what's playing in the Y | what changed in the house in the last hour | which devices are offline or unavailable | turn on/off X, set scene Y, pause the Z | list rooms, areas, devices or integrations in Home Assistant"
---

# Home Assistant via REST API

One Home Assistant instance at `$HOMEASSISTANT_URL`. Reads are safe and cheap. A service call
moves something physical in someone's house, so it is never a side effect of answering a
question.

## Setup

The `asha <harness>` dispatcher exports these from `~/.asha/secrets.env`:

- `HOMEASSISTANT_URL`: base URL with no `/api` and no trailing slash (e.g. `https://home.example.com`)
- `HOMEASSISTANT_TOKEN`: a long-lived access token (user profile, Security tab)

For a service-backed request, first run this local presence check, even when
network access is forbidden. Unset or empty means missing. If local commands
are forbidden or unavailable, report presence as unverified.

```sh
homeassistant_url_status=${HOMEASSISTANT_URL:+present}
homeassistant_token_status=${HOMEASSISTANT_TOKEN:+present}
printf 'HOMEASSISTANT_URL: %s\n' "${homeassistant_url_status:-missing}"
printf 'HOMEASSISTANT_TOKEN: %s\n' "${homeassistant_token_status:-missing}"
```

Report the observed names/statuses. If either is missing, halt the operation and
tell the user to set it in `~/.asha/secrets.env` and relaunch via `asha <harness>`.
Do not read or source secrets files during this diagnostic. If network access is
forbidden, stop after the check even when configured and state that no Home
Assistant request was made. Presence does not verify API access or device state.

**Blocked reply:** Include the observed variable names/statuses, the setup and
relaunch guidance above for every missing variable, and whether a service request
was made in the final response. These requirements are cumulative when both
configuration and network access are unavailable. Setup guidance describes a
future authorized session; it authorizes neither credential recovery nor a
service request. Apply the current task's permissions to any action.

Do not read `~/.asha/secrets.env` with `cat` or Read, never print the token, and
never ask for it in chat. A session started before the keys were added does not
have them: relaunch, or load them into a single subprocess without echoing. The
loading alternative is outside the diagnostic and must not run when credential
recovery is forbidden:

```bash
bash -s <<'EOF'
set -a; . ~/.asha/secrets.env; set +a
[ -n "${HOMEASSISTANT_TOKEN:-}" ] && echo "token loaded (len ${#HOMEASSISTANT_TOKEN})"
EOF
```

Every recipe below assumes this preamble:

```bash
U="${HOMEASSISTANT_URL%/}"; H="Authorization: Bearer $HOMEASSISTANT_TOKEN"
```

The token carries its user's full rights, usually administrator. Send it only to
`$HOMEASSISTANT_URL`.

## Rules

- **Read first.** Resolve a spoken name ("the office lamp") to entity ids from live state before
  answering or acting. Never guess an entity id.
- **Control needs the Keeper's word**, naming the thing and the action. "What lights are on?" is
  not permission to turn any off. If the name matches several entities, list them and ask once.
- **One target per call**, or one named area or scene. No `entity_id: all`, and no loops over a
  whole domain unless the Keeper asked for exactly that.
- **Ask again every time** for locks, alarm panels, garage doors and covers, climate and
  water-heater setpoints, valves, and anything under `homeassistant.*` (restart, stop,
  reload). An earlier yes does not carry over.
- **Verify after acting.** Read the entity back and report the state seen, not the HTTP 200.
  Cloud-polled devices can take several seconds; wait and re-read before calling it failed.
- **Configuration is out of scope**: adding or removing integrations, editing automations,
  users, tokens, or the container. Do those in the UI or through the host's own runbook.
- Entity names, areas and presence are private household detail. Keep them out of published
  notes and out of requests to any other service.

## Recipe: is it up

```bash
curl -sS -m 10 -w ' http=%{http_code}\n' "$U/api/" -H "$H"
curl -sS -m 10 "$U/api/config" -H "$H" | jq '{version, location_name, time_zone, state}'
```

## Recipe: what is there

Entity count per domain, then the integrations and whether each loaded:

```bash
curl -sS -m 15 "$U/api/states" -H "$H" \
  | jq -r '[.[].entity_id|split(".")[0]]|group_by(.)|map("\(.[0])\t\(length)")|.[]'
curl -sS -m 15 "$U/api/config/config_entries/entry" -H "$H" \
  | jq -r '.[] | "\(.domain)\t\(.title)\t\(.state)"'
```

`/api/states` returns everything in one call (hundreds of entities). Always filter with `jq`;
never dump it raw into context.

## Recipe: find entities by name or domain

```bash
Q='office'      # case-insensitive, matched against entity id and friendly name
curl -sS -m 15 "$U/api/states" -H "$H" | jq -r --arg q "$Q" '
  .[] | select((.entity_id + " " + (.attributes.friendly_name // "")) | ascii_downcase | contains($q|ascii_downcase))
      | "\(.entity_id)\t\(.state)\t\(.attributes.friendly_name // "")"'
```

Lights that are on, with brightness (0-255):

```bash
curl -sS -m 15 "$U/api/states" -H "$H" | jq -r '
  .[] | select(.entity_id|startswith("light.")) | select(.state=="on")
      | "\(.entity_id)\t\(.attributes.friendly_name)\t\(.attributes.brightness)"'
```

Swap the prefix for `media_player.`, `climate.`, `switch.`, `sensor.`. Entities that have
dropped off: `select(.state=="unavailable")`.

## Recipe: one entity in full

```bash
curl -sS -m 10 "$U/api/states/light.example" -H "$H" | jq '{entity_id, state, attributes, last_changed}'
```

A 404 means the entity id does not exist; go back and search.

## Recipe: rooms (areas)

The REST API has no area endpoint; the template endpoint renders Jinja against live state and
changes nothing:

```bash
tpl(){ curl -sS -m 15 -X POST "$U/api/template" -H "$H" -H 'Content-Type: application/json' \
         -d "$(jq -n --arg t "$1" '{template:$t}')"; echo; }
tpl '{% for a in areas() %}{{ a }}	{{ area_name(a) }}	{{ area_entities(a) | count }}
{% endfor %}'
tpl '{{ area_entities("kitchen") | join("\n") }}'
```

Other useful functions: `area_devices`, `device_entities`, `device_attr(id, "name")`,
`integration_entities("hue")`, `states.light | selectattr("state","eq","on") | list | count`.

## Recipe: what happened

Timestamps are ISO 8601; build them in UTC.

```bash
S=$(date -u -d '-2 hours' +%Y-%m-%dT%H:%M:%SZ)
curl -sS -m 30 "$U/api/logbook/$S" -H "$H" \
  | jq -r '.[] | "\(.when)\t\(.name)\t\(.state // .message)"' | tail -n 40
# one entity's history; minimal_response and no_attributes keep it small
curl -sS -m 30 "$U/api/history/period/$S?filter_entity_id=sensor.example&minimal_response&no_attributes" -H "$H" \
  | jq -r '.[0][] | "\(.last_changed)\t\(.state)"'
```

Add `&end_time=<ISO>` to bound a window; `?entity=<id>` narrows the logbook. Without
`filter_entity_id` the history call returns every entity: do not.

## Recipe: why is it broken

```bash
curl -sS -m 15 "$U/api/error_log" -H "$H" | tail -n 40 | cut -c1-240
```

Then the integration's `state` from the config-entries call (`setup_error`, `setup_retry`,
`not_loaded`). The log names client hosts and addresses; quote only the lines that matter.

## Recipe: control (only on the Keeper's word)

Find what a domain accepts before calling it:

```bash
curl -sS -m 15 "$U/api/services" -H "$H" \
  | jq '.[] | select(.domain=="light") | .services | map_values(.fields | keys)'
```

Call it, then read the state back:

```bash
E=light.example
curl -sS -m 15 -X POST "$U/api/services/light/turn_on" -H "$H" -H 'Content-Type: application/json' \
  -d "$(jq -n --arg e "$E" '{entity_id:$e, brightness_pct:40}')" | jq -r '.[]? | "\(.entity_id)\t\(.state)"'
sleep 2
curl -sS -m 10 "$U/api/states/$E" -H "$H" | jq -r '"\(.entity_id)\t\(.state)\t\(.attributes.brightness)"'
```

The response lists the entities whose state changed during the call; an empty list means
nothing changed yet, not that it failed. Common calls, all `POST /api/services/<domain>/<service>`:

| Want | Domain / service | Body besides `entity_id` |
|------|------------------|--------------------------|
| On, off, toggle | `light` or `switch` / `turn_on`, `turn_off`, `toggle` | lights: `brightness_pct`, `color_temp_kelvin`, `rgb_color` |
| Whole room | `light/turn_off` | `{"area_id": "kitchen"}` in place of `entity_id` |
| Scene | `scene/turn_on` | |
| Play, pause | `media_player` / `media_play`, `media_pause` | |
| Volume | `media_player/volume_set` | `volume_level` 0.0-1.0 |
| Setpoint | `climate/set_temperature` | `temperature`, or `target_temp_low` and `target_temp_high` |

A service that returns data (for example `weather.get_forecasts`) needs `?return_response`
on the URL, and fails with 400 without it.

## Errors

- **401**: token wrong, revoked, or pasted with quotes or whitespace. With IP banning enabled,
  repeated 401s ban the caller's address, so stop after one.
- **400 on a service call**: body does not match the service's fields; read the message, then
  the `/api/services` schema. Also returned when `return_response` is missing or unsupported.
- **400 before reaching the API, mentioning a reverse proxy**: a proxy sits in front and Home
  Assistant does not trust it. That is server configuration, not a client fault.
- **403**: an allowlist or proxy refused the caller (for example a LAN-only route reached from
  outside). Report it; do not look for another way in.
- **404**: entity or endpoint does not exist. `/api/` paths have no trailing-slash tolerance
  except the root.
- **Timeout**: check the root endpoint once, then report. Do not retry in a loop.

## Not covered

- Device and entity registry edits, dashboards, live event streams: WebSocket API only.
- Long-term statistics and energy data: WebSocket `recorder/statistics_during_period`.
- Anything on the host (container, upgrades, backups): outside this skill.

## See also

- REST API reference: <https://developers.home-assistant.io/docs/api/rest/>
- Template functions: <https://www.home-assistant.io/docs/configuration/templating/>
- Secrets pattern: `docs/secrets.md` in the asha repo
