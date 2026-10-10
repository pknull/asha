# API families and evidence

UniFi Network Integration API, legacy Network API and Site Manager API are
not interchangeable. A key for one deployment/family need not authenticate to
another. Consult the installed Network application's version-specific API docs.

Official references (checked 2026-10-10):
- https://developer.ui.com/network/v9.4.17
- https://developer.ui.com/network/v10.6.106/getconnectedclientdetails
- https://help.ui.com/hc/en-us/articles/28457353760919-UniFi-Local-Management

The official Integration family uses `/proxy/network/integration/v1` locally,
site IDs and endpoint-specific pagination. The cloud connector has a different
base. This collection does not automatically contact the cloud or enumerate
other sites. For Integration requests, use the installed version's contract,
handle bounded pagination and expose incomplete coverage. Do not reuse the
legacy helper's response parser for that API.

Bundled legacy adapter, only when documented as working for the intended site:

| Resource | GET suffix | Interpretation |
|---|---|---|
| clients | stat/sta | Controller's current client-interface records |
| devices | stat/device | Adopted/network-device telemetry |
| health | stat/health | Controller subsystem health summary |
| remembered | rest/user | Historical records, not online-machine count |

Legacy support is based on observed deployment behavior, not a vendor guarantee
for every version. The helper requires `meta.rc == ok` and a list `data`.
It neither infers ownership nor claims completeness. Failure/empty/missing
fields may reflect permissions or API differences. Avoid broad configuration
exports (WLANs, accounts, settings, full port profiles) that may expose secrets.
Only fetch port-forward/topology details beyond the helper when specifically
needed, with a locally filtered read and an explicit field allowlist.
