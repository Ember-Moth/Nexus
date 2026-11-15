CREATE TABLE publication.stored_responses (
 id uuid PRIMARY KEY, request_id uuid NOT NULL UNIQUE, api_key_id uuid NOT NULL,
 cluster_id uuid NOT NULL REFERENCES publication.gateway_clusters, model_id uuid NOT NULL,
 deployment_id uuid NOT NULL, credential_id uuid NOT NULL, credential_version integer NOT NULL,
 upstream_version integer NOT NULL, upstream_model text NOT NULL, provider_id text NOT NULL
 CHECK(provider_id ~ '^[A-Za-z0-9_-]{1,200}$'), deleted_at timestamptz
);
GRANT SELECT,INSERT,UPDATE ON publication.stored_responses TO nexus_publication;

CREATE FUNCTION publication.v1_stored_response(handle uuid,kid uuid) RETURNS jsonb
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('model',model_id,'deployment',deployment_id,'credential',credential_id,
  'credential_version',credential_version,'upstream_version',upstream_version,
  'upstream_model',upstream_model,'provider_id',provider_id)
 FROM publication.stored_responses WHERE id=handle AND api_key_id=kid AND deleted_at IS NULL
 AND cluster_id=(publication.v1_principal()).cluster_id
$$;
CREATE FUNCTION publication.v1_save_response(handle uuid,rid uuid,kid uuid,model uuid,d jsonb,pid text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 INSERT INTO publication.stored_responses(id,request_id,api_key_id,cluster_id,model_id,deployment_id,
 credential_id,credential_version,upstream_version,upstream_model,provider_id)
 VALUES(handle,rid,kid,(publication.v1_principal()).cluster_id,model,(d->>'id')::uuid,
 (d->>'credential_id')::uuid,(d->>'credential_version')::int,(d->>'upstream_version')::int,d->>'upstream_model',pid)
 ON CONFLICT(id) DO NOTHING;
 IF NOT EXISTS(SELECT FROM publication.stored_responses WHERE id=handle AND request_id=rid AND
 api_key_id=kid AND provider_id=pid AND deployment_id=(d->>'id')::uuid AND deleted_at IS NULL)
 THEN RAISE EXCEPTION 'resource_conflict'; END IF;
END $$;
CREATE FUNCTION publication.v1_forget_response(handle uuid,kid uuid) RETURNS void
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 UPDATE publication.stored_responses SET deleted_at=clock_timestamp() WHERE id=handle AND api_key_id=kid
 AND cluster_id=(publication.v1_principal()).cluster_id AND deleted_at IS NULL
$$;
