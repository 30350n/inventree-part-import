import json, re, traceback
from enum import Enum
from multiprocessing.pool import ThreadPool
from string import Formatter, _string

from cutie import select
from error_helper import BOLD, BOLD_END, error, hint, info, prompt, prompt_input, success, warning
from inventree.company import Company, ManufacturerPart, SupplierPart, SupplierPriceBreak
from inventree.part import Parameter, Part
from requests.compat import quote
from requests.exceptions import HTTPError
from thefuzz import fuzz

from .categories import setup_categories_and_parameters
from .config import CATEGORIES_CONFIG, CONFIG, get_config, get_pre_creation_hooks
from .inventree_helpers import (create_manufacturer, get_manufacturer_part,
                                get_parameter_templates, get_part, get_supplier_part,
                                update_object_data, upload_datasheet, upload_image)
from .suppliers import search
from .suppliers.base import ApiPart

class ImportResult(Enum):
    ERROR = 0
    FAILURE = 1
    INCOMPLETE = 2
    SUCCESS = 3

    def __or__(self, other):
        return self if self.value < other.value else other

class PartImporter:
    def __init__(self, inventree_api, interactive=False, verbose=False):
        self.api = inventree_api
        self.interactive = interactive
        self.verbose = verbose
        self.dry_run = hasattr(inventree_api, "DRY_RUN")

        # preload pre_creation_hooks
        get_pre_creation_hooks()

        self.category_map, self.parameter_map = setup_categories_and_parameters(self.api)
        self.parameter_templates = get_parameter_templates(self.api)

        self.part_category_to_category = {
            category.part_category.pk: category
            for category in self.category_map.values()
        }
        self.categories = set(self.category_map.values())

    def import_part(
            self,
            search_term,
            existing_part: Part = None,
            supplier_id=None,
            only_supplier=False
        ):
        info(f"searching for {search_term} ...", end="\n")
        import_result = ImportResult.SUCCESS

        self.existing_manufacturer_part = None
        search_results = search(search_term, supplier_id, only_supplier)
        for supplier, async_results in search_results:
            info(f"searching at {supplier.name} ...")
            results, result_count = async_results.get()

            if not results:
                hint(f"no results at {supplier.name}")
                continue

            if len(results) == 1:
                api_part = results[0]
            elif self.interactive:
                prompt(f"found multiple parts at {supplier.name}, select which one to import")
                results = results[:get_config()["interactive_part_matches"]]
                if result_count > len(results):
                    hint(f"found {result_count} results, only showing the first {len(results)}")
                if not (api_part := self.select_api_part(results)):
                    import_result |= ImportResult.INCOMPLETE
                    continue
            else:
                warning(f"found {result_count} parts at {supplier.name}, skipping import")
                import_result |= ImportResult.INCOMPLETE
                continue

            try:
                import_result |= self.import_supplier_part(supplier, api_part, existing_part)
            except HTTPError as e:
                import_result = ImportResult.ERROR

                error_str = "'unknown HTTPError'"
                if e.args and isinstance(e.args[0], dict) and (body := e.args[0].get("body")):
                    try:
                        error_str = "\n" + "\n".join((
                            f"    {key}: {value}\n" for key, value in json.loads(body).items()
                        ))
                    except json.JSONDecodeError:
                        pass
                error(f"failed to import part with: {error_str}")

                if self.verbose:
                    error(traceback.format_exc(), prefix="FULL TRACEBACK:\n")

            if import_result == ImportResult.ERROR:
                # let the other api calls finish
                for _, other_results in search_results:
                    other_results.wait()
                return ImportResult.ERROR

        if not self.existing_manufacturer_part:
            import_result |= ImportResult.FAILURE

        return import_result

    @staticmethod
    def select_api_part(api_parts: list[ApiPart]):
        format_str = str(get_config().get(
            "part_selection_format", "{MPN} | {manufacturer} | {SKU} | {supplier_link}"
        ))
        fields = [
            parsed[1] for parsed in Formatter().parse(format_str)
            if GET_FORMATSTR_FIELD.sub("", parsed[1]) in ApiPart.__dataclass_fields__
        ]

        formatter = SafeFormatter()
        api_part_values = [
            [formatter.format(f"{{{field}}}", **api_part.__dict__) for api_part in api_parts]
            for field in fields
        ]
        max_lengths = [max(len(value) for value in values) for values in api_part_values]
        api_part_format_kwargs = [
            {
                GET_FORMATSTR_FIELD.sub("", field): value.ljust(max_length)
                for field, value, max_length in zip(fields, values, max_lengths)
            }
            for values in zip(*api_part_values)
        ]

        format_str = SIMPLIFY_FORMATSTR.sub(SIMPLIFY_FORMATSTR_SUB, format_str)
        choices = [formatter.format(format_str, **kwargs) for kwargs in api_part_format_kwargs]
        choices.append(f"{BOLD}Skip ...{BOLD_END}")

        index = select(choices, deselected_prefix="  ", selected_prefix="> ")
        return [*api_parts, None][index]

    def import_supplier_part(self, supplier: Company, api_part: ApiPart, part: Part = None):
        import_result = ImportResult.SUCCESS

        if supplier_part := get_supplier_part(self.api, supplier, api_part.SKU):
            info(f"found existing {supplier.name} part {supplier_part.SKU} ...")
        else:
            info(f"importing {supplier.name} part {api_part.SKU} ...")

        if supplier_part and supplier_part.manufacturer_part is not None:
            manufacturer_part = ManufacturerPart(self.api, supplier_part.manufacturer_part)
        elif manufacturer_part := get_manufacturer_part(self.api, api_part.MPN):
            pass
        elif self.existing_manufacturer_part:
            manufacturer_part = self.existing_manufacturer_part
        else:
            if not api_part.finalize():
                return ImportResult.FAILURE
            result = self.create_manufacturer_part(api_part, part)
            if isinstance(result, ImportResult):
                return result
            manufacturer_part, part = result

        update_part = (
            not self.existing_manufacturer_part
            or self.existing_manufacturer_part.pk != manufacturer_part.pk
        )
        if not self.dry_run:
            if not part:
                part = Part(self.api, manufacturer_part.part)
            elif part.pk != manufacturer_part.part:
                update_object_data(manufacturer_part, {"part": part.pk})

            if update_part:
                if not api_part.finalize():
                    return ImportResult.FAILURE
                update_object_data(part, api_part.get_part_data(), f"part {api_part.MPN}")

            if not part.image and api_part.image_url:
                upload_image(part, api_part.image_url)

            attachment_types = {attachment.comment for attachment in part.getAttachments()}
            if "datasheet" not in attachment_types and api_part.datasheet_url:
                match get_config().get("datasheets"):
                    case "upload":
                        upload_datasheet(part, api_part.datasheet_url)
                    case "link":
                        datasheet_url_safe = quote(api_part.datasheet_url, safe=":/")
                        part.addLinkAttachment(datasheet_url_safe[:200], comment="datasheet")
                    case None | False:
                        pass
                    case invalid_mode:
                        warning(f"invalid value 'datasheets: {invalid_mode}' in {CONFIG}")

        if api_part.parameters:
            result = self.setup_parameters(part, api_part, update_part)
            import_result |= result

        self.existing_manufacturer_part = manufacturer_part

        supplier_part_data = {
            "part": 0 if self.dry_run else part.pk,
            "manufacturer_part": manufacturer_part.pk,
            "supplier": supplier.pk,
            "SKU": api_part.SKU,
            **api_part.get_supplier_part_data(),
        }
        if supplier_part:
            action_str = "updated"
            update_object_data(supplier_part, supplier_part_data, f"{supplier.name} part")
        else:
            action_str = "added"
            supplier_part = SupplierPart.create(self.api, supplier_part_data)

        self.setup_price_breaks(supplier_part, api_part)

        url = f"{self.api.base_url}supplier-part/{supplier_part.pk}/"
        success(f"{action_str} {supplier.name} part {supplier_part.SKU} ({url})")
        return import_result

    def create_manufacturer_part(
        self,
        api_part: ApiPart,
        part: Part = None,
    ) -> tuple[ManufacturerPart, Part]:
        part_data = api_part.get_part_data()
        if part or (part := get_part(self.api, api_part.MPN)):
            update_object_data(part, part_data, f"part {api_part.MPN}")
        else:
            for subcategory in reversed(api_part.category_path):
                if category := self.category_map.get(subcategory.lower()):
                    break
            else:
                path_str = f" {BOLD}/{BOLD_END} ".join(api_part.category_path)
                if not self.interactive:
                    error(f"failed to match category for '{path_str}'")
                    return ImportResult.FAILURE

                prompt(f"failed to match category for '{path_str}', select category")
                if not (category := self.select_category(api_part.category_path)):
                    return ImportResult.FAILURE

                category.add_alias(api_part.category_path[-1])
                self.category_map[api_part.category_path[-1].lower()] = category

            info(f"creating part {api_part.MPN} in '{category.part_category.pathstring}' ...")
            part = Part.create(self.api, {"category": category.part_category.pk, **part_data})

        manufacturer = create_manufacturer(self.api, api_part.manufacturer)
        info(f"creating manufacturer part {api_part.MPN} ...")
        manufacturer_part = ManufacturerPart.create(self.api, {
            "part": part.pk,
            "manufacturer": manufacturer.pk,
            **api_part.get_manufacturer_part_data(),
        })

        return manufacturer_part, part

    def select_category(self, category_path):
        search_terms = [category_path[-1], " ".join(category_path[-2:])]

        def rate_category(category):
            return max(
                fuzz.ratio(term, name)
                for name in (category.name, " ".join(category.path[-2:]))
                for term in search_terms
            )
        category_matches = sorted(self.categories, key=rate_category, reverse=True)

        max_matches = int(get_config().get("interactive_category_matches", 5))
        N_MATCHES = min(max_matches, len(category_matches))
        choices = (
            *(" / ".join(category.path) for category in category_matches[:N_MATCHES]),
            f"{BOLD}Enter Manually ...{BOLD_END}",
            f"{BOLD}Skip ...{BOLD_END}"
        )
        while True:
            index = select(choices, deselected_prefix="  ", selected_prefix="> ")
            if index == N_MATCHES + 1:
                return None
            elif index < N_MATCHES:
                return category_matches[index]

            name = prompt_input("category name")
            if (category := self.category_map.get(name.lower())) and category.name == name:
                return category
            warning(f"category '{name}' does not exist")
            prompt("select category")

    def setup_price_breaks(self, supplier_part, api_part: ApiPart):
        price_breaks = {
            price_break.quantity: price_break
            for price_break in SupplierPriceBreak.list(self.api, part=supplier_part.pk)
        }

        updated_pricing = False
        for quantity, price in api_part.price_breaks.items():
            if price_break := price_breaks.get(quantity):
                if price == float(price_break.price):
                    continue
                price_break.save({"price": price, "price_currency": api_part.currency})
                updated_pricing = True
            else:
                SupplierPriceBreak.create(self.api, {
                    "part": supplier_part.pk,
                    "quantity": quantity,
                    "price": price,
                    "price_currency": api_part.currency,
                })
                updated_pricing = True

        if updated_pricing:
            info("updating price breaks ...")

    def setup_parameters(self, part, api_part: ApiPart, update_existing=True):
        import_result = ImportResult.SUCCESS

        if self.dry_run and not part:
            return import_result

        if not (category := self.part_category_to_category.get(part.category)):
            name = part.getCategory().pathstring
            error(f"category '{name}' is not defined in {CATEGORIES_CONFIG}")
            return ImportResult.FAILURE

        existing_parameters = {
            parameter.template_detail["name"]: parameter
            for parameter in Parameter.list(self.api, part=part.pk)
        }

        matched_parameters = {}
        for api_part_parameter, value in api_part.parameters.items():
            for parameter in self.parameter_map.get(api_part_parameter.lower(), []):
                name = parameter.name
                if name in category.parameters and name not in matched_parameters:
                    matched_parameters[name] = value

        already_set_parameters = {
            name for name, parameter in existing_parameters.items() if parameter.data}
        unassigned_parameters = (
            set(category.parameters) - set(matched_parameters) - already_set_parameters)

        if unassigned_parameters and self.interactive:
            prompt(f"failed to match some parameters from '{api_part.supplier_link}'", end="\n")
            for parameter_name in unassigned_parameters.copy():
                prompt(
                    f"failed to match value for parameter '{parameter_name}', select parameter"
                )
                alias, value = self.select_parameter(parameter_name, api_part.parameters)
                if value is None:
                    continue
                matched_parameters[parameter_name] = value
                unassigned_parameters.remove(parameter_name)

                if not alias:
                    continue

                params = self.parameter_map.get(parameter_name.lower())
                if not params or len(params) != 1:
                    warning(f"failed to add alias '{alias}' for parameter '{parameter_name}'")
                    continue
                parameter = params[0]

                parameter.add_alias(alias)
                if existing := self.parameter_map.get(alias.lower()):
                    existing.append(parameter)
                else:
                    self.parameter_map[alias.lower()] = [parameter]

        thread_pool = ThreadPool(4)
        async_results = []
        for name, value in matched_parameters.items():
            if not (value := sanitize_parameter_value(value)):
                continue

            if existing_parameter := existing_parameters.get(name):
                if update_existing and existing_parameter.data != value:
                    async_results.append(thread_pool.apply_async(
                        update_parameter, (existing_parameter, value)
                    ))
            else:
                if parameter_template := self.parameter_templates.get(name):
                    async_results.append(thread_pool.apply_async(
                        create_parameter, (self.api, part, parameter_template, value)
                    ))
                elif not self.dry_run:
                    warning(f"failed to find template parameter for '{name}'")
                    import_result |= ImportResult.INCOMPLETE

        if async_results:
            info("updating part parameters ...")

        for result in async_results:
            if warning_str := result.get():
                warning(warning_str)
                import_result |= ImportResult.INCOMPLETE

        if unassigned_parameters:
            plural = "s" if len(unassigned_parameters) > 1 else ""
            warning(
                f"failed to match {len(unassigned_parameters)} parameter{plural} from supplier "
                f"API ({str(unassigned_parameters)[1:-1]})"
            )
            import_result |= ImportResult.INCOMPLETE

        return import_result

    @staticmethod
    def select_parameter(parameter_name, parameters) -> tuple[str, str]:
        max_matches = int(get_config().get("interactive_parameter_matches", 5))
        N_MATCHES = min(max_matches, len(parameters))
        parameter_matches_items = sorted(
            parameters.items(),
            key=lambda item: max(fuzz.partial_ratio(parameter_name, term) for term in item),
            reverse=True
        )
        parameter_matches = dict(parameter_matches_items[:N_MATCHES])

        max_value_length = max(len(str(value)) for value in parameter_matches.values())
        values = [str(value).ljust(max_value_length) for value in parameter_matches.values()]
        names = list(parameter_matches.keys())

        choices = (
            *(f"{value} | {BOLD}{name}{BOLD_END}" for value, name in zip(values, names)),
            f"{BOLD}Match Parameter Manually ...{BOLD_END}",
            f"{BOLD}Enter Value Manually ...{BOLD_END}",
            f"{BOLD}Skip ...{BOLD_END}"
        )
        while True:
            index = select(choices, deselected_prefix="  ", selected_prefix="> ")
            if index == N_MATCHES + 1:
                return None, prompt_input("value")
            if index == N_MATCHES + 2:
                return None, None
            elif index < N_MATCHES:
                return parameter_matches_items[index]

            name = prompt_input("parameter name")
            if (parameter_value := parameters.get(name)):
                return (name, parameter_value)
            warning(f"parameter '{name}' is not defined by the supplier")
            prompt("select parameter")

