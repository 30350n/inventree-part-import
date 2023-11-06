from pathlib import Path

import click
from cutie import select
from tablib import import_set
from tablib.exceptions import TablibException, UnsupportedFormat
from thefuzz import fuzz

from . import error_helper
from .config import (SUPPLIERS_CONFIG, get_config_dir, set_config_dir, setup_inventree_api,
                     update_config_file, update_supplier_config)
from .error_helper import *
from .part_importer import PartImporter
from .suppliers import get_suppliers, setup_supplier_companies

def handle_keyboard_interrupt(func):
    def wrapper(*args, **kwargs):
        try:
            func(*args, **kwargs)
        except KeyboardInterrupt:
            error("Aborting Execution! (KeyboardInterrupt)", prefix="")
    return wrapper

_suppliers, _available_suppliers = get_suppliers(setup=False)
SuppliersChoices = click.Choice(_suppliers.keys())
AvailableSuppliersChoices = click.Choice(_available_suppliers.keys())

@click.command
@click.argument("inputs", nargs=-1)
@click.option("-s", "--supplier", type=SuppliersChoices, help="Search this supplier first.")
@click.option("-o", "--only", type=SuppliersChoices, help="Only search this supplier.")
@click.option("-c", "--config-dir", help="Override path to config directory.")
@click.option("-v", "--verbose", is_flag=True, help="Enable verbose output for debugging.")
@click.option("--show-config-dir", is_flag=True, help="Show path to config directory and exit.")
@click.option("--configure", type=AvailableSuppliersChoices, help="Configure supplier.")
@handle_keyboard_interrupt
def inventree_part_import(
    inputs,
    supplier=None,
    only=None,
    config_dir=False,
    verbose=False,
    show_config_dir=False,
    configure=None,
):
    """Import supplier parts into InvenTree.

    INPUTS can either be supplier part numbers OR paths to tabular data files.
    """

    if config_dir:
        try:
            set_config_dir(Path(config_dir))
        except OSError as e:
            error(f"failed to create '{config_dir}' with '{e}'")
            return

        if not show_config_dir:
            info(f"set configuration directory to '{config_dir}'", end="\n")

        # update used/available suppliers, because they already got loaded before
        # also update the Choice types to be able to print the help message properly
        suppliers, available_suppliers = get_suppliers(reload=True, setup=False)

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
        _, available_suppliers = get_suppliers()
        supplier = available_suppliers[configure]
        with update_config_file(get_config_dir() / SUPPLIERS_CONFIG) as suppliers_config:
            supplier_config = config if (config := suppliers_config.get(configure)) else {}
            new_config = update_supplier_config(supplier, supplier_config, force_update=True)
            if new_config:
                suppliers_config[configure] = new_config
        return

    if not inputs:
        click.echo(click.get_current_context().get_help())
        return

    only_supplier = False
    if only:
        if supplier:
            hint("--supplier is being overridden by --only")
        supplier = only
        only_supplier = True

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

    if not verbose:
        error_helper.INFO_END = "\r"
    inventree_api = setup_inventree_api()

    # make sure suppliers.yaml exists
    get_suppliers(reload=True)
    setup_supplier_companies(inventree_api)
    importer = PartImporter(inventree_api)

    for part in parts.copy():
        info(f"searching for {part} ...", end="\n")
        if importer.import_part(part, supplier, only_supplier):
            parts.remove(part)
        print()

    if parts:
        failed_parts_str = "\n".join(parts)
        warning(f"failed to import the following parts:\n{failed_parts_str}")
    else:
        success("imported all parts!")

MPN_HEADERS = ("Manufacturer Part Number", "MPN")
def load_tabular_data(path: Path):
    info(f"reading {path.name} ...")
    with path.open() as file:
        try:
            data = import_set(file)
            headers = {
                stripped: i
                for i, header in enumerate(data.headers)
                if (stripped := header.strip())
            }
            sorted_headers = sorted(
                headers,
                key=lambda header: max(fuzz.ratio(header, term) for term in MPN_HEADERS),
                reverse=True,
            )

            if len(sorted_headers) == 0:
                column_index = 0
            elif sorted_headers[0] in MPN_HEADERS and sorted_headers[1] not in MPN_HEADERS:
                column_index = headers[sorted_headers[0]]
            else:
                prompt("\nselect the column to import:", end="\n")
                index = select(sorted_headers, deselected_prefix="  ", selected_prefix="> ")
                column_index = headers[sorted_headers[index]]

            return data.get_col(column_index)
        except UnsupportedFormat:
            error(f"{path.suffix} is not a supported file format")
            return None
        except TablibException as e:
            error(f"failed to parse file with '{e.__doc__}'")
            return None
