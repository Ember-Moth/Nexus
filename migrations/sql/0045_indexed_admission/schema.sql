-- API keys leave the snapshot payload: one versioned row per (cluster, key, added_sequence).
-- A key is a member of the cluster as of key sequence N when added_sequence <= N and it was not
-- removed by N. Rows are never rewritten, so the change between two sequences is exact.
ALTER TABLE publication.gateway_clusters
 ADD COLUMN key_sequence bigint NOT NULL DEFAULT 0 CHECK(key_sequence >= 0);
ALTER TABLE publication.config_snapshots
 ADD COLUMN key_sequence bigint NOT NULL DEFAULT 0 CHECK(key_sequence >= 0),
 ADD COLUMN required_capabilities text[] NOT NULL DEFAULT '{}';
-- Releases stored before this migration name no key set (sequence 0); a release made by it and
-- kept across a downgrade names its own. Nodes need a new release either way: the keys of a
-- stored release are not recoverable from its payload.
UPDATE publication.config_snapshots SET required_capabilities = ARRAY(
 SELECT jsonb_array_elements_text(manifest->'required_capabilities')),
 key_sequence = coalesce((manifest->'key_set'->>'sequence')::bigint, 0);
UPDATE publication.gateway_clusters c SET key_sequence = coalesce((SELECT max(s.key_sequence)
 FROM publication.config_snapshots s WHERE s.cluster_id = c.id), 0);
ALTER TABLE publication.config_snapshots ADD CONSTRAINT config_snapshots_key_set
 CHECK(coalesce((manifest->'key_set'->>'sequence')::bigint, 0) = key_sequence);

CREATE TABLE publication.cluster_keys(
 cluster_id uuid NOT NULL REFERENCES publication.gateway_clusters,
 key_id uuid NOT NULL,
 added_sequence bigint NOT NULL CHECK(added_sequence > 0),
 removed_sequence bigint CHECK(removed_sequence > added_sequence),
 digest text NOT NULL CHECK(digest ~ '^[0-9a-f]{64}$'),
 organization_id uuid NOT NULL,
 project_id uuid NOT NULL,
 service_account_id uuid NOT NULL,
 expires_at timestamptz,
 PRIMARY KEY(cluster_id, key_id, added_sequence));
CREATE UNIQUE INDEX cluster_keys_live ON publication.cluster_keys(cluster_id, key_id)
 WHERE removed_sequence IS NULL;
CREATE UNIQUE INDEX cluster_keys_live_digest ON publication.cluster_keys(cluster_id, digest)
 WHERE removed_sequence IS NULL;
CREATE INDEX cluster_keys_added ON publication.cluster_keys(cluster_id, added_sequence, key_id);
CREATE INDEX cluster_keys_removed ON publication.cluster_keys(cluster_id, removed_sequence, key_id)
 WHERE removed_sequence IS NOT NULL;
GRANT SELECT, INSERT, UPDATE, DELETE ON publication.cluster_keys TO nexus_publication;

-- The parts of one revision that admission reads, keyed for point lookups:
--   route      (organization_id, model_id)  the route object
--   deployment (deployment_id, nil)         the deployment object
--   model      (model_id, nil)              the model object
--   access     (organization_id, nil)       the organization's access policies (array)
CREATE TABLE publication.snapshot_index(
 cluster_id uuid NOT NULL,
 revision bigint NOT NULL,
 kind text NOT NULL CHECK(kind IN ('route', 'deployment', 'model', 'access')),
 primary_id uuid NOT NULL,
 secondary_id uuid NOT NULL,
 body jsonb NOT NULL,
 PRIMARY KEY(cluster_id, revision, kind, primary_id, secondary_id),
 FOREIGN KEY(cluster_id, revision) REFERENCES publication.config_snapshots(cluster_id, revision));
GRANT SELECT, INSERT, DELETE ON publication.snapshot_index TO nexus_publication;
INSERT INTO publication.snapshot_index
SELECT s.cluster_id, s.revision, 'route', (r->>'organization_id')::uuid, (r->>'model_id')::uuid, r
 FROM publication.config_snapshots s, jsonb_array_elements(s.manifest->'resources'->'routes') r
UNION ALL
SELECT s.cluster_id, s.revision, 'deployment', (d->>'id')::uuid,
 '00000000-0000-0000-0000-000000000000', d
 FROM publication.config_snapshots s, jsonb_array_elements(s.manifest->'resources'->'deployments') d
UNION ALL
SELECT s.cluster_id, s.revision, 'model', (m->>'id')::uuid, '00000000-0000-0000-0000-000000000000', m
 FROM publication.config_snapshots s, jsonb_array_elements(s.manifest->'resources'->'models') m
UNION ALL
SELECT s.cluster_id, s.revision, 'access', (a->>'organization_id')::uuid,
 '00000000-0000-0000-0000-000000000000', jsonb_agg(a)
 FROM publication.config_snapshots s, jsonb_array_elements(s.manifest->'resources'->'access_policies') a
 GROUP BY s.cluster_id, s.revision, (a->>'organization_id')::uuid;

-- One cluster-wide admission concurrency cap per organization, enforced in Redis by the Relay.
-- Like the resource counts it is a platform policy of a whole organization
-- (quota_policies_count_scope), and it overrides the Relay's default for that organization.
ALTER TABLE catalog.quota_policies
 DROP CONSTRAINT quota_policies_metric_check,
 ADD CONSTRAINT quota_policies_metric_check CHECK(metric IN ('rpm', 'tpm', 'concurrency',
  'admission_concurrency', 'resources_per_key', 'resources_per_organization',
  'vector_stores_per_key', 'vector_stores_per_organization'));
