import re
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from functools import cache
from hashlib import sha256

import requests
from error_helper import info, warning
from inventree.api import InvenTreeAPI
from inventree.base import ImageMixin, InventreeObject
from inventree.company import Company as InventreeCompany
from inventree.company import ManufacturerPart, SupplierPart
from inventree.part import ParameterTemplate, Part, PartCategory
from platformdirs import user_cache_path
from requests.compat import unquote, urlparse
from requests.exceptions import ConnectionError, HTTPError, Timeout

from .retries import retry_timeouts

INVENTREE_CACHE = user_cache_path(__package__, ensure_exists=True) / "inventree"
INVENTREE_CACHE.mkdir(parents=True, exist_ok=True)

def get_supplier_part(inventree_api: InvenTreeAPI, company: InventreeCompany, sku):
    supplier_parts = SupplierPart.list(inventree_api, SKU=sku)
    company_supplier_parts = [part for part in supplier_parts if part.supplier == company.pk]
    if len(company_supplier_parts) == 1:
        return company_supplier_parts[0]

    assert len(company_supplier_parts) == 0
    return None

def get_manufacturer_part(inventree_api: InvenTreeAPI, mpn):
    manufacturer_parts = ManufacturerPart.list(inventree_api, MPN=mpn)
    if len(manufacturer_parts) == 1:
        return manufacturer_parts[0]

    assert len(manufacturer_parts) == 0
    return None

def get_part(inventree_api: InvenTreeAPI, name):
    name_sanitized = FILTER_SPECIAL_CHARS_REGEX.sub(FILTER_SPECIAL_CHARS_SUB, name)
    parts = Part.list(inventree_api, name_regex=f"^{name_sanitized}$")
    if len(parts) == 1:
        return parts[0]

    assert len(parts) == 0
    return None

def get_category(inventree_api: InvenTreeAPI, category_path):
    name = category_path.split("/")[-1]
    for category in PartCategory.list(inventree_api, search=name):
        if category.pathstring == category_path:
            return category

    return None

def get_category_parts(part_category: PartCategory, cascade):
    return Part.list(
        part_category._api,
        category=part_category.pk,
        cascade=cascade,
        purchaseable=True,
    )

FILTER_SPECIAL_CHARS_REGEX = re.compile(r"(?<!\\)([\[\].^$*+?{}|()])")
FILTER_SPECIAL_CHARS_SUB = r"\\\g<1>"

def update_object_data(obj: InventreeObject, data: dict, info_label=""):
    for name, value in data.items():
        try:
            if value == type(value)(obj[name]):
                continue
        except TypeError:
            pass

        if info_label:
            info(f"updating {info_label} ...")
        obj.save(data)
        return

@cache
def get_parameter_templates(inventree_api: InvenTreeAPI):
    return {
        parameter_template.name: parameter_template
        for parameter_template in ParameterTemplate.list(inventree_api)
    }

@cache
def create_manufacturer(inventree_api: InvenTreeAPI, name):
    manufacturers = [
        manufacturer for manufacturer in InventreeCompany.list(inventree_api, search=name)
        if name.lower() == manufacturer.name.lower()
    ]
    if len(manufacturers) == 1:
        manufacturer = manufacturers[0]
        if not manufacturer.is_manufacturer:
            manufacturer.save({"is_manufacturer": True})
        return manufacturer
    assert len(manufacturers) == 0

    info(f"creating manufacturer '{name}' ...")
    return InventreeCompany.create(inventree_api, {
        "name": name,
        "description": name,
        "is_manufacturer": True,
        "is_supplier": False,
        "is_customer": False,
    })

def download_image_content(api_object: ImageMixin):
    if not api_object.image:
        return b""

    api_image_path = INVENTREE_CACHE / f"api_image.{api_object.image.rsplit('.')[-1]}"
    api_image_path.unlink(missing_ok=True)
    api_object.downloadImage(str(api_image_path))

    with open(api_image_path, "rb") as file:
        return file.read()

def upload_image(api_object: ImageMixin, image_url: str):
    info("uploading image ...")
    image_content, redirected_url = _download_file_content(image_url)
    if not image_content:
        warning(f"failed to download image from '{image_url}'")
        return

    file_extension = url2filename(redirected_url).split(".")[-1]
    if not file_extension.isalnum():
        warning(f"failed to get file extension for image from '{image_url}'")
        return

    image_hash = urlsafe_b64encode(sha256(image_content).digest()).decode()
    image_path = INVENTREE_CACHE / f"{image_hash}.{file_extension}"
    image_path.write_bytes(image_content)

    try:
        api_object.uploadImage(str(image_path))
    except HTTPError as e:
        warning(f"failed to upload image with: {e.args[0]['body']}")

def upload_datasheet(part: Part, datasheet_url: str):
    info("uploading datasheet ...")
    datasheet_content, redirected_url = _download_file_content(datasheet_url)
    if not datasheet_content:
        warning(f"failed to download datasheet from '{datasheet_url}'")
        return

    file_name = url2filename(redirected_url)
    file_extension = file_name.split(".")[-1]
    if file_extension.upper() not in {"PDF"}:
        warning(f"datasheet '{datasheet_url}' has invalid file extension '{file_extension}'")
        return

    datasheet_path = INVENTREE_CACHE / file_name
    datasheet_path.write_bytes(datasheet_content)

    try:
        part.uploadAttachment(str(datasheet_path), "datasheet")
    except HTTPError as e:
        warning(f"failed to upload datasheet with: {e.args[0]['body']}")

def url2filename(url):
    parsed = urlparse(url)
    if "." not in parsed.path:
        parsed = urlparse(url.replace("https://", "scheme://"))
    return unquote(parsed.path.split("/")[-1])

DOWNLOAD_HEADERS = {"User-Agent": "Mozilla/5.0"}

from ssl import PROTOCOL_TLSv1_2

class TLSv1_2HTTPAdapter(requests.adapters.HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False):
        self.poolmanager = requests.packages.urllib3.poolmanager.PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            ssl_version=PROTOCOL_TLSv1_2,
        )

@cache
def _download_file_content(url):
    session = requests.Session()
    session.mount("https://", TLSv1_2HTTPAdapter())

    try:
        for retry in retry_timeouts():
            with retry:
                result = session.get(url, headers=DOWNLOAD_HEADERS)
                result.raise_for_status()
    except (ConnectionError, HTTPError, Timeout) as e:
        warning(f"failed to download file with '{e}'")
        return None, None

    return result.content, result.url

@dataclass
class Company:
    name: str
    currency: str = None
    is_supplier: bool = False
    is_manufacturer: bool = False
    is_customer: bool = False
    primary_key: int = None

    def setup(self, inventree_api):
        api_company = None
        if self.primary_key is not None:
            try:
                api_company = InventreeCompany(inventree_api, self.primary_key)
            except HTTPError as e:
                if not e.args or e.args[0].get("status_code") != 404:
                    raise e

        if not api_company:
            api_companies = InventreeCompany.list(inventree_api, name=self.name)
            if len(api_companies) == 1:
                api_company = api_companies[0]

        if api_company:
            if self.name != api_company.name:
                info(f"updating name for '{api_company.name}' ...")
                api_company.save({"name": self.name})

            if self.currency != api_company.currency:
                info(f"updating currency for '{self.name}' ...")
                api_company.save({"currency": self.currency})

            return api_company

        info(f"creating supplier '{self.name}' ...")
        return InventreeCompany.create(inventree_api, {
            "name": self.name,
            "currency": self.currency,
            "is_supplier": self.is_supplier,
            "is_manufacturer": self.is_manufacturer,
            "is_customer": self.is_customer,
        })
