-- Supplier handles stay bound to both a tenant key and the original supplier account.
CREATE TABLE publication.execution_containers(
 id uuid PRIMARY KEY,cluster_id uuid NOT NULL,api_key_id uuid NOT NULL,model_id uuid NOT NULL,deployment_id uuid NOT NULL,
 endpoint text NOT NULL CHECK(endpoint IN ('openai_responses','anthropic_messages')),
 credential_id uuid NOT NULL,credential_version integer NOT NULL,upstream_version integer NOT NULL,
 provider_id text NOT NULL CHECK(provider_id ~ '^[A-Za-z0-9_-]{1,200}$'),
 metadata jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 deleted_at timestamptz, UNIQUE(cluster_id,credential_id,credential_version,provider_id)
);
CREATE INDEX execution_owner ON publication.execution_containers(cluster_id,api_key_id,endpoint,created_at,id) WHERE deleted_at IS NULL;
CREATE TABLE publication.container_files(
 id uuid PRIMARY KEY,container_id uuid NOT NULL REFERENCES publication.execution_containers,
 provider_id text NOT NULL CHECK(provider_id ~ '^[A-Za-z0-9_-]{1,200}$'),deleted_at timestamptz,
 UNIQUE(container_id,provider_id)
);
CREATE FUNCTION publication.v1_container(handle uuid,kid uuid,ep text) RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT to_jsonb(c) FROM publication.execution_containers c WHERE id=handle AND api_key_id=kid AND endpoint=ep AND deleted_at IS NULL
 AND cluster_id=(publication.v1_principal()).cluster_id
$$;
CREATE FUNCTION publication.v1_container_bind(kid uuid,d jsonb,pid text,ep text,meta jsonb,creating boolean) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE c publication.execution_containers; cluster uuid:=(publication.v1_principal()).cluster_id;
BEGIN
 IF pid IS NULL OR pid !~ '^[A-Za-z0-9_-]{1,200}$' OR ep NOT IN ('openai_responses','anthropic_messages') THEN RAISE EXCEPTION 'invalid_container'; END IF;
 IF creating THEN
  INSERT INTO publication.execution_containers(id,cluster_id,api_key_id,model_id,deployment_id,endpoint,credential_id,credential_version,
      upstream_version,provider_id,metadata) VALUES(gen_random_uuid(),cluster,kid,(d->>'model_id')::uuid,(d->>'id')::uuid,ep,
     (d->>'credential_id')::uuid,(d->>'credential_version')::integer,(d->>'upstream_version')::integer,pid,coalesce(meta,'{}'))
  ON CONFLICT(cluster_id,credential_id,credential_version,provider_id) DO NOTHING;
 END IF;
 SELECT * INTO c FROM publication.execution_containers WHERE cluster_id=cluster AND credential_id=(d->>'credential_id')::uuid
 AND credential_version=(d->>'credential_version')::integer AND provider_id=pid AND endpoint=ep AND api_key_id=kid
 AND deployment_id=(d->>'id')::uuid AND model_id=(d->>'model_id')::uuid AND upstream_version=(d->>'upstream_version')::integer AND deleted_at IS NULL;
 IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 IF meta IS NOT NULL THEN UPDATE publication.execution_containers SET metadata=metadata||meta||
  CASE WHEN c.metadata ? 'memory_limit' THEN jsonb_build_object('memory_limit',c.metadata->'memory_limit') ELSE '{}'::jsonb END WHERE id=c.id; END IF;
 RETURN c.id;
END $$;
CREATE FUNCTION publication.v1_container_remove(handle uuid,kid uuid) RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 UPDATE publication.execution_containers SET deleted_at=clock_timestamp() WHERE id=handle AND api_key_id=kid
 AND cluster_id=(publication.v1_principal()).cluster_id AND deleted_at IS NULL
$$;
CREATE FUNCTION publication.v1_container_file_bind(handle uuid,kid uuid,pid text,creating boolean) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE fid uuid;
BEGIN
 IF publication.v1_container(handle,kid,'openai_responses') IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 IF creating THEN
  INSERT INTO publication.container_files(id,container_id,provider_id) VALUES(gen_random_uuid(),handle,pid)
  ON CONFLICT(container_id,provider_id) DO NOTHING;
 END IF;
 SELECT id INTO fid FROM publication.container_files WHERE container_id=handle AND provider_id=pid AND deleted_at IS NULL;
 IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 RETURN fid;
