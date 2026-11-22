# 0028. Every finite response has a byte bound

Date: 2026-11-07

## Status

Accepted.

## Context

A page bounds rows, not bytes. One row can hold a large document or job outcome.
A mounted application can answer outside the store contract of [0026](0026-every-store-read-is-a-page.md).
An edge can refuse that response after the application records success.

A route can legitimately need a larger response. Raising the default for every route hides that decision.
Customers also deploy behind different edges. A provider's limit does not belong in a route.

A response over its expected size is still a correct answer. Refusing it in production turns a cost into an outage.
Only a response the edge or the process cannot carry must be refused.

## Decision

`app/response_bound.py` holds each finite response before sending its status or body.
It counts the body bytes emitted by ASGI. Each response is held to two sizes: its limit and the deployment ceiling.
A response over its limit is sent. The guard writes `response over limit` once for each route in each process, with the limit, the bytes and the reason.
The guard records each route in `Limits.reported`, so one route that answers too much on every request writes one line.
Each `request completed` line carries `response_over_limit`, so an alert counts every such response, and sees one route grow.
A response past the ceiling is refused. Equality fits. Overflow sends the `ResponseTooLarge` refusal with status `500`.
The guard discards the held bytes and stops the producer by raising through its send call.
The producer must release its resources when its ASGI call exits.
Swallowing the overflow does not suppress the refusal.
After response start, finite buffering must complete within `REQUEST_BUDGET_SECONDS`, the existing request budget.
The guard does not impose a new deadline on handler or store work before response start.
A buffer timeout sends the named `ResponseTimedOut` refusal and logs its cause.
A producer that returns without completing its body fails visibly.
The refusal carries a fresh length and `Cache-Control: no-store`.
No part of the refused body leaves the guard.

`app/wiring.py` builds the deployment policy before serving requests.
`RESPONSE_MAX_BYTES` sets the default limit, initially 4 MiB.
`RESPONSE_CEILING_BYTES` sets the ceiling, initially 16 MiB. It is also the largest permitted finite exception.
Set it below the largest response the edge in front of the app carries.
Both settings are integer byte counts. The default must fit under the ceiling.
Both must permit the small refusal itself. Invalid settings stop application construction.

A route declares a different finite limit through `app/response_limits.py`:

```python
response_limits.allow(request, max_bytes=8 * 1024 * 1024, reason="The export includes attachments")
```

The exception applies to that request only. It can narrow or widen the limit that the request is reported over.
It cannot exceed the deployment ceiling. It must name a reason before response start.
The reason is a code constant, never text from a request.
A limit outside the ceiling fails visibly. The guard never silently clamps it.
Customers keep these declarations beside their own routes. The template owns the mechanism.

A route that delivers incrementally calls `response_limits.stream(request, reason=...)` before response start.
The declaration exempts a successful response from the total byte limit.
Error responses remain bounded. The guard removes the stream's content length so the server can frame it incrementally.
Missing length, chunked delivery and a streaming response class grant no exception.
The route owns event size, download size, resource cleanup and stream lifetime.
An actor's stream also calls `admission.stream` under [0023](0023-the-server-admits-each-actor-s-requests-within-a-bound.md).
A failure after stream delivery starts terminates delivery. It cannot replace the HTTP status.
This declaration does not make streaming safe without those route contracts.

`app/main.py` places the guard inside request events, admission, logging and telemetry.
They observe the final finite-response status. Nested guards share the outer deployment policy.
An inner guard emits one body chunk. An outer guard checks that chunk without copying it.
A transformation between guards cannot bypass the outer byte check.
Mounted applications remain subject to the host guard.
`app/serve.py` guards its files and API mount with the same shared policy.
Its bundle route declares a finite exception up to the deployment ceiling.
Both entrypoints therefore enforce the policy.
The default unhandled-error response uses the declared JSON error shape.
An outer custom error handler must also enforce the policy.
A foreign mounted application can record its own status before the host guard changes it.

