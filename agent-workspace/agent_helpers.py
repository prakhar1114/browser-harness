"""Agent-editable browser helpers.

Add task-specific browser primitives here. Core helpers from browser_harness.helpers
load this file when BH_AGENT_WORKSPACE points at this directory, or when this
repo's default agent-workspace exists.
"""

import json
import random
import re
import time


def human_pace(kind="short"):
    """Sleep a randomized human-ish duration.

    kind: "tick" (0.2-0.5), "short" (0.6-1.4), "medium" (1.6-3.2), "long" (3.5-6.5).
    """
    ranges = {
        "tick": (0.2, 0.5),
        "short": (0.6, 1.4),
        "medium": (1.6, 3.2),
        "long": (3.5, 6.5),
    }
    lo, hi = ranges.get(kind, ranges["short"])
    time.sleep(random.uniform(lo, hi))


def click_like_human(x, y, jitter=5, steps=3):
    """Move the mouse along a short curved path to (x, y) with jitter, then click.

    A bare Input.dispatchMouseEvent mousePressed with no prior mouseMoved is a
    classic CDP signature. This sends a few mouseMoved events first, applies a
    small ±jitter to the final coords, and inserts sub-100ms gaps between
    events to look like a real cursor.
    """
    from browser_harness.helpers import cdp, js

    # Random landing offset within ±jitter px so we don't always hit dead-center.
    tx = int(x + random.uniform(-jitter, jitter))
    ty = int(y + random.uniform(-jitter, jitter))

    # Start near the current viewport center-ish; we don't read the real cursor
    # position (CDP doesn't expose it), so start from a random nearby point.
    sx = tx + random.randint(-120, 120)
    sy = ty + random.randint(-120, 120)

    for i in range(1, steps + 1):
        t = i / steps
        # ease-out-ish + small perpendicular wobble
        ix = int(sx + (tx - sx) * t + random.uniform(-2, 2))
        iy = int(sy + (ty - sy) * t + random.uniform(-2, 2))
        cdp("Input.dispatchMouseEvent", type="mouseMoved", x=ix, y=iy)
        time.sleep(random.uniform(0.02, 0.08))

    cdp("Input.dispatchMouseEvent", type="mouseMoved", x=tx, y=ty)
    time.sleep(random.uniform(0.04, 0.12))
    cdp("Input.dispatchMouseEvent", type="mousePressed", x=tx, y=ty, button="left", clickCount=1)
    time.sleep(random.uniform(0.04, 0.12))
    cdp("Input.dispatchMouseEvent", type="mouseReleased", x=tx, y=ty, button="left", clickCount=1)


def human_scroll(total=300, steps=4):
    """Scroll down `total` px in a few uneven steps with short pauses."""
    from browser_harness.helpers import js

    remaining = total
    for i in range(steps):
        chunk = int(remaining / (steps - i)) + random.randint(-30, 30)
        js(f"window.scrollBy(0, {chunk})")
        remaining -= chunk
        time.sleep(random.uniform(0.15, 0.45))