END $$;
CREATE FUNCTION publication.v1_container_file(handle uuid,kid uuid,fid uuid,removing boolean) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE pid text;
BEGIN
 IF publication.v1_container(handle,kid,'openai_responses') IS NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 SELECT provider_id INTO pid FROM publication.container_files WHERE id=fid AND container_id=handle AND deleted_at IS NULL;
 IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 IF removing THEN UPDATE publication.container_files SET deleted_at=clock_timestamp() WHERE id=fid; END IF;
 RETURN pid;
END $$;
-- Generated Anthropic files are real Files resources. New file IDs must not be claimed
-- from another owner's upload, even when the two tenants share supplier credentials.
CREATE FUNCTION publication.v1_execution_file(kid uuid,d jsonb,pid text,creating boolean) RETURNS uuid
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE f publication.tenant_files; cluster uuid:=(publication.v1_principal()).cluster_id; handle uuid;
BEGIN
 IF pid IS NULL OR pid !~ '^[A-Za-z0-9_-]{1,200}$' THEN RAISE EXCEPTION 'invalid_file_reference'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended((d->>'credential_id')||':'||pid,93817));
 IF EXISTS(SELECT FROM publication.tenant_files WHERE cluster_id=cluster AND credential_id=(d->>'credential_id')::uuid
 AND credential_version=(d->>'credential_version')::int AND provider_id=pid AND api_key_id<>kid AND endpoint='anthropic_messages')
 THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 SELECT * INTO f FROM publication.tenant_files WHERE cluster_id=cluster AND deployment_id=(d->>'id')::uuid AND api_key_id=kid
 AND credential_id=(d->>'credential_id')::uuid AND credential_version=(d->>'credential_version')::int
 AND upstream_version=(d->>'upstream_version')::int AND provider_id=pid AND endpoint='anthropic_messages'
 ORDER BY created_at DESC,id DESC LIMIT 1;
 IF FOUND THEN
  IF f.deleted_at IS NOT NULL THEN RAISE EXCEPTION 'resource_not_found'; END IF;
  RETURN f.id;
 END IF;
 IF NOT creating THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 handle=gen_random_uuid();
 PERFORM publication.v1_native_file_save(handle,kid,(d->>'model_id')::uuid,d,jsonb_build_object('type','file','id',pid),'anthropic_messages');
 RETURN handle;
END $$;
CREATE FUNCTION publication.v1_batch_execution(batch uuid,custom text,kid uuid) RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('rid',i.rid,'model',i.model_id,'deployment',i.deployment_id,'node',b.owner_node)
 FROM publication.batch_items i JOIN publication.message_batches b ON b.id=i.batch_id
 WHERE b.id=batch AND i.custom_id=custom AND b.api_key_id=kid AND b.cluster_id=(publication.v1_principal()).cluster_id AND NOT b.deleted
$$;
CREATE FUNCTION publication.v1_execution_file_refresh(handle uuid,kid uuid,meta jsonb) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF meta->>'type' IS DISTINCT FROM 'file' OR jsonb_typeof(meta->'filename') IS DISTINCT FROM 'string'
 OR jsonb_typeof(meta->'size_bytes') IS DISTINCT FROM 'number' OR jsonb_typeof(meta->'created_at') IS DISTINCT FROM 'string'
 OR jsonb_typeof(meta->'mime_type') IS DISTINCT FROM 'string' THEN RAISE EXCEPTION 'invalid_file_metadata'; END IF;
 UPDATE publication.tenant_files SET metadata=jsonb_strip_nulls(jsonb_build_object('id','file_nx_'||handle::text,'type','file',
   'filename',meta->'filename','mime_type',meta->'mime_type','size_bytes',meta->'size_bytes','created_at',meta->'created_at',
   'downloadable',meta->'downloadable','expires_at',meta->'expires_at')),expires_at=(meta->>'expires_at')::timestamptz
 WHERE id=handle AND api_key_id=kid AND provider_id=meta->>'id' AND endpoint='anthropic_messages' AND deleted_at IS NULL
 AND cluster_id=(publication.v1_principal()).cluster_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
END $$;
