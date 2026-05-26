"""SplideCarousel — reusable card carousel for Brython dashboards.

Usage:
    carousel = SplideCarousel(
        container_id="my-container",
        carousel_id="my-carousel",
        render_card=my_card_renderer,   # (item) -> html.DIV
        get_id=lambda item: item["id"], # extract unique ID from item
        on_select=my_callback,          # (identifier) -> None
        btn_label="Dispatch",           # action button text
        empty_msg="No items found.",
        per_page=2,
    )
    carousel.render(items)
"""

from browser import document, html, window


class SplideCarousel:
    """Generic Splide-powered card carousel with selection state."""

    def __init__(self, container_id, carousel_id, render_card, get_id,
                 on_select=None, on_action=None, btn_label="Select", btn_id=None,
                 empty_msg="No items.", per_page=2, show_action_btn=False):
        self.container_id = container_id
        self.carousel_id = carousel_id
        self.render_card = render_card
        self.get_id = get_id
        self.on_select = on_select
        self.on_action = on_action
        self.btn_label = btn_label
        self.btn_id = btn_id or f"{carousel_id}-btn"
        self.empty_msg = empty_msg
        self.per_page = per_page
        # show_action_btn=False (default) skips the carousel-level action button.
        # Callers that wire per-card actions inside their render_card don't need
        # the disconnected button below the carousel. Defaults to off so the
        # less-cluttered layout wins.
        self.show_action_btn = show_action_btn
        self.selected = ""
        self._splide = None

    def render(self, items):
        """Render the carousel with the given items."""
        container = document[self.container_id]
        container.clear()

        if not items:
            container <= html.P(self.empty_msg,
                                style={"color": "var(--text-dim)", "padding": "0.5rem",
                                       "font-size": "0.6875rem"})
            return

        self.selected = ""

        # Build Splide DOM
        splide_el = html.DIV(Class="splide", id=self.carousel_id)
        track = html.DIV(Class="splide__track")
        slist = html.DIV(Class="splide__list")

        for item in items:
            slide = html.DIV(Class="splide__slide")
            card = self.render_card(item)
            item_id = self.get_id(item)
            card.attrs["data-card-id"] = item_id
            card.classList.add("issue-card")
            card.bind("click", lambda ev, eid=item_id: self._select(eid))
            slide <= card
            slist <= slide

        track <= slist
        splide_el <= track
        container <= splide_el

        # Optional carousel-level action button. Default is off — callers
        # that render per-card actions inside render_card skip this entirely
        # to avoid a second disconnected button below the carousel.
        if self.show_action_btn:
            btn = html.BUTTON("Select a card", Class="dispatch-btn is-idle", id=self.btn_id)
            btn.attrs["type"] = "button"
            if self.on_action:
                btn.bind("click", lambda ev: self.on_action(self.selected) if self.selected else None)
            container <= btn

        # Init Splide
        self._splide = window.Splide.new(f"#{self.carousel_id}", {
            "perPage": self.per_page,
            "gap": "0.5rem",
            "pagination": False,
            "arrows": True,
            "padding": {"left": "1.5rem", "right": "1.5rem"},
            "autoHeight": True,
        })
        self._splide.mount()

    def _select(self, identifier):
        """Highlight selected card, update button."""
        self.selected = identifier
        css_sel = f"#{self.carousel_id} .issue-card"
        for card in document.select(css_sel):
            if card.attrs.get("data-card-id") == identifier:
                card.classList.add("selected")
            else:
                card.classList.remove("selected")

        if self.show_action_btn:
            btn = document[self.btn_id]
            btn.classList.remove("is-idle")
            btn.text = f"{self.btn_label} {identifier}"

        if self.on_select:
            self.on_select(identifier)

    def loading(self):
        """Show loading spinner in the container."""
        container = document[self.container_id]
        container.clear()
        container <= html.DIV(html.SPAN(Class="spinner") + " Loading...",
                              Class="loading-msg", style={"padding": "0.5rem"})
