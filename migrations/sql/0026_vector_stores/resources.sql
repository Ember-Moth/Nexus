CREATE TABLE publication.vector_stores (
 id uuid PRIMARY KEY, cluster_id uuid NOT NULL, api_key_id uuid NOT NULL, owner_node uuid NOT NULL,
 poll_node uuid, model_id uuid NOT NULL,deployment_id uuid NOT NULL,credential_id uuid NOT NULL,
 credential_version integer NOT NULL,upstream_version integer NOT NULL,
 provider_id text CHECK(provider_id ~ '^[A-Za-z0-9_-]{1,200}$'),
 state text NOT NULL DEFAULT 'submitting' CHECK(state IN ('submitting','active','expired','deleted','unknown','rejected')),
 metadata jsonb, provider_created_at timestamptz, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 observed_at timestamptz, next_poll timestamptz NOT NULL DEFAULT clock_timestamp()+interval '2 minutes',
 UNIQUE(cluster_id,api_key_id,deployment_id,provider_id)
);
CREATE INDEX vector_store_owner ON publication.vector_stores(cluster_id,api_key_id,provider_created_at,id);
CREATE INDEX vector_store_poll ON publication.vector_stores(next_poll) WHERE state IN ('submitting','active');
CREATE FUNCTION publication.v1_vector_prepare(handle uuid,kid uuid,d jsonb)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals := publication.v1_principal();
BEGIN
 INSERT INTO publication.vector_stores(id,cluster_id,api_key_id,owner_node,model_id,deployment_id,credential_id,credential_version,upstream_version)
 VALUES(handle,p.cluster_id,kid,p.node_id,(d->>'model_id')::uuid,(d->>'id')::uuid,(d->>'credential_id')::uuid,
   (d->>'credential_version')::integer,(d->>'upstream_version')::integer);
END $$;
CREATE FUNCTION publication.v1_vector(handle uuid,kid uuid) RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT to_jsonb(v) FROM publication.vector_stores v WHERE id=handle AND cluster_id=(publication.v1_principal()).cluster_id
 AND (kid IS NULL AND (publication.v1_principal()).node_id IN (owner_node,poll_node) OR api_key_id=kid)
$$;
CREATE FUNCTION publication.v1_vectors(kid uuid,stamp uuid,asc_order boolean,n integer,allowed uuid[]) RETURNS SETOF jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE cluster uuid := (publication.v1_principal()).cluster_id; cursor_at timestamptz;
BEGIN
 IF n NOT BETWEEN 1 AND 101 THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 IF stamp IS NOT NULL THEN
  SELECT provider_created_at INTO cursor_at FROM publication.vector_stores WHERE id=stamp AND api_key_id=kid AND cluster_id=cluster;
  IF NOT FOUND THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 END IF;
 RETURN QUERY SELECT metadata FROM publication.vector_stores WHERE api_key_id=kid AND cluster_id=cluster AND state IN ('active','expired') AND model_id=ANY(allowed)
 AND (stamp IS NULL OR CASE WHEN asc_order THEN (provider_created_at,id)>(cursor_at,stamp) ELSE (provider_created_at,id)<(cursor_at,stamp) END)
 ORDER BY CASE WHEN asc_order THEN provider_created_at END ASC, CASE WHEN asc_order THEN id END ASC,provider_created_at DESC,id DESC LIMIT n;
END $$;
CREATE FUNCTION publication.v1_vector_update(handle uuid,kid uuid,pid text,stamp timestamptz,metadata_in jsonb,new_state text)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE v publication.vector_stores; recovering boolean;
BEGIN
 SELECT * INTO v FROM publication.vector_stores WHERE id=handle AND cluster_id=(publication.v1_principal()).cluster_id
 AND (kid IS NULL AND (publication.v1_principal()).node_id IN (owner_node,poll_node) OR api_key_id=kid) FOR UPDATE;
 IF NOT FOUND OR v.provider_id IS NOT NULL AND v.provider_id IS DISTINCT FROM pid THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 recovering=v.state='unknown' AND v.provider_id IS NULL AND pid IS NOT NULL AND kid=v.api_key_id
  AND v.owner_node=(publication.v1_principal()).node_id AND new_state IN ('active','expired');
 IF NOT coalesce(recovering,false) AND (v.state IN ('deleted','rejected','unknown') OR (v.observed_at>=stamp AND new_state<>'deleted')) THEN RETURN false; END IF;
 IF new_state IN ('active','expired') AND (pid IS NULL OR metadata_in->>'id' IS DISTINCT FROM 'vs_nx_'||handle::text
    OR metadata_in->>'object' IS DISTINCT FROM 'vector_store' OR jsonb_typeof(metadata_in->'created_at') IS DISTINCT FROM 'number'
    OR (v.provider_created_at IS NOT NULL AND v.provider_created_at IS DISTINCT FROM to_timestamp((metadata_in->>'created_at')::bigint))) THEN RAISE EXCEPTION 'invalid_vector_metadata'; END IF;
 IF new_state IN ('rejected','unknown') AND v.state<>'submitting' THEN RAISE EXCEPTION 'resource_conflict'; END IF;
 UPDATE publication.vector_stores SET provider_id=pid,state=new_state,observed_at=greatest(v.observed_at,stamp),
    metadata=coalesce(metadata_in,v.metadata),provider_created_at=coalesce(v.provider_created_at,to_timestamp((metadata_in->>'created_at')::bigint)),next_poll=clock_timestamp()+interval '1 minute' WHERE id=handle;
 RETURN true;
