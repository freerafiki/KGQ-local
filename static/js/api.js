// API base (index.html and node.html share this rule):
//  - served by the dev static server (28080) → talk to uvicorn directly on :28000
//  - served by the production web server (Apache, 80/443 or any other port)
//    → same-origin, i.e. the reverse proxy forwards /search, /node, /health
export const API_BASE = (location.port === '28080')
  ? location.protocol + '//' + location.hostname + ':28000'
  : '';

// Error bodies are JSON `{"detail": ...}` (app.py answers failed searches
// that way); anything else — a proxy's HTML error page, an empty body —
// falls back to the bare status line.
async function errorText(res) {
  const fallback = 'HTTP ' + res.status + ' ' + res.statusText;
  try {
    const body = await res.json();
    return body && body.detail ? 'HTTP ' + res.status + ': ' + body.detail : fallback;
  } catch (e) {
    return fallback;
  }
}

// POST /search. Rejects with an Error carrying the HTTP status (plus the
// server's detail when present), which is what the results panel shows
// ("Search failed: HTTP 500 ...").
export async function fetchSearch(payload) {
  const res = await fetch(API_BASE + '/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(await errorText(res));
  return res.json();
}

// GET /node/{eid}. Rejects with the message the node page shows verbatim:
// 'Node not found' on 404, the HTTP status (+ detail) otherwise.
export async function fetchNode(eid) {
  const res = await fetch(API_BASE + '/node/' + encodeURIComponent(eid));
  if (res.status === 404) throw new Error('Node not found');
  if (!res.ok) throw new Error(await errorText(res));
  return res.json();
}
