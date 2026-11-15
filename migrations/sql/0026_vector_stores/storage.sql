-- Byte-seconds are integers. One GiB-day equals 2^30 bytes times 86,400 seconds.
CREATE TABLE billing.vector_storage_contracts(
 id uuid PRIMARY KEY,node_id uuid NOT NULL,cluster_id uuid NOT NULL,revision bigint NOT NULL,organization_id uuid NOT NULL,project_id uuid NOT NULL,service_account_id uuid NOT NULL,
 api_key_id uuid NOT NULL,model_id uuid NOT NULL,deployment_id uuid NOT NULL,
 sale_id uuid NOT NULL REFERENCES billing.price_versions,cost_id uuid NOT NULL REFERENCES billing.price_versions,
 discount_id uuid NOT NULL REFERENCES billing.discount_versions,
 segment integer NOT NULL DEFAULT 1, observation_unknown boolean NOT NULL DEFAULT false,observed_at timestamptz,usage_bytes bigint NOT NULL DEFAULT 0,expires_at timestamptz,closed boolean NOT NULL DEFAULT false,
 FOREIGN KEY(cluster_id,revision) REFERENCES publication.config_snapshots(cluster_id,revision)
);
CREATE TABLE billing.vector_storage_days(
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),attempt_id uuid NOT NULL DEFAULT gen_random_uuid(),store_id uuid NOT NULL REFERENCES billing.vector_storage_contracts,
 day date NOT NULL,segment integer NOT NULL,byte_seconds numeric(32,0) NOT NULL DEFAULT 0,covered_until timestamptz NOT NULL,
 uncertain boolean NOT NULL DEFAULT false,state text NOT NULL CHECK(state IN ('open','ready','staged','review')),reason text,next_attempt timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(store_id,day,segment)
);
CREATE TABLE billing.vector_storage_segments(
 day_id uuid NOT NULL REFERENCES billing.vector_storage_days, started_at timestamptz NOT NULL, ended_at timestamptz NOT NULL,
 usage_bytes bigint NOT NULL CHECK(usage_bytes>=0),uncertain boolean NOT NULL,
 PRIMARY KEY(day_id,started_at),CHECK(ended_at>started_at)
);
CREATE INDEX vector_storage_segment_end ON billing.vector_storage_segments(day_id,ended_at);
CREATE INDEX vector_storage_ready ON billing.vector_storage_days(next_attempt) WHERE state IN ('ready','review');
GRANT SELECT,UPDATE ON billing.vector_storage_days TO nexus_billing;
CREATE FUNCTION billing.v1_storage_writable(kid uuid,org uuid,project uuid,paid boolean) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp SET timezone='UTC' AS $$
DECLARE wallet billing.accounts;
BEGIN
 SELECT * INTO STRICT wallet FROM billing.accounts WHERE organization_id=org FOR SHARE;
 IF wallet.status<>'active' THEN RAISE EXCEPTION 'account_frozen'; END IF;
 IF NOT paid THEN RETURN; END IF;
 IF wallet.balance<=wallet.held_amount OR EXISTS(SELECT FROM billing.vector_storage_days u JOIN billing.vector_storage_contracts c
   ON c.id=u.store_id WHERE c.organization_id=org AND u.state='review' AND u.reason='insufficient_funds') THEN RAISE EXCEPTION 'insufficient_funds'; END IF;
 IF EXISTS(SELECT FROM billing.vector_storage_days u JOIN billing.vector_storage_contracts c ON c.id=u.store_id
   WHERE c.api_key_id=kid AND u.state='review' AND u.reason='budget_exceeded') OR EXISTS(
    SELECT FROM billing.budget_policies p LEFT JOIN billing.budget_periods b ON b.policy_id=p.id AND b.period_start=date_trunc(p.period,clock_timestamp())
    WHERE p.organization_id=org AND p.status='active' AND (p.scope_type='organization' OR p.scope_type='project' AND p.scope_id=project
     OR p.scope_type='key' AND p.scope_id=kid) AND p.limit_amount<=coalesce(b.spent+b.reserved,0))
 THEN RAISE EXCEPTION 'budget_exceeded'; END IF;
