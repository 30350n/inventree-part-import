from error_helper import error
from oauthlib.oauth2 import BackendApplicationClient
from requests.compat import quote
from requests.exceptions import HTTPError, JSONDecodeError, Timeout
from requests_oauthlib import OAuth2Session

from ..localization import get_country, get_language
from ..retries import retry_timeouts
from .base import ApiPart, Supplier, SupplierSupportLevel


class DigiKey(Supplier):
    SUPPORT_LEVEL = SupplierSupportLevel.OFFICIAL_API

    def setup(
        self,
        *,
        client_id,
        client_secret,
        currency,
        language,
        location,
        interactive_part_matches,
        **kwargs,
    ):
        self.limit = interactive_part_matches

        if not (country := get_country(location)):
            return self.load_error(f"invalid country code '{location}'")
        if (location := country["alpha_2"]) not in SUPPORTED_LOCATIONS:
            return self.load_error(f"unsupported location '{location}'")

        if not (lang := get_language(language)):
            return self.load_error(f"invalid language code '{language}'")
        if (language := lang["alpha_2"]) not in SUPPORTED_LANGUAGES:
            return self.load_error(f"unsupported language '{language}'")  # print supported ones

        if currency not in SUPPORTED_CURRENCIES:
            return self.load_error(f"unsupported currency '{currency}'")

        self.currency = currency
        self.digikey_api = DigiKeyApi(client_id, client_secret, self.currency, language, location)

        return True

    def search(self, search_term):
        if result := self.digikey_api.product_details(search_term):
            product_details = result["Product"]
            if search_term in self._get_product_variations(product_details):
                return [self.get_api_part(product_details, search_term)], 1

        if not (result := self.digikey_api.keyword_search(search_term, limit=self.limit)):
            return [], 0

        if exact_matches := result["ExactMatches"]:
            if len(exact_matches) == 1:
                return [self.get_api_part(exact_matches[0])], 1
            else:
                return list(map(self.get_api_part, exact_matches)), len(exact_matches)

        products = [
            product
            for product in result["Products"]
            if product["ManufacturerProductNumber"].lower().startswith(search_term.lower())
        ]
        return list(map(self.get_api_part, products)), result["ProductsCount"]

    def get_api_part(self, product_details, digikey_part_number=None):
        if digikey_part_number:
            product_variation = self._get_product_variations(product_details)[digikey_part_number]
        else:
            product_variation = sorted(
                product_details["ProductVariations"],
                key=lambda variation: variation["MinimumOrderQuantity"],
            )[0]

        category_path = []
        category = product_details["Category"]
        while True:
            category_path.append(category["Name"])
            if not category["ChildCategories"]:
                break
            category = category["ChildCategories"][0]

        parameters = {
            parameter["ParameterText"]: parameter["ValueText"]
            for parameter in product_details["Parameters"]
        }

        price_breaks = {
            price_break["BreakQuantity"]: price_break["UnitPrice"]
            for price_break in product_variation["StandardPricing"]
        }

        return ApiPart(
            description=product_details["Description"]["DetailedDescription"],
            image_url=product_details["PhotoUrl"],
            datasheet_url=product_details["DatasheetUrl"],
            supplier_link=product_details["ProductUrl"],
            SKU=product_variation["DigiKeyProductNumber"],
            manufacturer=product_details["Manufacturer"]["Name"],
            manufacturer_link="",
            MPN=product_details["ManufacturerProductNumber"],
            quantity_available=product_variation["QuantityAvailableforPackageType"],
            packaging=product_variation["PackageType"]["Name"],
            category_path=category_path,
            parameters=parameters,
            price_breaks=price_breaks,
            currency=self.currency,
        )

    @classmethod
    def _get_product_variations(cls, product_details):
        return {var["DigiKeyProductNumber"]: var for var in product_details["ProductVariations"]}


class DigiKeyApi:
    BASE_URL = "https://api.digikey.com"
    OAUTH2_TOKEN_URL = f"{BASE_URL}/v1/oauth2/token"
    KEYWORD_SEARCH_URL = f"{BASE_URL}/products/v4/search/keyword"
    PRODUCT_DETAILS_URL = f"{BASE_URL}/products/v4/search/{{}}/productdetails"

    def __init__(self, client_id, client_secret, currency, language, location):
        oauth_client = BackendApplicationClient(client_id)
        self.session = OAuth2Session(client=oauth_client)
        self.session.fetch_token(self.OAUTH2_TOKEN_URL, client_secret=client_secret)

        headers = {
            "X-DIGIKEY-Client-Id": client_id,
            "X-DIGIKEY-Locale-Language": language,
            "X-DIGIKEY-Locale-Currency": currency,
            "X-DIGIKEY-Locale-Site": location,
        }
        self.session.headers.update(headers)

    def keyword_search(self, search_term, limit=0):
        json = {"Keywords": search_term, "Limit": limit}
        if result := self._api_call(self.KEYWORD_SEARCH_URL, json=json):
            return result.json()

    def product_details(self, product_number):
        url = self.PRODUCT_DETAILS_URL.format(quote(product_number, safe=""))
        if result := self._api_call(url):
            return result.json()

    def _api_call(self, url, json=None):
        result = None

        try:
            for retry in retry_timeouts():
                with retry:
                    result = (
                        self.session.get(url) if json is None else self.session.post(url, json=json)
                    )
                    if result.status_code == 404 and result.json()["title"] == "Not Found":
                        return result
                    result.raise_for_status()
        except (HTTPError, Timeout):
            assert result is not None
            error(result.json()["detail"], prefix="DigiKey API error: ")
        except (JSONDecodeError, KeyError) as e:
            error(str(e), prefix="DigiKey API error: ")

        return result


SUPPORTED_LANGUAGES = [
    *["en", "ja", "de", "fr", "ko", "zhs", "zht", "it", "es", "he", "nl", "sv", "pl", "fi", "da"],
    *["no"],
]

SUPPORTED_CURRENCIES = [
    *["USD", "CAD", "JPY", "GBP", "EUR", "HKD", "SGD", "TWD", "KRW", "AUD", "NZD", "INR", "DKK"],
    *["NOK", "SEK", "ILS", "CNY", "PLN", "CHF", "CZK", "HUF", "RON", "ZAR", "MYR", "THB", "PHP"],
]

SUPPORTED_LOCATIONS = [
    *["US", "CA", "JP", "UK", "DE", "AT", "BE", "DK", "FI", "GR", "IE", "IT", "LU", "NL", "NO"],
    *["PT", "ES", "KR", "HK", "SG", "CN", "TW", "AU", "FR", "IN", "NZ", "SE", "MX", "CH", "IL"],
    *["PL", "SK", "SI", "LV", "LT", "EE", "CZ", "HU", "BG", "MY", "ZA", "RO", "TH", "PH"],
]
