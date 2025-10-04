import importlib.metadata, logging
from pathlib import Path

import click, error_helper, tablib, tablib.formats
from cutie import prompt_yes_or_no, select
from error_helper import error, hint, info, prompt, success, warning
from inventree.api import InvenTreeAPI
from inventree.part import Part
from requests.exceptions import HTTPError, Timeout
from tablib.exceptions import TablibException, UnsupportedFormat
from thefuzz import fuzz

from .config import (CONFIG, SUPPLIERS_CONFIG, get_config, get_config_dir, set_config_dir,
                     setup_inventree_api, update_config_file, update_supplier_config)
from .inventree_helpers import get_category, get_category_parts
from .part_importer import ImportResult, PartImporter
from .suppliers import get_suppliers, setup_supplier_companies

def handle_errors(func):
    def wrapper(*args, **kwargs):
        try:
            func(*args, **kwargs)
        except KeyboardInterrupt:
            error("Aborting Execution! (KeyboardInterrupt)", prefix="")
        except Timeout as e:
            error(f"connection timed out ({e})", prefix="FATAL: ")
        except ConnectionError as e:
            error(f"connection error ({e})", prefix="FATAL: ")
        except HTTPError as e:
            status_code = None
            if e.response is not None:
                status_code = e.response.status_code
            elif e.args:
                status_code = e.args[0].get("status_code")
            if status_code in {408, 409, 500, 502, 503, 504}:
                error(f"HTTP error ({e})", prefix="FATAL: ")
            else:
                raise e
    return wrapper

_suppliers, _available_suppliers = get_suppliers(setup=False)
SuppliersChoices = click.Choice(_suppliers.keys(), case_sensitive=False)
AvailableSuppliersChoices = click.Choice(_available_suppliers.keys(), case_sensitive=False)

InteractiveChoices = click.Choice(("default", "false", "true", "twice"), case_sensitive=False)

@click.command
@click.pass_context
@click.argument("inputs", nargs=-1)
@click.option("-s", "--supplier", type=SuppliersChoices, help="Search this supplier first.")
@click.option("-o", "--only", type=SuppliersChoices, help="Only search this supplier.")
@click.option("-i", "--interactive", type=InteractiveChoices, default="default", help=(
    "Enable interactive mode. 'twice' will run once normally, then rerun in interactive "
    "mode for any parts that failed to import correctly."
))
@click.option("-d", "--dry", is_flag=True, help="Run without modifying InvenTree database.")
@click.option("-c", "--config-dir", help="Override path to config directory.")
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose output for debugging.")
@click.option("--show-config-dir", is_flag=True, help="Show path to config directory and exit.")
@click.option("--configure", type=AvailableSuppliersChoices, help="Configure supplier.")
@click.option("--update", metavar="CATEGORY", help="Update all parts from InvenTree CATEGORY.")
@click.option("--update-recursive", metavar="CATEGORY",
    help="Update all parts from InvenTree CATEGORY and from any of it's subcategories."
)
@click.option("--version", is_flag=True, help="Show version and exit.")
@handle_errors
def inventree_part_import(
    context,
    inputs,
    supplier=None,
    only=None,
    interactive="false",
    dry=False,
    config_dir=False,
    verbose=False,
    show_config_dir=False,
    configure=None,
    update=None,
    update_recursive=None,
    version=False,
):
    """Import supplier parts into InvenTree.

    INPUTS can either be supplier part numbers OR paths to tabular data files.
    """

    from inventree.api import logger
    logger.disabled = True

    if version:
        print(importlib.metadata.version(__package__))
        return

    if config_dir:
        try:
            set_config_dir(Path(config_dir))
        except OSError as e:
            error(f"failed to create '{config_dir}' with '{e}'")
            return

        if not show_config_dir:
            info(f"set configuration directory to '{config_dir}'", end="\n")

        # update used/available suppliers, config because they already got loaded before
        # also update the Choice types to be able to print the help message properly
        suppliers, available_suppliers = get_suppliers(reload=True, setup=False)
        get_config(reload=True)

        params = {param.name: param for param in click.get_current_context().command.params}
        SuppliersChoices = click.Choice(suppliers.keys())
        AvailableSuppliersChoices = click.Choice(available_suppliers.keys())
        params["supplier"].type = SuppliersChoices
        params["only"].type = SuppliersChoices
        params["configure"].type = AvailableSuppliersChoices

    if show_config_dir:
        print(get_config_dir())
        return

    if configure:
        _, available_suppliers = get_suppliers(reload=True)
        supplier = available_suppliers[configure]
        with update_config_file(SUPPLIERS_CONFIG) as suppliers_config:
            supplier_config = config if (config := suppliers_config.get(configure)) else {}
            new_config = update_supplier_config(supplier, supplier_config, force_update=True)
            if new_config:
                suppliers_config[configure] = new_config
        return

    if not inputs and not (update or update_recursive):
        click.echo(context.get_help())
        return

    if interactive == "default":
        interactive = str(get_config()["interactive"]).lower()
        if interactive not in set(InteractiveChoices.choices) - {"default"}:
            warning(f"invalid value 'interactive: {interactive}' in '{CONFIG}'")
            interactive = "false"

    only_supplier = False
    if only:
        if supplier:
            hint("--supplier is being overridden by --only")
        supplier = only
        only_supplier = True

    if verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        error_helper.INFO_END = "\r"

    if dry:
        warning(DRY_MODE_WARNING, prefix="")
        inventree_api = DryInvenTreeAPI()
    else:
        inventree_api = setup_inventree_api()

    if (category_path := update_recursive or update):
        if update_recursive and update:
            hint("--update is being overridden by --update-recursive")

        recursive_str = "-recursive" if update_recursive else ""
        if dry:
            error(f"--update{recursive_str} does not work with --dry")
            return
        if inputs:
            hint(f"--update{recursive_str} is set, other inputs will be ignored")

        if not (category := get_category(inventree_api, category_path)):
            error(f"no such category '{category_path}'")
            return
        parts = [part for part in get_category_parts(category, bool(update_recursive))]
    else:
        parts = []
        for name in inputs:
            path = Path(name)
            if path.is_file():
                if (file_parts := load_tabular_data(path)) is None:
                    return
                parts += file_parts
            elif path.exists():
                warning(f"skipping '{path}' (path exists, but is not a file)")
            else:
                parts.append(name)

        parts = list(filter(bool, (part.strip() for part in parts)))

    if not parts:
        info("nothing to import.")
        return

    # make sure suppliers.yaml exists
    get_suppliers(reload=True)
    setup_supplier_companies(inventree_api)
    importer = PartImporter(inventree_api, interactive=interactive == "true", verbose=verbose)

    if update or update_recursive:
        info(f"updating {len(parts)} parts from '{category_path}'", end="\n")
        print()

    failed_parts = []
    incomplete_parts = []

    try:
        for index, part in enumerate(parts):
            last_import_result = (
                importer.import_part(part.name, part, supplier, only_supplier)
                if isinstance(part, Part) else
                importer.import_part(part, None, supplier, only_supplier)
            )
            print()
            match last_import_result:
                case ImportResult.ERROR:
                    failed_parts.append(part)
                    incomplete_parts += parts[index + 1:]
                    break
                case ImportResult.FAILURE:
                    failed_parts.append(part)
                case ImportResult.INCOMPLETE:
                    incomplete_parts.append(part)

        parts2 = [*failed_parts, *incomplete_parts]
        if parts2 and interactive == "twice" and last_import_result != ImportResult.ERROR:
            success("reimporting failed/incomplete parts in interactive mode ...\n", prefix="")
            failed_parts = []
            incomplete_parts = []

            importer.interactive = True
            for part in parts2:
                import_result = (
                    importer.import_part(part.name, part, supplier, only_supplier)
                    if isinstance(part, Part) else
                    importer.import_part(part, None, supplier, only_supplier)
                )
                match import_result:
                    case ImportResult.ERROR | ImportResult.FAILURE:
                        failed_parts.append(part)
                    case ImportResult.INCOMPLETE:
                        incomplete_parts.append(part)
                print()

    finally:
        if failed_parts:
            failed_parts_str = "\n".join(
                (part.name if isinstance(part, Part) else part for part in failed_parts)
            )
            error(f"the following parts failed to import:\n{failed_parts_str}\n", prefix="")
        if incomplete_parts:
            incomplete_parts_str = "\n".join(
                (part.name if isinstance(part, Part) else part for part in incomplete_parts)
            )
            warning(f"the following parts are incomplete:\n{incomplete_parts_str}\n", prefix="")

    if not failed_parts and not incomplete_parts:
        action = "updated" if update or update_recursive else "imported"
        success(f"{action} all parts!")