`response too large` names the route template, limit, observed bytes and exception reason.
It carries the request identifier through the existing log context.
Observed bytes mean bytes seen before stopping, not the producer's hypothetical final size.
The guard does not log the body. The existing 5xx signal counts the refusal.
A route must still supply a useful bounded representation after this guard exposes its failure.

Each successful answer also has a largest size before the app runs.
`tests/models/test_response_bounds.py` reads the schema of each route's `2xx` answer, of each media type, in the OpenAPI document the app builds.
It works out the largest size that answer can take, and fails when that is over `DEFAULT_BYTES`.
A list needs `maxItems`, a string needs `maxLength`, a format or an `enum`, and a map needs `maxProperties` and a `maxLength` on its keys.
A value that is any JSON has no largest size.
The message names the route and the path in the answer that has no bound.
So a route that answers a whole document fails in the gate, not in production.
`UNBOUNDED_ANSWERS` in `tests/integration/filled.py` names each route that does not fit, with the reason.
The list debt holds it, so it only shrinks. The template's own entries are `GET /reads`, which the ceiling bounds part by part, and the two routes that answer a job, whose outcome is any JSON.

A string in an answer gets its bound from `at_most`, as the types `Id`, `Name` and `Title` do.
The schema carries the bound, and nothing checks it when the answer is built.
So a value stored before the bound still reads, and the guard reports an answer over its limit.
A request model checks its own bounds, so a write over a bound answers `422`.

`tests/responses/` checks finite limits, exceptions, mounted applications, assets, headers and stream declarations.
It checks complete pages separately from deliberate overflow.
The telemetry test checks the final span and metric status.
The normal gate runs these tests in a generated project.

## Considered options

- A response class alone misses mounted applications and raw ASGI producers.
- Counting while forwarding can discover overflow after success has reached the client.
- Trusting `Content-Length` misses producers that omit it or state it incorrectly.
- A path-based exception list separates a policy from its handler and can match the wrong method or mount.
- An unlimited finite exception removes the guarantee. A larger finite limit must fit the deployment ceiling.
- Status `413` describes excessive request content. Oversized server output is a server failure.
- Refuse each response over the default limit. A route that answers more than expected then fails in production, where it is a cost and not an error.
- Logging and continuing at any size leaves the edge in charge of the refusal. The ceiling keeps the refusal in the app.
- A log line for each response over its limit. One route on a busy path then floods the log.
- `max_length` on a field of an answer. A value stored before the bound then fails the read, and the gate's bound becomes a refusal in production.
- The OWASP ruleset of Spectral. It needs a bound on each field and adds no sizes, so a route whose fields each have a bound can still answer more than its limit.

## Consequences

Finite responses wait until their complete body fits. Chunked responses retain bytes up to the ceiling.
One-chunk responses pass through without a buffer copy. Combining chunks requires another copy.
Application serialization can allocate more before the guard sees any bytes.
The ceiling is not a process memory budget. Size instance memory and concurrency together.
A finite response is held up to the ceiling, not up to its limit, so each request in flight can hold that much.
Store reads, field sizes and job outcomes still need their own bounds.

The guard counts the encoding it receives. Compression outside the guard follows validation.
Compression inside a mounted application is counted as emitted bytes.
This policy does not claim a bound on decompressed content.
Finite responses use body messages. The guard advertises no optional protocol extensions.
File responses then send counted chunks. Other unsupported ASGI extensions fail visibly.
An extension that sends a file without body bytes requires a separate reviewed adapter.

A refusal does not roll back a committed mutation. Response background tasks do not run after overflow. Clients must not automatically retry such writes.
The guard counts ASGI body bytes. HEAD may advertise a larger representation length without sending a body.
If a producer still emits a body on HEAD, the guard counts it before the server discards it.
A response over its limit reaches the client. `request completed` marks it with `response_over_limit: true`. Alert on that count by route.
A response past the ceiling fails. Existing streams must declare incremental delivery. Use pagination, a smaller representation, or an explicit route limit.
Set the deployment ceiling below the actual edge limit, with headroom.
Cloud Run documents 32 MiB for HTTP/1 responses without streaming or chunked delivery.
See [deployment](../../deployment.md) for the provider boundary.
