# Historical deployment regression fixture

This self-contained fixture preserves the 33 profile shapes, routing policy and
catalog values consumed by the pre-extraction golden tests. It is test data, not
a default for an installation or an example of enrolled services. Tests select
this manifest explicitly and resolve `faces/` relative to it.

The small generic example is `bobclaw-core/config/roster.example.yaml`. Production
uses an explicit `BOB_ROSTER_FILE`; with no selector the provider-free configured
lane policy remains active. Invalid explicit selectors fail closed. Tests and
these fixtures are excluded from the runtime image.
