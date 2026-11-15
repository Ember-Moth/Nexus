ALTER TABLE publication.tenant_files ADD COLUMN endpoint text NOT NULL DEFAULT 'anthropic_messages'
 CHECK(endpoint IN ('anthropic_messages','openai_responses')), ADD COLUMN expires_at timestamptz;
UPDATE publication.tenant_files SET expires_at=(metadata->>'expires_at')::timestamptz
 WHERE pg_input_is_valid(metadata->>'expires_at','timestamp with time zone');
CREATE INDEX tenant_files_protocol_page ON publication.tenant_files(cluster_id,api_key_id,endpoint,created_at,id)
 WHERE deleted_at IS NULL;
CREATE FUNCTION publication.v1_native_file(handle uuid,kid uuid,ep text) RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('model',model_id,'deployment',deployment_id,'credential',credential_id,
    'credential_version',credential_version,'upstream_version',upstream_version,'provider_id',provider_id)
 FROM publication.tenant_files WHERE id=handle AND api_key_id=kid AND endpoint=ep AND deleted_at IS NULL
 AND (expires_at IS NULL OR expires_at>clock_timestamp()) AND cluster_id=(publication.v1_principal()).cluster_id
$$;
CREATE FUNCTION publication.v1_native_file_save(handle uuid,kid uuid,model uuid,d jsonb,meta jsonb,ep text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE view jsonb; expiry timestamptz; created timestamptz := clock_timestamp();
BEGIN
 IF ep='anthropic_messages' AND meta->>'type'='file' THEN
  view=jsonb_strip_nulls(jsonb_build_object('type','file','filename',meta->'filename','mime_type',meta->'mime_type',
     'created_at',meta->'created_at','size_bytes',meta->'size_bytes','downloadable',meta->'downloadable','expires_at',meta->'expires_at'));
  expiry=(meta->>'expires_at')::timestamptz;
 ELSIF ep='openai_responses' AND meta->>'object'='file' AND jsonb_typeof(meta->'created_at')='number'
 AND jsonb_typeof(meta->'purpose')='string' THEN
  SELECT jsonb_object_agg(key,value) INTO view FROM jsonb_each(meta)
   WHERE key IN ('object','bytes','created_at','expires_at','filename','purpose','status','status_details');
  expiry=to_timestamp((meta->>'expires_at')::bigint);
  created=to_timestamp((meta->>'created_at')::bigint);
 ELSE RAISE EXCEPTION 'invalid_file_metadata'; END IF;
 INSERT INTO publication.tenant_files(id,api_key_id,cluster_id,model_id,deployment_id,credential_id,credential_version,
    upstream_version,provider_id,metadata,endpoint,expires_at,created_at)
 VALUES(handle,kid,(publication.v1_principal()).cluster_id,model,(d->>'id')::uuid,(d->>'credential_id')::uuid,
    (d->>'credential_version')::int,(d->>'upstream_version')::int,meta->>'id',view||jsonb_build_object('id','file_nx_'||handle::text),ep,expiry,created)
 ON CONFLICT(id) DO NOTHING;
 IF NOT EXISTS(SELECT FROM publication.tenant_files WHERE id=handle AND api_key_id=kid AND model_id=model AND endpoint=ep
  AND cluster_id=(publication.v1_principal()).cluster_id AND deployment_id=(d->>'id')::uuid AND provider_id=meta->>'id'
  AND deleted_at IS NULL) THEN RAISE EXCEPTION 'resource_conflict'; END IF;
END $$;
CREATE FUNCTION publication.v1_native_files(kid uuid,stamp uuid,newer boolean,n integer,ep text,ascending boolean,purpose text)
RETURNS SETOF jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE cluster uuid := (publication.v1_principal()).cluster_id; at_cursor timestamptz;
BEGIN
 IF ep NOT IN ('anthropic_messages','openai_responses') OR n NOT BETWEEN 1 AND
  (CASE ep WHEN 'openai_responses' THEN 10001 ELSE 1001 END) THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 IF stamp IS NOT NULL THEN
  SELECT created_at INTO at_cursor FROM publication.tenant_files WHERE id=stamp AND api_key_id=kid AND endpoint=ep AND cluster_id=cluster;
  IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 END IF;
 RETURN QUERY SELECT metadata FROM publication.tenant_files WHERE api_key_id=kid AND cluster_id=cluster AND endpoint=ep
 AND deleted_at IS NULL AND (expires_at IS NULL OR expires_at>clock_timestamp()) AND (purpose IS NULL OR metadata->>'purpose'=purpose)
 AND (stamp IS NULL OR CASE WHEN newer THEN (created_at,id)>(at_cursor,stamp) ELSE (created_at,id)<(at_cursor,stamp) END)
 ORDER BY CASE WHEN ascending THEN created_at END ASC,CASE WHEN ascending THEN id END ASC,
   CASE WHEN NOT ascending THEN created_at END DESC,CASE WHEN NOT ascending THEN id END DESC LIMIT n;
END $$;
CREATE OR REPLACE FUNCTION publication.v1_files(kid uuid,stamp uuid,newer boolean,n integer) RETURNS SETOF jsonb
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT * FROM publication.v1_native_files(kid,stamp,newer,n,'anthropic_messages',false,NULL)
$$;
CREATE OR REPLACE FUNCTION publication.v1_file(handle uuid,kid uuid) RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT publication.v1_native_file(handle,kid,'anthropic_messages')
$$;
CREATE OR REPLACE FUNCTION publication.v1_file_save(handle uuid,kid uuid,model uuid,d jsonb,meta jsonb) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT publication.v1_native_file_save(handle,kid,model,d,meta,'anthropic_messages')
$$;
CREATE INDEX tenant_files_upstream_id ON publication.tenant_files(cluster_id,api_key_id,deployment_id,provider_id);
CREATE FUNCTION publication.v1_openai_file_id(kid uuid,dep uuid,pid text) RETURNS uuid
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT id FROM publication.tenant_files WHERE api_key_id=kid AND cluster_id=(publication.v1_principal()).cluster_id
 AND deployment_id=dep AND provider_id=pid AND endpoint='openai_responses'
 ORDER BY (deleted_at IS NULL) DESC,created_at DESC,id DESC LIMIT 1
$$;
