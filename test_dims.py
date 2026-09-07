# -*- coding: utf-8 -*-
import requests

r = requests.get('http://127.0.0.1:5000/futures/popup/integrated', timeout=5)
html = r.text

# Check iframe dimensions via frame-wrap
for name, w, h in [
    ('amountflow', 320, 500),
    ('zhangfu', 320, 180),
    ('capital', 320, 400),
    ('industry', 400, 400),
    ('external', 400, 600),
    ('news', 410, 380),
    ('option', 830, 380),
    ('futures', 1240, 700),
]:
    search = f'.{name} .frame-wrap {{ width:{w}px; height:{h}px'
    found = search in html
    alt_search = f'width:{w}px; height:{h}px'
    found2 = alt_search in html
    print(f'  {"OK" if found or found2 else "FAIL"}: {name} frame-wrap {w}x{h}')

# Check if popup pages use window.showModalDialog or similar
for label, url in [
    ('futures', '/futures/popup'),
    ('industry', '/futures/industry/popup'),
    ('zhangfu', '/futures/zhangfu/popup'),
]:
    r2 = requests.get('http://127.0.0.1:5000' + url, timeout=5)
    has_modal = 'showModalDialog' in r2.text
    has_dialog = 'dialog(' in r2.text
    has_window_open = 'window.open' in r2.text
    print(f'  {label}: modal={has_modal} dialog={has_dialog} window_open={has_window_open} len={len(r2.text)}')
