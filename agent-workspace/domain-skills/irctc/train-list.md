# IRCTC — Train List Selectors

URL: `https://www.irctc.co.in/nget/booking/train-list`

One Angular component per train: `app-train-avl-enq`. Everything is scoped to that card.

## Sequence & Stable Selectors

**1. Click Class Box**
```js
const card = document.querySelectorAll('app-train-avl-enq')[index];
// Match by class code in parens: (SL), (3A), (CC), etc.
const box = Array.from(card.querySelectorAll('div.pre-avl'))
  .find(d => / \(CC\)/.test(d.innerText));
box.click();
```

**2. Click Date Cell**
After class click, a row of `td.link.ng-star-inserted` appears. 
*CRITICAL*: You must click the inner `div.pre-avl`, not the `<td>`.
```js
const dateCell = Array.from(card.querySelectorAll('td.link.ng-star-inserted'))
  .find(td => td.innerText.includes('Wed, 10 Jun'));
dateCell.querySelector('div.pre-avl').click();
```

**3. Click Book Now**
*CRITICAL*: `btn.disabled` is always false. The button is only ready when the `disable-book` class is removed.
```js
const btn = card.querySelector('button.train_Search');
if (!btn.classList.contains('disable-book')) {
  btn.click(); // text "Book Now"
}
```

## State Signals
- **Date Selected**: Inner div gains `selected-class` (`div.pre-avl.selected-class`).
- **Button Ready**: Book Now button *loses* the `disable-book` class.
- **Fare Presence**: Not a ready signal (appears before date click).

## Quirks & Traps
- **`div.pre-avl` reuse**: It's used for both the top-level class boxes and the inner date cells. Always use text filtering (`(CODE)` vs `Day, DD Mon`) or precise scoping (`td.link` parent) to distinguish them.
- **Date click**: Clicking `<td>` does nothing. Click the inner `div.pre-avl`.
- **Login Modal**: If logged out, Book Now opens a login modal instead of navigating. Don't automate login credentials, ask the user.