def create_parameter(inventree_api, part, parameter_template, value):
    try:
        Parameter.create(inventree_api, {
            "part": part.pk,
            "template": parameter_template.pk,
            "data": value,
        })
    except HTTPError as e:
        msg = e.args[0]["body"]
        return f"failed to create parameter '{parameter_template.name}' with '{msg}'"

def update_parameter(parameter, value):
    try:
        parameter.save({"data": value})
    except HTTPError as e:
        msg = e.args[0]["body"]
        parameter_name = parameter.template_detail["name"]
        return f"failed to update parameter '{parameter_name}' to '{value}' with '{msg}'"

SANITIZE_PARAMETER = re.compile("±")

def sanitize_parameter_value(value: str) -> str:
    value = value.strip()
    if value == "-":
        return ""
    value = SANITIZE_PARAMETER.sub("", value)
    value = value.replace("Ohm", "ohm").replace("ohms", "ohm")
    return value

class SafeFormatter(Formatter):
    def get_field(self, field_name, args, kwargs):
        first, _ = _string.formatter_field_name_split(field_name)
        try:
            return super().get_field(field_name, args, kwargs)
        except (KeyError, TypeError):
            return "", first

GET_FORMATSTR_FIELD = re.compile(r"[\[.].*$")
SIMPLIFY_FORMATSTR = re.compile(r"([^{]{[^[.}]*)[^}]*(})")
SIMPLIFY_FORMATSTR_SUB = "\\g<1>\\g<2>"
