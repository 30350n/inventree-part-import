import re
from functools import cache, wraps
from time import sleep, time
from timeit import default_timer
from types import MethodType
from typing import Any, Callable, ParamSpec, TypeVar
from urllib.parse import quote

from requests.exceptions import JSONDecodeError

from .. import retries
from ..exceptions import SupplierError
from ..localization import get_country, get_language
from .base import REMOVE_HTML_TAGS, ApiPart, Supplier, SupplierSupportLevel


QueryParams = dict[str, Any] | list[tuple[str, Any]] | None


class TMEV2(Supplier):
    SUPPORT_LEVEL = SupplierSupportLevel.OFFICIAL_API

    @property
    def name(self) -> str:
        return "TME"

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
        temp_api = TMEV2Api(api_token, api_secret)
        tme_languages = temp_api.get_languages()
        tme_countries = {country["CountryId"]: country for country in temp_api.get_countries()}

        if not (lang := get_language(language)):
            return self.load_error(f"invalid language code '{language}'")
        language = lang.alpha_2.lower()
        if language not in tme_languages:
            return self.load_error(f"unsupported language '{language}'")

        if not (country := get_country(location)):
            return self.load_error(f"invalid country code '{location}'")
        location = country.alpha_2.upper()
        if location not in tme_countries:
            return self.load_error(f"unsupported location '{location}'")

        currency = currency.upper()
        tme_currencies = temp_api.get_currencies(location)
        if currency not in tme_currencies:
            return self.load_error(f"unsupported currency '{currency}' for location '{location}'")

        self.tme_api = TMEV2Api(api_token, api_secret, language, location, currency)

    def search(self, search_term: str) -> tuple[list[ApiPart], int]:
        search_term = search_term.strip()
        if not search_term:
            return [], 0

        tme_part = self.tme_api.get_product(search_term)
        if tme_part:
            symbol = as_str(tme_part.get("symbol"))
            if not symbol:
                return [], 0

            tme_stocks = self.tme_api.get_prices_and_stocks([symbol])
            return [self.get_api_part(tme_part, tme_stocks.get(symbol, {}))], 1

        results = self.tme_api.product_search(search_term)
        if not results:
            return [], 0

        filtered_matches = [
            tme_part
            for tme_part in results
            if is_product_identifier_prefix_match(tme_part, search_term)
        ]

        exact_matches = [
            tme_part for tme_part in filtered_matches if is_exact_product_match(tme_part, search_term)
        ]
        if len(exact_matches) == 1:
            filtered_matches = exact_matches

        symbols = [as_str(tme_part.get("symbol")) for tme_part in filtered_matches]
        tme_stocks = self.tme_api.get_prices_and_stocks(symbols)

        return [
            self.get_api_part(tme_part, tme_stocks.get(as_str(tme_part.get("symbol")), {}))
            for tme_part in filtered_matches
        ], len(filtered_matches)

    def get_api_part(self, tme_part: dict[str, Any], tme_stock: dict[str, Any]):
        symbol = as_str(tme_part.get("symbol"))
        manufacturer = as_dict(tme_part.get("manufacturer"))
        category = as_dict(tme_part.get("category"))

        category_path: list[str] = []
        category_id = category.get("id")
        if isinstance(category_id, int | float | str):
            category_path = self.tme_api.get_category_path(category_id)

        api_part = ApiPart(
            description=as_str(tme_part.get("description")),
            image_url=get_primary_photo_url(tme_part),
            datasheet_url=None,
            supplier_link=get_supplier_link(symbol, self.tme_api.language),
            SKU=symbol,
            manufacturer=as_str(manufacturer.get("name")) or "TME",
            manufacturer_link="",
            MPN=get_mpn(tme_part) or symbol,
            quantity_available=as_number(tme_stock.get("stock_quantity")),
            packaging=get_packaging(tme_part),
            category_path=category_path,
            parameters={},
            price_breaks=get_price_breaks(tme_stock),
            currency=self.tme_api.currency,
        )

        api_part.finalize_hook = MethodType(self.finalize_hook, api_part)
        return api_part

    def finalize_hook(self, api_part: ApiPart):
        api_part.parameters.update(self.tme_api.get_parameters(api_part.SKU))

        if datasheet_url := self.tme_api.get_datasheet_url(api_part.SKU):
            api_part.datasheet_url = datasheet_url


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def as_number(value: Any, default: int | float = 0) -> int | float:
    return value if isinstance(value, int | float) else default


