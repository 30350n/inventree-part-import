import time
import requests
from requests.compat import quote, urlencode
from requests.exceptions import HTTPError, JSONDecodeError, Timeout

from ..error_helper import error
from ..retries import retry_timeouts
from .base import REMOVE_HTML_TAGS, ApiPart, Supplier, SupplierSupportLevel

class TI(Supplier):
    name = "Texas Instruments"
    SUPPORT_LEVEL = SupplierSupportLevel.OFFICIAL_API

    def setup(self, client_key, client_secret, currency):
        self.ti_api = TIApi(client_key, client_secret, currency)
        return True
    
    def search(self, search_term):
        # like me you might assume the products search API would return a single result
        # if you give it an exact part number
        # it does not, so we have to do theses two requests back to back
        ti_parts = self._searchForExactPart(search_term)
        if ti_parts is not None and len(ti_parts) == 0:
            ti_parts = self.ti_api.products(search_term)
        if ti_parts is None:
            return [], 0

        return list(map(self.get_api_part, ti_parts)), len(ti_parts)

    def _searchForExactPart(self, search_term):
        ti_part = self.ti_api.product(search_term)
        if ti_part is None:
            return None
        if "errors" in ti_part and len(errors := ti_part["errors"]):
            for error in errors:
                if error["errorCode"] == "ERR-TICOM-INV-API-1002":
                    return [] # exact OPN does not exists
        return [ti_part]

    def get_api_part(self, ti_part):
        pricingData = {
            "currency": self.ti_api.currency,
            "priceBreaks": []
        }
        for v in ti_part["pricing"]:
            if v["currency"] == self.ti_api.currency:
                pricingData = v
                break
            if v["currency"] == "USD": # as a fallback if we can't find the requested currency give back USD
                pricingData = v

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
            category_path=[], # FIXME: find out a category
            parameters=None,
            price_breaks={v["priceBreakQuantity"]: v["price"] for v in pricingData["priceBreaks"]},
            currency=pricingData["currency"],
        )

class TIApi:
    BASE_URL = "https://transact.ti.com/"
    oauthAccessToken = ""
    oauthValidUntil = 0

    def __init__(self, key, secret, currency):
        self.key = key
        self.secret = secret
        self.currency = currency

    def product(self, orderablePartNumber):
        result = self._api_call(f"v2/store/products/{quote(orderablePartNumber, safe="")}", urldata={
            "exclude-evms": "true",
            "currency": self.currency
        })
        if result is None:
            return None
        if result.status_code == 403 or result.status_code == 404:
            return None
        
        return result.json()

    def products(self, genericPartNumber):
        return self._paginate_api_call("v2/store/products", urldata={
            "gpn": genericPartNumber,
            "exclude-evms": "true",
            "currency": self.currency
        })

    def _getOauthToken(self):
        now = time.monotonic()
        nextRequestBuffer = 60
        if now < self.oauthValidUntil-nextRequestBuffer:
            return self.oauthAccessToken

        result = self._do_request("v1/oauth/accesstoken", bodydata={
            "grant_type": "client_credentials",
            "client_id": self.key,
            "client_secret": self.secret,
        })
        if result is None:
            return None
        result.raise_for_status()

        j = result.json()
        if j["token_type"] != "bearer":
            error(f"unknown token type '{j["token_type"]}'; expected 'bearer'", prefix="TI API error: ")
            return None
        self.oauthAccessToken = j["access_token"]
        self.oauthValidUntil = now + j["expires_in"]
        return self.oauthAccessToken

    def _paginate_api_call(self, action, urldata=None):
        results = []
        page = 0
        while True:
            if urldata is None:
                urldata = {}
            urldata["size"] = 100
            urldata["page"] = page

            result = self._api_call(action, urldata)
            if result is None:
                return None
            if result.status_code == 403 or result.status_code == 404:
                return None
            
            j = result.json()
            
            results += j["content"]
            if j["last"]:
                return results
            
            page += 1

    def _api_call(self, action, urldata=None,):
        return self._do_request(action, urldata, auth=True)
    
    def _do_request(self, action, urldata=None, *, auth=False, bodydata=None):
        url = f"{self.BASE_URL}{action}"
        if urldata is not None and len(urldata) > 0:
            url += f"?{urlencode(urldata)}"

        headers = {"Accept": "application/json"}
        if bodydata is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        
        def send():
            if auth:
                token = self._getOauthToken()
                if token is None:
                    return None
                headers["Authorization"] = f"Bearer {token}"
            if bodydata is not None:
                return requests.post(url, data=bodydata, headers=headers)
            return requests.get(url, headers=headers)

        try:
            for retry in retry_timeouts():
                with retry:
                    result = send()
                    if result is None:
                        return None
                    # we have to ignore 403s because it appear TI's inventory database includes non TI parts however trying to fetch theses yield 403
                    # > curl --request GET --url https://transact.ti.com/v2/store/products?gpn=SM03B-SRSS-TB%28LF%29%28SN%29&exclude-evms=true&currency=EUR&size=100&page=0 --header "Authorization: Bearer $TI_BEARER_TOKEN"
                    # <!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">
                    # <html><head>
                    # <title>403 Forbidden</title>
                    # </head><body>
                    # <h1>Forbidden</h1>
                    # <p>You don't have permission to access this resource.</p>
                    # </body></html>
                    if result.status_code != 200 and result.status_code != 404 and result.status_code != 403:
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