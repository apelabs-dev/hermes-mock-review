// The Mocks tab of Hermes Desktop: a native list of mock-review boards, a read-only viewer and delete. Commenting and
// approving stay in the browser ("Open in browser"). Plain ESM, no JSX; imports only the SDK, react and the jsx runtime.
// Data comes from dashboard/plugin_api.py through ctx.rest (GET /reviews, GET /reviews/<id>/view, POST
// /reviews/<id>/delete).
//
// The viewer shows the snapshot in <SandboxedFrame> as a `data:` URL: the frame cannot send the app's credentials, so a
// plugin_api route is not loadable from it (every /api/plugins/ route needs the session token). The SDK frame always
// carries `allow-scripts` (an empty sandbox falls back to it); the document is script-free anyway (mockreview/export.py
// rebuilds it through an allowlist) and carries a CSP meta with `default-src 'none'`. A snapshot whose data: URL is over
// the backend's limit (api.DESK_DATA_URL_LIMIT, 2,000,000 characters) is not truncated: the tab says so and offers
// "Open in browser". Measured in the packaged Desktop: a data: URL of 2,096,500 characters renders, 2,097,100 kills the
// renderer.
import {
  Badge,
  Button,
  Codicon,
  ConfirmDialog,
  EmptyState,
  ErrorState,
  ROUTES_AREA,
  SIDEBAR_NAV_AREA,
  SandboxedFrame,
  ScrollArea,
  SearchField,
  Skeleton,
  host,
  relativeTime,
  useMutation,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const PATH = '/mocks'
const KEY = 'mock-review'
const STATUS_TONE = { open: 'success', approved: 'default', closed: 'muted' }

let api = null // the PluginContext, set by register()

function errorText(error) {
  const text = error && (error.message || String(error))
  return text || 'Something went wrong.'
}

function when(iso) {
  const ms = iso ? Date.parse(iso) : NaN
  return Number.isNaN(ms) ? 'no activity' : relativeTime(ms)
}

function openInBrowser(url) {
  if (!url) {
    host.notify({ kind: 'error', message: 'This review has no board address to open.' })
    return
  }
  api.os.openExternal(url).then(
    ok => {
      if (!ok) host.notify({ kind: 'error', message: 'Could not open the browser.' })
    },
    error => host.notifyError(error, 'Could not open the browser.')
  )
}

function StatusBadge({ status }) {
  return jsx(Badge, { variant: STATUS_TONE[status] || 'outline', children: status || 'unknown' })
}

function Row({ review, onOpen, onDelete }) {
  return jsxs('div', {
    className:
      'group flex cursor-pointer items-center gap-3 border-b border-(--ui-stroke-secondary) px-4 py-2.5 hover:bg-(--chrome-action-hover)',
    'data-mocks-row': review.review_id,
    role: 'button',
    tabIndex: 0,
    onClick: () => onOpen(review.review_id),
    onKeyDown: event => {
      if (event.key === 'Enter') onOpen(review.review_id)
    },
    children: [
      jsxs('div', {
        className: 'min-w-0 flex-1',
        children: [
          jsx('div', { className: 'truncate text-sm font-medium', children: review.title || review.review_id }),
          jsx('div', {
            className: 'truncate text-[0.6875rem] text-(--ui-text-tertiary)',
            children: `Round ${review.round} · ${when(review.last_activity)}`
          })
        ]
      }),
      jsx(StatusBadge, { status: review.status }),
      jsx(Button, {
        'aria-label': `Delete ${review.title || review.review_id}`,
        'data-mocks-delete': review.review_id,
        size: 'icon-xs',
        variant: 'ghost',
        onClick: event => {
          event.stopPropagation()
          onDelete(review)
        },
        children: jsx(Codicon, { name: 'trash' })
      })
    ]
  })
}

function ReviewList({ onOpen, onDelete }) {
  const [query, setQuery] = useState('')
  const list = useQuery({
    queryKey: [KEY, 'reviews'],
    queryFn: () => api.rest('/reviews'),
    refetchInterval: 30000
  })
  const needle = query.trim().toLowerCase()
  const rows = ((list.data && list.data.reviews) || []).filter(
    r => !needle || `${r.title} ${r.review_id} ${r.status}`.toLowerCase().includes(needle)
  )
  let body
  if (list.isError) {
    body = jsx(ErrorState, {
      title: 'Could not load reviews',
      description: errorText(list.error),
      children: jsx(Button, { size: 'sm', variant: 'outline', onClick: () => list.refetch(), children: 'Retry' })
    })
  } else if (list.isLoading) {
    body = jsx('div', {
      className: 'grid gap-2 p-4',
      children: [0, 1, 2].map(i => jsx(Skeleton, { className: 'h-10 w-full' }, i))
    })
  } else if (rows.length === 0) {
    body = jsx(EmptyState, {
      title: needle ? 'No reviews match' : 'No reviews yet',
      description: needle ? 'Try another search.' : 'Reviews Hermes creates for your mockups show up here.'
    })
  } else {
    body = rows.map(r => jsx(Row, { review: r, onOpen, onDelete }, r.review_id))
  }
  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-3 border-b border-(--ui-stroke-secondary) px-4 py-2',
        children: [
          jsx('div', { className: 'text-sm font-semibold', children: 'Mocks' }),
          jsx(SearchField, {
            'aria-label': 'Search reviews',
            containerClassName: 'ml-auto w-64',
            placeholder: 'Search reviews',
            value: query,
            onChange: setQuery
          })
        ]
      }),
      jsx(ScrollArea, { className: 'min-h-0 flex-1', children: body })
    ]
  })
}