def normalize_document_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def is_same_part_identifier(left: str, right: str) -> bool:
    left = left.strip()
    right = right.strip()
    if not left or not right:
        return False

    return left.lower() == right.lower()


def is_part_identifier_prefix(value: str, prefix: str) -> bool:
    value = value.strip()
    prefix = prefix.strip()
    if not value or not prefix:
        return False

    return value.lower().startswith(prefix.lower())


def is_exact_product_match(product: dict[str, Any], search_term: str) -> bool:
    if is_same_part_identifier(as_str(product.get("symbol")), search_term):
        return True

    return any(
        is_same_part_identifier(as_str(manufacturer_symbol), search_term)
        for manufacturer_symbol in as_list(product.get("manufacturer_symbols"))
    )


def is_product_identifier_prefix_match(product: dict[str, Any], search_term: str) -> bool:
    if is_part_identifier_prefix(as_str(product.get("symbol")), search_term):
        return True

    return any(
        is_part_identifier_prefix(as_str(manufacturer_symbol), search_term)
        for manufacturer_symbol in as_list(product.get("manufacturer_symbols"))
    )


def fix_tme_url(url: str | None) -> str:
    if not url:
        return ""

    if url.startswith("//"):
        url = f"https:{url}"

    # fix supplier part url if language is set to czech (#15)
    return url.replace("tme.eu/cs/", "tme.eu/cz/", 1)


def get_supplier_link(symbol: str, language: str) -> str:
    symbol = symbol.strip()
    if not symbol:
        return ""

    language = language.lower() or "en"
    details_symbol = symbol.lower().replace("/", "_")
    return fix_tme_url(f"https://www.tme.eu/{language}/details/{quote(details_symbol, safe='')}/")


def get_mpn(product: dict[str, Any]) -> str:
    for manufacturer_symbol in as_list(product.get("manufacturer_symbols")):
        if mpn := as_str(manufacturer_symbol):
            return mpn

    return ""


def get_primary_photo_url(product: dict[str, Any]) -> str:
    primary_photo = as_dict(as_dict(product.get("assets")).get("primary_photo"))
    return fix_tme_url(
        as_str(primary_photo.get("prime"))
        or as_str(primary_photo.get("high_resolution"))
        or as_str(primary_photo.get("thumbnail"))
    )


def get_packaging(product: dict[str, Any]) -> str:
    packing_elements = as_list(as_dict(product.get("packing")).get("elements"))
    if not packing_elements:
        return ""

    first_packing = as_dict(packing_elements[0])
    packing_name = as_str(first_packing.get("translation")) or as_str(first_packing.get("id"))
    packing_amount = first_packing.get("amount")

    if packing_name and packing_amount:
        return f"{packing_name} {packing_amount}"

    return packing_name


def get_price_breaks(product_data: dict[str, Any]) -> dict[int | float, float]:
    prices = as_dict(product_data.get("prices"))
    tax_rate = float(as_number(as_dict(prices.get("tax")).get("rate"), 0))
    is_gross = prices.get("type") == "GROSS"

    price_breaks: dict[int | float, float] = {}

    for price_break in as_list(prices.get("elements")):
        price_break_data = as_dict(price_break)
        amount = price_break_data.get("amount")
        price = price_break_data.get("price")

        if not isinstance(amount, int | float) or not isinstance(price, int | float):
            continue

        price_value = float(price)
        if is_gross and tax_rate:
            price_value /= 1 + tax_rate / 100

        price_breaks[amount] = price_value

    return price_breaks


def get_document_url(document: dict[str, Any]) -> str:
    return fix_tme_url(as_str(document.get("url")))


