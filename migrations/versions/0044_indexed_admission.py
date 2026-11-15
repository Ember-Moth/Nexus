"""Indexed admission, keys outside the snapshot, and one transaction for reserve + first attempt.

- API keys leave the snapshot payload. ``publication.cluster_keys`` keeps one row per
  (cluster, key, added_sequence); a release names the cluster's key sequence in its manifest
  (``key_set``) and ``config_snapshots.key_sequence``. Nodes read the net changes between two key
  sequences with ``gateway_v1.config_keys``.
- ``publication.snapshot_index`` holds the routes, deployments, models and access policies of
  each revision by key. Admission reads them through ``publication.v1_admission`` instead of
  expanding the manifest, so its cost no longer depends on the number of keys.
- ``gateway_v1.config_head`` is the node's poll: revision, digest, key sequence and the node's
  applied revision. ``gateway_v1.get_config_snapshot`` returns the stored bytes only.
- The Relay's reserve contracts take the first attempt (``aid``, ``dep``) and register it in the
  reservation's transaction (``gateway_v1.admit``). ``start_attempt`` serves later attempts.
- ``catalog.quota_policies`` accepts ``admission_concurrency``: a platform policy of a whole
  organization that overrides the Relay's cluster-wide admission cap.
"""

from pathlib import Path

from alembic import op

revision = "0044_indexed_admission"
down_revision = "0043_cluster_policy"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / revision

PUBLICATION_BRIDGE = [
    "v1_revision(publication.gateway_principals,bigint,boolean)",
    "v1_admission(publication.gateway_principals,bigint,uuid,uuid,uuid,boolean)",
    "v1_config_head()",
    "v1_config_payload(bigint)",
    "v1_config_keys(bigint,bigint,bigint,uuid,boolean,integer)",
]
PUBLICATION_PRIVATE = ["stage_keys(uuid,jsonb)", "index_snapshot(uuid,bigint)"]
GATEWAY_PUBLIC = [
    "reserve_request(uuid,uuid,uuid,bigint,bigint,bigint,uuid,uuid)",
    "reserve_search_request(uuid,uuid,uuid,bigint,bigint,bigint,integer,uuid,uuid)",
    "reserve_file_request(uuid,uuid,uuid,bigint,bigint,bigint,integer,integer,uuid,uuid)",
    "reserve_execution_request(uuid,uuid,uuid,bigint,bigint,bigint,integer,text,integer,uuid,uuid)",
    "reserve_guardrail_request(uuid,uuid,uuid,bigint,bigint,bigint,uuid,integer,uuid)",
    "reserve_interpreter(uuid,uuid,uuid,bigint,bigint,bigint,integer,integer,uuid,uuid,text,boolean,uuid)",
    "config_head()",
    "get_config_snapshot(bigint)",
    "config_keys(bigint,bigint,bigint,uuid,boolean,integer)",
]
GATEWAY_PRIVATE = [
    "admit(uuid,uuid,uuid,bigint,bigint,bigint,boolean,integer,integer,text,integer,uuid,integer,uuid,uuid)",
    "open_attempt(publication.gateway_principals,jsonb,uuid,integer,uuid,jsonb)",
    "reserve_with_execution(uuid,uuid,uuid,bigint,bigint,bigint,boolean,integer,integer,text,integer)",
]
# Restored by the downgrade with the grants they had in 0043.
PRIOR_PUBLIC = [
    "reserve_request(uuid,uuid,uuid,bigint,bigint,bigint)",
    "reserve_search_request(uuid,uuid,uuid,bigint,bigint,bigint,integer)",
    "reserve_file_request(uuid,uuid,uuid,bigint,bigint,bigint,integer,integer)",
    "reserve_execution_request(uuid,uuid,uuid,bigint,bigint,bigint,integer,text,integer)",
    "reserve_guardrail_request(uuid,uuid,uuid,bigint,bigint,bigint,uuid,integer)",
    "reserve_interpreter(uuid,uuid,uuid,bigint,bigint,bigint,integer,integer,uuid,uuid,text,boolean)",
    "get_config_snapshot(bigint)",
]
PRIOR_PRIVATE = [
    "reserve_with_guardrail(uuid,uuid,uuid,bigint,bigint,bigint,boolean,integer,integer,text,integer,uuid,integer)",
    "reserve_with_execution(uuid,uuid,uuid,bigint,bigint,bigint,boolean,integer,integer,text,integer)",
    "reserve_tools(uuid,uuid,uuid,bigint,bigint,bigint,boolean,integer,integer)",
    "reserve_metered(uuid,uuid,uuid,bigint,bigint,bigint,boolean,integer)",
    "reserve_item(uuid,uuid,uuid,bigint,bigint,bigint,boolean)",
]
RATES = "'rpm','tpm','concurrency'"
COUNTS = (
    "'resources_per_key','resources_per_organization',"
    "'vector_stores_per_key','vector_stores_per_organization'"
)


def install(schema, owner, sql, grants):
    op.execute(f"GRANT CREATE ON SCHEMA {schema} TO {owner}")
    op.execute(f"SET LOCAL ROLE {owner}")
    op.get_bind().exec_driver_sql(sql, execution_options={"no_parameters": True})
    for signature, grantee in grants:
        op.execute(f"REVOKE ALL ON FUNCTION {schema}.{signature} FROM PUBLIC")
        if grantee:
            op.execute(f"GRANT EXECUTE ON FUNCTION {schema}.{signature} TO {grantee}")
    op.execute("RESET ROLE")
    op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM {owner}")


def upgrade():
    op.get_bind().exec_driver_sql(
        (SQL / "schema.sql").read_text(), execution_options={"no_parameters": True}
    )
    install(
        "publication",
        "nexus_publication",
        (SQL / "publication.sql").read_text(),
        [(s, "nexus_bridge") for s in PUBLICATION_BRIDGE]
        + [(s, None) for s in PUBLICATION_PRIVATE],
    )
    install(
        "gateway_v1",
        "nexus_bridge",
        (SQL / "gateway.sql").read_text(),
        [(s, "nexus_gateway") for s in GATEWAY_PUBLIC] + [(s, None) for s in GATEWAY_PRIVATE],
    )


def downgrade():
    drops = "".join(
        f"DROP FUNCTION gateway_v1.{s};\n" for s in GATEWAY_PUBLIC + GATEWAY_PRIVATE
    )
    install(
        "gateway_v1",
        "nexus_bridge",
        drops + (SQL / "downgrade_gateway.sql").read_text(),
        [(s, "nexus_gateway") for s in PRIOR_PUBLIC] + [(s, None) for s in PRIOR_PRIVATE],
    )
    drops = "".join(
        f"DROP FUNCTION publication.{s};\n" for s in PUBLICATION_BRIDGE + PUBLICATION_PRIVATE
    )
    install(
        "publication",
        "nexus_publication",
        (SQL / "downgrade_publication.sql").read_text() + drops,
        [],
    )
    op.execute("DELETE FROM catalog.quota_policies WHERE metric='admission_concurrency'")
    op.execute(f"""ALTER TABLE catalog.quota_policies
        DROP CONSTRAINT quota_policies_metric_check,
        ADD CONSTRAINT quota_policies_metric_check CHECK(metric IN ({RATES},{COUNTS}))""")
    op.execute("DROP TABLE publication.snapshot_index, publication.cluster_keys")
    op.execute("""ALTER TABLE publication.config_snapshots
        DROP CONSTRAINT config_snapshots_key_set,
        DROP COLUMN required_capabilities, DROP COLUMN key_sequence""")
    op.execute("ALTER TABLE publication.gateway_clusters DROP COLUMN key_sequence")
