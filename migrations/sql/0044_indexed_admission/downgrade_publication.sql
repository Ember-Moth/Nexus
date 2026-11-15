-- Definitions as of 0043, restored by the 0044 downgrade (pg_get_functiondef output).
CREATE OR REPLACE FUNCTION publication.v1_snapshot(rev bigint, for_admission boolean)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE p publication.gateway_principals; s publication.config_snapshots; desired bigint;
BEGIN
 p=publication.v1_principal();
 SELECT desired_revision INTO desired FROM publication.gateway_clusters WHERE id=p.cluster_id;
 SELECT * INTO s FROM publication.config_snapshots WHERE cluster_id=p.cluster_id AND revision=coalesce(rev,desired);
 IF NOT FOUND THEN RAISE EXCEPTION 'snapshot_unavailable'; END IF;
 IF NOT EXISTS(SELECT FROM publication.gateway_clusters c WHERE c.id=p.cluster_id
   AND c.scope_generation=(s.source_revision->>'scope_generation')::integer
   AND (c.key_version=(s.source_revision->>'key_version')::integer OR EXISTS(
    SELECT FROM publication.previous_keys k WHERE k.cluster_id=c.id
    AND k.key_version=(s.source_revision->>'key_version')::integer AND k.valid_until>clock_timestamp())))
 THEN RAISE EXCEPTION 'snapshot_scope_expired'; END IF;
 IF for_admission AND (p.applied_revision IS DISTINCT FROM rev OR (rev<>desired AND NOT EXISTS(
   SELECT FROM publication.config_snapshots newest WHERE newest.cluster_id=p.cluster_id AND newest.revision=desired
   AND p.applied_at<=newest.created_at AND clock_timestamp()<newest.created_at+interval '5 seconds')))
 THEN RAISE EXCEPTION 'revision_not_current'; END IF;
 RETURN jsonb_build_object('node_id',p.node_id,'applied_revision',p.applied_revision::text,'manifest',s.manifest,'sha256',s.sha256,'bytes',encode(s.payload_bytes,'base64'));
END $function$;

CREATE OR REPLACE FUNCTION publication.v1_envelope(rev bigint, cid uuid, ver integer)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE p publication.gateway_principals; e publication.credential_envelopes;
BEGIN
 p=publication.v1_principal();
 PERFORM publication.v1_snapshot(rev,false);
 SELECT * INTO e FROM publication.credential_envelopes WHERE cluster_id=p.cluster_id AND revision=rev
 AND credential_id=cid AND credential_version=ver;
 IF NOT FOUND THEN RAISE EXCEPTION 'credential_unavailable'; END IF;
 RETURN jsonb_build_object('algorithm','libsodium-sealed-box','key_version',e.key_version,'ciphertext',encode(e.ciphertext,'base64'));
END $function$;

CREATE OR REPLACE FUNCTION publication.v1_ack(rev bigint, hash text, applied boolean, reason text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
DECLARE p publication.gateway_principals; snap jsonb;
BEGIN
 -- Serialize acknowledgements on this node without upgrading a shared row lock.
 p=publication.v1_principal(true);
 snap=publication.v1_snapshot(rev,false);
 IF hash IS DISTINCT FROM snap->>'sha256' OR applied IS NULL OR reason IS NULL OR reason !~ '^[a-z0-9_]{0,64}$'
 THEN RAISE EXCEPTION 'invalid_ack'; END IF;
 INSERT INTO publication.node_config_acks(node_id,cluster_id,revision,digest,applied,error_code)
 VALUES(p.node_id,p.cluster_id,rev,hash,applied,reason);
 IF applied THEN
  IF EXISTS(SELECT FROM jsonb_array_elements_text(snap->'manifest'->'required_capabilities') c
            WHERE NOT (c=ANY(p.capabilities))) THEN RAISE EXCEPTION 'unsupported_capability'; END IF;
  UPDATE publication.gateway_principals SET applied_revision=rev,applied_at=clock_timestamp()
   WHERE node_id=p.node_id AND applied_revision<rev;
 END IF;
 RETURN jsonb_build_object('revision',rev::text,'applied',applied);
END $function$;

