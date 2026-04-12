import hmac
from base64 import b64encode
from functools import cache, wraps
from hashlib import sha1
from time import sleep
from timeit import default_timer
from types import MethodType
from typing import Any, Callable, ParamSpec, TypeVar

from requests.compat import quote, urlencode
from requests.exceptions import JSONDecodeError

from .. import retries
from ..exceptions import SupplierError
from ..localization import get_country, get_language
from .base import REMOVE_HTML_TAGS, ApiPart, Supplier, SupplierSupportLevel


class TME(Supplier):
    SUPPORT_LEVEL = SupplierSupportLevel.OFFICIAL_API

    def setup(
        self,
        *,
        api_token: str,
        api_secret: str,
        currency: str,
        language: str,
        location: str,
        **kwargs: Any,
    ):
        temp_api = TMEApi(api_token, api_secret)
        tme_languages = temp_api.get_languages()

        tme_countries = {country["CountryId"]: country for country in temp_api.get_countries()}

        if not (lang := get_language(language)):
            return self.load_error(f"invalid language code '{language}'")
        if (language := lang.alpha_2) not in tme_languages:
            return self.load_error(f"unsupported language '{language}'")
        language = lang.alpha_2

        if not (country := get_country(location)):
            return self.load_error(f"invalid country code '{location}'")
        if (location := country.alpha_2) not in tme_countries:
            return self.load_error(f"unsupported location '{location}'")
        if currency not in tme_countries[country.alpha_2]["CurrencyList"]:
            return self.load_error(f"unsupported currency '{currency}' for location '{location}'")

        self.tme_api = TMEApi(api_token, api_secret, language, location, currency)

    def search(self, search_term: str) -> tuple[list[ApiPart], int]:
        tme_part = self.tme_api.get_product(search_term)
        if tme_part:
            tme_stocks = self.tme_api.get_prices_and_stocks([tme_part["Symbol"]])
            tme_stock = tme_stocks[0] if tme_stocks else {}
            return [self.get_api_part(tme_part, tme_stock)], 1

        if not (results := self.tme_api.product_search(search_term)):
            return [], 0

        filtered_matches = [
            tme_part
            for tme_part in results["ProductList"]
            if tme_part["OriginalSymbol"].lower().startswith(search_term.lower())
            or tme_part["Symbol"].lower().startswith(search_term.lower())
        ]

        exact_matches = [
            tme_part
            for tme_part in filtered_matches
            if tme_part["OriginalSymbol"].lower() == search_term.lower()
            or tme_part["Symbol"].lower() == search_term.lower()
        ]
        if len(exact_matches) == 1:
            filtered_matches = exact_matches

        tme_stocks = self.tme_api.get_prices_and_stocks([m["Symbol"] for m in filtered_matches])
        return list(map(self.get_api_part, filtered_matches, tme_stocks)), len(filtered_matches)

    def get_api_part(self, tme_part: dict[str, Any], tme_stock: dict[str, Any]):
        price_breaks = {
            price_break["Amount"]: price_break["PriceValue"]
            for price_break in tme_stock.get("PriceList", [])
        }

        api_part = ApiPart(
            description=tme_part.get("Description", ""),
            image_url=fix_tme_url(tme_part.get("Photo", "")),
            datasheet_url=None,
            supplier_link=quote(fix_tme_url(tme_part.get("ProductInformationPage", "")), safe=":/"),
            SKU=tme_part.get("Symbol", ""),
            manufacturer=tme_part.get("Producer", "") or "TME",
            manufacturer_link="",
            MPN=tme_part.get("OriginalSymbol", "") or tme_part.get("Symbol", ""),
            quantity_available=tme_stock.get("Amount", 0),
            packaging="",
            category_path=self.tme_api.get_category_path(tme_part["CategoryId"]),
            parameters={},
            price_breaks=price_breaks,
            currency=self.tme_api.currency,
        )

        api_part.finalize_hook = MethodType(self.finalize_hook, api_part)

        return api_part

    def finalize_hook(self, api_part: ApiPart):
        if not (parameters := self.tme_api.get_parameters(api_part.SKU)):
            return False

        for parameter in parameters:
            name = parameter["ParameterName"]
            value = REMOVE_HTML_TAGS.sub("", parameter["ParameterValue"])
            if existing_value := api_part.parameters.get(name):
                value = ", ".join((existing_value, value))
            api_part.parameters[name] = value

        if product_files := self.tme_api.get_product_files(api_part.SKU):
            for document in product_files.get("DocumentList", []):
                if document.get("DocumentType") == "DTE":
                    api_part.datasheet_url = fix_tme_url(document.get("DocumentUrl"))
                    break

        return True


def fix_tme_url(url: str):
    # fix supplier part url if language is set to czech (#15)
    return url.replace("tme.eu/cs/", "tme.eu/cz/", 1)


def limit_frequency(seconds: float):
    P = ParamSpec("P")
    R = TypeVar("R")

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        last_call = default_timer() - seconds

        @wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs):
            nonlocal last_call
            now = default_timer()
            if (timeout := seconds - (now - last_call)) > 0:
                sleep(timeout)
            last_call = now
            return func(*args, **kwargs)

        return wrapper

    return decorator


