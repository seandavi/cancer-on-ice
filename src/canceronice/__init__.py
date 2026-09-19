"""cancerOnIce: population cancer data as Apache Iceberg tables."""

import os
from pathlib import Path

from pyiceberg.catalog import load_catalog


def catalog():
    """The cancerOnIce catalog.

    Defaults to a local sqlite-backed warehouse (`./warehouse`, override with
    `CANCERONICE_WAREHOUSE`) so nothing needs cloud credentials. Set
    `CANCERONICE_URI` to talk to a REST catalog (icegate) instead, with
    `CANCERONICE_WAREHOUSE` as the catalog name and `CANCERONICE_TOKEN` as the key.
    """
    uri = os.environ.get("CANCERONICE_URI")
    if uri:
        props = {"type": "rest", "uri": uri,
                 "warehouse": os.environ.get("CANCERONICE_WAREHOUSE", "canceronice")}
        token = os.environ.get("CANCERONICE_TOKEN")
        if token:
            props["token"] = token
        return load_catalog("canceronice", **props)

    warehouse = Path(os.environ.get("CANCERONICE_WAREHOUSE", "warehouse")).absolute()
    warehouse.mkdir(parents=True, exist_ok=True)
    return load_catalog("canceronice", type="sql",
                        uri=f"sqlite:///{warehouse}/catalog.db",
                        warehouse=warehouse.as_uri())
