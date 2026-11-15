-- Keys first observed unusable. A key that becomes usable again leaves the table.
CREATE TABLE publication.inactive_keys(
 api_key_id uuid PRIMARY KEY, organization_id uuid,
 reason text NOT NULL CHECK(reason ~ '^[a-z_]{1,64}$'),
 inactive_since timestamptz NOT NULL, observed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
-- Upstream copies to delete. Rows stay as the audit trail of each purge.
CREATE TABLE publication.upstream_purges(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), cluster_id uuid NOT NULL,
 kind text NOT NULL CHECK(kind IN ('response','file','conversation','vector_store','container','batch')),
 resource_id uuid, api_key_id uuid NOT NULL, organization_id uuid,
 deployment_id uuid NOT NULL, credential_id uuid NOT NULL, credential_version integer NOT NULL,
 endpoint text NOT NULL CHECK(endpoint IN ('openai_responses','anthropic_messages')),
 provider_id text NOT NULL CHECK(provider_id ~ '^[A-Za-z0-9_-]{1,200}$'),
 reason text NOT NULL CHECK(reason IN ('key_inactive','orphan')),
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','done','abandoned')),
 attempts integer NOT NULL DEFAULT 0, next_attempt timestamptz NOT NULL DEFAULT clock_timestamp(),
 claimed_by uuid, last_error text CHECK(last_error ~ '^[a-z0-9_]{1,64}$'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), finished_at timestamptz,
 UNIQUE(cluster_id,kind,deployment_id,provider_id)
);
CREATE INDEX upstream_purges_due ON publication.upstream_purges(cluster_id,next_attempt) WHERE state='pending';

CREATE FUNCTION publication.v1_purge_owners(cluster uuid) RETURNS uuid[]
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT coalesce(array_agg(DISTINCT k),'{}') FROM (
  SELECT api_key_id k FROM publication.stored_responses WHERE cluster_id=cluster AND deleted_at IS NULL
  UNION ALL SELECT api_key_id FROM publication.tenant_files WHERE cluster_id=cluster AND deleted_at IS NULL
  UNION ALL SELECT api_key_id FROM publication.conversations WHERE cluster_id=cluster AND deleted_at IS NULL
  UNION ALL SELECT api_key_id FROM publication.vector_stores WHERE cluster_id=cluster AND state IN ('active','expired')
  UNION ALL SELECT api_key_id FROM publication.execution_containers WHERE cluster_id=cluster AND deleted_at IS NULL
  UNION ALL SELECT api_key_id FROM publication.message_batches WHERE cluster_id=cluster AND NOT deleted
 ) owners
$$;

CREATE FUNCTION publication.v1_purge_schedule(cluster uuid,owners uuid[],activity jsonb,grace interval)
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
 SELECT cluster,kind,id,api_key_id,organization_id,deployment_id,credential_id,credential_version,
  endpoint,provider_id,'key_inactive'
 FROM (
  SELECT 'response' kind,r.id,r.api_key_id,r.deployment_id,r.credential_id,r.credential_version,
   'openai_responses' endpoint,r.provider_id FROM publication.stored_responses r
   WHERE r.cluster_id=cluster AND r.deleted_at IS NULL AND r.api_key_id=ANY(owners)
  UNION ALL SELECT 'file',f.id,f.api_key_id,f.deployment_id,f.credential_id,f.credential_version,f.endpoint,f.provider_id
   FROM publication.tenant_files f WHERE f.cluster_id=cluster AND f.deleted_at IS NULL AND f.api_key_id=ANY(owners)
  UNION ALL SELECT 'conversation',c.id,c.api_key_id,c.deployment_id,c.credential_id,c.credential_version,
   'openai_responses',c.provider_id FROM publication.conversations c
   WHERE c.cluster_id=cluster AND c.deleted_at IS NULL AND c.api_key_id=ANY(owners)
  UNION ALL SELECT 'vector_store',v.id,v.api_key_id,v.deployment_id,v.credential_id,v.credential_version,
   'openai_responses',v.provider_id FROM publication.vector_stores v
   WHERE v.cluster_id=cluster AND v.state IN ('active','expired') AND v.provider_id IS NOT NULL AND v.api_key_id=ANY(owners)
  UNION ALL SELECT 'container',x.id,x.api_key_id,x.deployment_id,x.credential_id,x.credential_version,x.endpoint,x.provider_id
   FROM publication.execution_containers x WHERE x.cluster_id=cluster AND x.deleted_at IS NULL AND x.api_key_id=ANY(owners)
  UNION ALL SELECT 'batch',b.id,b.api_key_id,b.deployment_id,b.credential_id,b.credential_version,
   'anthropic_messages',b.provider_id FROM publication.message_batches b
   WHERE b.cluster_id=cluster AND NOT b.deleted AND b.provider_id IS NOT NULL AND b.api_key_id=ANY(owners)
 ) live JOIN publication.inactive_keys i USING(api_key_id)
 WHERE i.inactive_since<=clock_timestamp()-grace
 ON CONFLICT(cluster_id,kind,deployment_id,provider_id) DO NOTHING;
