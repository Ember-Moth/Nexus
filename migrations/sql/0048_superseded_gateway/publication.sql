-- Definitions dropped by 0048_superseded_gateway, restored by its downgrade.
CREATE OR REPLACE FUNCTION publication.v1_file(handle uuid, kid uuid)
 RETURNS jsonb
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
 SELECT publication.v1_native_file(handle,kid,'anthropic_messages')
$function$
;
CREATE OR REPLACE FUNCTION publication.v1_file_save(handle uuid, kid uuid, model uuid, d jsonb, meta jsonb)
 RETURNS void
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
 SELECT publication.v1_native_file_save(handle,kid,model,d,meta,'anthropic_messages')
$function$
;
CREATE OR REPLACE FUNCTION publication.v1_files(kid uuid, stamp uuid, newer boolean, n integer)
 RETURNS SETOF jsonb
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
 SELECT * FROM publication.v1_native_files(kid,stamp,newer,n,'anthropic_messages',false,NULL)
$function$
;
