CREATE FUNCTION publication.v1_principal(for_ack boolean DEFAULT false)
RETURNS publication.gateway_principals LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals; cid uuid;
BEGIN
 SELECT cluster_id INTO cid FROM publication.gateway_principals WHERE database_role=session_user;
 PERFORM id FROM publication.gateway_clusters WHERE id=cid AND active FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'gateway_unauthorized'; END IF;
 IF for_ack THEN
  SELECT * INTO p FROM publication.gateway_principals WHERE database_role=session_user AND cluster_id=cid AND active FOR UPDATE;
 ELSE
  SELECT * INTO p FROM publication.gateway_principals WHERE database_role=session_user AND cluster_id=cid AND active FOR SHARE;
 END IF;
 IF NOT FOUND THEN RAISE EXCEPTION 'gateway_unauthorized'; END IF;
 RETURN p;
END $$;

CREATE FUNCTION publication.v1_snapshot(rev bigint, for_admission boolean)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
END $$;

CREATE FUNCTION publication.v1_envelope(rev bigint, cid uuid, ver integer)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE p publication.gateway_principals; e publication.credential_envelopes;
BEGIN
 p=publication.v1_principal();
 PERFORM publication.v1_snapshot(rev,false);
 SELECT * INTO e FROM publication.credential_envelopes WHERE cluster_id=p.cluster_id AND revision=rev
 AND credential_id=cid AND credential_version=ver;
 IF NOT FOUND THEN RAISE EXCEPTION 'credential_unavailable'; END IF;
 RETURN jsonb_build_object('algorithm','libsodium-sealed-box','key_version',e.key_version,'ciphertext',encode(e.ciphertext,'base64'));
END $$;

