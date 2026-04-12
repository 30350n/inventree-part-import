import re
from typing import Any

from bs4 import BeautifulSoup
from error_helper import warning
from requests.compat import quote

from ..localization import get_country, get_language
from .base import ApiPart, ScrapeSupplier, SupplierSupportLevel, money2float

BASE_URL = "https://reichelt.com"
PRODUCT_PAGE = "shop/product"
SEARCH_PAGE = "shop/search"


class Reichelt(ScrapeSupplier):
    SUPPORT_LEVEL = SupplierSupportLevel.SCRAPING

    def setup(
        self,
        *,
        language: str,
        location: str,
        scraping: str,
        interactive_part_matches: int,
        browser_cookies: str = "",
        **kwargs: Any,
    ):
        if not scraping:
            self.load_error("scraping is disabled")

        if not get_country(location):
            self.load_error(f"unsupported location '{location}'")

        if not get_language(language):
            self.load_error(f"invalid language code '{language}'")

        self.language = language
        self.location = location
        self.localized_url = f"{BASE_URL}/{self.location.lower()}/{self.language.lower()}"

        if browser_cookies:
            self.cookies_from_browser(browser_cookies, "reichelt.com")

        self.max_results = interactive_part_matches

    def search(self, search_term: str) -> tuple[list[ApiPart], int]:
        if SKU_REGEX.fullmatch(search_term):
            sku_link = f"{self.localized_url}/{PRODUCT_PAGE}/-{search_term}.html"
            if product_page := self.scrape(sku_link):
                product_page_soup = BeautifulSoup(product_page.content, "html.parser")
                return [self.get_api_part(product_page_soup, search_term, sku_link)], 1

        search_safe = quote(search_term, safe="")
        search_url = f"{self.localized_url}/{SEARCH_PAGE}/{search_term}?search={search_safe}"
        if not (result := self.scrape(search_url)):
            return [], 0

        search_soup = BeautifulSoup(result.content, "html.parser")

        api_parts: list[ApiPart] = []
        search_results = search_soup.find_all("div", class_="al_gallery_article")
        for result in search_results[: self.max_results]:
            assert (url_tag := result.find("a", itemprop="url"))
            assert isinstance(product_url := url_tag["href"], str)
            assert (sku_match := PRODUCT_URL_SKU_REGEX.match(product_url))
            sku = sku_match.group(1).upper()

            sku_link = f"{self.localized_url}-{sku.lower()}.html"
            if not (product_page := self.scrape(sku_link)):
                continue

            product_page_soup = BeautifulSoup(product_page.content, "html.parser")
            api_part = self.get_api_part(product_page_soup, sku, sku_link)

            if len(search_results) > 1 and search_term.lower() not in api_part.MPN.lower():
                continue

            api_parts.append(api_part)

        exact_matches = [
            api_part
            for api_part in api_parts
            if api_part.SKU.lower() == search_term.lower()
            or api_part.MPN.lower() == search_term.lower()
        ]
        if len(exact_matches) == 1:
            return [exact_matches[0]], 1

        n_results = len(search_results)
        return api_parts, n_results if n_results > self.max_results else len(api_parts)

    def get_api_part(self, soup: BeautifulSoup, sku: str, url: str):
        assert (desc_tag := soup.select_one('div.productDescription p[itemprop="description"]'))
        description = desc_tag.text

        image_url = None
        if image_tag := soup.select_one('div[id="product"] img'):
            assert isinstance(image_tag_src := image_tag["src"], str)
            image_url = IMAGE_URL_REGEX.sub(IMAGE_URL_SUB, image_tag_src)

        datasheet_url = None
        if datasheet_tag := soup.select_one("div.articleDatasheet a"):
            assert isinstance(datasheet_url := datasheet_tag["href"], str)

        availability = 0
        if availability_tag := soup.select_one("a.availability"):
            status = next((c for c in availability_tag["class"] if c.startswith("status_")), None)
            if status and not (availability := AVAILABILITY_MAP.get(status, 0)):
                warning(f"unknown reichelt availability '{status}' ({url})")

        category_path = [
            span.text.strip()
            for span in soup.select('ol#breadcrumb li a span[itemprop="name"]')[1:]
        ]

        parameters_flat = [li.text.strip() for li in soup.select("ul.articleAttribute li")]
        assert len(parameters_flat) % 2 == 0
        parameters: dict[str, str] = dict(zip(parameters_flat[::2], parameters_flat[1::2]))

        if not (manufacturer := parameters.get("Manufacturer")):
            manufacturer = "Reichelt"

        assert (mpn_tag := soup.select_one("li[itemprop='mpn']"))
        mpn = mpn_tag.text

        assert (meta_price := soup.select_one('meta[itemprop="price"]'))
        assert isinstance(meta_price_content := meta_price["content"], str)
        price_breaks: dict[int | float, float] = {1: money2float(meta_price_content)}
        for discount_tag in soup.select("div.discountValue ul li p#productPrice")[1:]:
            assert discount_tag.parent and (quantity_tag := discount_tag.parent.select_one("span"))
            quantity_str = PRICE_BREAK_QUANTITIY_REGEX.sub("", quantity_tag.text).strip()
            assert " " not in quantity_str
            price_breaks[float(quantity_str)] = money2float(discount_tag.text)

        assert (meta_currency := soup.select_one('meta[itemprop="price"]'))
        assert isinstance(currency := meta_currency["content"], str)

        return ApiPart(
            description=description,
            image_url=image_url,
            datasheet_url=datasheet_url,
            supplier_link=url,
            SKU=sku.upper(),
            manufacturer=manufacturer,
            manufacturer_link="",
            MPN=mpn,
            quantity_available=availability,
            packaging="",
            category_path=category_path,
            parameters=parameters,
            price_breaks=price_breaks,
            currency=currency,
        )


IMAGE_URL_REGEX = re.compile(r"/resize/[^/]+/([^?]+)\?.*")
IMAGE_URL_SUB = r"/images/\g<1>"
SKU_REGEX = re.compile(r"^[pP]\d+$")
PRODUCT_URL_SKU_REGEX = re.compile(r"^.*([pP]\d+)\.html[^\.]*$")
PRICE_BREAK_QUANTITIY_REGEX = re.compile(r"[^0-9 ]")

# True -> available, 0 -> not available
AVAILABILITY_MAP = {
    "status_1": True,
    "status_2": 0,
    "status_3": True,
    "status_4": True,
    "status_5": 0,
    "status_6": 0,
    "status_7": True,
    "status_8": 0,
}
