"""
BoltDepot supplier class for inventree_part_import.  Implements HTML scraping to
extract part information from the BoltDepot website.

Copyright (c) 2024 Chris Midgley
"""

import re
from typing import Any, Dict, List, Optional, Tuple, Union
from bs4 import BeautifulSoup
from bs4.element import Tag
from ..error_helper import warning, info
from .base import ScrapeSupplier, ApiPart, SupplierSupportLevel

DOMAIN = "boltdepot.com"
API_BASE_URL = f"https://{DOMAIN}/"
PRODUCT_URL = f"{API_BASE_URL}Product-Details?product={{}}"
# some BoltDepot parts use `http:` and other `https:`.  The assumption is that the `http` version is
# older, use the `https  version, since we can not have more than one barcode linked to the same part.
BARCODE_URL = f"https://{DOMAIN}/Product-Details.aspx?product={{}}"

class BoltDepot(ScrapeSupplier):
    """
    BoleDepot supplier, implement using HTML scraping of product pages.
    """
    SUPPORT_LEVEL = SupplierSupportLevel.SCRAPING

    def setup(self):
        """
        No setup required for BoltDepot supplier.

        Returns:
            bool: True always.
        """
        return True

    def search(self, search_term: str) -> Tuple[List[ApiPart], int]:
        """
        Public interface from Supplier to search for a part at BoltDepot using the specified search
        term.  Search term is the BoltDepot SKU, as there is no manufacturer information provided by
        them.

        Args:
            search_term (str): The BoltDepot SKU to search for.

        Returns:
            Tuple[List[ApiPart], int]: A tuple containing a list of found ApiPart objects and 
            the number of found parts.
        """
        # load the product page for the specified search term
        for _ in range(3):
            scrape_url = PRODUCT_URL.format(search_term)
            product_page_result = self.scrape(scrape_url)
            if product_page_result:
                break
        else:
            warning(f"failed to search part at BoltDepot (url {scrape_url} did not respond)")
            return [], 0

        # parse the product page using BeautifulSoup
        soup = BeautifulSoup(product_page_result.content, "html.parser")

        # verify we got a valid product page
        if not self.is_product_page(soup):
            return [], 0

        # capture the product info
        api_part = self._scrape_api_part(soup, search_term)
        if not api_part:
            warning(f"failed to parse found part at BoltDepot (url {scrape_url})")
            return [], 0

        # return the part
        return [api_part], 1

    def _find_tag(self, tag: Optional[Tag], *args: Union[Tuple[str,
                  Optional[Dict[str, Any]]], str]) -> Optional[Tag]:
        """
        Find a specific tag within a given tag or its descendants.  This is a pylint safe method to
        access BeautifulSoup tags.

        Args:
            tag (Optional[Tag]): The starting tag to search from.
            *args (Union[Tuple[str, Optional[Dict[str, Any]]], str]): Variable-length argument list 
            of tag names and optional attributes.

        Returns:
            Optional[Tag]: The found tag or None if not found.
        """
        current_tag = tag
        for arg in args:
            if isinstance(arg, tuple):
                # tuple containing tag name and attributes
                tag_name, attrs = arg
                if isinstance(current_tag, Tag):
                    # use keyword argument unpacking for attributes
                    current_tag = current_tag.find(
                        tag_name, **(attrs if isinstance(attrs, dict) else {}))
                else:
                    return None
            elif isinstance(arg, str):
                # only the tag name is provided, no attributes
                if isinstance(current_tag, Tag):
                    current_tag = current_tag.find(arg)
                else:
                    return None
            else:
                # ignore incorrect types
                continue

            if current_tag is None:
                return None
        return current_tag if isinstance(current_tag, Tag) else None

    def _find_text(self, tag: Optional[Tag], *args) -> str:
        """
        Find the text content of a given tag criteria, using a Pylint safe method to access
        BeautifulSoup tags.  It also returns the direct contents of the element, and not any content
        from children elements.  Also cleans the text to remove unwanted newlines and extra
        whitespace.

        Args:
            tag (Optional[Tag]): The tag to search for.  *args: Additional arguments to pass to the
            `_find_tag` method.

        Returns:
            str: The text content of the found tag, or an empty string if the tag is not found.
        """
        if found_tag := self._find_tag(tag, *args):
            return re.sub(r'\s+', ' ', found_tag.contents[0].get_text()).strip()
        return ""

    def is_product_page(self, soup: BeautifulSoup) -> bool:
        """
        Checks if the scraped page has likely resulted in a valid product having been found.

        Args:
            soup (BeautifulSoup): The web page to check if it is a product page.

        Returns:
            bool: True if the page is a product page, False otherwise.
        """
        # we know we have a product when we find the 'content-main' div with a link
        # for "Product Catalog"
        return self._find_text(
            soup, ("div", {"id": "content-main"}), "a").lower() == "product catalog"

    def _scrape_api_part(self, soup: BeautifulSoup, part_id: str) -> ApiPart | None:
        """
        Scrapes the product page and returns a part (ApiPart) to add or None if parsing fails to
        gather sufficient part details.

        Args:
            soup (BeautifulSoup): The HTML product page information to parse.
            part_id (str): The ID of the part to parse.

        Returns:
            ApiPart | None: An instance of ApiPart if the parsing is successful, None otherwise.
        """
        # set up access to the main content section
        content_main = self._find_tag(soup, ("div", {"id": "content-main"}))
        if not content_main:
            return None

        description = self._get_description(content_main)
        if not description:
            return None
        price_breaks = self._get_pricing(content_main, part_id)
        image_url = self._get_image_url(content_main)
        parameters = self._get_parameters(content_main)
        category_path = self._get_category_path(content_main)
        if len(category_path) == 0:
            return None

        return ApiPart(
            description=description,
            image_url=image_url,
            datasheet_url="",
            supplier_link=PRODUCT_URL.format(part_id),
            SKU=part_id,
            manufacturer="BoltDepot",
            manufacturer_link=PRODUCT_URL.format(part_id),
            MPN=part_id,
            quantity_available=0,
            packaging="",
            category_path=category_path,
            parameters=parameters,
            price_breaks=price_breaks,
            currency="USD",
            barcode=BARCODE_URL.format(part_id)
        )

    def _get_description(self, content_main: Tag) -> str:
        """
        Extracts the part description from the "h1" tag in the content_main section.

        Parameters:
            content_main (Tag): The main content element containing the description.

        Returns:
            str: The extracted description or "" if no description is found.
        """
        description = self._find_text(content_main, "h1")
        return description

    def _get_parameters(self, content_main: Tag) -> Dict[str, str]:
        """
        Extracts useful parameters from the product details table in the HTML content (dropping the
        product number and category/subcategory properties).

        Args:
            content_main (Tag): The main content of the HTML page.

        Returns:
            Dict[str, str]: A dictionary containing the extracted parameters
        """
        product_details_table = self._find_tag(
            content_main, ("table", { "class": "product-details-table" }))
        if not product_details_table:
            return {}
        product_details_rows = product_details_table.find_all("tr")
        if not product_details_rows or len(product_details_rows) == 0:
            return {}

        # gather the parameters
        parameters: Dict[str, str] = {}
        ignore_properties = ["bolt depot", "category", "subcategory"]
        for row in product_details_rows:
            property_name = self._find_text(row, ("td", { "class": "property-name" }))
            property_value = self._find_text(row, ("td", { "class": "property-value" }))
            if not property_name or not property_value:
                continue
            # ignore certain properties
            if any(property in property_name.lower() for property in ignore_properties):
                continue
            # everything else is considered a parameter
            parameters[property_name] = property_value

        return parameters

    def _get_pricing(self, content_main: Tag, part_id: str) -> Dict[int, float]:
        """
        Retrieves the pricing information from the product-list-table, decoding the various
        price breaks (`$xx / ea`, `$yy / 1000`, etc).

        Args:
            content_main (Tag): The main content of the webpage.
            part_id (str): The ID of the part

        Returns:
            Dict[int, float]: A dictionary of price breaks
        """
        pricing_table = self._find_tag(content_main, ("table", { "id": "product-list-table" }))
        if not pricing_table:
            return {}
        pricing_row = self._find_tag(pricing_table, ("tr", { "id": f"p{part_id}" }))
        if not pricing_row:
            return {}
        pricing_cells = pricing_row.find_all("td")
        if not pricing_cells or len(pricing_cells) == 0:
            return {}

        price_breaks: Dict[int, float] = {}
        for cell in pricing_cells:
            price_break = self._find_text(cell, ("span", { "class": "price-break" }))
            price_per_qty = self._find_text(cell, ("span", { "class": "perQty"}))
            if not price_break or not price_per_qty:
                continue

            # adjust price from purchase price to unit, and store into price breaks
            if price_per_qty == "/ ea":
                adjusted_quantity = 1
            else:
                try:
                    adjusted_quantity = int(price_per_qty[2:].replace(",", ""))
                except ValueError:
                    continue
            try:
                purchase_price = float(price_break[1:])
            except ValueError:
                continue

            price_breaks[adjusted_quantity] = round(purchase_price / adjusted_quantity, 3)
        return price_breaks

    def _get_image_url(self, content_main: Tag) -> str:
        """
        Retrieves the image URL for the BoltDepot part from div class "row" containing
        an img tag.

        Args:
            content_main (Tag): The main content tag of the product page.

        Returns:
            str: The URL of the image, or an empty string if no image URL is found.
        """
        image_tag = self._find_tag(content_main, ("div", {"class": "row"}), "img")
        if not isinstance(image_tag, Tag):
            return ""
        url = image_tag.get("src")
        if not url or not isinstance(url, str):
            return ""
        return f"{API_BASE_URL}{url}"

    def _get_category_path(self, content_main: Tag) -> List[str]:
        """
        Extracts the category path from the navigation breadcrumbs, removing the "product catalog"
        and any size (e.g. "3mm x 0.5mm", "#4", etc) from the end of the path.

        Args:
            content_main (Tag): The main content tag containing the navigation breadcrumbs.

        Returns:
            List[str]: The category path extracted from the navigation breadcrumbs.
        """
        # locate the breadcrumbs used for navigation
        nav = self._find_tag(content_main, "nav")
        if not nav:
            return []
        nav_a_tags = nav.find_all("a")
        if not nav_a_tags or len(nav_a_tags) == 0:
            return []

        # extract all, except for "product catalog", and what we believe to be size
        category_path = []
        for tag in nav_a_tags:
            if tag.get_text().lower() == "product catalog":
                continue
            category_path.append(tag.get_text())

        # remove the last element if it is a size (starts with "#", a number, contains "mm" or
        # "inch" or '"')
        if re.search(r"^(?:[#0-9])|(?:(?:mm|#|inch|\"))", category_path[-1]):
            category_path.pop()

        if category_path[-1].startswith("Size: "):
            category_path.pop()

        return category_path
