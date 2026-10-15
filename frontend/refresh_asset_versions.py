"""Update local asset URLs after changing console styles or scripts."""

from hashlib import sha256
from pathlib import Path
import re


frontend = Path(__file__).resolve().parent
pages = ('index.html',)

for page in pages:
    path = frontend / page
    html = path.read_text(encoding='utf-8')
    assets = ['design.css', 'utilities.css', 'world.js', 'app.js']
    for asset in assets:
        url = f'assets/{asset}'
        digest = sha256((frontend / url).read_bytes()).hexdigest()[:12]
        html, count = re.subn(re.escape(url) + r'(?:\?v=[a-f0-9]+)?', f'{url}?v={digest}', html)
        if count != 1:
            raise ValueError(f'{page}: expected one reference to {url}, found {count}')
    path.write_text(html, encoding='utf-8', newline='')
    print(f'Updated {page}')
