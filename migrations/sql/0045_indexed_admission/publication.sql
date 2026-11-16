-- Every gateway call names its principal. Admission holds these row locks while it queues on
-- the organization's lock, so they are the weakest that still keep the rows from being deleted
-- or re-keyed: FOR KEY SHARE does not block updates of other columns, so a release (desired and
-- distributed revision, key sequence), a scope change, an acknowledgement (applied revision)
-- or a deactivation never waits behind queued admissions. Acknowledgements of one node still
-- serialize on FOR NO KEY UPDATE.
CREATE OR REPLACE FUNCTION publication.v1_principal(for_ack boolean DEFAULT false)
RETURNS publication.gateway_principals LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals; cid uuid;
BEGIN
 SELECT cluster_id INTO cid FROM publication.gateway_principals WHERE database_role=session_user;
 PERFORM id FROM publication.gateway_clusters WHERE id=cid AND active FOR KEY SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'gateway_unauthorized'; END IF;
 IF for_ack THEN
  SELECT * INTO p FROM publication.gateway_principals WHERE database_role=session_user AND cluster_id=cid AND active FOR NO KEY UPDATE;
 ELSE
  SELECT * INTO p FROM publication.gateway_principals WHERE database_role=session_user AND cluster_id=cid AND active FOR KEY SHARE;
 END IF;
 IF NOT FOUND THEN RAISE EXCEPTION 'gateway_unauthorized'; END IF;
 RETURN p;
END $$;

-- The snapshot a node may use at `rev` (NULL: the desired one), validated as v1_snapshot always
-- did, without reading its manifest or payload.
CREATE FUNCTION publication.v1_revision(p publication.gateway_principals, rev bigint, for_admission boolean)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE c publication.gateway_clusters; s record;
BEGIN
 SELECT * INTO c FROM publication.gateway_clusters WHERE id=p.cluster_id;
 SELECT revision,sha256,source_revision,key_sequence,required_capabilities INTO s
 FROM publication.config_snapshots WHERE cluster_id=p.cluster_id AND revision=coalesce(rev,c.desired_revision);
 IF NOT FOUND THEN RAISE EXCEPTION 'snapshot_unavailable'; END IF;
 IF c.scope_generation IS DISTINCT FROM (s.source_revision->>'scope_generation')::integer
  OR (c.key_version IS DISTINCT FROM (s.source_revision->>'key_version')::integer AND NOT EXISTS(
   SELECT FROM publication.previous_keys k WHERE k.cluster_id=c.id
   AND k.key_version=(s.source_revision->>'key_version')::integer AND k.valid_until>clock_timestamp()))
 THEN RAISE EXCEPTION 'snapshot_scope_expired'; END IF;
 IF for_admission AND (p.applied_revision IS DISTINCT FROM rev OR (rev<>c.desired_revision AND NOT EXISTS(
   SELECT FROM publication.config_snapshots newest WHERE newest.cluster_id=p.cluster_id
   AND newest.revision=c.desired_revision AND p.applied_at<=newest.created_at
   AND clock_timestamp()<newest.created_at+interval '5 seconds')))
 THEN RAISE EXCEPTION 'revision_not_current'; END IF;
 RETURN jsonb_build_object('node_id',p.node_id,'cluster_id',p.cluster_id,'revision',s.revision::text,
  'applied_revision',p.applied_revision::text,'sha256',s.sha256,'key_sequence',s.key_sequence::text,
  'required_capabilities',to_jsonb(s.required_capabilities));
END $$;

-- Other gateway contracts read resources of a revision through its manifest. The manifest no
-- longer lists API keys, and the base64 copy of the payload is gone.
CREATE OR REPLACE FUNCTION publication.v1_snapshot(rev bigint, for_admission boolean)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals; r jsonb;
BEGIN
 p=publication.v1_principal(); r=publication.v1_revision(p,rev,for_admission);
 RETURN r || jsonb_build_object('manifest',(SELECT s.manifest FROM publication.config_snapshots s
  WHERE s.cluster_id=p.cluster_id AND s.revision=(r->>'revision')::bigint));
END $$;

