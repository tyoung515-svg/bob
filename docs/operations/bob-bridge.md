# Loopback bridge

The bridge at `bobclaw-core/tools/bob_bridge.py` adapts a small text model API to
core's `POST /api/chat`. It is separate from `portal-broker/portal-broker.py`,
which transports desktop operations through the authenticated gateway.

The runtime provisions one bridge per lane. Standalone source setup does not
install this service. The code defaults to core `http://127.0.0.1:7835` and bridge
port 8902; `BOB_BRIDGE_ENGINE` and `BOB_BRIDGE_PORT` select the actual lane.
The listener is fixed to `127.0.0.1`. There is no bridge authentication: do not
publish it, proxy it to other devices or treat it as the gateway's scoped API.
For remote desktop use, follow [AGENTS-SETUP.md](../../AGENTS-SETUP.md).

## Implemented endpoints

| Endpoint | Behavior |
| --- | --- |
| `GET /health` | Bridge process health; not proof that a model or memory backend is ready. |
| `GET /v1/models`, `POST /v1/models` | Advertises faces classified as workers. |
| `POST /v1/chat/completions` | Chat Completions-shaped text input/output, including streaming. |
| `POST /v1/responses` | A small Responses-shaped text compatibility surface, including streaming. |

The model `bob` selects `assistant`; `bob-<face-id>` selects that face.
Only registry faces with role exactly `worker` are accepted. Unknown,
planner/critic and unclassified faces return 503. The bridge pins the selected
face with `pin_authoritative: true`; that pin selects routing, not authority.

This is not a full provider implementation. It flattens text; image content is
not forwarded as vision, and it does not implement an external client's tool
execution loop. Check the client's protocol requirements against this code
before configuring it. No particular external CLI version is guaranteed here.

## Conversations and memory

The bridge flattens supplied system/developer text and the user/assistant/tool
transcript into one task. A stable UUID conversation identity comes from
`session-id`, body/metadata session keys, or a fallback hash of initial messages.
Core's direct chat endpoint uses supplied history rather than retrieving the
gateway's message history, so reusing an ID alone does not replay a conversation.

`BOB_BRIDGE_MAX_HISTORY` defaults to 30 input messages; longer requests are
rejected. Start a fresh client session rather than assuming an unlimited replay
window. Core's configured memory hooks still apply to these turns, but the bridge
does not prove that an event was projected or recalled. Inspect the lane's actual
memory configuration and completion state separately; see
[the memory checks](../MEMORY_LIVE_SMOKE_RUNBOOK.md).

Non-`bob` models fail with 501 unless `BOB_BRIDGE_UPSTREAM` is configured.
That option forwards to the chosen upstream and can forward the client's
Authorization header. Leave it unset unless this is intentional.

## Runtime checks

Inside the runtime, select the lane's actual bridge port:

```bash
curl --fail --silent http://127.0.0.1:8902/health
curl --fail --silent http://127.0.0.1:8902/v1/models
ss -ltn '( sport = :8902 )'
```

Expect a loopback listener and inspect the returned model list. These checks do
not invoke a model. A completions request can call an enrolled provider and write
normal memory records, so use harmless content in a test lane for a live check.

The packaged unit is `bob-bridge@<lane>.service`; it is provisioned with the
lane's generated identity and environment. Do not replace it with a generic
user unit or guess a different lane's core port. For a manually configured source
service, explicitly set its engine URL and Python environment and keep it private.
