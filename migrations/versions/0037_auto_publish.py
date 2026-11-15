"""Automatic gateway publication and rule-based organization scopes."""

from alembic import op

revision = "0037_auto_publish"
down_revision = "0036_identity_abuse_limits"
branch_labels = None
depends_on = None
SOURCES = ("identity", "catalog", "billing")
CHANNEL = "nexus_source_revision"
SCOPE = "identity.v1_scope_organizations(boolean)"


def upgrade():
    op.execute("""ALTER TABLE publication.gateway_clusters
        ADD COLUMN organization_scope text NOT NULL DEFAULT 'listed'
         CHECK(organization_scope IN ('listed','all','personal')),
        ADD COLUMN auto_publish boolean NOT NULL DEFAULT false""")
    # Existing clusters keep manual releases until an operator opts in; new ones publish.
    op.execute(
        "ALTER TABLE publication.gateway_clusters ALTER COLUMN auto_publish SET DEFAULT true"
    )
    op.execute("ALTER TABLE publication.gateway_clusters DROP CONSTRAINT gateway_clusters_check")
    op.execute("""ALTER TABLE publication.gateway_clusters ADD CONSTRAINT gateway_clusters_scope
        CHECK(cardinality(deployment_ids)>0
         AND (organization_scope='listed')=(cardinality(organization_ids)>0))""")
    # One row per cluster: what the desired snapshot represents, the pending change, the
    # last automatic release and failure, a rollback pause, and the worker lease.
    op.execute("""CREATE TABLE publication.cluster_automation(
        cluster_id uuid PRIMARY KEY REFERENCES publication.gateway_clusters,
        settled jsonb, content_digest text, content_revision bigint, price_models uuid[],
        observed jsonb, changed_at timestamptz, pending_since timestamptz,
        retry_after timestamptz, failed jsonb, checked_at timestamptz, lease_until timestamptz,
        published_revision bigint, published_at timestamptz, published_trigger jsonb,
        failure_code text, failure_message text, failure_at timestamptz,
        paused_revision bigint, paused_at timestamptz)""")
    op.execute(
        "INSERT INTO publication.cluster_automation(cluster_id) "
        "SELECT id FROM publication.gateway_clusters"
    )
    op.execute("GRANT SELECT,INSERT,UPDATE ON publication.cluster_automation TO nexus_publication")
    # Every source revision bump wakes the auto-publisher when its transaction commits.
    for schema in SOURCES:
        op.execute(f"""CREATE FUNCTION {schema}.announce_revision() RETURNS trigger
            LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
            PERFORM pg_notify('{CHANNEL}','{schema}'); RETURN NULL; END $$""")
        op.execute(
            f"CREATE TRIGGER announce_revision AFTER UPDATE ON {schema}.source_revision "
            f"FOR EACH STATEMENT EXECUTE FUNCTION {schema}.announce_revision()"
        )
    # Scope, key, node and auto_publish changes all advance the cluster version.
    op.execute(f"""CREATE FUNCTION publication.announce_cluster() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
        PERFORM pg_notify('{CHANNEL}','publication'); RETURN NULL; END $$""")
    op.execute(
        "CREATE TRIGGER announce_cluster AFTER INSERT OR UPDATE OF version "
        "ON publication.gateway_clusters FOR EACH STATEMENT "
        "EXECUTE FUNCTION publication.announce_cluster()"
    )
    # Rule-based scopes resolve active organizations through identity's read contract.
    op.execute("GRANT CREATE ON SCHEMA identity TO nexus_identity")
    op.execute("SET LOCAL ROLE nexus_identity")
    op.execute(f"""CREATE FUNCTION {SCOPE} RETURNS SETOF uuid
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        SELECT id FROM identity.organizations WHERE status='active'
        AND (NOT $1 OR personal_owner_id IS NOT NULL) ORDER BY id $$""")
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA identity FROM nexus_identity")
    op.execute(f"REVOKE ALL ON FUNCTION {SCOPE} FROM PUBLIC")
    op.execute("GRANT USAGE ON SCHEMA identity TO nexus_publication")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SCOPE} TO nexus_publication")


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM publication.gateway_clusters
        WHERE organization_scope<>'listed') THEN
        RAISE EXCEPTION 'rule-based cluster scopes are retained'; END IF; END $$""")
    op.execute("REVOKE USAGE ON SCHEMA identity FROM nexus_publication")
    op.execute("SET LOCAL ROLE nexus_identity")
    op.execute(f"DROP FUNCTION {SCOPE}")
    op.execute("RESET ROLE")
    op.execute("DROP TRIGGER announce_cluster ON publication.gateway_clusters")
    op.execute("DROP FUNCTION publication.announce_cluster()")
    for schema in SOURCES:
        op.execute(f"DROP TRIGGER announce_revision ON {schema}.source_revision")
        op.execute(f"DROP FUNCTION {schema}.announce_revision()")
    op.execute("DROP TABLE publication.cluster_automation")
    op.execute("ALTER TABLE publication.gateway_clusters DROP CONSTRAINT gateway_clusters_scope")
    op.execute("""ALTER TABLE publication.gateway_clusters ADD CONSTRAINT gateway_clusters_check
        CHECK(cardinality(organization_ids)>0 AND cardinality(deployment_ids)>0)""")
    op.execute(
        "ALTER TABLE publication.gateway_clusters DROP COLUMN auto_publish, "
        "DROP COLUMN organization_scope"
    )
