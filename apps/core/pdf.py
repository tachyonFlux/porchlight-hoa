"""HTML -> PDF with WeasyPrint, with all external fetching disabled."""


def render_pdf(html):
    # Imported lazily: WeasyPrint is slow to import and only needed here.
    from weasyprint import HTML
    from weasyprint.urls import URLFetcher

    class NoFetch(URLFetcher):
        """Rendered documents are self-contained; never let content fetch files or URLs."""

        def fetch(self, url, headers=None):
            raise ValueError("External resources are disabled for PDFs.")

    return HTML(string=html, url_fetcher=NoFetch()).write_pdf()