def is_pdf_document(document: dict[str, Any]) -> bool:
    document_url = get_document_url(document).lower().split("?", 1)[0]
    file_name = as_str(document.get("file_name")).lower().split("?", 1)[0]

    return document_url.endswith(".pdf") or file_name.endswith(".pdf")


def get_identifier_prefixes(value: str, min_length: int = 5) -> list[str]:
    identifier = normalize_document_identifier(value)
    if len(identifier) < min_length:
        return []

    return [identifier[:length] for length in range(len(identifier), min_length - 1, -1)]


def get_document_symbol_match_length(document: dict[str, Any], product_symbol: str) -> int:
    document_text = normalize_document_identifier(
        " ".join(
            (
                as_str(document.get("file_name")),
                get_document_url(document),
            )
        )
    )

    for prefix in get_identifier_prefixes(product_symbol):
        if prefix in document_text:
            return len(prefix)

    return 0


def get_preferred_document_url(product_files: dict[str, Any], product_symbol: str = "") -> str:
    documents = as_dict(product_files.get("documents"))
    document_elements = [
        document
        for document in as_list(documents.get("elements"))
        if isinstance(document, dict) and get_document_url(document)
    ]
    preferred_types = ("DTE", "PDF", "DS", "DATASHEET", "LNK")

    if product_symbol:
        matched_documents = [
            (
                is_pdf_document(document),
                get_document_symbol_match_length(document, product_symbol),
                document,
            )
            for document in document_elements
        ]
        matched_documents = [
            matched_document
            for matched_document in matched_documents
            if matched_document[1] > 0
        ]

        if matched_documents:
            matched_documents.sort(key=lambda item: (item[0], item[1]), reverse=True)
            return get_document_url(matched_documents[0][2])

    for preferred_type in preferred_types:
        for document in document_elements:
            if document.get("type") == preferred_type:
                return get_document_url(document)

    return ""


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