END $$;
CREATE FUNCTION publication.v1_vector_claim() RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE job publication.vector_stores;
BEGIN
 WITH candidate AS (SELECT id FROM publication.vector_stores WHERE cluster_id=(publication.v1_principal()).cluster_id
  AND state IN ('submitting','active') AND next_poll<=clock_timestamp() ORDER BY next_poll LIMIT 1 FOR UPDATE SKIP LOCKED)
 UPDATE publication.vector_stores v SET poll_node=(publication.v1_principal()).node_id,next_poll=clock_timestamp()+interval '1 minute' FROM candidate c WHERE v.id=c.id RETURNING v.* INTO job;
 IF job.id IS NULL THEN RETURN NULL; END IF; RETURN to_jsonb(job);
END $$;
CREATE TABLE publication.vector_file_batches (
 id uuid PRIMARY KEY,store_id uuid NOT NULL REFERENCES publication.vector_stores,provider_id text NOT NULL CHECK(provider_id ~ '^[A-Za-z0-9_-]{1,200}$'),
 UNIQUE(store_id,provider_id)
);
CREATE FUNCTION publication.v1_vector_batch(store uuid,id_in uuid,pid text,create_in boolean) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE result text;
BEGIN
 IF create_in THEN INSERT INTO publication.vector_file_batches(id,store_id,provider_id)
   VALUES(id_in,store,pid) ON CONFLICT(id) DO NOTHING; END IF;
 SELECT provider_id INTO result FROM publication.vector_file_batches WHERE id=id_in AND store_id=store;
 IF result IS NULL OR (create_in AND result IS DISTINCT FROM pid) THEN RAISE EXCEPTION 'resource_not_found'; END IF;
 RETURN result;
END $$;
CREATE FUNCTION publication.v1_vector_id(kid uuid,dep uuid,pid text) RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT id FROM publication.vector_stores WHERE api_key_id=kid AND deployment_id=dep AND provider_id=pid
 AND cluster_id=(publication.v1_principal()).cluster_id
$$;
CREATE FUNCTION publication.v1_vector_due(handle uuid,kid uuid) RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 UPDATE publication.vector_stores SET next_poll=least(next_poll,clock_timestamp()) WHERE id=handle AND api_key_id=kid
 AND cluster_id=(publication.v1_principal()).cluster_id AND state='active'
$$;
CREATE FUNCTION publication.v1_vector_mark_missed(handle uuid,stamp timestamptz) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 UPDATE publication.vector_stores SET observed_at=stamp WHERE id=handle AND state IN ('submitting','active') AND provider_id IS NOT NULL
 AND cluster_id=(publication.v1_principal()).cluster_id AND poll_node=(publication.v1_principal()).node_id
 AND stamp>=created_at AND (observed_at IS NULL OR observed_at<stamp);
 RETURN FOUND;
END $$;
-- Keep the locator even if a successful create returned an unreadable size.
-- The poller can subsequently obtain a fresh native receipt without re-creating it.
CREATE FUNCTION publication.v1_vector_locator(handle uuid,kid uuid,pid text) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 UPDATE publication.vector_stores SET provider_id=pid,next_poll=clock_timestamp()
 WHERE id=handle AND api_key_id=kid AND owner_node=(publication.v1_principal()).node_id
 AND cluster_id=(publication.v1_principal()).cluster_id AND state='submitting'
 AND (provider_id IS NULL OR provider_id=pid);
 IF NOT FOUND AND NOT EXISTS(SELECT FROM publication.vector_stores WHERE id=handle AND api_key_id=kid
  AND owner_node=(publication.v1_principal()).node_id AND provider_id=pid) THEN RAISE EXCEPTION 'resource_conflict'; END IF;
END $$;
