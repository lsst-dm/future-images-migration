#!/usr/bin/env python

import os

import sqlalchemy

from lsst.daf.butler import Butler

MIGRATE_DIR = os.path.dirname(__file__)

BASE_NAMES = ["butler.yaml", "butler+sasquatch_dev.yaml", "butler_future.yaml"]

DATASET_TYPES = {
    "deep_coadd": "CellCoadd",
    "visit_image": "VisitImage",
    "difference_image": "DifferenceImage",
}


def do_update(connection: sqlalchemy.Connection, base_name: str) -> None:
    connection.execute(
        sqlalchemy.text(f"UPDATE dp2_prep.dataset_type SET name='legacy_{base_name}' WHERE name='{base_name}'")
    )
    connection.execute(
        sqlalchemy.text(f"UPDATE dp2_prep.dataset_type SET name='{base_name}' WHERE name='future_{base_name}'")
    )


def undo_update(connection: sqlalchemy.Connection, base_name: str) -> None:
    connection.execute(
        sqlalchemy.text(f"UPDATE dataset_type SET name='future_{base_name}' WHERE name='{base_name}'")
    )
    connection.execute(
        sqlalchemy.text(f"UPDATE dataset_type SET name='{base_name}' WHERE name='legacy_{base_name}'")
    )


def replace_file_content(root: str, base_name: str, suffix: str) -> None:
    # We use I/O instead of mv/cp to avoid messing up access control.
    with open(os.path.join(MIGRATE_DIR, base_name + suffix), "rt") as input_stream:
        content = input_stream.read()
    with open(os.path.join(root, base_name), "wt") as output_stream:
        output_stream.write(content)
    print(f"Rewrote {os.path.join(root, base_name)} from {os.path.join(MIGRATE_DIR, base_name + suffix)}")


def run(undo: bool = False) -> None:
    with Butler.from_config("dp2_prep") as butler:
        engine: sqlalchemy.Engine = butler.registry._registry._managers.datasets._db._engine
        root = butler._datastore.root.ospath
        renames_done = []
        try:
            with engine.begin() as connection:
                for dataset_type in DATASET_TYPES:
                    if not undo:
                        do_update(connection, dataset_type)
                    else:
                        undo_update(connection, dataset_type)
                for base_name in BASE_NAMES:
                    replace_file_content(root, base_name, ".new" if not undo else ".old")
                    renames_done.append(base_name)
        except BaseException:
            print("Error; undoing file edits...")
            for base_name in renames_done:
                replace_file_content(root, base_name, ".new" if undo else ".old")
            raise


def check() -> None:
    with Butler.from_config("dp2_prep") as butler:
        for dataset_type, storage_class_name in DATASET_TYPES.items():
            if (dt := butler.get_dataset_type(dataset_type)).storageClass_name != "ExposureF":
                raise ValueError(
                    f"storage class for {dt.name} in dp2_prep is {dt.storageClass_name}, not ExposureF."
                )
            if (
                dt := butler.get_dataset_type(f"future_{dataset_type}")
            ).storageClass_name != storage_class_name:
                raise ValueError(
                    f"storage class for future_{dt.name} in dp2_prep is "
                    f"{dt.storageClass_name}, not {storage_class_name}."
                )
    print("Checked dp2_prep.")
    with Butler.from_config("dp2_prep+sasquatch_dev") as butler:
        for dataset_type, storage_class_name in DATASET_TYPES.items():
            if (dt := butler.get_dataset_type(dataset_type)).storageClass_name != "ExposureF":
                raise ValueError(
                    f"storage class for {dt.name} in dp2_prep+sasquatch_dev is "
                    f"{dt.storageClass_name}, not ExposureF."
                )
            if (
                dt := butler.get_dataset_type(f"future_{dataset_type}")
            ).storageClass_name != storage_class_name:
                raise ValueError(
                    f"storage class for future_{dt.name} in dp2_prep+sasquatch_dev is "
                    f"{dt.storageClass_name}, not {storage_class_name}."
                )
    print("Checked dp2_prep+sasquatch_dev.")
    with Butler.from_config("dp2_prep_future") as butler:
        for dataset_type, storage_class_name in DATASET_TYPES.items():
            if (dt := butler.get_dataset_type(dataset_type)).storageClass_name != storage_class_name:
                raise ValueError(
                    f"storage class for {dt.name} in dp2_prep_future is "
                    f"{dt.storageClass_name}, not {storage_class_name}."
                )

            if (dt := butler.get_dataset_type(f"legacy_{dataset_type}")).storageClass_name != "ExposureF":
                raise ValueError(
                    f"storage class for legacy_{dt.name} in dp2_prep_future is "
                    f"{dt.storageClass_name}, not ExposureF."
                )
    print("Checked dp2_prep_future.")


if __name__ == "__main__":
    import sys

    undo = False
    if len(sys.argv) > 1 and sys.argv[1] == "undo":
        undo = True
    run(undo=undo)
    check()