def blinkit_search(query, max_scrolls=10, scroll_pause=0.4):
    """Search Blinkit for `query` and return all listing rows.

    Uses the direct search URL to skip the search-bar interaction and the
    location dialog (which only re-triggers when no delivery address is set).
    Scrolls to force lazy-loaded cards to render, then extracts each card
    from the role=button containers that hold an "ADD" label.

    Returns a list of dicts: {name, size, price, mrp, discount, eta}.
    """
    from browser_harness.helpers import goto_url, wait_for_load, js

    goto_url(f"https://blinkit.com/s/?q={query}")
    wait_for_load()

    # Wait for product cards to actually render. Blinkit hydrates results after
    # initial paint, so a fixed sleep races against slow networks and an empty
    # DOM gets misread as "no listings". Poll for numeric-id role=button cards
    # whose innerText mentions ADD or a price — that's the rendered state.
    deadline = time.time() + 15
    while time.time() < deadline:
        ready = js(r"""
const cards = document.querySelectorAll('[role="button"][id]');
let n = 0;
cards.forEach(el => {
  if (!/^\d+$/.test(el.id)) return;
  const t = el.innerText || '';
  if (t.includes('ADD') || /₹/.test(t)) n++;
});
return n;
""")
        if ready and ready > 0:
            break
        time.sleep(0.3)

    # Scroll until the page height stops growing (or we hit the cap).
    last_h = 0
    for _ in range(max_scrolls):
        h = js("return document.body.scrollHeight")
        if h == last_h:
            break
        last_h = h
        js(f"window.scrollTo(0, {h})")
        time.sleep(scroll_pause)

    rows = js(r"""
const out = [];
document.querySelectorAll('[role="button"][id]').forEach(el => {
  const lines = (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
  if (!lines.includes('ADD') && !lines.some(l => /^\d+$/.test(l))) return;
  // numeric id = Blinkit product id; skip non-numeric (filter chips, etc.)
  if (!/^\d+$/.test(el.id)) return;
  const addIdx = lines.indexOf('ADD');
  const head = addIdx >= 0 ? lines.slice(0, addIdx) : lines;
  const discount = head.find(s => /%\s*OFF/i.test(s)) || '';
  const eta = head.find(s => /\bMINS?\b/i.test(s)) || '';
  const prices = head.filter(s => /^₹/.test(s));
  const price = prices[0] || '';
  const mrp = prices[1] || '';
  const meta = new Set([discount, eta, ...prices].filter(Boolean));
  const rest = head.filter(s => !meta.has(s));
  const name = rest[0] || '';
  const size = rest[1] || '';
  if (!name) return;
  out.push({ id: el.id, name, size, price, mrp, discount, eta });
});
// Dedupe by product id.
const seen = new Set();
return out.filter(r => { if (seen.has(r.id)) return false; seen.add(r.id); return true; });
""")

    if not rows:
        raise RuntimeError(f"No listings parsed for {query!r} — likely no delivery address set on blinkit.com.")

    return rows


def blinkit_add_to_cart(product_id):
    """Click the ADD button on a product card by its Blinkit product id.

    Product ids are returned by `blinkit_search` as `row["id"]`. The card must
    be in the current page's DOM (i.e., same search result set). Returns the
    new cart-item count so the caller can confirm it incremented.
    """
    from browser_harness.helpers import js

    target = js(f"""
const card = document.getElementById({int(product_id)!r});
if (!card) return {{found: false, reason: 'card not on page'}};
card.scrollIntoView({{block: 'center', behavior: 'instant'}});
// If already in cart, the card shows a "− N +" stepper — click "+" to add another.
// Otherwise click the "ADD" label.
const plus = card.querySelector('.icon-plus');
const addEl = [...card.querySelectorAll('div, button, span')].find(e => (e.textContent || '').trim() === 'ADD');
const target = plus || addEl;
if (!target) return {{found: false, reason: 'no ADD or + control'}};
const r = target.getBoundingClientRect();
return {{found: true, mode: plus ? 'increment' : 'add', x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)}};
""")

    if not target.get("found"):
        raise RuntimeError(f"Could not add product {product_id}: {target.get('reason', 'card not on page')}")

    # Let the smooth-scroll settle before clicking, then click like a human.
    time.sleep(random.uniform(0.4, 0.8))
    click_like_human(target["x"], target["y"])
    human_pace("short")

    # Confirm via the cart-item count rendered in the bottom bar.
    count = js(r"""
const m = (document.body.innerText || '').match(/(\d+)\s+items?/i);
return m ? parseInt(m[1], 10) : null;
""")
    return count


def blinkit_recent_order_urls(limit=10):
    """Return URLs for the most-recent `limit` orders on blinkit.com.

    Reads the React fiber on the order-history list — each card carries
    `identity.id = "order_<orderId>_<merchantId>"` in its memoizedProps,
    and the per-order details URL is /account/orders/<merchantId>/<orderId>.
    No clicking, no navigation; one page load, one DOM read.
    """
    from browser_harness.helpers import new_tab, wait_for_load, js

    new_tab("https://blinkit.com/account/orders")
    wait_for_load()
    time.sleep(1.5)

    ids = js(r"""
const isFiberKey = k => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance');
let fiber = null;
for (const el of document.querySelectorAll('*')) {
  const k = Object.keys(el).find(isFiberKey);
  if (k) { fiber = el[k]; break; }
}
if (!fiber) return [];
const out = [];
const seen = new WeakSet();
function walkProps(node, depth) {
  if (!node || typeof node !== 'object' || depth > 12 || seen.has(node)) return;
  seen.add(node);
  if (Array.isArray(node)) { for (const v of node) walkProps(v, depth + 1); return; }
  const id = node.identity && node.identity.id;
  if (typeof id === 'string') {
    const m = id.match(/^order_(\d+)_(\d+)$/);
    if (m) out.push({orderId: m[1], merchantId: m[2]});
  }
  for (const key of Object.keys(node)) {
    try { walkProps(node[key], depth + 1); } catch (e) {}
  }
}
function walkFiber(f, depth) {
  if (!f || depth > 400) return;
  if (f.memoizedProps) walkProps(f.memoizedProps, 0);
  walkFiber(f.child, depth + 1);
  walkFiber(f.sibling, depth + 1);
}
walkFiber(fiber, 0);
const dedup = [];
const keys = new Set();
for (const o of out) {
  const key = o.orderId + ':' + o.merchantId;
  if (!keys.has(key)) { keys.add(key); dedup.push(o); }
}
return dedup;
""")

    urls = [
        f"https://blinkit.com/account/orders/{o['merchantId']}/{o['orderId']}"
        for o in ids[:limit]
    ]
    return urls