class TMEV2Api:
    BASE_URL = "https://api.tme.eu/"

    def __init__(
        self,
        token: str,
        secret: str,
        language: str = "en",
        country: str = "PL",
        currency: str = "EUR",
    ):
        self._categories = None
        self.token = token
        self.secret = secret
        self.language = language.lower()
        self.country = country.upper()
        self.currency = currency.upper()

        self._access_token: str | None = None
        self._access_token_expires_at = 0.0

        self.session = retries.setup_session()

    def get_product(self, product_symbol: str) -> dict[str, Any]:
        product_symbol = product_symbol.strip()
        if not product_symbol:
            return {}

        endpoint = "products"

        for query_param in ("symbols[]", "mpns[]"):
            products = self._get_response_elements(
                endpoint,
                self._api_call(
                    "GET",
                    endpoint,
                    [
                        ("country", self.country),
                        (query_param, product_symbol),
                    ],
                ),
            )

            exact_matches = [product for product in products if is_exact_product_match(product, product_symbol)]
            if len(exact_matches) == 1:
                return exact_matches[0]

        search_products = self.product_search(product_symbol)
        exact_matches = [
            product for product in search_products if is_exact_product_match(product, product_symbol)
        ]
        if len(exact_matches) == 1:
            return exact_matches[0]

        return {}

    def product_search(self, search_term: str) -> list[dict[str, Any]]:
        search_term = search_term.strip()
        if not search_term:
            return []

        endpoint = "products/search"
        data = self._get_response_data(
            endpoint,
            self._api_call(
                "GET",
                endpoint,
                [
                    ("country", self.country),
                    ("scope[]", "products"),
                    ("phrase", search_term),
                    ("limit", 100),
                    ("page", 1),
                ],
            ),
        )

        products = as_dict(data.get("products"))
        return [element for element in as_list(products.get("elements")) if isinstance(element, dict)]

    @limit_frequency(0.6)
    def get_prices_and_stocks(self, product_symbols: list[str]) -> dict[str, dict[str, Any]]:
        symbols = [
            product_symbol.strip()
            for product_symbol in product_symbols
            if isinstance(product_symbol, str) and product_symbol.strip()
        ]

        if not symbols:
            return {}

        endpoint = "products/data"
        products_data: dict[str, dict[str, Any]] = {}

        for offset in range(0, len(symbols), 50):
            chunk = symbols[offset : offset + 50]

            params: list[tuple[str, Any]] = [
                ("country", self.country),
                ("currency", self.currency),
                ("scope[]", "prices"),
                ("scope[]", "stock"),
            ]
            params.extend(("symbols[]", symbol) for symbol in chunk)

            elements = self._get_response_elements(endpoint, self._api_call("GET", endpoint, params))

            for element in elements:
                symbol = as_str(element.get("symbol"))
                if symbol:
                    products_data[symbol] = element

        return products_data

    def get_category_path(self, category_id: int | float | str) -> list[str]:
        if self._categories is None:
            self._categories = {}

            for category in self.get_categories():
                raw_id = category.get("id")
                raw_parent_id = category.get("parent_id")
                if not isinstance(raw_id, int | float) or not isinstance(raw_parent_id, int | float):
                    continue

                self._categories[int(raw_id)] = (as_str(category.get("name")), int(raw_parent_id))

        try:
            parent_id = int(category_id)
        except (TypeError, ValueError):
            return []

        category_path: list[str] = []
        visited: set[int] = set()

        while parent_id in self._categories and parent_id not in visited:
            visited.add(parent_id)

            name, next_parent_id = self._categories[parent_id]
            if not name:
                break

            category_path.insert(0, name)

            if next_parent_id == parent_id:
                break

            parent_id = next_parent_id

        return category_path

    def get_categories(self) -> list[dict[str, Any]]:
        if not hasattr(self, "_category_list"):
            endpoint = "products/categories/list"
            self._category_list = self._get_response_elements(
                endpoint,
                self._api_call("GET", endpoint, [("country", self.country)]),
            )

        return self._category_list

    def get_parameters(self, product_symbol: str) -> dict[str, str]:
        product_symbol = product_symbol.strip()
        if not product_symbol:
            return {}

        endpoint = "products/parameters"
        elements = self._get_response_elements(
            endpoint,
            self._api_call(
                "GET",
                endpoint,
                [
                    ("country", self.country),
                    ("symbols[]", product_symbol),
                ],
            ),
        )

        product_parameters: dict[str, str] = {}

        for element in elements:
            if element.get("symbol") != product_symbol:
                continue

            parameters = as_dict(element.get("parameters"))
            for parameter in as_list(parameters.get("elements")):
                parameter_data = as_dict(parameter)
                name = as_str(parameter_data.get("name"))
                if not name:
                    continue

                value_parts = [
                    REMOVE_HTML_TAGS.sub("", raw_value)
                    for value in as_list(parameter_data.get("values"))
                    if (raw_value := as_str(as_dict(value).get("value")))
                ]

                if not value_parts:
                    continue

                value_text = ", ".join(value_parts)
                if existing_value := product_parameters.get(name):
                    value_text = ", ".join((existing_value, value_text))

                product_parameters[name] = value_text

        return product_parameters

    def get_datasheet_url(self, product_symbol: str) -> str:
        return get_preferred_document_url(self.get_product_files(product_symbol), product_symbol)

    def get_product_files(self, product_symbol: str) -> dict[str, Any]:
        product_symbol = product_symbol.strip()
        if not product_symbol:
            return {}

        endpoint = "products/files"
        elements = self._get_response_elements(
            endpoint,
            self._api_call(
                "GET",
                endpoint,
                [
                    ("country", self.country),
                    ("symbols[]", product_symbol),
                ],
            ),
        )

        for element in elements:
            if element.get("symbol") == product_symbol:
                return element

        return elements[0] if elements else {}

    def get_countries(self) -> list[dict[str, Any]]:
        if not hasattr(self, "_countries"):
            endpoint = "utils/countries"
            elements = self._get_response_elements(endpoint, self._api_call("GET", endpoint))

            self._countries = [
                {
                    "CountryId": country_id.upper(),
                    "Name": as_str(element.get("name")),
                }
                for element in elements
                if (country_id := as_str(element.get("id")))
            ]

        return self._countries

    @cache
    def get_currencies(self, country: str) -> list[str]:
        endpoint = "utils/currencies"
        data = self._get_response_data(
            endpoint,
            self._api_call("GET", endpoint, {"country": country.upper()}),
        )

        currencies = data.get("currencies")
        if not isinstance(currencies, list):
            raise SupplierError("TMEV2", f"{endpoint} returned invalid currencies payload: {data}")

        return [currency.upper() for currency in currencies if isinstance(currency, str)]

    @cache
    def get_languages(self) -> list[str]:
        endpoint = "utils/languages"
        data = self._get_response_data(endpoint, self._api_call("GET", endpoint))

        languages = data.get("languages")
        if not isinstance(languages, list):
            raise SupplierError("TMEV2", f"{endpoint} returned invalid languages payload: {data}")

        return [language.lower() for language in languages if isinstance(language, str)]

    def _get_response_data(self, endpoint: str, response: dict[str, Any]) -> dict[str, Any]:
        if response.get("status") != "OK":
            raise SupplierError("TMEV2", f"{endpoint} returned non-OK status: {response}")

        data = response.get("data")
        if not isinstance(data, dict):
            raise SupplierError("TMEV2", f"{endpoint} returned invalid data payload: {response}")

        return data

    def _get_response_elements(self, endpoint: str, response: dict[str, Any]) -> list[dict[str, Any]]:
        data = self._get_response_data(endpoint, response)

        elements = data.get("elements")
        if not isinstance(elements, list):
            raise SupplierError("TMEV2", f"{endpoint} returned invalid elements payload: {data}")

        return [element for element in elements if isinstance(element, dict)]

    def _get_access_token(self) -> str:
        if self._access_token and time() < self._access_token_expires_at - 60:
            return self._access_token

        result = self.session.post(
            f"{self.BASE_URL}auth/token",
            auth=(self.token, self.secret),
            data={"grant_type": "client_credentials"},
            headers={"Accept": "application/json"},
            timeout=30,
        )

        if not result.content:
            raise SupplierError(
                "TMEV2", f"Token request failed with code {result.status_code} (no content)"
            )

        try:
            content_json: dict[str, Any] = result.json()
        except JSONDecodeError as e:
            raise SupplierError("TMEV2", str(e))

        if result.status_code != 200:
            raise SupplierError(
                "TMEV2",
                content_json.get("error_description")
                or content_json.get("error")
                or content_json.get("message")
                or str(content_json),
            )

        access_token = content_json.get("access_token")
        if not access_token:
            raise SupplierError("TMEV2", f"Token response did not contain access_token: {content_json}")

        self._access_token = access_token
        self._access_token_expires_at = time() + int(content_json.get("expires_in", 3600))

        return access_token

    def _api_call(
        self,
        method: str,
        endpoint: str,
        params: QueryParams = None,
    ) -> dict[str, Any]:
        url = f"{self.BASE_URL}{endpoint.lstrip('/')}"
        headers = {
            "Accept": "application/json",
            "Accept-Language": self.language,
            "Authorization": f"Bearer {self._get_access_token()}",
        }

        result = self.session.request(method, url, params=params, headers=headers, timeout=30)

        if result.status_code == 401:
            self._access_token = None
            headers["Authorization"] = f"Bearer {self._get_access_token()}"
            result = self.session.request(method, url, params=params, headers=headers, timeout=30)

        if not result.content:
            raise SupplierError(
                "TMEV2",
                f"Request to {endpoint} failed with code {result.status_code} (no content)",
            )

        try:
            content_json: dict[str, Any] = result.json()
        except JSONDecodeError as e:
            raise SupplierError("TMEV2", str(e))

        if result.status_code != 200:
            raise SupplierError(
                "TMEV2",
                content_json.get("error_description")
                or content_json.get("error")
                or content_json.get("message")
                or str(content_json),
            )

        return content_json
