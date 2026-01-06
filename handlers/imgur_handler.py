from playwright.sync_api import sync_playwright

class ImgurHandler:
    def __init__(self, bot):
        self.bot = bot

    async def get_imgur_post_info(self, url):
        """
        Returns the user-provided title and description of an Imgur post.
        Works for single images and gallery posts.
        """
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url)

        # Wait for either the title or description to appear
        page.wait_for_selector("h1.Post-title, div.Gallery-Content--descr", timeout=5000)

        # --- Extract title ---
        title_el = page.query_selector("h1.Post-title")
        if title_el:
            # Prefer the user-provided title in the DOM
            title = title_el.inner_text().strip()
        else:
            # Fallback to meta tags or <title>
            title_el = (
                page.query_selector("meta[property='og:title']") or
                page.query_selector("title")
            )
            if title_el:
                if title_el.evaluate("el => el.tagName") == "META":
                    title = title_el.get_attribute("content").strip()
                else:
                    title = title_el.inner_text().strip()
                # Remove the common "- Imgur" suffix
                if title.endswith(" - Imgur"):
                    title = title.rsplit(" - Imgur", 1)[0].strip()
            else:
                title = "No title"

        # --- Extract description ---
        desc_el = (
            page.query_selector("div.Gallery-Content--descr") or
            page.query_selector("meta[property='og:description']")
        )
        if desc_el:
            if desc_el.evaluate("el => el.tagName") == "META":
                description = desc_el.get_attribute("content").strip()
            else:
                description = desc_el.inner_text().strip()
        else:
            description = "No description"

        browser.close()
        return title, description