def blinkit_order_contents(url):
    """Return the line items for a Blinkit order details page.

    `url` should be /account/orders/<merchantId>/<orderId>. Reads the
    React fiber and locates the snippets array whose entries have
    widget_type starting with "z_v3_image_text_snippet" — those are the
    product rows. Each row exposes:
      data.title.text       -> name
      data.subtitle1.text   -> "<size> x <qty>" (e.g. "500 g x 1", "2 x 200 g x 1")
      data.subtitle3.text   -> price (markdown may include a struck-through
                               MRP, which we discard)

    Returns a list of dicts: {name, size, qty, price}. price is integer rupees.
    """
    from browser_harness.helpers import new_tab, goto_url, wait_for_load, js

    # Reuse the current tab if it's already on a Blinkit account page (cuts
    # tab-spawn churn that anti-bot rules flag). Otherwise open a fresh tab.
    try:
        from browser_harness.helpers import page_info
        cur = (page_info() or {}).get("url", "")
    except Exception:
        cur = ""
    if "blinkit.com" in cur:
        goto_url(url)
    else:
        new_tab(url)
    wait_for_load()
    time.sleep(1.5)

    rows = js(r"""
const isFiberKey = k => k.startsWith('__reactFiber') || k.startsWith('__reactInternalInstance');
let fiber = null;
for (const el of document.querySelectorAll('*')) {
  const k = Object.keys(el).find(isFiberKey);
  if (k) { fiber = el[k]; break; }
}
if (!fiber) return [];

let products = null;
const seen = new WeakSet();
function walkProps(node, depth) {
  if (products || !node || typeof node !== 'object' || depth > 14 || seen.has(node)) return;
  seen.add(node);
  if (Array.isArray(node)) {
    const hits = node.filter(s => s && typeof s === 'object'
      && typeof s.widget_type === 'string'
      && s.widget_type.startsWith('z_v3_image_text_snippet')
      && s.data && s.data.title && s.data.subtitle1 && s.data.subtitle3);
    if (hits.length) { products = hits; return; }
    for (const v of node) walkProps(v, depth + 1);
    return;
  }
  for (const k of Object.keys(node)) {
    try { walkProps(node[k], depth + 1); } catch (e) {}
  }
}
function walkFiber(f, d) {
  if (!f || d > 400 || products) return;
  if (f.memoizedProps) walkProps(f.memoizedProps, 0);
  walkFiber(f.child, d + 1);
  walkFiber(f.sibling, d + 1);
}
walkFiber(fiber, 0);
if (!products) return [];

return products.map(p => ({
  name: (p.data.title && p.data.title.text) || '',
  subtitle: (p.data.subtitle1 && p.data.subtitle1.text) || '',
  priceText: (p.data.subtitle3 && p.data.subtitle3.text) || '',
}));
""")

    def parse_int_rupees(s):
        m = re.search(r"₹\s*([\d,]+)", s or "")
        return int(m.group(1).replace(",", "")) if m else None

    out = []
    for r in rows:
        sub = (r.get("subtitle") or "").strip()
        m = re.match(r"^(.*)\s*x\s*(\d+)\s*$", sub)
        size, qty = (m.group(1).strip(), int(m.group(2))) if m else (sub, 1)

        price_clean = re.sub(r"~~.*?~~", "", r.get("priceText") or "")
        price = parse_int_rupees(price_clean)

        out.append({
            "name": r.get("name") or "",
            "size": size,
            "qty": qty,
            "price": price,
        })
    return out
