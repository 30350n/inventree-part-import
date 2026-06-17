import time
from typing import Any

from requests import Response
from requests.compat import quote, urlencode
from requests.exceptions import HTTPError, JSONDecodeError, Timeout

from ..exceptions import SupplierError
from ..retries import setup_session
from .base import ApiPart, Supplier, SupplierSupportLevel


class TI(Supplier):
    name = "Texas Instruments"
    SUPPORT_LEVEL = SupplierSupportLevel.OFFICIAL_API

    def setup(self, *, client_key: str, client_secret: str, currency: str, **kwargs: Any):
        if not (client_key and client_secret):
            self.load_error("missing client_key or client_secret")
        self.ti_api = TIApi(client_key, client_secret, currency)

    def search(self, search_term: str) -> tuple[list[ApiPart], int]:
        # like me you might assume the products search API would return a single result
        # if you give it an exact part number
        # it does not, so we have to do theses two requests back to back
        ti_parts = self._search_for_exact_part(search_term)
        if ti_parts is not None and len(ti_parts) == 0:
            ti_parts = self.ti_api.products(search_term)
        if ti_parts is None:
            return [], 0

        return list(map(self.get_api_part, ti_parts)), len(ti_parts)

    def _search_for_exact_part(self, search_term: str) -> list[dict[str, Any]] | None:
        ti_part = self.ti_api.product(search_term)
        if ti_part is None:
            return None
        if "errors" in ti_part and len(errors := ti_part["errors"]):
            for err in errors:
                if err["errorCode"] == "ERR-TICOM-INV-API-1002":
                    return []  # exact OPN does not exists
        return [ti_part]

    def get_api_part(self, ti_part: dict[str, Any]) -> ApiPart:
        pricing_data: dict[str, Any] = {"currency": self.ti_api.currency, "priceBreaks": []}
        for pricing in ti_part["pricing"]:
            if pricing["currency"] == self.ti_api.currency:
                pricing_data = pricing
                break
            # as a fallback if we can't find the requested currency give back USD
            if pricing["currency"] == "USD":
                pricing_data = pricing

        return ApiPart(
            description=ti_part["description"],
            image_url=None,
            datasheet_url=None,
            supplier_link=ti_part["buyNowUrl"],
            SKU=ti_part["tiPartNumber"],
            manufacturer=self.name,
            manufacturer_link=ti_part["buyNowUrl"],
            MPN=ti_part["tiPartNumber"],
            quantity_available=ti_part["quantity"],
            packaging=ti_part["packageCarrier"],
            category_path=[],  # FIXME: find out a category
            parameters={},
            price_breaks={
                price_break["priceBreakQuantity"]: price_break["price"]
                for price_break in pricing_data["priceBreaks"]
            },
            currency=pricing_data["currency"],
        )


class TIApi:
    NAME = "Texas Instruments"
    BASE_URL = "https://transact.ti.com/"

    def __init__(self, key: str, secret: str, currency: str):
        self.key = key
        self.secret = secret
        self.currency = currency
        self.session = setup_session()
        self.oauth_access_token = ""
        self.oauth_valid_until = 0.0

    def product(self, orderable_part_number: str) -> dict[str, Any] | None:
        opn = quote(orderable_part_number, safe="")
        result = self._api_call(
            f"v2/store/products/{opn}",
            urldata={"exclude-evms": "true", "currency": self.currency},
        )
        if result.status_code in (403, 404):
            return None

        return result.json()

    def products(self, generic_part_number: str) -> list[dict[str, Any]] | None:
        return self._paginate_api_call(
            "v2/store/products",
            urldata={
                "gpn": generic_part_number,
                "exclude-evms": "true",
                "currency": self.currency,
            },
        )

    def _get_oauth_token(self) -> str:
        now = time.monotonic()
        next_request_buffer = 60
        if now < self.oauth_valid_until - next_request_buffer:
            return self.oauth_access_token

        result = self._do_request(
            "v1/oauth/accesstoken",
            bodydata={
                "grant_type": "client_credentials",
                "client_id": self.key,
                "client_secret": self.secret,
            },
        )
        result.raise_for_status()

        token = result.json()
        if (token_type := token["token_type"]) != "bearer":
            raise SupplierError(
                self.NAME, f"unknown token type '{token_type}'; expected 'bearer'"
            )
        self.oauth_access_token = token["access_token"]
        self.oauth_valid_until = now + token["expires_in"]
        return self.oauth_access_token

    def _paginate_api_call(
        self, action: str, urldata: dict[str, Any] | None = None
    ) -> list[dict[str, Any]] | None:
        results: list[dict[str, Any]] = []
        page = 0
        while True:
            if urldata is None:
                urldata = {}
            urldata["size"] = 100
            urldata["page"] = page

            result = self._api_call(action, urldata)
            if result.status_code in (403, 404):
                return None

            page_data = result.json()
            results += page_data["content"]
            if page_data["last"]:
                return results

            page += 1

    def _api_call(self, action: str, urldata: dict[str, Any] | None = None) -> Response:
        return self._do_request(action, urldata, auth=True)

    def _do_request(
        self,
        action: str,
        urldata: dict[str, Any] | None = None,
        *,
        auth: bool = False,
        bodydata: dict[str, Any] | None = None,
    ) -> Response:
        url = f"{self.BASE_URL}{action}"
        if urldata is not None and len(urldata) > 0:
            url += f"?{urlencode(urldata)}"

        headers = {"Accept": "application/json"}
        if bodydata is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        if auth:
            headers["Authorization"] = f"Bearer {self._get_oauth_token()}"

        result = None
        try:
            if bodydata is not None:
                result = self.session.post(url, data=bodydata, headers=headers)
            else:
                result = self.session.get(url, headers=headers)
            # we have to ignore 403s because it appears TI's inventory database includes non TI
            # parts, however trying to fetch theses yields 403
            # > curl --request GET --url https://transact.ti.com/v2/store/products?gpn=SM03B-SRSS-TB%28LF%29%28SN%29&exclude-evms=true&currency=EUR&size=100&page=0 --header "Authorization: Bearer $TI_BEARER_TOKEN"
            # <!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">
            # <html><head>
            # <title>403 Forbidden</title>
            # </head><body>
            # <h1>Forbidden</h1>
            # <p>You don't have permission to access this resource.</p>
            # </body></html>
            if result.status_code not in (200, 403, 404):
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
