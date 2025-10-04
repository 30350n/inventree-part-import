import re

from error_helper import warning

from .base import REMOVE_HTML_TAGS, ApiPart, ScrapeSupplier, SupplierSupportLevel

API_BASE_URL = "https://wmsc.lcsc.com/ftps/wm/"
CURRENCY_URL = "https://wmsc.lcsc.com/wmsc/home/currency?currencyCode={}"
SEARCH_URL = f"{API_BASE_URL}search/global?keyword={{}}"
PRODUCT_INFO_URL = f"{API_BASE_URL}product/detail?productCode={{}}"


class LCSC(ScrapeSupplier):
    SUPPORT_LEVEL = SupplierSupportLevel.INOFFICIAL_API

    def setup(self, *, currency, browser_cookies="", ignore_duplicates=True, **kwargs):
        if currency not in CURRENCY_MAP.values():
            return self.load_error(f"unsupported currency '{currency}'")

        self.currency = currency
        self.ignore_duplicates = ignore_duplicates

        if browser_cookies:
            self.cookies_from_browser(browser_cookies, "lcsc.com")

        return True

    def search(self, search_term):
        for _ in range(3):
            search_result = self.scrape(SEARCH_URL.format(search_term))
            if search_result and (result := search_result.json().get("result")):
                break
        else:
            warning("failed to search part at LCSC (internal API error)")
            return [], 0

        if product_detail := result.get("tipProductDetailUrlVO"):
            url = PRODUCT_INFO_URL.format(product_detail["productCode"])
            for _ in range(3):
                detail_request = self.scrape(url)
                if detail_request and (detail_result := detail_request.json().get("result")):
                    return [self.get_api_part(detail_result)], 1
            warning("failed to retrieve product data from LCSC (internal API error)")
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

    def get_api_part(self, lcsc_part):
        if not (description := lcsc_part.get("productDescEn")):
            description = lcsc_part.get("productIntroEn")
        description = description.strip() if description else ""

        image_url = lcsc_part.get("productImageUrlBig", lcsc_part.get("productImageUrl"))
        if not image_url and (image_urls := lcsc_part.get("productImages")):
            for image_url in reversed(image_urls):
                if "front" in image_url:
                    break

        datasheet_url = lcsc_part.get("pdfUrl").replace(
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

        category_path = []
        if parent := lcsc_part.get("parentCatalogName"):
            category_path.append(parent)
        if category := lcsc_part.get("catalogName"):
            category_path.append(category)

        parameters = {}
        if lcsc_parameters := lcsc_part.get("paramVOList"):
            parameters = {
                parameter.get("paramNameEn"): parameter.get("paramValueEn")
                for parameter in lcsc_parameters
            }

        if package := lcsc_part.get("encapStandard"):
            parameters["Package Type"] = package

        price_list = lcsc_part.get("productPriceList", [])
        price_breaks = {
            price_break.get("ladder"): price_break.get("currencyPrice")
            for price_break in price_list
        }

        if price_list:
            currency = CURRENCY_MAP.get(price_list[0].get("currencySymbol")) or self.currency
        else:
            currency = self.currency

        return ApiPart(
            description=REMOVE_HTML_TAGS.sub("", description),
            image_url=image_url,
            datasheet_url=datasheet_url,
            supplier_link=supplier_link,
            SKU=lcsc_part.get("productCode", ""),
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

    def setup_hook(self):
        self.session.get(CURRENCY_URL.format(self.currency), timeout=self.request_timeout)


CLEANUP_URL_ID_REGEX = re.compile(r"[^\w\d\.]")


def cleanup_url_id(url):
    url = url.replace(" / ", "_")
    url = CLEANUP_URL_ID_REGEX.sub("_", url)
    return url


CURRENCY_MAP = {
    "$": "USD",
    "€": "EUR",
    "¥": "CNY",
    "HK$": "HKD",
}