CREATE FUNCTION publication.v1_ack(rev bigint, hash text, applied boolean, reason text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
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
END $$;

CREATE FUNCTION identity.v1_authorize(kid uuid, model uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE k identity.api_keys;
BEGIN
 PERFORM revision FROM identity.source_revision WHERE id=1 FOR SHARE;
 SELECT ak.* INTO k FROM identity.api_keys ak JOIN identity.organizations o ON o.id=ak.organization_id
 JOIN identity.projects pr ON pr.id=ak.project_id JOIN identity.service_accounts sa ON sa.id=ak.service_account_id
 WHERE ak.id=kid AND o.status='active' AND pr.status='active' AND sa.status='active'
 AND ak.revoked_at IS NULL AND (ak.expires_at IS NULL OR ak.expires_at>clock_timestamp());
 IF NOT FOUND THEN RAISE EXCEPTION 'key_inactive'; END IF;
 IF NOT EXISTS(SELECT FROM identity.access_policies WHERE organization_id=k.organization_id
 AND layer='platform' AND scope_type='organization') OR EXISTS(
 SELECT FROM identity.access_policies ap WHERE ap.organization_id=k.organization_id AND
 (ap.scope_type='organization' OR ap.scope_type='project' AND ap.scope_id=k.project_id
 OR ap.scope_type='key' AND ap.scope_id=kid) AND NOT EXISTS(
 SELECT FROM identity.access_policy_models pm WHERE pm.policy_id=ap.id AND pm.model_id=model))
 THEN RAISE EXCEPTION 'model_forbidden'; END IF;
 RETURN jsonb_build_object('organization_id',k.organization_id,'project_id',k.project_id,
 'service_account_id',k.service_account_id,'api_key_id',k.id);
END $$;

CREATE FUNCTION catalog.v1_eligible(model uuid, dep uuid, snapshot jsonb, org uuid, inputs bigint, outputs bigint)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE m catalog.models; candidate jsonb; resource jsonb;
BEGIN
 PERFORM revision FROM catalog.source_revision WHERE id=1 FOR SHARE;
 SELECT * INTO m FROM catalog.models WHERE id=model AND status='active';
 SELECT v INTO resource FROM jsonb_array_elements(snapshot->'resources'->'models') v WHERE v->>'id'=model::text;
 IF m.id IS NULL OR resource IS NULL OR (resource->>'version')::int<>m.version THEN RAISE EXCEPTION 'model_unavailable'; END IF;
 IF inputs IS NULL OR outputs IS NULL OR inputs NOT BETWEEN 0 AND m.max_input_tokens OR outputs NOT BETWEEN 0 AND m.max_output_tokens
 THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 FOR candidate IN SELECT d FROM jsonb_array_elements(snapshot->'resources'->'deployments') d
 WHERE d->>'model_id'=model::text AND (dep IS NULL OR d->>'id'=dep::text) AND EXISTS(
 SELECT FROM jsonb_array_elements(snapshot->'resources'->'routes') route,
 jsonb_array_elements(route->'candidates') c WHERE route->>'organization_id'=org::text
 AND route->>'model_id'=model::text AND c->>'deployment_id'=d->>'id') LOOP
  IF EXISTS(SELECT FROM catalog.deployments d JOIN catalog.upstreams u ON u.id=d.upstream_id
    JOIN catalog.upstream_credentials c ON c.id=d.credential_id WHERE d.id=(candidate->>'id')::uuid
    AND d.model_id=model AND d.status='active' AND u.status='active' AND c.status='active'
    AND d.version=(candidate->>'version')::int AND u.version=(candidate->>'upstream_version')::int
    AND c.id=(candidate->>'credential_id')::uuid AND c.version=(candidate->>'credential_version')::int
    AND m.protocols ? u.protocol_family) THEN RETURN m.token_dimensions; END IF;
 END LOOP;
 RAISE EXCEPTION 'deployment_unavailable';
END $$;

CREATE FUNCTION billing.v1_status(rid uuid,node uuid)
RETURNS jsonb LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT jsonb_build_object('request_id',id,'node_id',node_id,'organization_id',organization_id,
 'api_key_id',api_key_id,'model_id',model_id,'config_revision',config_revision::text,'input_limit',input_limit,
 'output_limit',output_limit,'state',state,'reservation_state',reservation_state,'reserved_amount',reserved_amount::text,
 'sale_price_version_id',sale_price_version_id,'discount_version_id',discount_version_id,'charged_amount',charged_amount::text,
 'review_reason',review_reason,'processing_state',(SELECT j.state FROM billing.usage_jobs j WHERE j.request_id=rid),
 'closed',EXISTS(SELECT FROM billing.usage_events e WHERE e.request_id=rid AND e.kind='request.closed'),
 'attempts',coalesce((SELECT jsonb_agg(jsonb_build_object('id',a.id,'sequence',a.sequence,'state',a.state)
 ORDER BY a.sequence) FROM billing.request_attempts a WHERE a.request_id=rid),'[]'::jsonb))
 FROM billing.request_runs WHERE id=rid AND node_id=node
$$;

CREATE FUNCTION billing.v1_cancel(rid uuid,node uuid,reason text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE req billing.request_runs;
BEGIN
 IF reason IS NULL OR reason !~ '^[a-z0-9_]{1,64}$' THEN RAISE EXCEPTION 'invalid_reason'; END IF;
 SELECT * INTO req FROM billing.request_runs WHERE id=rid AND node_id=node;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF;
 PERFORM id FROM billing.accounts WHERE id=req.account_id FOR UPDATE;
 SELECT * INTO req FROM billing.request_runs WHERE id=rid FOR UPDATE;
 IF req.state='canceled' THEN RETURN billing.v1_status(rid,node); END IF;
 IF req.reservation_state<>'held' OR EXISTS(SELECT FROM billing.request_attempts WHERE request_id=rid)
 OR EXISTS(SELECT FROM billing.usage_events WHERE request_id=rid AND kind='request.closed')
 THEN RAISE EXCEPTION 'cannot_cancel'; END IF;
 UPDATE billing.budget_periods p SET reserved=p.reserved-h.reserved FROM billing.budget_holds h
 WHERE h.request_id=rid AND h.period_id=p.id;
 UPDATE billing.budget_holds SET reserved=0 WHERE request_id=rid;
 UPDATE billing.accounts SET held_amount=held_amount-req.reserved_amount,version=version+1 WHERE id=req.account_id;
 UPDATE billing.request_runs SET state='canceled',reservation_state='released',charged_amount=0,charge_details='[]',
 settled_at=clock_timestamp(),settlement_digest=encode(sha256(convert_to(reason,'UTF8')),'hex'),review_reason=NULL WHERE id=rid;
 INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),
 req.organization_id,'request.canceled',rid,jsonb_build_object('reason',reason,'node_id',node));
 RETURN billing.v1_status(rid,node);
END $$;

CREATE FUNCTION catalog.v1_credential_current(cid uuid,ver integer)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 PERFORM revision FROM catalog.source_revision WHERE id=1 FOR SHARE;
 RETURN EXISTS(SELECT FROM catalog.upstream_credentials c JOIN catalog.upstreams u ON u.id=c.upstream_id
 WHERE c.id=cid AND c.version=ver AND c.status='active' AND u.status='active');
END $$;
