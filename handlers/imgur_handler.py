from playwright.sync_api import sync_playwright

from playwright.async_api import async_playwright

class ImgurHandler:
    def __init__(self, bot):
        self.bot = bot

    async def get_imgur_data(self, url):
        """
        Returns the user-provided title and description of an Imgur post asynchronously.
        """
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url)

            await page.wait_for_selector("h1.Post-title, div.Gallery-Content--descr", timeout=5000)

            # Title
            title_el = await page.query_selector("h1.Post-title")
            if title_el:
                title = (await title_el.inner_text()).strip()
            else:
                title_el = await page.query_selector("meta[property='og:title']") or await page.query_selector("title")
                if title_el:
                    tag_name = await title_el.evaluate("el => el.tagName")
                    if tag_name == "META":
                        title = (await title_el.get_attribute("content")).strip()
                    else:
                        title = (await title_el.inner_text()).strip()
                    if title.endswith(" - Imgur"):
                        title = title.rsplit(" - Imgur", 1)[0].strip()
                else:
                    title = "No title"

            # Description
            desc_el = await page.query_selector("div.Gallery-Content--descr") or await page.query_selector("meta[property='og:description']")
            if desc_el:
                tag_name = await desc_el.evaluate("el => el.tagName")
                if tag_name == "META":
                    description = (await desc_el.get_attribute("content")).strip()
                else:
                    description = (await desc_el.inner_text()).strip()
            else:
                description = "No description"

            await browser.close()

        imgur_data = {
            "title": title,
            "description": description
        }
        return imgur_data