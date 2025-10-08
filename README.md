[![pypi](https://img.shields.io/pypi/v/inventree-part-import)](https://pypi.org/project/inventree-part-import/)
[![python](https://img.shields.io/badge/Python-3.10+-blue)](https://www.python.org/)
[![mit](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

# InvenTree Part Import

This project offers a command line interface to easily import parts from suppliers like
DigiKey, LCSC, Mouser, etc. into your InvenTree instance.

## Installation

```console
pipx install inventree-part-import
```

*(`pip` should also work, but [`pipx`](https://github.com/pypa/pipx) is the new recommended way
to install standalone applications)*

### Arch Linux

Latest version from PyPI is available on AUR as
[python-inventree-part-import](https://aur.archlinux.org/packages/python-inventree-part-import).<br>
Git master version is available as
[python-inventree-part-import-git](https://aur.archlinux.org/packages/python-inventree-part-import-git).<br>
(Thanks to [@randrej](https://github.com/randrej)!)

## Getting Started

### Initial Configuration

When using the CLI tool for the first time, it will guide you through creating your own
configuration.

When configuring suppliers, it's **highly recommended** to always enable the DigiKey API:

```console
select the suppliers you want to setup: (SPACEBAR to toggle, ENTER to confirm)
> [x] DigiKey
  [ ] LCSC
  [ ] Mouser
  [ ] Reichelt
  [ ] TME
```

#### Default categories/parameters configuration

```console
setup default categories/parameters configuration:
> Copy categories from InvenTree
  Copy default categories configuration
  Create empty configuration (manual setup)
```

If you have already been using InvenTree for some time (so you already have setup your category
structure, parts, etc.), select the `Copy categories from InvenTree` option, to automatically
setup a configuration which matches your InvenTree database.

If you are a new user, you can select the `Copy default categories configuration` to create
a basic configuration which you can extend in the future.

You can also create your own configuration from scratch (or manually copy
[someones existing configuration](https://github.com/30350n/inventree_part_import_config)
) by selecting the `Create empty configuration (manual setup)` option.

### Basic Usage

To import parts, simply use the `inventree-part-import` command, followed by the supplier or
manufacturer part numbers of the parts you want to import.

```console
$ inventree-part-import <part_number_1> <part_number_2> ...
```

You can also batch import multiple parts from tabular data files (`.csv`, `.xlsx`, etc.) like
so:

```console
$ inventree-part-import parts.csv
```

## Configuration

### `inventree.yaml`

This file is used to configure authentication to your InvenTree host.
It has two parameters:

- `host`: the host url to connect to (including port, if required)
- `token`: the user token to authenticate with (this will be retrieved automatically by the CLI)

### `config.yaml`

This file is used to configure general settings of the CLI tool, as well as default locales.
The following parameters have to be set:

- `currency`: the default currency to use when searching suppliers (ISO4217 code)
- `language`: the default language to use when searching suppliers (ISO639 code)
- `location`: the default location to use when searching suppliers (ISO3166 code)
- `scrape`: whether or not web page scraping is allowed (this can get you temporarily blocked)
- `interactive_part_matches`: the maximum number of parts to display in interactive mode
  (set to null to disable)
- `interactive_category_matches`: the maximum number of categories to display in interactive mode
- `interactive_parameter_matches`: the maximum number of parameters to display in interactive mode
- `part_selection_format`: standard python format str used to format each line of the
  interactive part selection menu (any fields from the `ApiPart` dataclass can be used,
  defaults to: `"{MPN} | {manufacturer} | {SKU} | {supplier_link}"`)
- `auto_detect_columns`: list of column names in tabular data files that will be automatically
  detected (defaults to `["Manufacturer Part Number", "MPN", "part_id"]`)

### `suppliers.yaml`

This file is used to configure supplier specific behavior.
The following parameters are always available:

- `currency`: overrides the currency for searching this supplier (see [`config.yaml`](#configyaml))
- `language`: overrides the language for searching this supplier (see [`config.yaml`](#configyaml))
- `location`: overrides the location for searching this supplier (see [`config.yaml`](#configyaml))

Additionally suppliers can have extra parameters for authentifcation to their respective APIs.
These can be set via the CLI like so: `inventree-part-import --configure <supplier>`.

#### DigiKey

Register a [**Production App**](https://developer.digikey.com/documentation?atab=tab_link_3) on the [Digikey API portal](https://developer.digikey.com) and enable the "ProductInformation V4" API for it.

**Using a Sandbox app will not work.**

#### Mouser

Request a **Search API** key from the [Mouser API Hub](https://www.mouser.com/api-hub/).

#### TME

Request an API key at the [Developers Page](https://developers.tme.eu/).

### `categories.yaml`

This file should specify all your InvenTree categories, as well as metadata like category
aliases, parameters, etc. for them.

It's defined as hierarchical tree structure where every 'node' represents a category.
For example:

```yaml
Electronics:
    Capacitors:
        Ceramic:
        Electrolytic:
Products:
```

Additionally you can define the following meta attributes (starting with `_`):

- `_aliases` has to be a list of supplier category names which will be mapped to that category
- `_description` specifies the categories description (defaults to category name)
- `_ignore` makes `inventree-part-import` ignore that category, as well as any subcategories
- `_parameters` has to be a list of parameter names (for parameters defined in
  [`parameters.yaml`](#parametersyaml)) this category uses<br>
  **note: parameters get inherited by sub categories**
- `_omit_parameters` has to be a list of parameters names defined in parent categories that
  get omitted from the category
- `_structural` can be set to `true` to make the category structural

Here's an example for a config with special attributes:

```yaml
Electronics:
    _description: Electronic Components # custom description
    _structural: true # no parts are allowed to be directly in this category
    Capacitors:
        _parameters: # parameters for both the 'Ceramic' and 'Electrolytic' categories
            - Capacitance
            - Tolerance
        Ceramic:
        Electrolytic:
            _aliases: # category names mapped to this category from various suppliers
                - Aluminum Electrolytic Capacitors
                - Aluminum Electrolytic Capacitors - SMD
                - Aluminum Electrolytic Capacitors - Leaded
                - Electrolyte Capacitors
Products:
    _ignore: true # this category contains our own products, so we won't import anything into it
```

### `parameters.yaml`

This file should specify all your InvenTree parameters, as well as metadata for them.

The following meta attributes are available:

- `_aliases` has to be a list of supplier parameter names which will be mapped to that parameter
- `_description` specifies the parameters description (defaults to parameter name)
- `_unit` specifies the parameters unit (experimental)

Here's an example for a single parameter:

```yaml
Input Voltage:
    _aliases:
        - Voltage - Input
        - Voltage - Input (Max)
        - Maximum Input Voltage
    _description: Max Input Voltage # optional
    _unit: V # experimental, this can lead to import problems
```

### Pre Creation Hooks (`hooks.py`)

Pre creation hooks are functions that get run after part information has been parsed from a
supplier, but before the InvenTree part gets created. They basically let you modify a part,
before it gets imported. This can be very useful in some cases.

For example, here's one that assigns transistors into different categories, based on their type:

```py
def fix_transistor_categories(api_part):
    if "BJT" in api_part.category_path[-1] or "Bipolar (BJT)" in api_part.category_path:
        transistor_type = api_part.parameters.get("Transistor Type", "")
        if "NPN" in transistor_type:
            api_part.category_path.append("NPN")
        elif "PNP" in transistor_type:
            api_part.category_path.append("PNP")
```

You can define any number of them in a `hooks.py` file in your configuration directory.
They'll get called in the order they're defined in.

For more examples, checkout my [config repository](https://github.com/30350n/inventree_part_import_config).

## Goal

The end goal of this project is to not exist anymore in it's current form. Ideally everything
the CLI tool does would be directly available from the InvenTree web interface. This will most
likely be done by implementing plugins which offer the required functionality for each supplier.

## Credits

- [InvenTree](https://inventree.org/) ([@SchrodingersGat](https://github.com/SchrodingersGat) and [@matmair](https://github.com/matmair))
  This project wouldn't exist without their brilliant work on creating the awesome open-source
  inventory management solution.

- [Ki-nTree](https://github.com/sparkmicro/Ki-nTree) ([@eeintech](https://github.com/eeintech))
  This project is inspired by Ki-nTree and aims to fix most of the issues I've had with it.
  It only provides the part importing functionality, but tries to improve it in every way.

- Thanks to [@atanisoft](https://github.com/atanisoft) for extensive beta testing!

## License

- This project is licensed under the [MIT](LICENSE) license.
