# Public hosting live checks

**Status: NOT YET RUN.** This is the release checklist for a real public route. The code path is covered by unit and
integration tests with a mocked Tailscale CLI, but a five-minute stream through an actual Funnel mapping and the
identity headers of a real public request have **not** been verified on this install. Run every step on the release
node, attach the logs, and only then call public hosting verified.

Who runs it: the release owner (REL). Use example values below only as placeholders; replace `<node>` with the node's
MagicDNS name.

Hard rules, at every step:

- Never run `tailscale funnel reset`.
- Never touch `:443`. The board goes on `:8443` unless the release explicitly says otherwise.
- Only ever make and remove the mapping with the two commands in this document (`public on --port 8443`, `public off
  --port 8443`), never `tailscale funnel` by hand.
- The live board on `:8790` and its store are not the test target. Keep the Hermes home that has the release's plugin
  installed (a scratch `HERMES_HOME` has no `mockreview` command), point `MOCKREVIEW_HOME` at a scratch store and
  `MOCKREVIEW_PORT` at `8791`, and stop every process you start.

## 1. Scratch board and baseline

Save the baseline outside the repository (for example under `/tmp`), before any mutation:

    export MOCKREVIEW_HOME="$(mktemp -d /tmp/mockreview-rel-store.XXXXXX)"
    export MOCKREVIEW_PORT=8791
    hermes mockreview owner          # own_store is the scratch path above, and the port is 8791
    hermes mockreview serve --host 127.0.0.1 --port 8791 &
    tailscale funnel status --json > /tmp/mockreview-funnel-baseline.json

Record the scratch server's PID so you can kill exactly that process later. Confirm the board answers on
`http://127.0.0.1:8791/healthz`.

## 2. Make the port public

Run and capture the result:

    hermes mockreview public on --port 8443 > /tmp/mockreview-public-on.json

Check the JSON: `commands` contains exactly one mutation, `tailscale funnel --bg --yes --https=8443
http://127.0.0.1:8791`, and no `reset`, no `off`, no `--https=443`.

## 3. Public request handling from off the tailnet

From a device **off the tailnet** (a phone on cellular, with Wi-Fi and Tailscale off):

- `https://<node>.ts.net:8443/` → `401` and the sign-in page, with **no** "Your proxy says you are" line.
- `https://<node>.ts.net:8443/healthz` → `{"ok": true}` exactly.
- `https://<node>.ts.net:8443/api/owner` → `404`.

## 4. Ingress provenance

A tailnet device cannot prove this step: the node name resolves to the tailnet address there, so its requests can go
through private Serve instead of public Funnel. Use the off-tailnet device and a target that prints every request
header.

Stop the scratch board by its recorded PID, run a header-echo HTTP server on `127.0.0.1:8791` (it must print every
request header), and open `https://<node>.ts.net:8443/` from the off-tailnet device. The echoed request must show:

- `Tailscale-Funnel-Request: ?1`; and
- **no** `Tailscale-User-Login`, `Tailscale-User-Name` or `Tailscale-User-Profile-Pic` header.

This is the only proof that the request used public Funnel ingress and carried no Tailscale identity. Save the echoed
headers under `/tmp`, outside the repository. Kill the echo server by its recorded PID afterwards; the port stays free
for step 5.

## 5. Five-minute stream

On the release node, against the same `127.0.0.1:8791` the Funnel mapping points at:

1. Stop the scratch board by its recorded PID and confirm `127.0.0.1:8791` is free (`lsof -nP -iTCP:8791` prints
   nothing).
2. From the repository checkout, start the SSE producer and keep its log:

        python3 scripts/sse_probe.py serve --port 8791 --heartbeat 15 > /tmp/mockreview-sse-producer.log 2>&1 &

   Record `$!` as the producer PID.
3. Confirm locally that it answers:

        curl -sS http://127.0.0.1:8791/healthz
        curl -sS -m 3 -D- -o /dev/null http://127.0.0.1:8791/events

   The first prints `{"ok": true}`; the second prints `200` with `Content-Type: text/event-stream`.
4. From the **off-tailnet device**, in a checkout of this repository, run the five-minute client:

        python3 scripts/sse_probe.py client --url https://<node>.ts.net:8443/events --seconds 300 --heartbeat 15

   Require `RESULT ok=true` and `seconds >= 300` in the final line. Save the whole log outside the repository. A
   `RESULT ok=false` or an early close fails the release.

The probe server answers `/events` with the same `mockreview.sse` framing the board's stream uses; the board itself has
no `/events` route, which is why this step starts the producer. The board is restarted only after step 6.

**Local dry run first (no Funnel).** With no mapping, run the same `serve` command, then
`python3 scripts/sse_probe.py client --url http://127.0.0.1:8791/events --seconds 20`, require `RESULT ok=true`, kill
the producer by its recorded PID, and show the port closed (`lsof -nP -iTCP:8791` prints nothing). This is the part
actually proven against a scratch server this round.

## 6. Restore and verify nothing else moved

    hermes mockreview public off --port 8443
    tailscale funnel status --json > /tmp/mockreview-funnel-after.json
    diff /tmp/mockreview-funnel-baseline.json /tmp/mockreview-funnel-after.json

The diff must be empty. Check the snapshot by hand too: the `:443` Web entry and its `TCP["443"]` entry are still
present and byte-identical to the baseline, and the app already served on `:443` still answers from the tailnet.

Then clean up: stop the SSE producer and the scratch server, each by its recorded PID, delete the scratch store
(`MOCKREVIEW_HOME`), and remove the scratch header-echo process.

## 7. Release record

Attach to the release record, outside the repository:

- `/tmp/mockreview-funnel-baseline.json` and `/tmp/mockreview-funnel-after.json` (and the empty `diff`);
- `/tmp/mockreview-public-on.json`, showing the captured `commands`;
- the echoed headers from step 4;
- the SSE producer log (`/tmp/mockreview-sse-producer.log`) and the SSE probe log from step 5.

Any failure blocks the release. Do not "fix" it with `tailscale funnel reset`; remove only this board's route with
`hermes mockreview public off --port 8443` and report the failure.