function Viewer({ reviewId, onBack, onDelete }) {
  const view = useQuery({
    queryKey: [KEY, 'view', reviewId],
    queryFn: () => api.rest(`/reviews/${encodeURIComponent(reviewId)}/view`)
  })
  const data = view.data
  let body
  if (view.isError) {
    body = jsx(ErrorState, { title: 'Could not open this review', description: errorText(view.error) })
  } else if (!data) {
    body = jsx('div', { className: 'p-4', children: jsx(Skeleton, { className: 'h-40 w-full' }) })
  } else if (data.too_large) {
    body = jsx(EmptyState, {
      title: 'This round is too large to show here',
      description: 'Open it in the browser to see the whole round.'
    })
  } else {
    body = jsx(SandboxedFrame, {
      src: data.data_url,
      title: `${data.title} round ${data.round}`
    })
  }
  return jsxs('div', {
    className: 'flex h-full min-h-0 flex-col',
    'data-mocks-viewer': reviewId,
    children: [
      jsxs('div', {
        className: 'flex items-center gap-3 border-b border-(--ui-stroke-secondary) px-4 py-2',
        children: [
          jsxs(Button, {
            'data-mocks-back': true,
            size: 'sm',
            variant: 'ghost',
            onClick: onBack,
            children: [jsx(Codicon, { name: 'arrow-left' }), 'Back']
          }),
          jsx('div', { className: 'min-w-0 truncate text-sm font-semibold', children: data ? data.title : reviewId }),
          data && jsx(StatusBadge, { status: data.status }),
          data && jsx('div', { className: 'text-xs text-(--ui-text-tertiary)', children: `Round ${data.round}` }),
          jsxs('div', {
            className: 'ml-auto flex items-center gap-2',
            children: [
              jsxs(Button, {
                'data-mocks-open': true,
                disabled: !data,
                size: 'sm',
                variant: 'outline',
                onClick: () => openInBrowser(data && data.board_url),
                children: [jsx(Codicon, { name: 'link-external' }), 'Open in browser']
              }),
              jsxs(Button, {
                'data-mocks-delete': reviewId,
                disabled: !data,
                size: 'sm',
                variant: 'outline',
                onClick: () => onDelete({ review_id: reviewId, title: data.title }),
                children: [jsx(Codicon, { name: 'trash' }), 'Delete']
              })
            ]
          })
        ]
      }),
      jsx('div', { className: 'min-h-0 flex-1', children: body })
    ]
  })
}

function MocksPage() {
  const client = useQueryClient()
  const [selected, setSelected] = useState(null)
  const [pending, setPending] = useState(null)
  const remove = useMutation({
    mutationFn: review => api.rest(`/reviews/${encodeURIComponent(review.review_id)}/delete`, { method: 'POST' })
  })
  const confirmDelete = async () => {
    const review = pending
    await remove.mutateAsync(review) // a throw keeps the dialog open and shows the error inline
    if (selected === review.review_id) setSelected(null)
    client.removeQueries({ queryKey: [KEY, 'view', review.review_id] })
    await client.invalidateQueries({ queryKey: [KEY, 'reviews'] })
    host.notify({ kind: 'info', message: `Deleted “${review.title || review.review_id}”.` })
  }
  return jsxs('div', {
    className: 'h-full min-h-0',
    children: [
      selected
        ? jsx(Viewer, { reviewId: selected, onBack: () => setSelected(null), onDelete: setPending })
        : jsx(ReviewList, { onOpen: setSelected, onDelete: setPending }),
      jsx(ConfirmDialog, {
        open: pending !== null,
        destructive: true,
        title: pending ? `Delete “${pending.title || pending.review_id}”?` : 'Delete review?',
        description:
          'This deletes the review, its rounds and all of its comments, and closes its share link. It cannot be undone.',
        confirmLabel: 'Delete',
        onClose: () => setPending(null),
        onConfirm: confirmDelete
      })
    ]
  })
}

export default {
  id: 'mock-review',
  name: 'Mock review',
  register(ctx) {
    api = ctx
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: PATH }, render: () => jsx(MocksPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: PATH, label: 'Mocks', codicon: 'layout' } }
    ])
  }
}