def load_tabular_data(path: Path):
    info(f"reading {path.name} ...")
    with path.open(encoding="utf-8") as file:
        try:
            data = tablib.import_set(file)
        except UnsupportedFormat:
            # try to import the file as a single column csv file
            if column := load_single_column_csv(path):
                return column
            error(f"{path.suffix} is not a supported file format")
            return None
        except TablibException as e:
            error(f"failed to parse file with '{e.__doc__}'")
            return None

    mpn_headers = get_config().get(
        "auto_detect_columns", ["Manufacturer Part Number", "MPN", "part_id"]
    )

    headers = {
        stripped: i
        for i, header in enumerate(data.headers)
        if (stripped := header.strip())
    }
    sorted_headers = sorted(
        headers,
        key=lambda header: max(fuzz.partial_ratio(header, mpn) for mpn in mpn_headers),
        reverse=True,
    )

    if len(sorted_headers) == 0:
        column_index = 0
    elif sorted_headers[0] in mpn_headers and sorted_headers[1] not in mpn_headers:
        column_index = headers[sorted_headers[0]]
    else:
        prompt("select the column to import")
        index = select(sorted_headers, deselected_prefix="  ", selected_prefix="> ")
        column_index = headers[sorted_headers[index]]

    return data.get_col(column_index)

def load_single_column_csv(path: Path):
    if path.suffix not in {".csv", ".txt", ""}:
        return
    content = path.read_text()
    if content.count(",") >= content.count("\n"):
        return

    data = content.split("\n")
    info(f"importing '{path.name}' as single column csv file", end="\n")
    has_header = prompt_yes_or_no(
        f"is the first row '{data[0]}' a header?", default_is_yes=True
    )
    return data[1:] if has_header else data

DRY_MODE_WARNING = (
    "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
    "!!!!!!!!!!!!!!!!!!! RUNNING IN DRY MODE !!!!!!!!!!!!!!!!!!!\n"
    "!!!!!!!!!!!!!!! (no parts will be imported) !!!!!!!!!!!!!!!\n"
    "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!\n"
)

class DryInvenTreeAPI(InvenTreeAPI):
    DRY_RUN = True

    def __init__(self, host=None, **kwargs):
        self.base_url = "inventree/"
        pass

    def get(self, url, **kwargs):
        url_split = url.strip("/").split("/")
        if url_split[-1].isnumeric():
            raise HTTPError({"status_code": 404})
        return []

    def post(self, url, data, **kwargs):
        return {"pk": 1, "url": "", **data}

    def testServer(self):
        raise NotImplementedError()

    def request(self, api_url, **kwargs):
        raise NotImplementedError()

    def downloadFile(self, url, destination, overwrite=False, params=None, proxies=...):
        raise NotImplementedError()
