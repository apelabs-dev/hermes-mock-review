# The Mocks tab in Hermes Desktop

Hermes Desktop shows a **Mocks** entry in its sidebar once the plugin is enabled (`plugins.enabled`). The tab lists your
reviews, shows a round read-only, and deletes. Commenting and approving stay in the browser: the tab never changes a
review except to delete it, and it adds nothing to the board's own HTTP surface (the board's CSP, `frame-ancestors`,
cookies and CSRF are untouched).

The tab is one unified package: `desktop/plugin.js` is the Desktop half, `dashboard/plugin_api.py` is the backend half
(a thin adapter over `mockreview/api.py`) and `dashboard/manifest.json` registers it.

Hermes mounts the backend half on the dashboard app. Desktop spawns a `hermes dashboard` backend rather than a gateway,
so the backend half runs in that process, or in the plugin host under `plugins.isolation: host`. A gateway that serves
the dashboard routes mounts it too, but it is not the usual host. The process is not always the one running the board
server: hooks that only the board server installs, such as the live-stream closer, are installed there only when the
board server runs in the same process (the gateway with `autostart` on, or `hermes mockreview serve`).

## The list

Every review is listed: open, approved and closed ones, and reviews created by 2.0.0 and older. The newest activity is
first, and the search box filters on title, id and status. A row shows the title, the status, the current round and the
last activity (the latest comment, submission or update). An unread count is not shown.

## The viewer

A click opens the review's current round, read-only, with a Back button. The header carries the title, the status, the
round, **Open in browser** and **Delete**.

The round is the same self-contained HTML as `hermes mockreview export`: `mockreview/export.py` rebuilds the markup
through an allowlist, so a `<script>`, an event handler or a remote URL in a block does not survive, and the document
carries a CSP meta tag with `default-src 'none'`.

### `data:` URL, not a route

The frame gets the document as a `data:text/html;base64,...` URL. A plugin_api route cannot be the frame's `src`: every
`/api/plugins/` route needs the app's session token, which a frame cannot send. Measured in the packaged Desktop, a
`data:` URL of 2,096,500 characters renders and one of 2,097,100 characters kills the renderer, so the backend stops at
**2,000,000 characters** (`DESK_DATA_URL_LIMIT` in `mockreview/api.py`).

A round above that limit is never truncated. The tab says "This round is too large to show here" and **Open in
browser** shows the whole round. Large rounds come from inlined images (each image up to 2 MiB is inlined as base64,
about 1.33 times its size, and the HTML is then base64-encoded again).

### Sandbox flags

The frame is the SDK's `SandboxedFrame` with its default sandbox. The SDK always includes `allow-scripts` (an empty
sandbox falls back to it), so scripts cannot be switched off from a plugin. The snapshot is script-free by
construction (see above), the frame has an opaque origin (`null`) and `referrerPolicy` is `no-referrer`.

## Open in browser

The button opens the review's normal board URL through the SDK's open-external call (`ctx.os.openExternal`). The host is
the one every board link uses (`store.review_url` with `config.public_link_base()` and `config.local_link_base()`):

- a `local` review: this computer's own address, `lan_address` or the detected LAN address when the board is shared on
  the LAN, else `127.0.0.1`, on the board port;
- any other review: `public_url`, else the Tailscale Funnel URL, else `public_base`;
- if none of those is set, the loopback address `config.local_base()`.

Desktop can be a client on another computer than the gateway. A loopback address then points at the client, not the
gateway: configure a tailnet or public host (see [hosting](hosting.md)) for such a client.

## Delete

**Delete** is on every row and in the viewer header. It opens a confirmation that names the review's title and says it
cannot be undone. An open review is closed first, then deleted. Closing ends its share link at once. Live guest
connections end at once only when the board server runs in the same process as the caller. Otherwise they end at their
own access recheck, within about 5 seconds.

The same operation is `api.delete_review(review_id)`, behind `hermes mockreview delete <id> --yes` (without `--yes` it
refuses) and the `prune` step of housekeeping. It removes the review's directory and every database row keyed by the
review: threads (with their comments and mentions), reads, events, guests (with their sessions), revoked sessions,
share links, invites and grants. The audit log keeps one `review.delete` entry. Other reviews are not touched.
