"""FilterBar — dropdown filters + preset pills for Brython tables."""

from browser import document, html


class FilterBar:
    """Renders a horizontal bar with preset pills and filter dropdowns.

    ``filters``: list of dicts:
        - key (str): filter identifier
        - label (str): displayed label (optional, used as placeholder)
        - options: list of {value, label} dicts
    ``presets``: list of dicts:
        - label (str): button text
        - filters (dict): {filter_key: value} to apply
    ``on_change``: callable(state: dict) called when any filter changes
    """

    def __init__(self, container_id, filters, presets, on_change):
        self.container_id = container_id
        self.filters = filters
        self.presets = presets
        self.on_change = on_change
        self.state = {f["key"]: "all" for f in filters}
        self._active_preset = None

    def render(self):
        container = document[self.container_id]
        container.clear()

        bar = html.DIV(Class="filter-bar")

        # Preset pills
        for i, preset in enumerate(self.presets):
            cls = "preset-pill active" if i == self._active_preset else "preset-pill"
            btn = html.BUTTON(preset["label"], Class=cls)
            btn.bind("click", lambda ev, idx=i, p=preset: self._apply_preset(idx, p))
            bar <= btn

        # Filter dropdowns
        for f in self.filters:
            sel = html.SELECT(id=f"filter-{f['key']}", Class="filter-select")
            sel <= html.OPTION(f.get("label", "All"), value="all")
            for opt in f["options"]:
                sel <= html.OPTION(opt["label"], value=opt["value"])
            sel.bind("change", lambda ev, k=f["key"]: self._on_change(k, ev.target.value))
            bar <= sel

        container <= bar

    def _on_change(self, key, value):
        self.state[key] = value
        self._active_preset = None
        self.render()
        self.on_change(dict(self.state))

    def _apply_preset(self, idx, preset):
        self._active_preset = idx
        self.state = {f["key"]: "all" for f in self.filters}
        self.state.update(preset.get("filters", {}))
        # Sync dropdown UI
        for key, val in self.state.items():
            try:
                document[f"filter-{key}"].value = val
            except Exception:
                pass
        self.render()
        self.on_change(dict(self.state))
