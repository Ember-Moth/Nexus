from enum import StrEnum


class ModuleName(StrEnum):
    IDENTITY = "identity"
    CATALOG = "catalog"
    BILLING = "billing"
    PUBLICATION = "publication"
    REPORTING = "reporting"


SCHEMA_REVISION = "0043_cluster_policy"
