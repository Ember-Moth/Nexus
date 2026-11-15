-- The 0035 body of v1_purge_schedule, restored when 0038 is downgraded.
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
