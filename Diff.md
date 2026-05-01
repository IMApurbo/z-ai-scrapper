**Old vs New approach 

| Old | New |
|---|---|
| 4 phases: shimmer scan → stop-button appear → stop-button disappear with 3–4 tick confirmation window → HTML size stabilisation fallback | 1 signal: `button#send-message-button` goes **DISABLED** |
| Worst case added 5–8 s of dead-waiting after the response already finished | Scrape fires the **instant** the button flips — zero dead-wait |
| Could break if z.ai renamed a CSS class (`shimmer`, stop button selector) | Button `id` is far more stable than class names |
| Different code paths for web search vs plain vs thinking | All 4 modes share the exact same button lifecycle, so one path handles all |

