CREATE TABLE publication.conversations(
 id uuid PRIMARY KEY, api_key_id uuid NOT NULL, cluster_id uuid NOT NULL, model_id uuid NOT NULL,
 deployment_id uuid NOT NULL, credential_id uuid NOT NULL, credential_version integer NOT NULL,
 upstream_version integer NOT NULL, provider_id text NOT NULL CHECK(provider_id ~ '^[A-Za-z0-9_-]{1,200}$'),
 deleted_at timestamptz,
 UNIQUE(cluster_id,api_key_id,deployment_id,provider_id)
);
CREATE FUNCTION publication.v1_conversation(handle uuid,kid uuid) RETURNS jsonb LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,pg_temp AS $$
 SELECT to_jsonb(c) FROM publication.conversations c WHERE id=handle AND api_key_id=kid AND deleted_at IS NULL
 AND cluster_id=(publication.v1_principal()).cluster_id
$$;
CREATE FUNCTION publication.v1_conversation_id(kid uuid,dep uuid,pid text) RETURNS uuid LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,pg_temp AS $$
 SELECT id FROM publication.conversations WHERE api_key_id=kid AND deployment_id=dep AND provider_id=pid
 AND cluster_id=(publication.v1_principal()).cluster_id
$$;
CREATE FUNCTION publication.v1_conversation_save(handle uuid,kid uuid,d jsonb,pid text) RETURNS void LANGUAGE plpgsql SECURITY DEFINER
 SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 INSERT INTO publication.conversations(id,api_key_id,cluster_id,model_id,deployment_id,credential_id,credential_version,
   upstream_version,provider_id) VALUES(handle,kid,(publication.v1_principal()).cluster_id,(d->>'model_id')::uuid,
   (d->>'id')::uuid,(d->>'credential_id')::uuid,(d->>'credential_version')::int,(d->>'upstream_version')::int,pid)
 ON CONFLICT(id) DO NOTHING;
 IF publication.v1_conversation_id(kid,(d->>'id')::uuid,pid) IS DISTINCT FROM handle THEN RAISE EXCEPTION 'resource_conflict'; END IF;
END $$;
CREATE FUNCTION publication.v1_conversation_remove(handle uuid,kid uuid) RETURNS void LANGUAGE sql SECURITY DEFINER
 SET search_path=pg_catalog,pg_temp AS $$
 UPDATE publication.conversations SET deleted_at=clock_timestamp() WHERE id=handle AND api_key_id=kid AND deleted_at IS NULL
 AND cluster_id=(publication.v1_principal()).cluster_id
$$;
