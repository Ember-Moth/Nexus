-- Stored responses and conversations record when their handle was created, like the others.
ALTER TABLE publication.stored_responses
 ADD COLUMN created_at timestamptz NOT NULL DEFAULT clock_timestamp();
ALTER TABLE publication.conversations
 ADD COLUMN created_at timestamptz NOT NULL DEFAULT clock_timestamp();

-- An organization owner may ask for copies to be deleted before the grace period ends.
ALTER TABLE publication.upstream_purges DROP CONSTRAINT upstream_purges_reason_check,
 ADD CONSTRAINT upstream_purges_reason_check
  CHECK(reason IN ('key_inactive','orphan','owner_request'));

-- Every upstream copy bound to a key, one row per local handle. Copies whose provider ID was
-- never learned (a vector store or batch still being created, or rejected) are not listed.
-- retained: the copy still exists upstream as far as Nexus knows, so a purge can delete it.
CREATE VIEW publication.upstream_resources AS
 SELECT 'response'::text AS kind,r.id,r.cluster_id,r.api_key_id,r.deployment_id,r.credential_id,
  r.credential_version,'openai_responses'::text AS endpoint,r.provider_id,r.created_at,
  r.deleted_at IS NULL AS retained
 FROM publication.stored_responses r
 UNION ALL SELECT 'file',f.id,f.cluster_id,f.api_key_id,f.deployment_id,f.credential_id,
  f.credential_version,f.endpoint,f.provider_id,f.created_at,f.deleted_at IS NULL
 FROM publication.tenant_files f
 UNION ALL SELECT 'conversation',c.id,c.cluster_id,c.api_key_id,c.deployment_id,c.credential_id,
  c.credential_version,'openai_responses',c.provider_id,c.created_at,c.deleted_at IS NULL
 FROM publication.conversations c
 UNION ALL SELECT 'vector_store',v.id,v.cluster_id,v.api_key_id,v.deployment_id,v.credential_id,
  v.credential_version,'openai_responses',v.provider_id,v.created_at,v.state IN ('active','expired')
 FROM publication.vector_stores v WHERE v.provider_id IS NOT NULL
 UNION ALL SELECT 'container',x.id,x.cluster_id,x.api_key_id,x.deployment_id,x.credential_id,
  x.credential_version,x.endpoint,x.provider_id,x.created_at,x.deleted_at IS NULL
 FROM publication.execution_containers x
 UNION ALL SELECT 'batch',b.id,b.cluster_id,b.api_key_id,b.deployment_id,b.credential_id,
  b.credential_version,'anthropic_messages',b.provider_id,b.created_at,NOT b.deleted
 FROM publication.message_batches b WHERE b.provider_id IS NOT NULL;

-- An organization's request to delete upstream copies now: one resource, or every copy a key
-- holds. Items link the purges the request covers, whether it queued them or found them queued.
CREATE TABLE publication.purge_requests(
 id uuid PRIMARY KEY, organization_id uuid NOT NULL, actor_id uuid NOT NULL,
 api_key_id uuid NOT NULL,
 kind text CHECK(kind IN ('response','file','conversation','vector_store','container','batch')),
 resource_id uuid, key_digest text NOT NULL, parameters text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK((kind IS NULL)=(resource_id IS NULL)),
 UNIQUE(organization_id,actor_id,key_digest)
);
CREATE TABLE publication.purge_request_items(
 request_id uuid NOT NULL REFERENCES publication.purge_requests,
 purge_id uuid NOT NULL REFERENCES publication.upstream_purges,
 PRIMARY KEY(request_id,purge_id)
);
CREATE INDEX upstream_purge_resource ON publication.upstream_purges(resource_id)
 WHERE resource_id IS NOT NULL;
-- Requests find a key's purges; the listing also pages its unsaved copies newest first.
CREATE INDEX upstream_purge_key ON publication.upstream_purges(api_key_id,created_at DESC,id DESC);
-- The listing pages each key's copies newest first.
CREATE INDEX stored_responses_key_created ON publication.stored_responses(api_key_id,created_at DESC,id DESC);
CREATE INDEX tenant_files_key_created ON publication.tenant_files(api_key_id,created_at DESC,id DESC);
CREATE INDEX conversations_key_created ON publication.conversations(api_key_id,created_at DESC,id DESC);
CREATE INDEX vector_stores_key_created ON publication.vector_stores(api_key_id,created_at DESC,id DESC);
CREATE INDEX execution_containers_key_created ON publication.execution_containers(api_key_id,created_at DESC,id DESC);
CREATE INDEX message_batches_key_created ON publication.message_batches(api_key_id,created_at DESC,id DESC);

-- The schedule reads the same view, so automatic and requested purges select the same copies.
CREATE OR REPLACE FUNCTION publication.v1_purge_schedule(cluster uuid,owners uuid[],activity jsonb,grace interval)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 DELETE FROM publication.inactive_keys i USING jsonb_array_elements(activity) a
 WHERE i.api_key_id=(a->>'api_key_id')::uuid AND a->>'reason' IS NULL;
 INSERT INTO publication.inactive_keys(api_key_id,organization_id,reason,inactive_since)
 SELECT (a->>'api_key_id')::uuid,(a->>'organization_id')::uuid,a->>'reason',
  least(coalesce((a->>'since')::timestamptz,clock_timestamp()),clock_timestamp())
 FROM jsonb_array_elements(activity) a WHERE a->>'reason' IS NOT NULL
 ON CONFLICT(api_key_id) DO UPDATE SET reason=excluded.reason,observed_at=clock_timestamp();
 INSERT INTO publication.upstream_purges(cluster_id,kind,resource_id,api_key_id,organization_id,
  deployment_id,credential_id,credential_version,endpoint,provider_id,reason)
 SELECT cluster,r.kind,r.id,r.api_key_id,i.organization_id,r.deployment_id,r.credential_id,
  r.credential_version,r.endpoint,r.provider_id,'key_inactive'
 FROM publication.upstream_resources r JOIN publication.inactive_keys i USING(api_key_id)
 WHERE r.cluster_id=cluster AND r.retained AND r.api_key_id=ANY(owners)
  AND i.inactive_since<=clock_timestamp()-grace
 ON CONFLICT(cluster_id,kind,deployment_id,provider_id) DO NOTHING;
END $$;
