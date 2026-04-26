import re
from types import MethodType
from typing import Any

from fake_useragent import UserAgent
from requests.compat import quote
from requests.exceptions import JSONDecodeError

from .. import retries
from ..exceptions import SupplierError
from .base import REMOVE_HTML_TAGS, ApiPart, Supplier, SupplierSupportLevel


class LCSC(Supplier):
    SUPPORT_LEVEL = SupplierSupportLevel.INOFFICIAL_API

    def setup(self, *, currency: str, ignore_duplicates: bool = True, **kwargs: Any):
        if currency not in CURRENCY_MAP.values():
            self.load_error(f"unsupported currency '{currency}'")

        self.currency = currency
        self.ignore_duplicates = ignore_duplicates

        self.lcsc_api = LCSCApi(self.currency)

    def search(self, search_term: str) -> tuple[list[ApiPart], int]:
        if not (result := self.lcsc_api.search(search_term)):
            return [], 0

        if product_detail := result.get("tipProductDetailUrlVO"):
            if detail_result := self.lcsc_api.product_detail(product_detail["productCode"]):
                return [self.get_api_part(detail_result)], 1

        elif products := result.get("productSearchResultVO"):
            filtered_matches = [
                product
                for product in products["productList"]
                if product["productModel"].lower().startswith(search_term.lower())
                or product["productCode"].lower() == search_term.lower()
            ]

            exact_matches = [
                product
                for product in filtered_matches
                if product["productModel"].lower() == search_term.lower()
                or product["productCode"].lower() == search_term.lower()
            ]
            if self.ignore_duplicates:
                exact_filtered = [
                    product
                    for product in exact_matches
                    if product.get("stockNumber")
                    or product.get("productImageUrlBig")
                    or product.get("productImageUrl")
                    or product.get("productImages")
                ]
                exact_matches = exact_filtered if exact_filtered else exact_matches

            if len(exact_matches) == 1:
                return [self.get_api_part(exact_matches[0])], 1

            return list(map(self.get_api_part, filtered_matches)), len(filtered_matches)

        return [], 0

    def get_api_part(self, lcsc_part: dict[str, Any]):
        if not (description := lcsc_part.get("productDescEn")):
            description = lcsc_part.get("productIntroEn")
        description = description.strip() if description else ""

        image_url = lcsc_part.get("productImageUrlBig", lcsc_part.get("productImageUrl"))
        if not image_url and (image_urls := lcsc_part.get("productImages")):
            for image_url in reversed(image_urls):
                if "front" in image_url:
                    break

        datasheet_url = lcsc_part["pdfUrl"].replace(
            "//datasheet.lcsc.com/", "//wmsc.lcsc.com/wmsc/upload/file/pdf/v2/"
        )

        if url := lcsc_part.get("url"):
            url_separator = "/product-detail/"
            prefix, product_url_id = url.split(url_separator)
            product_url_id = product_url_id
            supplier_link = url_separator.join((prefix, cleanup_url_id(product_url_id)))
        else:
            product_url_id = cleanup_url_id(
                "_".join((lcsc_part["catalogName"], lcsc_part["title"], lcsc_part["productCode"]))
            )
            supplier_link = f"https://www.lcsc.com/product-detail/{product_url_id}.html"

        product_arrange = lcsc_part.get("productArrange")
        packaging = REMOVE_HTML_TAGS.sub("", product_arrange) if product_arrange else ""

        category_path: list[str] = []
        if parent := lcsc_part.get("parentCatalogName"):
            category_path.append(parent)
        if category := lcsc_part.get("catalogName"):
            category_path.append(category)

        finalize = False
        parameters = {}
        if lcsc_parameters := lcsc_part.get("paramVOList"):
            parameters = {
                parameter.get("paramNameEn"): parameter.get("paramValueEn")
                for parameter in lcsc_parameters
            }
        else:
            finalize = True

        if package := lcsc_part.get("encapStandard"):
            parameters["Package Type"] = package

        price_list = lcsc_part["productPriceList"]
        price_breaks = {
            price_break.get("ladder"): price_break.get("currencyPrice")
            for price_break in price_list
        }
        currency = CURRENCY_MAP.get(price_list[0].get("currencySymbol")) or self.currency

        api_part = ApiPart(
            description=REMOVE_HTML_TAGS.sub("", description),
            image_url=image_url,
            datasheet_url=datasheet_url,
            supplier_link=supplier_link,
            SKU=lcsc_part["productCode"],
            manufacturer=REMOVE_HTML_TAGS.sub("", lcsc_part.get("brandNameEn", "")),
            manufacturer_link="",
            MPN=lcsc_part.get("productModel", ""),
            quantity_available=float(lcsc_part.get("stockNumber", 0)),
            packaging=packaging,
            category_path=category_path,
            parameters=parameters,
            price_breaks=price_breaks,
            currency=currency,
        )

        if finalize:
            api_part.finalize_hook = MethodType(self.finalize_hook, api_part)

        return api_part

    def finalize_hook(self, api_part: ApiPart):
        api_part.parameters |= {
            parameter.get("paramNameEn"): parameter.get("paramValueEn")
            for parameter in self.lcsc_api.product_detail(api_part.SKU)["paramVOList"]
        }


class LCSCApi:
    API_BASE_URL = "https://wmsc.lcsc.com/ftps/wm/"
    SEARCH_URL = f"{API_BASE_URL}search/v2/global"
    PRODUCT_INFO_URL = f"{API_BASE_URL}product/detail?productCode={{}}"
    CURRENCY_URL = "https://wmsc.lcsc.com/wmsc/home/currency?currencyCode={}"

    def __init__(self, currency: str):
        self.session = retries.setup_session()
        self.session.headers.update(
            {"User-Agent": UserAgent(os=["iOS"]).random, "Accept-Language": "en-US,en"}
        )
        self.session.get(self.CURRENCY_URL.format(currency))

    def search(self, keyword: str):
        return self._api_call(self.SEARCH_URL, json={"keyword": keyword})

    def product_detail(self, product_code: str):
        return self._api_call(self.PRODUCT_INFO_URL.format(quote(product_code, safe="")))

    def _api_call(self, url: str, json: dict[str, Any] | None = None):
        result = self.session.get(url) if json is None else self.session.post(url, json=json)

        if not result.content:
            raise SupplierError(
                "LCSC", f"Request failed with code {result.status_code} (no content)"
            )

        try:
            content_json: dict[str, Any] = result.json()
        except JSONDecodeError as e:
            raise SupplierError("LCSC", str(e))

        if result.status_code != 200:
            if message := content_json.get("msg"):
                raise SupplierError("LCSC", message)
            else:
                raise SupplierError("LCSC", "Unknown error")

        return content_json["result"]


CLEANUP_URL_ID_REGEX = re.compile(r"[^\w\d\.]")


def cleanup_url_id(url: str):
    url = url.replace(" / ", "_")
    url = CLEANUP_URL_ID_REGEX.sub("_", url)
    return url


CURRENCY_MAP = {
    "$": "USD",
    "€": "EUR",
    "¥": "CNY",
    "HK$": "HKD",
}
