# body-demo

Four small reviews about one made-up notes app, Acme Notes, one for each way a board body can be built. The board
around the body (header, footer, Send) is the same in all four; the body is the agent's choice for the task.

    python examples/body-demo/make_demo.py OUTDIR

This writes `OUTDIR/<name>/decisions.json` for each body, plus the page it points at for the last three. Create a
review from one with `mockreview create --decisions OUTDIR/<name>/decisions.json ...`. Every address in the content is
on `example.test`.

| Body | Review | Use it when |
| --- | --- | --- |
| rows | `rows`: pick a look for the app, three mocks per decision | The reviewer chooses between a few options and needs to see each one first. Native `decision` blocks; the board draws the rows, the tally and the mocks. |
| stage | `stage`: three layouts of one settings screen side by side | The options are whole screens to compare in context. One artifact with `display: "page"`; the page is a pannable canvas of frames with its own zoom, and a mark group under each frame. |
| page with section marks | `plan`: a launch plan in four parts | The thing is a document and the reviewer's answer is a verdict per part. An artifact with `display: "page"` and `markable: false`; the page keeps `{part: {status, note}}` in its own data. |
| canvas | `deck`: a six-slide product overview as one long page | The thing is presentational and should be read in order at full width. An artifact with `display: "canvas"`: no frame of its own, the page scrolls with the board, and the reviewer pins what to change. |

## Marks kept in the page

`pages/marks.js` is shared by `stage.html` and `plan.html` and is inlined into each page by `make_demo.py`. It stores
the marks through `window.mockreview.use('data')` under the key `marks`:

    {"layout-tabs": {"status": "change", "note": "Fewer rows in the first tab."}}

`status` is `approve`, `change` or `skip`. The agent receives the object in the submit under `page_state[<block id>]`.
Outside a board the marks stay in memory, so the page also works when opened on its own. Give every part a
`data-mr-part` as well, so a pin can name the same thing a mark does.

Checked by `tests/test_body_demo.py`: each review builds and validates, the marked pages carry one mark group per
part, and the canvas block has `display: "canvas"`.
