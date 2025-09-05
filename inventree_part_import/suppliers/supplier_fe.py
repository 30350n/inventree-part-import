from enum import StrEnum

import requests
from requests.compat import quote, urlencode
from requests.exceptions import HTTPError, JSONDecodeError, Timeout

from ..error_helper import error
from ..retries import retry_timeouts
from ..localization import get_language
from .base import REMOVE_HTML_TAGS, ApiPart, Supplier, SupplierSupportLevel

class FE(Supplier):
    name = "Future Electronics"
    SUPPORT_LEVEL = SupplierSupportLevel.OFFICIAL_API

    def setup(self, key):
        self.fe_api = FEApi(key)
        return True
    
    def search(self, search_term):
        result = self.fe_api.lookup(search_term)
        if result is None:
            return [], 0

        fe_parts = result["offers"]

        return list(map(self.get_api_part, fe_parts)), len(fe_parts)

    def get_api_part(self, fe_part):
        attributes = {v["name"]: v["value"] for v in fe_part["part_attributes"]}

        # The API gives us price breaks bellow the minimum
        # Inventree does not have a concept of minimum order quantity
        # However it perfectly calculates as if it had if there are
        # no price breaks bellow the minimum order quantity.
        # So remove price breaks we can't order at.
        # Also make sure there is a price break at the minimum order qty.
        pricing = {}
        best_price_for_minimum = None
        minimum_qty = fe_part["quantities"]["quantity_minimum"]
        for v in fe_part["pricing"]:
            qty = v["quantity_from"]
            price = v["unit_price"]
            if qty <= minimum_qty:
                if best_price_for_minimum is None or best_price_for_minimum > price:
                    best_price_for_minimum = price
            else:
                pricing[qty] = price
        if best_price_for_minimum is not None:
            pricing[minimum_qty] = best_price_for_minimum

        # FIXME: Some of theses fields names suggest we can get localized info
        # I don't know how to make that happen, I didn't really cared enough to find out either.
        # Given I can't test this I'm hardcoding « en » for now.
        return ApiPart(
            description=attributes["description (en)"] if "description (en)" in attributes else "",

            # some products have different resolutions of the same image
            # the format also supports using more than one format
            # it only tell us URL and format (I've never seen anything else than JPG)
            # it doesn't tell us the resolution but afait it's always ordered by size
            # rather than downloading them and checking their resolution
            # grab the last one which should be the biggest one
            # at worst we just get a smaller resolution image, it is FINE
            image_url=fe_part["images"][-1]["url"] if len(fe_part["images"]) else "",

            datasheet_url=next((v["url"] for v in fe_part["documents"] if v["type"].lower() == "datasheet"), ""),
            supplier_link=fe_part["part_id"]["web_url"],
            SKU=fe_part["part_id"]["seller_part_number"],
            manufacturer=attributes["manufacturerName"] if "manufacturerName" in attributes else "",
            manufacturer_link="",
            MPN=fe_part["part_id"]["mpn"],
            quantity_available=fe_part["quantities"]["quantity_available"],
            packaging=attributes["packageType"] if "packageType" in attributes else "",

            # The API does returns categories but they are often missing or useless for example:
            # {
            #   "id": "32-bit",
            #   "name": "32-bit",
            #   "subcategory_name": "32-bit"
            # }
            # This is all the data given to us for an MCU.
            # @Jorropo: I wonder if this not a server bug ? The data would make sense if it only ever give us the last component of the path.
            category_path=[],

            parameters=None,
            price_breaks=pricing,
            currency=fe_part["currency"]["currency_code"],
        )

class searchKind(StrEnum):
    EXACT = "exact"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"

class FEApi:
    BASE_URL = "https://api.futureelectronics.com/api/"

    def __init__(self, key):
        self.key = key

    def lookup(self, search_term, kind = searchKind.CONTAINS):
        result = self._do_request(f"v1/pim-future/lookup", urldata={
            "part_number": search_term,
            "lookup_type": kind
        })
        if result is None:
            return None
        
        return result.json()
    
    def _do_request(self, action, urldata=None):
        url = f"{self.BASE_URL}{action}"
        if urldata is not None and len(urldata) > 0:
            url += f"?{urlencode(urldata)}"

        headers = {
            "Accept": "application/json",
            "x-orbweaver-licensekey": self.key,
        }

        try:
            for retry in retry_timeouts():
                with retry:
                    result = requests.get(url, headers=headers)
                    result.raise_for_status()
        except (HTTPError, Timeout) as e:
            try:
                # The API can return more than one error, I don't know when that happens or what this means, so just use the first one.
                errors = result.json()["errors"][0]
                error(f"'{action}' action failed with '{errors["message"]}'", prefix="TI API error: ")
                return None
            except (JSONDecodeError, KeyError):
                error(f"'{action}' action failed with '{e}'", prefix="TI API error: ")
                return None

        return result