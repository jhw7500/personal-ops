# Chat host-access approval

Approved scope: replace the admin-page detour with an explicit confirmation card
inside ChatGPT. Keep the existing admin approval path and expiring host-user
grant semantics. Source changes and tests are authorized; runtime activation is
a separate step. Do not run host commands, models, Docker, or DB operations.

1. Add regression tests for a private, expiring approval capability. Opening a
   card creates a request only. Confirmation consumes the capability before
   calling the existing one-use grant issuer. Option changes rotate the
   capability without extending the card lifetime. Cancellation leaves any
   current grant intact. Bind to project root, owner UID, runtime, and boot.
2. Expose one model-only opener and app-only update/confirm/cancel callbacks.
   Put the capability only in result `_meta`, never text, structuredContent,
   widget state, model context, URLs, or logs. Prevalidate private arguments
   before SDK schema errors can reflect their values.
3. Add a self-contained MCP App resource with standard and legacy ChatGPT
   bridges. Show the actual host user, installation-wide scope, cwd context,
   duration, and Codex setting. Require a real button action; never auto-grant
   on initialization, option edits, or result notifications.
4. Verify backend concurrency, replay, expiry, stale bindings, strict schemas,
   resource packaging, real MCP transport, and browser behavior with a local
   host fixture. Review security boundaries, then integrate only owned changes
   after comparing the original checkout against the saved baseline.
   Pin the browser fixture in rollout control inventory and run it as a required
   preflight stage using `/usr/bin/python3` and its installed Playwright Chromium.
   Keep failure names/counts in the existing private rollout receipt. Cancellation
   must remove the grant issuer's pending request under its lock before recording
   a terminal card; already-consumed requests must not report cancellation success.
5. Record tested source state and remaining deployment/client verification.
   Real ChatGPT web/Android rendering cannot be claimed from the local fixture.

Trust boundary: app-only visibility is routing, not authentication. The opaque
capability is held by the trusted MCP host/component, hidden from the model.
Anyone who controls the authenticated MCP installation or that host is inside
this boundary. Widget session IDs and model assertions are not owner identity.
The grant applies to callers of this MCP installation; selecting a project does
not create filesystem isolation. No admin credential is supplied to the card.
