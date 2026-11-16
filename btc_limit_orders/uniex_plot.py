from html.parser import HTMLParser
from urllib.parse import urlsplit
from urllib.request import urlopen


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None


def print_secret_message(url):
    """Fetch a published Google Doc and print its character grid."""
    if urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("URL must use http or https")

    with urlopen(url, timeout=30) as response:
        encoding = response.headers.get_content_charset() or "utf-8"
        html = response.read().decode(encoding, errors="replace")

    parser = _TableParser()
    parser.feed(html)

    # Skip the header row. Each data row contains x, character, y.
    cells_by_position = {}
    for row in parser.rows[1:]:
        if len(row) < 3:
            raise ValueError(f"Expected three cells in data row: {row!r}")

        x, character, y = int(row[0]), row[1], int(row[2])
        if x < 0 or y < 0:
            raise ValueError("Coordinates must be non-negative")
        cells_by_position[(x, y)] = character

    if not cells_by_position:
        return

    max_x = max(x for x, _ in cells_by_position)
    max_y = max(y for _, y in cells_by_position)

    for y in range(max_y, -1, -1):
        print("".join(cells_by_position.get((x, y), " ") for x in range(max_x + 1)))


# Usage:
print_secret_message("https://docs.google.com/document/d/e/2PACX-1vSvM5gDlNvt7npYHhp_XfsJvuntUhq184By5xO_pA4b_gCWeXb6dM6ZxwN8rE6S4ghUsCj2VKR21oEP/pub")