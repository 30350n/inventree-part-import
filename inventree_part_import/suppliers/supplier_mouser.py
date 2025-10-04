import os
from types import MethodType

from bs4 import BeautifulSoup
from error_helper import hint, warning
from mouser.api import MouserPartSearchRequest

from ..retries import retry_timeouts
from .base import (
    DOMAIN_REGEX,
    DOMAIN_SUB,
    REMOVE_HTML_TAGS,
    ApiPart,
    ScrapeSupplier,
    SupplierSupportLevel,
    money2float,
)

class Mouser(ScrapeSupplier):
    SUPPORT_LEVEL = SupplierSupportLevel.SCRAPING

    fallback_domains = ["www2.mouser.com", "eu.mouser.com"]

    def setup(
        self,
        *,
        api_key,
        currency,
        scraping,
        browser_cookies="",
        locale_url="www.mouser.com",
        **kwargs,
    ):
        os.environ["MOUSER_PART_API_KEY"] = api_key

        self.currency = currency
        self.use_scraping = scraping
        self.locale_url = locale_url

        if browser_cookies:
            self.cookies_from_browser(browser_cookies, "mouser.com")

        return True

    def search(self, search_term):
        search_request = MouserPartSearchRequest("partnumber")
        for retry in retry_timeouts():
            with retry:
                search_request.part_search(search_term)

        response = search_request.get_response()
        if not isinstance(response, dict):
            return [], 0
        if not ((results := response.get("SearchResults")) and (parts := results.get("Parts"))):
            return [], 0

        valid_parts = [part for part in parts if part.get("MouserPartNumber", "N/A") != "N/A"]

        search_term_lower = search_term.lower()
        filtered_matches = [
            part for part in valid_parts
            if part.get("MouserPartNumber", "").lower().startswith(search_term_lower)
            or part.get("ManufacturerPartNumber", "").lower().startswith(search_term_lower)
        ]

        exact_matches = [
            part for part in filtered_matches
            if part.get("MouserPartNumber", "").lower() == search_term_lower
            or part.get("ManufacturerPartNumber", "").lower() == search_term_lower
        ]
        if len(exact_matches) == 1:
            return [self.get_api_part(exact_matches[0])], 1

        return list(map(self.get_api_part, filtered_matches)), len(filtered_matches)

    def get_api_part(self, mouser_part):
        mouser_part_number = mouser_part.get("MouserPartNumber")

        supplier_link = DOMAIN_REGEX.sub(
            DOMAIN_SUB.format(self.locale_url), mouser_part.get("ProductDetailUrl"))

        category = mouser_part.get("Category")
        incomplete_category_path = [category] if category else []

        parameters = {}
        for attribute in mouser_part.get("ProductAttributes", []):
            name = attribute.get("AttributeName")
            value = attribute.get("AttributeValue")
            if existing_value := parameters.get(name):
                value = ", ".join((existing_value, value))
            parameters[name] = value

        mouser_price_breaks = mouser_part.get("PriceBreaks", [])
        price_breaks = {
            price_break.get("Quantity"): money2float(price_break.get("Price"))
            for price_break in mouser_price_breaks
        }

        currency = None
        if mouser_price_breaks:
            currency = mouser_price_breaks[0].get("Currency")
        if not currency:
            currency = self.currency

        if not (quantity_available := mouser_part.get("AvailabilityInStock")):
            quantity_available = 0

        api_part = ApiPart(
            description=REMOVE_HTML_TAGS.sub("", mouser_part.get("Description", "")),
            image_url=mouser_part.get("ImagePath"),
            datasheet_url=mouser_part.get("DataSheetUrl"),
            supplier_link=supplier_link,
            SKU=mouser_part_number,
            manufacturer=mouser_part.get("Manufacturer", ""),
            manufacturer_link="",
            MPN=mouser_part.get("ManufacturerPartNumber", ""),
            quantity_available=float(quantity_available),
            packaging=parameters.get("Packaging", ""),
            category_path=incomplete_category_path,
            parameters=parameters,
            price_breaks=price_breaks,
            currency=currency,
        )

        api_part.finalize_hook = MethodType(self.finalize_hook, api_part)

        return api_part

    def finalize_hook(self, api_part: ApiPart):
        if not self.use_scraping:
            hint("scraping is disabled: can't finalize parameters and category_path")
            return True

        url = api_part.supplier_link
        if not (result := self.scrape(url)):
            warning(f"failed to finalize part specifications from '{url}' (blocked)")
            return True

        soup = BeautifulSoup(result.content, "html.parser")

        if specs_table := soup.find("table", class_="specs-table"):
            api_part.parameters.update(dict(
                tuple(map(lambda column: column.text.strip().strip(":"), row.find_all("td")[:2]))
                for row in specs_table.find_all("tr")[1:]
            ))
        else:
            warning(f"failed to get parameters from '{url}' (might be blocked)")
            return True

        if breadcrumb := soup.find("ol", class_="breadcrumb"):
            api_part.category_path = [li.text.strip() for li in breadcrumb.find_all("li")[1:-1]]
        else:
            warning(f"failed to get category path from '{url}' (might be blocked)")
            return True

        return True
