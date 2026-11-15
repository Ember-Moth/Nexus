-- Live resources of a key and of the given keys of its organization in the caller's cluster,
-- counted up to the limits the node received in its snapshot. A count that reaches its limit
-- stops there, so a full organization does not cost a scan of all its resources.
CREATE FUNCTION publication.v1_resource_usage(kind text,kid uuid,keys uuid[],key_cap bigint,org_cap bigint)
RETURNS jsonb LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE c uuid := (publication.v1_principal()).cluster_id; mine bigint; org bigint; owners uuid[] := array_append(keys,kid);
BEGIN
 IF key_cap IS NULL OR org_cap IS NULL OR key_cap<1 OR org_cap<1 THEN RAISE EXCEPTION 'invalid_resource_limit'; END IF;
 CASE kind
 WHEN 'file' THEN
  SELECT count(*) INTO mine FROM (SELECT FROM publication.tenant_files
   WHERE cluster_id=c AND api_key_id=kid AND deleted_at IS NULL LIMIT key_cap) k;
  SELECT count(*) INTO org FROM (SELECT FROM publication.tenant_files
   WHERE cluster_id=c AND api_key_id=ANY(owners) AND deleted_at IS NULL LIMIT org_cap) o;
 WHEN 'conversation' THEN
  SELECT count(*) INTO mine FROM (SELECT FROM publication.conversations
   WHERE cluster_id=c AND api_key_id=kid AND deleted_at IS NULL LIMIT key_cap) k;
  SELECT count(*) INTO org FROM (SELECT FROM publication.conversations
   WHERE cluster_id=c AND api_key_id=ANY(owners) AND deleted_at IS NULL LIMIT org_cap) o;
 WHEN 'vector_store' THEN
  SELECT count(*) INTO mine FROM (SELECT FROM publication.vector_stores
   WHERE cluster_id=c AND api_key_id=kid AND state IN ('submitting','active','unknown') LIMIT key_cap) k;
  SELECT count(*) INTO org FROM (SELECT FROM publication.vector_stores
   WHERE cluster_id=c AND api_key_id=ANY(owners) AND state IN ('submitting','active','unknown') LIMIT org_cap) o;
 WHEN 'container' THEN
  SELECT count(*) INTO mine FROM (SELECT FROM publication.execution_containers
   WHERE cluster_id=c AND api_key_id=kid AND deleted_at IS NULL
   AND metadata->>'status' IS DISTINCT FROM 'expired' LIMIT key_cap) k;
  SELECT count(*) INTO org FROM (SELECT FROM publication.execution_containers
   WHERE cluster_id=c AND api_key_id=ANY(owners) AND deleted_at IS NULL
   AND metadata->>'status' IS DISTINCT FROM 'expired' LIMIT org_cap) o;
 WHEN 'batch' THEN
  SELECT count(*) INTO mine FROM (SELECT FROM publication.message_batches
   WHERE cluster_id=c AND api_key_id=kid AND NOT deleted LIMIT key_cap) k;
  SELECT count(*) INTO org FROM (SELECT FROM publication.message_batches
   WHERE cluster_id=c AND api_key_id=ANY(owners) AND NOT deleted LIMIT org_cap) o;
 ELSE RAISE EXCEPTION 'invalid_resource_kind';
 END CASE;
 RETURN jsonb_build_object('key',mine,'organization',org);
END $$;