END $$;

CREATE FUNCTION publication.v1_purge_claim(cluster uuid,node uuid,n integer) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE result jsonb;
BEGIN
 WITH picked AS (SELECT id FROM publication.upstream_purges WHERE cluster_id=cluster AND state='pending'
  AND next_attempt<=clock_timestamp() ORDER BY next_attempt LIMIT n FOR UPDATE SKIP LOCKED),
 claimed AS (UPDATE publication.upstream_purges p SET claimed_by=node,attempts=p.attempts+1,
  next_attempt=clock_timestamp()+interval '10 minutes' FROM picked WHERE p.id=picked.id RETURNING p.*)
 SELECT coalesce(jsonb_agg(jsonb_build_object('id',id,'kind',kind,'resource_id',resource_id,
  'api_key_id',api_key_id,'deployment_id',deployment_id,'credential_id',credential_id,
  'endpoint',endpoint,'provider_id',provider_id)),'[]'::jsonb) INTO result FROM claimed;
 RETURN result;
END $$;

CREATE FUNCTION publication.v1_purge_finish(purge uuid,cluster uuid,node uuid,outcome text,error text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.upstream_purges; action text;
BEGIN
 SELECT * INTO p FROM publication.upstream_purges WHERE id=purge AND cluster_id=cluster
 AND claimed_by=node AND state='pending' FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 IF outcome='done' THEN
  CASE p.kind
  WHEN 'response' THEN UPDATE publication.stored_responses SET deleted_at=coalesce(deleted_at,clock_timestamp()) WHERE id=p.resource_id;
  WHEN 'file' THEN UPDATE publication.tenant_files SET deleted_at=coalesce(deleted_at,clock_timestamp()) WHERE id=p.resource_id;
  WHEN 'conversation' THEN UPDATE publication.conversations SET deleted_at=coalesce(deleted_at,clock_timestamp()) WHERE id=p.resource_id;
  WHEN 'container' THEN UPDATE publication.execution_containers SET deleted_at=coalesce(deleted_at,clock_timestamp()) WHERE id=p.resource_id;
  WHEN 'batch' THEN UPDATE publication.message_batches SET deleted=true WHERE id=p.resource_id;
  -- The node records a deleted vector store through its storage observation, which closes metering.
  ELSE NULL;
  END CASE;
  UPDATE publication.upstream_purges SET state='done',finished_at=clock_timestamp(),last_error=NULL WHERE id=purge;
  action='upstream_resource.purged';
 ELSIF outcome='failed' AND error ~ '^[a-z0-9_]{1,64}$' THEN
  IF p.attempts>=30 THEN
   UPDATE publication.upstream_purges SET state='abandoned',finished_at=clock_timestamp(),last_error=error WHERE id=purge;
   action='upstream_resource.purge_abandoned';
  ELSE
   UPDATE publication.upstream_purges SET last_error=error,
    next_attempt=clock_timestamp()+least(interval '1 minute'*power(2,least(p.attempts,11)),interval '1 day')
   WHERE id=purge;
   action='upstream_resource.purge_failed';
  END IF;
 ELSE RAISE EXCEPTION 'invalid_purge_outcome';
 END IF;
 INSERT INTO publication.audit_events(organization_id,action,resource_id,details)
 VALUES(p.organization_id,action,coalesce(p.resource_id,p.id),jsonb_build_object('purge_id',p.id,'kind',p.kind,
  'reason',p.reason,'cluster_id',cluster,'node_id',node,'deployment_id',p.deployment_id,
  'attempts',p.attempts,'error',error));
END $$;

CREATE FUNCTION publication.v1_purge_orphan(cluster uuid,kid uuid,kind text,d jsonb,ep text,pid text)
RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 INSERT INTO publication.upstream_purges(cluster_id,kind,api_key_id,deployment_id,credential_id,
  credential_version,endpoint,provider_id,reason)
 VALUES(cluster,kind,kid,(d->>'id')::uuid,(d->>'credential_id')::uuid,(d->>'credential_version')::int,ep,pid,'orphan')
 ON CONFLICT(cluster_id,kind,deployment_id,provider_id) DO NOTHING
$$;