END $$;
CREATE FUNCTION billing.v1_storage_write(handle uuid) RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT billing.v1_storage_writable(c.api_key_id,c.organization_id,c.project_id,(p.rates#>>'{vector_storage_byte_seconds,unit_price}')::numeric>0)
 FROM billing.vector_storage_contracts c JOIN billing.price_versions p ON p.id=c.sale_id WHERE c.id=handle
$$;
CREATE FUNCTION billing.v1_storage_open(handle uuid,principal jsonb,kid uuid,model uuid,dep uuid,node uuid,cluster uuid,rev bigint) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE org uuid := (principal->>'organization_id')::uuid; wallet billing.accounts; sale billing.price_versions;
 cost billing.price_versions; discount billing.discount_versions; now_at timestamptz:=clock_timestamp();
BEGIN
 SELECT * INTO STRICT wallet FROM billing.accounts WHERE organization_id=org FOR SHARE;
 IF wallet.status<>'active' THEN RAISE EXCEPTION 'account_frozen'; END IF;
 PERFORM revision FROM billing.source_revision WHERE id=1 FOR SHARE;
 SELECT * INTO sale FROM billing.price_versions WHERE kind='sale' AND scope_id=model AND state='published' AND effective_at<=now_at
 ORDER BY effective_at DESC LIMIT 1;
 SELECT * INTO cost FROM billing.price_versions WHERE kind='cost' AND scope_id=dep AND state='published' AND effective_at<=now_at
 ORDER BY effective_at DESC LIMIT 1;
 IF sale.id IS NULL OR NOT sale.enabled OR cost.id IS NULL OR NOT cost.enabled OR sale.currency<>wallet.currency
 OR cost.currency<>wallet.currency OR sale.model_id<>model OR cost.model_id<>model OR NOT sale.rates ? 'vector_storage_byte_seconds'
 OR sale.rates#>>'{vector_storage_byte_seconds,unit_quantity}' IS DISTINCT FROM '92771293593600'
 OR cost.rates#>>'{vector_storage_byte_seconds,unit_quantity}' IS DISTINCT FROM '92771293593600' THEN RAISE EXCEPTION 'price_unavailable'; END IF;
 PERFORM billing.v1_storage_writable(kid,org,(principal->>'project_id')::uuid,(sale.rates#>>'{vector_storage_byte_seconds,unit_price}')::numeric>0);
 SELECT * INTO STRICT discount FROM billing.discount_versions WHERE (organization_id=org OR organization_id IS NULL)
 AND state='published' AND effective_at<=now_at ORDER BY organization_id NULLS LAST,effective_at DESC LIMIT 1;
 INSERT INTO billing.vector_storage_contracts(id,node_id,cluster_id,revision,organization_id,project_id,service_account_id,api_key_id,model_id,deployment_id,sale_id,cost_id,discount_id)
 VALUES(handle,node,cluster,rev,org,(principal->>'project_id')::uuid,(principal->>'service_account_id')::uuid,kid,model,dep,sale.id,cost.id,discount.id);
END $$;
CREATE FUNCTION billing.v1_storage_sample(handle uuid,at_time timestamptz,bytes bigint,expiry timestamptz,closing boolean,uncertain boolean)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp SET timezone='UTC' AS $$
DECLARE c billing.vector_storage_contracts; start_at timestamptz; end_at timestamptz; until_at timestamptz; quantity numeric; gap boolean; day_id_in uuid;
BEGIN
 IF bytes<0 OR at_time IS NULL OR at_time>clock_timestamp()+interval '5 seconds' THEN RAISE EXCEPTION 'invalid_storage_sample'; END IF;
 SELECT * INTO STRICT c FROM billing.vector_storage_contracts WHERE id=handle FOR UPDATE;
 IF c.closed THEN
  IF NOT closing AND NOT uncertain AND at_time>=c.observed_at THEN
   UPDATE billing.vector_storage_contracts SET closed=false,segment=segment+1,observed_at=date_trunc('second',at_time),
      usage_bytes=bytes,expires_at=expiry,observation_unknown=false WHERE id=handle;
  END IF;
  RETURN;
 END IF;
 IF c.observed_at>at_time AND NOT closing THEN RETURN; END IF;
 -- Recording/splitting at whole seconds avoids double rounding at adjacent boundaries.
 at_time=date_trunc('second',greatest(c.observed_at,at_time));
 start_at=c.observed_at; end_at=least(at_time,coalesce(c.expires_at,at_time));
 gap=uncertain OR c.observation_unknown OR at_time-start_at>interval '5 minutes';
 IF closing AND expiry IS NOT NULL THEN end_at=least(end_at,expiry); END IF;
 IF NOT closing AND c.expires_at<at_time AND (expiry IS NULL OR expiry>c.expires_at) THEN
  -- A renewed store bridged a previously observed expiry without a supporting sample.
  gap=true; end_at=at_time;
 END IF;
 IF c.observed_at IS NULL AND uncertain THEN
  INSERT INTO billing.vector_storage_days(store_id,day,segment,byte_seconds,covered_until,state,uncertain,reason)
  VALUES(handle,at_time::date,c.segment,0,at_time,CASE WHEN closing THEN 'ready' ELSE 'open' END,true,
      CASE WHEN closing THEN 'submission_unknown' ELSE 'observation_gap' END) ON CONFLICT(store_id,day,segment) DO NOTHING;
 END IF;
 WHILE start_at<end_at AND (c.usage_bytes>0 OR gap) LOOP
  until_at=least(end_at,date_trunc('day',start_at)+interval '1 day');
  quantity=c.usage_bytes::numeric*extract(epoch FROM until_at-start_at);
  INSERT INTO billing.vector_storage_days(store_id,day,segment,byte_seconds,covered_until,state,uncertain,reason)
  VALUES(handle,start_at::date,c.segment,quantity,until_at,CASE WHEN closing OR until_at::date>start_at::date THEN 'ready' ELSE 'open' END,gap,
     CASE WHEN gap THEN 'observation_gap' END)
  ON CONFLICT(store_id,day,segment) DO UPDATE SET byte_seconds=billing.vector_storage_days.byte_seconds+excluded.byte_seconds,
      covered_until=excluded.covered_until,state=excluded.state,uncertain=billing.vector_storage_days.uncertain OR excluded.uncertain,
      reason=coalesce(billing.vector_storage_days.reason,excluded.reason) WHERE billing.vector_storage_days.state='open'
  RETURNING id INTO day_id_in;
  IF day_id_in IS NULL THEN RAISE EXCEPTION 'storage_period_conflict'; END IF;
  UPDATE billing.vector_storage_segments AS seg SET ended_at=until_at WHERE seg.day_id=day_id_in AND seg.ended_at=start_at
      AND seg.usage_bytes=c.usage_bytes AND seg.uncertain=gap;
  IF NOT FOUND THEN INSERT INTO billing.vector_storage_segments(day_id,started_at,ended_at,usage_bytes,uncertain)
    VALUES(day_id_in,start_at,until_at,c.usage_bytes,gap); END IF;
  start_at=until_at;
 END LOOP;
 UPDATE billing.vector_storage_days SET state='ready' WHERE store_id=handle AND state='open' AND (day<at_time::date OR closing);
 UPDATE billing.vector_storage_contracts SET observation_unknown=uncertain,observed_at=at_time,usage_bytes=bytes,expires_at=expiry,closed=closing WHERE id=handle;
END $$;
-- A completed UTC day (or the last, partial day) is one billable operation. Prepare
-- the ordinary wallet and budget hold, procurement attempt and durable worker facts atomically.
CREATE FUNCTION billing.v1_stage_storage() RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp SET timezone='UTC' AS $$
DECLARE period billing.vector_storage_days; c billing.vector_storage_contracts; dims jsonb; stamp text; base jsonb; answer jsonb; gap boolean;
BEGIN
 SELECT * INTO period FROM billing.vector_storage_days WHERE (state='ready' OR (state='review' AND reason IN ('account_frozen','insufficient_funds','budget_exceeded')))
 AND next_attempt<=clock_timestamp() ORDER BY next_attempt,id LIMIT 1 FOR UPDATE SKIP LOCKED;
 IF NOT FOUND THEN RETURN false; END IF;
 SELECT * INTO STRICT c FROM billing.vector_storage_contracts WHERE id=period.store_id;
 SELECT jsonb_agg(k) INTO dims FROM billing.price_versions p CROSS JOIN LATERAL jsonb_object_keys(p.rates) k WHERE p.id=c.sale_id;
 gap=period.uncertain;
 BEGIN
  PERFORM billing.v1_reserve_tools(period.id,c.organization_id,c.project_id,c.service_account_id,c.api_key_id,c.model_id,
    0,0,86400,encode(sha256(convert_to(period.id::text,'UTF8')),'hex'),dims,period.day::timestamptz,c.node_id,c.cluster_id,
    c.revision,false,0,0,CASE WHEN gap THEN 0 ELSE period.byte_seconds END,c.id);
 EXCEPTION WHEN raise_exception THEN
  IF SQLERRM NOT IN ('account_frozen','insufficient_funds','budget_exceeded','price_unavailable','invalid_reserve_amount','currency_config_mismatch','invalid_bounds') THEN RAISE; END IF;
  UPDATE billing.vector_storage_days SET state='review',next_attempt=clock_timestamp()+interval '1 minute',
     reason=SQLERRM WHERE id=period.id;
  RETURN true;
 END;
 INSERT INTO billing.request_attempts(id,request_id,deployment_id,cost_price_version_id,sequence,expected_usage_format)
 VALUES(period.attempt_id,period.id,c.deployment_id,c.cost_id,1,'openai.vector_storage.v1');
 stamp=to_char(period.covered_until AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.MS"Z"');
 base=jsonb_build_object('schema_version',1,'request_id',period.id,'occurred_at',stamp,'config_revision',c.revision::text);
 answer=billing.v1_append((base||jsonb_build_object('event_id',gen_random_uuid(),'event_type','attempt.finished','attempt_id',period.attempt_id,
 'payload',jsonb_build_object('outcome',CASE WHEN gap THEN 'unknown' ELSE 'succeeded' END,'elapsed_ms',0,'upstream_request_id',c.id::text,
    'usage_format','openai.vector_storage.v1','raw_usage',CASE WHEN gap THEN NULL ELSE jsonb_build_object('vector_storage_byte_seconds',period.byte_seconds) END)))::text,c.node_id);
 IF answer->>'status'<>'accepted' THEN RAISE EXCEPTION 'event_conflict'; END IF;
 answer=billing.v1_append((base||jsonb_build_object('event_id',gen_random_uuid(),'event_type','request.closed','attempt_id',NULL,
 'payload',jsonb_build_object('delivery_outcome',CASE WHEN gap THEN 'unknown' ELSE 'succeeded' END,'attempt_ids',jsonb_build_array(period.attempt_id),
    'delivered_attempt_id',CASE WHEN gap THEN NULL ELSE period.attempt_id END)))::text,c.node_id);
 IF answer->>'status'<>'accepted' THEN RAISE EXCEPTION 'event_conflict'; END IF;
 UPDATE billing.vector_storage_days SET state='staged' WHERE id=period.id;
 RETURN true;
END $$;

CREATE FUNCTION billing.v1_storage_missed(handle uuid,at_time timestamptz) RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE c billing.vector_storage_contracts;
BEGIN
 SELECT * INTO STRICT c FROM billing.vector_storage_contracts WHERE id=handle FOR UPDATE;
 PERFORM billing.v1_storage_sample(handle,at_time,c.usage_bytes,c.expires_at,false,true);
END $$;