CREATE OR REPLACE FUNCTION publication.v1_envelope(rev bigint, cid uuid, ver integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals; e publication.credential_envelopes;
BEGIN
 p=publication.v1_principal();
 PERFORM publication.v1_revision(p,rev,false);
 SELECT * INTO e FROM publication.credential_envelopes WHERE cluster_id=p.cluster_id AND revision=rev
 AND credential_id=cid AND credential_version=ver;
 IF NOT FOUND THEN RAISE EXCEPTION 'credential_unavailable'; END IF;
 RETURN jsonb_build_object('algorithm','libsodium-sealed-box','key_version',e.key_version,'ciphertext',encode(e.ciphertext,'base64'));
END $$;

CREATE OR REPLACE FUNCTION publication.v1_ack(rev bigint, hash text, applied boolean, reason text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals; r jsonb;
BEGIN
 -- Serialize acknowledgements on this node without upgrading a shared row lock.
 p=publication.v1_principal(true);
 r=publication.v1_revision(p,rev,false);
 IF hash IS DISTINCT FROM r->>'sha256' OR applied IS NULL OR reason IS NULL OR reason !~ '^[a-z0-9_]{0,64}$'
 THEN RAISE EXCEPTION 'invalid_ack'; END IF;
 INSERT INTO publication.node_config_acks(node_id,cluster_id,revision,digest,applied,error_code)
 VALUES(p.node_id,p.cluster_id,rev,hash,applied,reason);
 IF applied THEN
  IF EXISTS(SELECT FROM jsonb_array_elements_text(r->'required_capabilities') c
            WHERE NOT (c=ANY(p.capabilities))) THEN RAISE EXCEPTION 'unsupported_capability'; END IF;
  UPDATE publication.gateway_principals SET applied_revision=rev,applied_at=clock_timestamp()
   WHERE node_id=p.node_id AND applied_revision<rev;
 END IF;
 RETURN jsonb_build_object('revision',rev::text,'applied',applied);
END $$;

-- What admission needs from revision `rev`, read by key instead of by expanding the manifest:
-- the key as of the revision's key sequence (null when it was not a member), and a manifest of
-- the same shape as the published one that only holds the organization's routes (for `model`,
-- or all), their deployments and models, and the organization's access policies. The cost does
-- not depend on how many keys, organizations or deployments the cluster has.
-- The caller passes the principal it already holds in this transaction.
CREATE FUNCTION publication.v1_admission(p publication.gateway_principals, rev bigint, kid uuid,
 org uuid, model uuid, for_admission boolean)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE r jsonb; k jsonb; seq bigint; routes jsonb; deps uuid[]; models uuid[];
 nil constant uuid:='00000000-0000-0000-0000-000000000000';
BEGIN
 r=publication.v1_revision(p,rev,for_admission);
 seq=(r->>'key_sequence')::bigint; rev=(r->>'revision')::bigint;
 IF kid IS NOT NULL THEN
  SELECT jsonb_build_object('id',c.key_id,'organization_id',c.organization_id,'project_id',c.project_id,
   'service_account_id',c.service_account_id,'expires_at',c.expires_at) INTO k
  FROM publication.cluster_keys c WHERE c.cluster_id=p.cluster_id AND c.key_id=kid
   AND c.added_sequence<=seq AND (c.removed_sequence IS NULL OR c.removed_sequence>seq);
  org=coalesce(org,(k->>'organization_id')::uuid);
 END IF;
 SELECT coalesce(jsonb_agg(i.body ORDER BY i.secondary_id),'[]'::jsonb),
  array_agg(DISTINCT i.secondary_id) INTO routes,models
 FROM publication.snapshot_index i WHERE i.cluster_id=p.cluster_id AND i.revision=rev
  AND i.kind='route' AND i.primary_id=org AND (model IS NULL OR i.secondary_id=model);
 SELECT array_agg(DISTINCT (c->>'deployment_id')::uuid) INTO deps
 FROM jsonb_array_elements(routes) x, jsonb_array_elements(x->'candidates') c;
 RETURN r || jsonb_build_object('key',k,'manifest',jsonb_build_object(
  'required_capabilities',r->'required_capabilities','resources',jsonb_build_object(
   'routes',routes,
   'deployments',(SELECT coalesce(jsonb_agg(i.body ORDER BY i.primary_id),'[]'::jsonb)
     FROM publication.snapshot_index i WHERE i.cluster_id=p.cluster_id AND i.revision=rev
     AND i.kind='deployment' AND i.primary_id=ANY(deps) AND i.secondary_id=nil),
   'models',(SELECT coalesce(jsonb_agg(i.body ORDER BY i.primary_id),'[]'::jsonb)
     FROM publication.snapshot_index i WHERE i.cluster_id=p.cluster_id AND i.revision=rev
     AND i.kind='model' AND (i.primary_id=ANY(models) OR i.primary_id=model) AND i.secondary_id=nil),
   'access_policies',coalesce((SELECT i.body FROM publication.snapshot_index i
     WHERE i.cluster_id=p.cluster_id AND i.revision=rev AND i.kind='access'
     AND i.primary_id=org AND i.secondary_id=nil),'[]'::jsonb))));
END $$;

-- What a node polls: the desired revision, its digest and key sequence, and what the node has
-- applied. It raises snapshot_scope_expired exactly as the full snapshot read did.
CREATE FUNCTION publication.v1_config_head()
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 RETURN publication.v1_revision(publication.v1_principal(),NULL,false);
END $$;

-- The published bytes of one revision, as stored; the node checks them against the digest.
CREATE FUNCTION publication.v1_config_payload(rev bigint)
RETURNS bytea LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals; r jsonb;
BEGIN
 p=publication.v1_principal(); r=publication.v1_revision(p,rev,false);
 RETURN (SELECT s.payload_bytes FROM publication.config_snapshots s
  WHERE s.cluster_id=p.cluster_id AND s.revision=(r->>'revision')::bigint);
END $$;

-- The net key changes from key sequence `since` to the key sequence of revision `rev`, in
-- (sequence, key_id, present) order, after the cursor. A key that left and came back within the
-- range is one removal followed by one addition. From `since` = 0 this is the full key set.
CREATE FUNCTION publication.v1_config_keys(rev bigint, since bigint, after_sequence bigint,
 after_key uuid, after_present boolean, page integer)
RETURNS TABLE(change_sequence bigint, change_key uuid, present boolean, digest text,
 organization_id uuid, project_id uuid, service_account_id uuid, expires_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals; r jsonb; target bigint;
 cs bigint:=coalesce(after_sequence,0); ck uuid:=coalesce(after_key,'00000000-0000-0000-0000-000000000000');
 cp boolean:=coalesce(after_present,false);
BEGIN
 p=publication.v1_principal(); r=publication.v1_revision(p,rev,false); target=(r->>'key_sequence')::bigint;
 IF since IS NULL OR since<0 OR since>target OR page IS NULL OR page NOT BETWEEN 1 AND 10000
 THEN RAISE EXCEPTION 'invalid_cursor'; END IF;
 RETURN QUERY SELECT x.s,x.k,x.present,x.digest,x.organization_id,x.project_id,x.service_account_id,x.expires_at FROM (
  SELECT c.added_sequence s,c.key_id k,true present,c.digest,c.organization_id,c.project_id,
   c.service_account_id,c.expires_at
  FROM publication.cluster_keys c WHERE c.cluster_id=p.cluster_id
   AND c.added_sequence>greatest(since,cs-1) AND c.added_sequence<=target
   AND (c.removed_sequence IS NULL OR c.removed_sequence>target)
   AND (c.added_sequence,c.key_id,true)>(cs,ck,cp)
  UNION ALL
  SELECT c.removed_sequence,c.key_id,false,NULL,NULL,NULL,NULL,NULL
  FROM publication.cluster_keys c WHERE c.cluster_id=p.cluster_id
   AND c.removed_sequence>greatest(since,cs-1) AND c.removed_sequence<=target AND c.added_sequence<=since
   AND (c.removed_sequence,c.key_id,false)>(cs,ck,cp)
 ) x ORDER BY x.s,x.k,x.present LIMIT page;
END $$;

-- Record the cluster's key set for the next release (the caller holds the cluster row lock).
-- Keys that left or changed get removed_sequence; new or changed keys get a new row. Returns the
-- key sequence the release must name: the next one when anything changed, else the current one.
-- Removed rows that no active node can still ask for are deleted.
CREATE FUNCTION publication.stage_keys(cid uuid, keys jsonb)
RETURNS bigint LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE cur_seq bigint; next_seq bigint; changed bigint:=0; n bigint; horizon bigint;
BEGIN
 SELECT key_sequence INTO STRICT cur_seq FROM publication.gateway_clusters WHERE id=cid FOR NO KEY UPDATE;
 next_seq=cur_seq+1;
 WITH incoming AS MATERIALIZED (SELECT * FROM jsonb_to_recordset(keys) AS x(id uuid, digest text,
  organization_id uuid, project_id uuid, service_account_id uuid, expires_at timestamptz))
 UPDATE publication.cluster_keys k SET removed_sequence=next_seq
 WHERE k.cluster_id=cid AND k.removed_sequence IS NULL AND NOT EXISTS(SELECT FROM incoming i
  WHERE i.id=k.key_id AND i.digest=k.digest AND i.organization_id=k.organization_id
  AND i.project_id=k.project_id AND i.service_account_id=k.service_account_id
  AND i.expires_at IS NOT DISTINCT FROM k.expires_at);
 GET DIAGNOSTICS n=ROW_COUNT; changed=changed+n;
 INSERT INTO publication.cluster_keys(cluster_id,key_id,added_sequence,digest,organization_id,
  project_id,service_account_id,expires_at)
 SELECT cid,i.id,next_seq,i.digest,i.organization_id,i.project_id,i.service_account_id,i.expires_at
 FROM jsonb_to_recordset(keys) AS i(id uuid, digest text, organization_id uuid, project_id uuid,
  service_account_id uuid, expires_at timestamptz)
 WHERE NOT EXISTS(SELECT FROM publication.cluster_keys k
  WHERE k.cluster_id=cid AND k.key_id=i.id AND k.removed_sequence IS NULL);
 GET DIAGNOSTICS n=ROW_COUNT; changed=changed+n;
 SELECT min(s.key_sequence) INTO horizon FROM publication.gateway_principals g
 JOIN publication.config_snapshots s ON s.cluster_id=g.cluster_id AND s.revision=g.applied_revision
 WHERE g.cluster_id=cid AND g.active;
 IF horizon IS NOT NULL THEN
  DELETE FROM publication.cluster_keys WHERE cluster_id=cid AND removed_sequence<=horizon;
 END IF;
 IF changed=0 THEN RETURN cur_seq; END IF;
 UPDATE publication.gateway_clusters SET key_sequence=next_seq WHERE id=cid;
 RETURN next_seq;
END $$;

-- Index a stored revision for admission, and drop the index of revisions that were superseded
-- more than a day ago (no request can still start an attempt against them).
CREATE FUNCTION publication.index_snapshot(cid uuid, rev bigint)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE nil constant uuid:='00000000-0000-0000-0000-000000000000'; m jsonb; stale bigint;
BEGIN
 SELECT s.manifest->'resources' INTO STRICT m FROM publication.config_snapshots s
 WHERE s.cluster_id=cid AND s.revision=rev;
 INSERT INTO publication.snapshot_index(cluster_id,revision,kind,primary_id,secondary_id,body)
 SELECT cid,rev,'route',(r->>'organization_id')::uuid,(r->>'model_id')::uuid,r
  FROM jsonb_array_elements(m->'routes') r
 UNION ALL SELECT cid,rev,'deployment',(d->>'id')::uuid,nil,d FROM jsonb_array_elements(m->'deployments') d
 UNION ALL SELECT cid,rev,'model',(x->>'id')::uuid,nil,x FROM jsonb_array_elements(m->'models') x
 UNION ALL SELECT cid,rev,'access',(a->>'organization_id')::uuid,nil,jsonb_agg(a)
  FROM jsonb_array_elements(m->'access_policies') a GROUP BY (a->>'organization_id')::uuid;
 SELECT max(s.revision) INTO stale FROM publication.config_snapshots s
 WHERE s.cluster_id=cid AND s.revision<rev AND s.created_at<clock_timestamp()-interval '1 day';
 IF stale IS NOT NULL THEN
  DELETE FROM publication.snapshot_index WHERE cluster_id=cid AND revision<stale;
 END IF;
END $$;
