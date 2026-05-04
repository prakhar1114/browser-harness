# Blinkit — Order History

Field-tested against blinkit.com on 2026-05-03 using a logged-in Chrome
session. Uses only core helpers (`new_tab`, `goto_url`, `wait_for_load`,
`js`, `page_info`).

## URL patterns

```
https://blinkit.com/account/orders                              # list
https://blinkit.com/account/orders/<merchantId>/<orderId>       # detail
```

**Trap:** the detail URL is `merchantId` first, `orderId` second. The
identifier you pull off the React fiber (see below) lists them in the
*opposite* order — `order_<orderId>_<merchantId>`. Swap when building the
URL or you'll 404.

## Selector scraping does not work

Blinkit's account pages render through a Zomato widget framework. The data
you want is **not in the DOM text** — it lives in `memoizedProps` on React
fibers. CSS selectors return empty for both list and detail pages.

## Hydration wait

`wait_for_load()` returns at `readyState=complete`, but `memoizedProps` is
still partial at that point. Sleep ~1.5s after the load before reading the
fiber, otherwise you'll match a subset of the rows.

## Fiber-walk recipe

Generic scaffolding used by both the list and detail extractors:

1. Pick any rendered element. Find its `__reactFiber*` /
   `__reactInternalInstance*` key — that's the entry into the fiber tree.
2. Recursively walk `child` + `sibling` (cap depth ~400 for safety).
3. At each fiber, walk `memoizedProps` recursively (cap depth ~12, use a
   `WeakSet` for cycle detection).
4. Match by *shape* (key name / `widget_type`), not by index.

## Order list — extract recent order URLs

Each order card carries an identifier in props:

```
identity.id === "order_<orderId>_<merchantId>"
```

```python
new_tab("https://blinkit.com/account/orders")
wait_for_load()
import time; time.sleep(1.5)

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
    for o in ids
]
```

## Order detail — extract line items

Line items are array members with:

- `widget_type` starts with `z_v3_image_text_snippet`
- shape `data.{title, subtitle1, subtitle3}`

Field map:

| Field | Source | Notes |
|---|---|---|
| `name` | `data.title.text` | |
| `subtitle1` | `data.subtitle1.text` | `"<size> x <qty>"`, e.g. `"500 g x 1"` |
| `subtitle3` | `data.subtitle3.text` | price; may include struck-through MRP as `~~₹X~~` |

```python
# Reuse the current tab if already on Blinkit (see anti-bot tip below).
cur = (page_info() or {}).get("url", "")
if "blinkit.com" in cur:
    goto_url(detail_url)
else:
    new_tab(detail_url)
wait_for_load()
import time; time.sleep(1.5)

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
```

Parse each row in Python:

```python
import re

def parse_row(r):
    sub = (r.get("subtitle") or "").strip()
    m = re.match(r"^(.*)\s*x\s*(\d+)\s*$", sub)
    size, qty = (m.group(1).strip(), int(m.group(2))) if m else (sub, 1)

    price_clean = re.sub(r"~~.*?~~", "", r.get("priceText") or "")
    pm = re.search(r"₹\s*([\d,]+)", price_clean)
    price = int(pm.group(1).replace(",", "")) if pm else None

    return {"name": r.get("name") or "", "size": size, "qty": qty, "price": price}
```

The `~~.*?~~` strip is essential — `subtitle3` often contains the
struck-through MRP before the live price, and a naive `₹\s*([\d,]+)` would
pick up the MRP first.

## Tab reuse (anti-bot)

If `page_info().url` already contains `blinkit.com`, use `goto_url` instead
of `new_tab`. Spawning a fresh tab for each order detail is a churn signal
and wastes a load. The detail snippet above branches on this automatically.

## Not covered

Order cancellation, refunds, and the re-order flow are not field-tested.
Add sibling sections or files when verified.
