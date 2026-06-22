from enum import StrEnum
from typing import Any

from requests import Response
from requests.compat import urlencode
from requests.exceptions import HTTPError, JSONDecodeError, Timeout

from ..exceptions import SupplierError
from ..retries import setup_session
from .base import ApiPart, Supplier, SupplierSupportLevel


class FE(Supplier):
    name = "Future Electronics"
    SUPPORT_LEVEL = SupplierSupportLevel.OFFICIAL_API

    def setup(self, *, key: str, **kwargs: Any):
        if not key:
            self.load_error("missing API key")
        self.fe_api = FEApi(key)

    def search(self, search_term: str) -> tuple[list[ApiPart], int]:
        fe_parts = self.fe_api.lookup(search_term)["offers"]
        return list(map(self.get_api_part, fe_parts)), len(fe_parts)

    def get_api_part(self, fe_part: dict[str, Any]) -> ApiPart:
        attributes = {attr["name"]: attr["value"] for attr in fe_part["part_attributes"]}

        # FIXME: Some of theses fields names suggest we can get localized info
        # I don't know how to make that happen, not really cared enough to find out either.
        # Given I can't test this I'm hardcoding en for now.
        return ApiPart(
            description=attributes.get("description (en)", ""),

            # some products have different resolutions of the same image
            # the format also supports using more than one format
            # it only tell us URL and format (I've never seen anything else than JPG)
            # it doesn't tell us the resolution but afait it's always ordered by size
            # rather than downloading them and checking their resolution
            # grab the last one which should be the biggest one
            # at worst we just get a smaller resolution image, it is FINE
            image_url=fe_part["images"][-1]["url"] if fe_part["images"] else "",

            datasheet_url=next(
                (doc["url"] for doc in fe_part["documents"] if doc["type"].lower() == "datasheet"),
                "",
            ),
            supplier_link=fe_part["part_id"]["web_url"],
            SKU=fe_part["part_id"]["seller_part_number"],
            manufacturer=attributes.get("manufacturerName", ""),
            manufacturer_link="",
            MPN=fe_part["part_id"]["mpn"],
            quantity_available=fe_part["quantities"]["quantity_available"],
            packaging=attributes.get("packageType", ""),

            # The API does returns categories but they are often missing or useless for example:
            # {
            #   "id": "32-bit",
            #   "name": "32-bit",
            #   "subcategory_name": "32-bit"
            # }
            # This is all the data given to us for an MCU.
            # @Jorropo: I wonder if this not a server bug ? The data would make sense if it only ever give us the last component of the path.
            category_path=[],

            parameters={},
            price_breaks={
                price_break["quantity_from"]: price_break["unit_price"]
                for price_break in fe_part["pricing"]
            },
            currency=fe_part["currency"]["currency_code"],
        )


class SearchKind(StrEnum):
    EXACT = "exact"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"


class FEApi:
    NAME = "Future Electronics"
    BASE_URL = "https://api.futureelectronics.com/api/"

    def __init__(self, key: str):
        self.key = key
        self.session = setup_session()

    def lookup(self, search_term: str, kind: SearchKind = SearchKind.CONTAINS) -> dict[str, Any]:
        result = self._do_request(
            "v1/pim-future/lookup",
            urldata={"part_number": search_term, "lookup_type": kind},
        )
        return result.json()

    def _do_request(self, action: str, urldata: dict[str, Any] | None = None) -> Response:
        url = f"{self.BASE_URL}{action}"
        if urldata is not None and len(urldata) > 0:
            url += f"?{urlencode(urldata)}"

        headers = {
            "Accept": "application/json",
            "x-orbweaver-licensekey": self.key,
        }

        result = None
        try:
            result = self.session.get(url, headers=headers)
            result.raise_for_status()
        except (HTTPError, Timeout) as e:
            try:
                # The API can return more than one error, I don't know when that happens or
                # what this means, so just use the first one.
                first_error = result.json()["errors"][0]
                raise SupplierError(
                    self.NAME, f"'{action}' action failed with '{first_error['message']}'"
                )
            except (JSONDecodeError, KeyError, AttributeError):
                raise SupplierError(self.NAME, f"'{action}' action failed with '{e}'")

        return result
