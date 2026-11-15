CREATE TABLE publication.tenant_files (
 id uuid PRIMARY KEY, api_key_id uuid NOT NULL, cluster_id uuid NOT NULL,
 model_id uuid NOT NULL, deployment_id uuid NOT NULL, credential_id uuid NOT NULL, credential_version integer NOT NULL,
 upstream_version integer NOT NULL, provider_id text NOT NULL CHECK(provider_id ~ '^[A-Za-z0-9_-]{1,200}$'),
 metadata jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(), deleted_at timestamptz
);
CREATE INDEX tenant_file_page ON publication.tenant_files(cluster_id,api_key_id,created_at,id) WHERE deleted_at IS NULL;
CREATE FUNCTION publication.v1_file(handle uuid,kid uuid) RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('model',model_id,'deployment',deployment_id,'credential',credential_id,'credential_version',credential_version,
 'upstream_version',upstream_version,'provider_id',provider_id)
 FROM publication.tenant_files WHERE id=handle AND api_key_id=kid AND cluster_id=(publication.v1_principal()).cluster_id AND deleted_at IS NULL
$$;
CREATE FUNCTION publication.v1_file_save(handle uuid,kid uuid,model uuid,d jsonb,meta jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 INSERT INTO publication.tenant_files(id,api_key_id,cluster_id,model_id,deployment_id,credential_id,credential_version,upstream_version,provider_id,metadata)
 VALUES(handle,kid,(publication.v1_principal()).cluster_id,model,(d->>'id')::uuid,(d->>'credential_id')::uuid,
 (d->>'credential_version')::int,(d->>'upstream_version')::int,meta->>'id',jsonb_strip_nulls(jsonb_build_object(
 'id','file_nx_'||handle::text,'type','file','filename',meta->'filename','mime_type',meta->'mime_type',
 'created_at',meta->'created_at','size_bytes',meta->'size_bytes','downloadable',meta->'downloadable','expires_at',meta->'expires_at')));
END $$;
CREATE FUNCTION publication.v1_files(kid uuid, stamp uuid, newer boolean, n integer) RETURNS SETOF jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE cluster uuid := (publication.v1_principal()).cluster_id; cursor_at timestamptz;
BEGIN
 IF stamp IS NOT NULL THEN
  SELECT created_at INTO cursor_at FROM publication.tenant_files WHERE id=stamp AND api_key_id=kid AND cluster_id=cluster;
  IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 END IF;
 IF n NOT BETWEEN 1 AND 1001 THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 RETURN QUERY SELECT metadata FROM publication.tenant_files WHERE api_key_id=kid AND cluster_id=cluster AND deleted_at IS NULL AND
 (stamp IS NULL OR CASE WHEN newer THEN (created_at,id)>(cursor_at,stamp) ELSE (created_at,id)<(cursor_at,stamp) END)
 ORDER BY created_at DESC,id DESC LIMIT n;
END $$;
CREATE FUNCTION publication.v1_file_remove(handle uuid,kid uuid) RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 UPDATE publication.tenant_files SET deleted_at=clock_timestamp() WHERE id=handle AND api_key_id=kid AND cluster_id=(publication.v1_principal()).cluster_id AND deleted_at IS NULL
$$;
