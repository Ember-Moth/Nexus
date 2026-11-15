-- Live resources of a key and of the given keys of its organization in the caller's cluster.
-- Each count stays on the cluster/key indexes of its resource table.
CREATE FUNCTION publication.v1_resource_usage(kind text,kid uuid,keys uuid[]) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE c uuid := (publication.v1_principal()).cluster_id; mine bigint; org bigint; owners uuid[] := array_append(keys,kid);
BEGIN
 CASE kind
 WHEN 'file' THEN
  SELECT count(*) FILTER (WHERE api_key_id=kid),count(*) INTO mine,org FROM publication.tenant_files
  WHERE cluster_id=c AND api_key_id=ANY(owners) AND deleted_at IS NULL;
 WHEN 'conversation' THEN
  SELECT count(*) FILTER (WHERE api_key_id=kid),count(*) INTO mine,org FROM publication.conversations
  WHERE cluster_id=c AND api_key_id=ANY(owners) AND deleted_at IS NULL;
 WHEN 'vector_store' THEN
  SELECT count(*) FILTER (WHERE api_key_id=kid),count(*) INTO mine,org FROM publication.vector_stores
  WHERE cluster_id=c AND api_key_id=ANY(owners) AND state IN ('submitting','active','unknown');
 WHEN 'container' THEN
  SELECT count(*) FILTER (WHERE api_key_id=kid),count(*) INTO mine,org FROM publication.execution_containers
  WHERE cluster_id=c AND api_key_id=ANY(owners) AND deleted_at IS NULL
  AND metadata->>'status' IS DISTINCT FROM 'expired';
 WHEN 'batch' THEN
  SELECT count(*) FILTER (WHERE api_key_id=kid),count(*) INTO mine,org FROM publication.message_batches
  WHERE cluster_id=c AND api_key_id=ANY(owners) AND NOT deleted;
 ELSE RAISE EXCEPTION 'invalid_resource_kind';
 END CASE;
 RETURN jsonb_build_object('key',mine,'organization',org);
END $$;
CREATE INDEX message_batch_owner ON publication.message_batches(cluster_id,api_key_id) WHERE NOT deleted;
