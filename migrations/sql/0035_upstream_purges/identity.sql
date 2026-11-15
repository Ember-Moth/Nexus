-- Liveness of API keys for upstream content retention. A null reason means the key is usable.
CREATE FUNCTION identity.v1_key_activity(kids uuid[]) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT coalesce(jsonb_agg(jsonb_build_object(
  'api_key_id',k.id,'organization_id',ak.organization_id,
  'reason',CASE WHEN ak.id IS NULL THEN 'key_missing'
   WHEN ak.revoked_at IS NOT NULL THEN 'key_revoked'
   WHEN ak.expires_at<=clock_timestamp() THEN 'key_expired'
   WHEN o.status<>'active' THEN 'organization_'||o.status
   WHEN pr.status<>'active' THEN 'project_'||pr.status
   WHEN sa.status<>'active' THEN 'service_account_'||sa.status END,
  'since',CASE WHEN ak.revoked_at IS NOT NULL THEN ak.revoked_at
   WHEN ak.expires_at<=clock_timestamp() THEN ak.expires_at END)),'[]'::jsonb)
 FROM unnest(kids) k(id) LEFT JOIN identity.api_keys ak ON ak.id=k.id
 LEFT JOIN identity.organizations o ON o.id=ak.organization_id
 LEFT JOIN identity.projects pr ON pr.id=ak.project_id
 LEFT JOIN identity.service_accounts sa ON sa.id=ak.service_account_id
$$;
