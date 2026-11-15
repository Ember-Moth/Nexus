CREATE FUNCTION billing.v1_reserve_capacity(rid uuid, org uuid, project uuid, service uuid, kid uuid,
 model uuid, inputs bigint, outputs bigint, ttl integer, fingerprint text, dimensions jsonb,
 at_time timestamptz, node uuid, cluster uuid, rev bigint, batch boolean, searches integer, files integer, storage numeric, store_id uuid, exec_mode text, exec_bound integer,cu text,minutes integer,tariff text)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp SET timezone='UTC' AS $$
DECLARE wallet billing.accounts; prior billing.request_runs; sale billing.price_versions;
 contract billing.vector_storage_contracts; discount billing.discount_versions; over_budget boolean;
 numerator numeric; r numeric; matched uuid[];
BEGIN
 IF minutes IS NULL OR minutes<0 OR minutes>1000000000 OR (cu IS NULL)<>(minutes=0) OR (tariff IS NULL)<>(cu IS NULL)
 OR (cu IS NOT NULL AND (cu NOT IN ('container_1g_minutes','container_4g_minutes','container_16g_minutes','container_64g_minutes')
    OR NOT dimensions ? cu OR tariff NOT IN ('minute_min_5','session_20m') OR minutes<(CASE WHEN tariff='minute_min_5' THEN 5 ELSE 20 END)
    OR inputs<>0 OR outputs<>0 OR files<>0 OR searches<>0 OR store_id IS NOT NULL OR exec_mode<>'none' OR batch))
 THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 IF exec_mode IS NULL OR exec_mode NOT IN ('none','bundled','metered','preloaded') OR exec_bound IS NULL
 OR (exec_mode IN ('metered','preloaded') AND (exec_bound NOT BETWEEN 300 AND 1000000000 OR NOT dimensions ? 'code_execution_seconds'))
 OR (exec_mode IN ('none','bundled') AND exec_bound<>0) OR (store_id IS NOT NULL AND exec_mode<>'none')
 THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 IF files IS NULL OR files NOT BETWEEN 0 AND 1000000000 OR (files>0 AND NOT dimensions ? 'file_search_calls') OR
 storage IS NULL OR storage<0 OR trunc(storage)<>storage OR storage>999999999999999999999999 OR (storage>0 AND store_id IS NULL)
 THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 IF store_id IS NOT NULL THEN
  SELECT * INTO STRICT contract FROM billing.vector_storage_contracts WHERE id=store_id AND api_key_id=kid AND organization_id=org AND model_id=model AND node_id=node;
 END IF;
 IF searches IS NULL OR searches NOT BETWEEN 0 AND 1000000000 OR (searches>0 AND NOT dimensions ? 'web_search_requests') THEN RAISE EXCEPTION 'invalid_bounds'; END IF;
 IF inputs IS NULL OR outputs IS NULL OR inputs NOT BETWEEN 0 AND 1000000000
 OR outputs NOT BETWEEN 0 AND 1000000000 OR ttl NOT BETWEEN 1 AND (CASE WHEN batch THEN 90000 ELSE 86400 END) THEN
  RAISE EXCEPTION 'invalid_bounds'; END IF;
 -- Serialize gateway admissions for this wallet before contending on its tuple.
 -- Other writers still coordinate on the authoritative account row.
 IF node IS NOT NULL THEN
  PERFORM pg_advisory_xact_lock(hashtextextended(org::text,398221));
 END IF;
 SELECT * INTO wallet FROM billing.accounts WHERE organization_id=org FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'account_not_ready'; END IF;
 IF NOT EXISTS(SELECT FROM billing.settings WHERE id=1 AND currency=wallet.currency) THEN
  RAISE EXCEPTION 'currency_config_mismatch'; END IF;
 SELECT * INTO prior FROM billing.request_runs WHERE id=rid;
 IF FOUND THEN
  IF prior.container_unit IS DISTINCT FROM cu OR prior.container_minutes_limit<>minutes OR prior.container_tariff IS DISTINCT FROM tariff OR prior.execution_mode<>exec_mode OR prior.execution_seconds_limit<>exec_bound OR prior.file_search_limit<>files OR prior.storage_byte_seconds<>storage OR prior.web_search_limit<>searches OR prior.request_digest<>fingerprint OR prior.account_id<>wallet.id OR prior.node_id IS DISTINCT FROM node THEN
   RAISE EXCEPTION 'request_conflict'; END IF;
  RETURN rid;
 END IF;
 IF wallet.status<>'active' THEN RAISE EXCEPTION 'account_frozen'; END IF;
 PERFORM revision FROM billing.source_revision WHERE id=1 FOR SHARE;
 IF node IS NOT NULL AND store_id IS NULL THEN at_time=clock_timestamp(); END IF;
 IF store_id IS NOT NULL THEN SELECT * INTO sale FROM billing.price_versions WHERE id=contract.sale_id; ELSE
 SELECT * INTO sale FROM billing.price_versions WHERE kind='sale' AND scope_id=model
  AND state='published' AND effective_at<=at_time ORDER BY effective_at DESC LIMIT 1; END IF;
 IF sale.id IS NULL OR NOT sale.enabled OR sale.model_id<>model OR sale.currency<>wallet.currency
 OR NOT (sale.rates ? 'input_uncached_tokens' AND sale.rates ? 'output_tokens')
 OR (SELECT array_agg(k ORDER BY k) FROM jsonb_object_keys(sale.rates) k)
 IS DISTINCT FROM (SELECT array_agg(k ORDER BY k) FROM jsonb_array_elements_text(dimensions) k)
 THEN RAISE EXCEPTION 'price_unavailable'; END IF;
 IF store_id IS NOT NULL THEN SELECT * INTO STRICT discount FROM billing.discount_versions WHERE id=contract.discount_id; ELSE
 SELECT * INTO STRICT discount FROM billing.discount_versions
 WHERE (organization_id=org OR organization_id IS NULL) AND state='published' AND effective_at<=at_time
 ORDER BY organization_id NULLS LAST,effective_at DESC LIMIT 1; END IF;
 IF cu IS NOT NULL THEN
  IF (sale.rates->cu->>'unit_quantity') NOT IN ('1','20') THEN RAISE EXCEPTION 'price_unavailable'; END IF;
  numerator=minutes*(sale.rates->cu->>'unit_price')::numeric*discount.factor*1000000000000;
  r=(div(numerator,(sale.rates->cu->>'unit_quantity')::numeric)+sign(mod(numerator,(sale.rates->cu->>'unit_quantity')::numeric)))*0.000000000001;
 ELSIF store_id IS NOT NULL THEN
  numerator=storage*(sale.rates#>>'{vector_storage_byte_seconds,unit_price}')::numeric*discount.factor*1000000000000;
  r=(div(numerator,92771293593600)+sign(mod(numerator,92771293593600)))*0.000000000001;
 ELSE
 SELECT (
    (inputs*max(rate) FILTER(WHERE dim LIKE 'input_%') + outputs*max(rate) FILTER(WHERE dim='output_tokens'))
    * (CASE WHEN batch THEN 0.5 ELSE 1 END)
    + greatest(searches*coalesce(max(rate) FILTER(WHERE dim='web_search_requests'),0),
        files*coalesce(max(rate) FILTER(WHERE dim='file_search_calls'),0))
 )*3600 + exec_bound*coalesce(max(hour_price) FILTER(WHERE dim='code_execution_seconds'),0) INTO numerator
 FROM (SELECT key dim,(value->>'unit_price')::numeric *
   CASE (value->>'unit_quantity')::bigint WHEN 1 THEN 1 WHEN 1000 THEN 0.001 WHEN 1000000 THEN 0.000001 END rate,
   (value->>'unit_price')::numeric * CASE (value->>'unit_quantity')::bigint WHEN 3600 THEN 1 WHEN 1 THEN 3600 END hour_price
 FROM jsonb_each(sale.rates)) rates;
 numerator=numerator*discount.factor*1000000000000;
 r=(div(numerator,3600)+sign(mod(numerator,3600)))*0.000000000001;
 END IF;
 IF r IS NULL OR r>=10000000000000000 THEN RAISE EXCEPTION 'invalid_reserve_amount'; END IF;
 IF wallet.balance-wallet.held_amount<r THEN RAISE EXCEPTION 'insufficient_funds'; END IF;
 -- The wallet lock serializes all changes to this organization's budget policies,
 -- periods and holds. Reserve every matching period in one write and reject the
 -- whole transaction (including newly created periods) if any limit is exceeded.
 WITH policies AS (
   SELECT id,version,limit_amount,period,date_trunc(period,at_time) AS start_at
   FROM billing.budget_policies
   WHERE organization_id=org AND status='active' AND
     (scope_type='organization' OR (scope_type='project' AND project_id=project)
      OR (scope_type='key' AND api_key_id=kid))
 ), reserved_periods AS (
   INSERT INTO billing.budget_periods AS bp
     (id,organization_id,policy_id,policy_version,period_start,period_end,limit_amount,reserved)
   SELECT gen_random_uuid(),org,id,version,start_at,
     start_at+CASE period WHEN 'day' THEN interval '1 day' ELSE interval '1 month' END,
     limit_amount,r FROM policies ORDER BY id
   ON CONFLICT(policy_id,period_start) DO UPDATE SET
     policy_version=excluded.policy_version,limit_amount=excluded.limit_amount,
     reserved=bp.reserved+excluded.reserved
   RETURNING bp.id,bp.spent,bp.reserved,bp.limit_amount
 )
 SELECT array_agg(id),bool_or(spent+reserved>limit_amount)
 INTO matched,over_budget FROM reserved_periods;
 IF over_budget THEN RAISE EXCEPTION 'budget_exceeded'; END IF;
 INSERT INTO billing.request_runs(id,organization_id,account_id,project_id,service_account_id,api_key_id,
 model_id,request_digest,sale_price_version_id,discount_version_id,billing_policy_version,input_limit,
 output_limit,container_unit,container_minutes_limit,container_tariff,execution_mode,execution_seconds_limit,web_search_limit,file_search_limit,storage_byte_seconds,reserved_amount,expires_at,created_at,node_id,cluster_id,config_revision)
 VALUES(rid,org,wallet.id,project,service,kid,model,fingerprint,sale.id,discount.id,CASE WHEN cu IS NOT NULL THEN 'openai_container_v1' WHEN store_id IS NOT NULL THEN 'openai_vector_storage_v1' WHEN batch THEN 'anthropic_message_batch_v1' ELSE 'customer_usage_v1' END,
 inputs,outputs,cu,minutes,tariff,exec_mode,exec_bound,searches,files,storage,r,(CASE WHEN store_id IS NULL THEN at_time ELSE clock_timestamp() END)+make_interval(secs=>ttl),at_time,node,cluster,rev);
 IF matched IS NOT NULL THEN
   INSERT INTO billing.budget_holds(request_id,period_id,organization_id,reserved,spent)
   SELECT rid,period_id,org,r,0 FROM unnest(matched) AS period_id;
 END IF;
 UPDATE billing.accounts SET held_amount=held_amount+r,version=version+1 WHERE id=wallet.id;
 INSERT INTO billing.audit_events(id,organization_id,action,resource_id,details) VALUES(gen_random_uuid(),
 org,'request.reserved',rid,jsonb_build_object('amount',r::text,'sale_price_version_id',sale.id,'discount_version_id',discount.id));
 RETURN rid;
END $$;





CREATE OR REPLACE FUNCTION billing.v1_reserve_execution(rid uuid, org uuid, project uuid, service uuid, kid uuid,
 model uuid, inputs bigint, outputs bigint, ttl integer, fingerprint text, dimensions jsonb,
 at_time timestamptz,node uuid,cluster uuid,rev bigint,batch boolean,searches integer,files integer,storage numeric,store_id uuid,exec_mode text,exec_bound integer)
RETURNS uuid LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
 SELECT billing.v1_reserve_capacity(rid,org,project,service,kid,model,inputs,outputs,ttl,fingerprint,dimensions,
     at_time,node,cluster,rev,batch,searches,files,storage,store_id,exec_mode,exec_bound,NULL,0,NULL)
$$;