class TMEApi:
    BASE_URL = "https://api.tme.eu/"

    def __init__(
        self,
        token: str,
        secret: str,
        language: str = "EN",
        country: str = "PL",
        currency: str = "EUR",
    ):
        self._categories = None
        self.token = token
        self.secret = secret
        self.language = language
        self.country = country
        self.currency = currency

        self.session = retries.setup_session()

    def get_category_path(self, category_id: str):
        if self._categories is None:
            self._categories = {
                category["Id"]: (category["Name"], category["ParentId"])
                for category in self.get_categories()
            }

        parent_id = category_id
        category_path: list[str] = []
        while True:
            name, parent_id = self._categories[parent_id]
            if not name:
                return category_path
            category_path.insert(0, name)

    def get_product(self, product_symbol: str) -> dict[str, Any]:
        try:
            result = self._api_call(
                action := "Products/GetProducts",
                {
                    "Country": self.country,
                    "Language": self.language,
                    "SymbolList[0]": product_symbol,
                },
            )
        except SupplierError as e:
            if "These products do not exist in our offer." in e.message:
                return {}
            raise e

        if len(products := result["Data"]["ProductList"]) == 1:
            return products[0]
        else:
            raise SupplierError("TME", f"{action} returned multiple results for '{product_symbol}'")

    def product_search(self, search_term: str) -> dict[str, Any]:
        result = self._api_call(
            "Products/Search",
            {
                "Country": self.country,
                "Language": self.language,
                "SearchPlain": search_term,
            },
        )
        return result["Data"]

    @limit_frequency(0.6)
    def get_prices_and_stocks(self, product_symbols: list[str]) -> list[dict[str, Any]]:
        if not product_symbols:
            return []

        # this api call only supports up to 50 symbols
        assert len(product_symbols) <= 50

        data = {
            "Country": self.country,
            "Language": self.language,
            "Currency": self.currency,
        }
        for i, symbol in enumerate(product_symbols):
            data[f"SymbolList[{i}]"] = symbol

        result = self._api_call("Products/GetPricesAndStocks", data)["Data"]
        assert result["Currency"] == self.currency

        if result["PriceType"] == "GROSS":
            for product in result["ProductList"]:
                to_net_price = 100 / (100 + product["VatRate"])
                for price_break in product["PriceList"]:
                    price_break["PriceValue"] *= to_net_price

        return result["ProductList"]

    def get_categories(self) -> list[dict[str, Any]]:
        result = self._api_call(
            "Products/GetCategories",
            {
                "Country": self.country,
                "Language": self.language,
                "Tree": "false",
            },
        )
        return result["Data"]["CategoryTree"]

    def get_parameters(self, product_symbol: str) -> list[dict[str, Any]]:
        result = self._api_call(
            "Products/GetParameters",
            {
                "Country": self.country,
                "Language": self.language,
                "SymbolList[0]": product_symbol,
            },
        )
        return result["Data"]["ProductList"][0]["ParameterList"]

    def get_product_files(self, product_symbol: str) -> dict[str, Any]:
        result = self._api_call(
            "Products/GetProductsFiles",
            {
                "Country": self.country,
                "Language": self.language,
                "SymbolList[0]": product_symbol,
            },
        )
        return result["Data"]["ProductList"][0]["Files"]

    def get_countries(self) -> list[dict[str, Any]]:
        if not hasattr(self, "_countries"):
            lang = {"Language": "EN"}
            self._countries = self._api_call("Utils/GetCountries", lang)["Data"]["CountryList"]

        return self._countries

    @cache
    def get_languages(self) -> list[str]:
        if not hasattr(self, "_languages"):
            self._languages = self._api_call("Utils/GetLanguages", {})["Data"]["LanguageList"]

        return self._languages

    def _api_call(self, action: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.BASE_URL}{action}.json"
        data_sorted = dict(sorted({**(data or {}), "Token": self.token}.items()))

        signature_base = (
            f"POST&{quote(url, '')}&{quote(urlencode(data_sorted, quote_via=quote), '')}".encode()
        )
        signature = b64encode(hmac.new(self.secret.encode(), signature_base, sha1).digest())
        data_sorted["ApiSignature"] = signature

        result = self.session.post(
            url,
            urlencode(data_sorted),
            headers={"Content-type": "application/x-www-form-urlencoded"},
        )

        if not result.content:
            raise SupplierError(
                "TME", f"Request failed with code {result.status_code} (no content)"
            )

        try:
            content_json: dict[str, Any] = result.json()
        except JSONDecodeError as e:
            raise SupplierError("TME", str(e))

        if result.status_code != 200:
            match content_json.get("Status"):
                case "E_INPUT_PARAMS_VALIDATION_ERROR":
                    validation_errors = content_json.get("Error", {}).get("Validation", {})
                    message = "Input Validation Error\n" + "\n".join(
                        (f"    {e['message']} ({e['value']})" for e in validation_errors.values())
                    )
                    raise SupplierError("TME", message)
                case status:
                    raise SupplierError("TME", content_json.get("ErrorMessage") or str(status))

        return content_json
