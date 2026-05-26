"""SortableTable — click-to-sort generic table for Brython."""

from browser import document, html


class SortableTable:
    """Renders a table with clickable sortable headers.

    Usage:
        tbl = SortableTable("container-id", columns, render_row)
        tbl.set_data(rows)
        tbl.render()

    ``columns``: list of dicts with keys:
        - label (str): header text
        - sortable (bool): whether the column is sortable
        - key (str): sort key — used to read row[key] for default sort
        - sort_value (callable, optional): row -> comparable for custom sort
    ``render_row``: callable(row) -> html.TR
    """

    def __init__(self, container_id, columns, render_row):
        self.container_id = container_id
        self.columns = columns
        self.render_row = render_row
        self.rows = []
        self.sort_key = None
        self.sort_asc = True
        self.empty_msg = "No data."

    def set_data(self, rows):
        self.rows = list(rows)

    def render(self):
        container = document[self.container_id]
        container.clear()

        if not self.rows:
            container <= html.P(self.empty_msg,
                                style={"color": "var(--text-dim)", "padding": "1rem"})
            return

        # Sort
        display = list(self.rows)
        if self.sort_key:
            col = next((c for c in self.columns if c.get("key") == self.sort_key), None)
            if col:
                sfn = col.get("sort_value", lambda r: str(r.get(self.sort_key, "")))
                try:
                    display.sort(key=sfn, reverse=not self.sort_asc)
                except Exception:
                    pass

        # Header
        table = html.TABLE(Class="runs-table")
        thead = html.THEAD()
        tr = html.TR()
        for col in self.columns:
            label = col.get("label", "")
            indicator = ""
            if self.sort_key == col.get("key"):
                indicator = " \u25b2" if self.sort_asc else " \u25bc"
            th = html.TH(label + indicator)
            if col.get("sortable"):
                th.style.cursor = "pointer"
                th.classList.add("sortable-th")
                th.bind("click", lambda ev, k=col["key"]: self._toggle_sort(k))
            tr <= th
        thead <= tr
        table <= thead

        # Body
        tbody = html.TBODY()
        for row in display:
            tbody <= self.render_row(row)
        table <= tbody
        container <= table

    def _toggle_sort(self, key):
        if self.sort_key == key:
            self.sort_asc = not self.sort_asc
        else:
            self.sort_key = key
            self.sort_asc = True
        self.render()
