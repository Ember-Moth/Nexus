"""Redis contract shared by control-plane publication and the gateway simulator."""

import hashlib
import json
from time import monotonic
from uuid import UUID, uuid4

# Returns the superseded current revision when the pointer advances, else "". Copies of
# revisions other than the current one expire after ARGV[4] ms; PostgreSQL stays authoritative.
PUBLISH = """
local old=redis.call('GET',KEYS[2])
local rev=ARGV[1]
if not old or #old<#rev or (#old==#rev and old<rev) then
 redis.call('SET',KEYS[1],ARGV[2])
 redis.call('SET',KEYS[2],rev)
 redis.call('PUBLISH',ARGV[3],rev)
 return old or ''
end
if old==rev then
 redis.call('SET',KEYS[1],ARGV[2])
else
 redis.call('SET',KEYS[1],ARGV[2],'PX',ARGV[4])
end
return ''
"""

PREPARE = """
local run=redis.call('INFO','server'):match('run_id:(%w+)')
local old=redis.call('GET',KEYS[1])
if old and cjson.decode(old).run==run then return 0 end
local t=redis.call('TIME')
local after=tonumber(t[1])*1000+math.floor(tonumber(t[2])/1000)
if old or ARGV[1]~='1' then after=after+60000 end
redis.call('SET',KEYS[1],cjson.encode({run=run,epoch=ARGV[2],after=after}))
return 1
"""

ACQUIRE = """
local t=redis.call('TIME'); local now=tonumber(t[1])*1000+math.floor(tonumber(t[2])/1000)
local specs=cjson.decode(ARGV[5]); local epoch=''
if #specs>0 then
 local marker=redis.call('GET',KEYS[#KEYS]); if not marker then return 'recovering' end
 local gate=cjson.decode(marker)
 local run=redis.call('INFO','server'):match('run_id:(%w+)')
 if gate.after>now or gate.run~=run then return 'recovering' end
 epoch=gate.epoch
end
local prior=redis.call('GET',KEYS[1])
if prior then
 local p=cjson.decode(prior)
 if p.epoch~=epoch then return 'expired' end
 if p.fingerprint~=ARGV[1] then return 'conflict' end
 if p.finished or p.deadline<=now then return 'expired' end
 return prior
end
local window=math.floor(now/60000)
if window~=tonumber(ARGV[2]) then return 'retry_window' end
local upper=tonumber(ARGV[3]); local ttl=tonumber(ARGV[4]);
for i,s in ipairs(specs) do
 local key=KEYS[i+1]
 if s.metric=='concurrency' then
  redis.call('ZREMRANGEBYSCORE',key,'-inf',now)
  if redis.call('ZCARD',key)>=s.limit_value then return 'limited' end
 elseif tonumber(redis.call('GET',key) or '0')+(s.metric=='rpm' and 1 or upper)>s.limit_value then
  return 'limited'
 end
end
for i,s in ipairs(specs) do
 local key=KEYS[i+1]
 if s.metric=='concurrency' then
  redis.call('ZADD',key,now+ttl,ARGV[6])
  redis.call('PEXPIRE',key,120000)
 else
  redis.call('INCRBY',key,s.metric=='rpm' and 1 or upper)
  redis.call('PEXPIREAT',key,(window+1)*60000)
 end
end
local state=cjson.encode({fingerprint=ARGV[1],upper=upper,deadline=now+ttl,window=window,
 keys=KEYS,specs=specs,epoch=epoch,owner=ARGV[6],finished=false,ttl=ttl})
redis.call('SET',KEYS[1],state,'PX',120000)
return state
"""

CHANGE = """
local v=redis.call('GET',KEYS[1]); if not v then return 0 end
local s=cjson.decode(v)
if s.owner~=ARGV[1] or #s.keys~=#KEYS then return 0 end
for i,k in ipairs(KEYS) do if s.keys[i]~=k then return 0 end end
local t=redis.call('TIME'); local now=tonumber(t[1])*1000+math.floor(tonumber(t[2])/1000)
if #s.specs>0 then
 local marker=redis.call('GET',KEYS[#KEYS]); if not marker then return 0 end
 local gate=cjson.decode(marker)
 local run=redis.call('INFO','server'):match('run_id:(%w+)')
 if gate.epoch~=s.epoch or gate.run~=run then return 0 end
end
if s.finished then return ARGV[2]=='finish' and s.actual==ARGV[3] and 1 or 0 end
if ARGV[2]=='renew' then
 if now>=s.deadline then return 0 end
 for i,p in ipairs(s.specs) do
  if p.metric=='concurrency' and not redis.call('ZSCORE',KEYS[i+1],s.owner) then return 0 end
 end
 s.deadline=now+s.ttl
end
for i,p in ipairs(s.specs) do
 local k=KEYS[i+1]
 if p.metric=='concurrency' then
  if ARGV[2]=='renew' then redis.call('ZADD',k,s.deadline,s.owner); redis.call('PEXPIRE',k,120000)
  else redis.call('ZREM',k,s.owner) end
 elseif ARGV[2]=='finish' and p.metric=='tpm' and ARGV[3]~='unknown'
 and math.floor(now/60000)==s.window and redis.call('EXISTS',k)==1 then
  redis.call('INCRBY',k,tonumber(ARGV[3])-s.upper)
 end
end
if ARGV[2]=='finish' then s.finished=true; s.actual=ARGV[3] end
redis.call('SET',KEYS[1],cjson.encode(s),'PX',120000)
return 1
"""


class GatewayLimits:
    def __init__(self, redis, namespace="nexus:v1"):
        self.redis = redis
        self.namespace = namespace

    async def acquire(
        self, org, project, key, request_id, upper, policies, ttl_ms=15000, *, binding=None
    ):
        org, project, key, request_id = map(
            str, map(UUID, map(str, (org, project, key, request_id)))
        )
        if type(upper) is not int or not 0 <= upper <= 2_000_000_000 or not 100 <= ttl_ms <= 60000:
            raise ValueError("Invalid admission limits")
        ids = {"organization": org, "project": project, "key": key}
        applicable = sorted(
            (
                p
                for p in policies
                if p["organization_id"] == org and p["scope_id"] == ids[p["scope_type"]]
            ),
            key=lambda p: p["id"],
        )
        fingerprint = hashlib.sha256(
            json.dumps(
                [org, project, key, upper, applicable, ttl_ms, binding], sort_keys=True
            ).encode()
        ).hexdigest()
        for _ in range(3):
            started = monotonic()
            window = int((await self.redis.time())[0]) // 60
            keys = [f"{self.namespace}:lease:{{{org}}}:{request_id}"]
            for p in applicable:
                suffix = f"{p['id']}:{p['metric']}"
                keys.append(
                    f"{self.namespace}:concurrency:{{{org}}}:{suffix}"
                    if p["metric"] == "concurrency"
                    else f"{self.namespace}:limit:{{{org}}}:{suffix}:{window}"
                )
            keys.append(f"{self.namespace}:limit:{{{org}}}:epoch")
            result = await self.redis.eval(
                ACQUIRE,
                len(keys),
                *keys,
                fingerprint,
                window,
                upper,
                ttl_ms,
                json.dumps(applicable),
                str(uuid4()),
            )
            if result == "retry_window":
                continue
            if result in {"limited", "conflict", "expired", "recovering"}:
                raise ValueError(result)
            lease = json.loads(result)
            # Redis TIME gives the remaining interval for a replayed lease.
            seconds, micros = await self.redis.time()
            lease["safe_until"] = started + max(
                0, lease["deadline"] / 1000 - seconds - micros / 1e6
            )
            return lease
        raise RuntimeError("Window changed during admission")

    async def renew(self, lease):
        start = monotonic()
        ok = await self.redis.eval(
            CHANGE, len(lease["keys"]), *lease["keys"], lease["owner"], "renew", ""
        )
        if ok:
            lease["safe_until"] = start + lease["ttl"] / 1000
        return bool(ok)

    async def finish(self, lease, actual=None):
        if actual is not None and (type(actual) is not int or not 0 <= actual <= 5_000_000_000):
            raise ValueError("Invalid confirmed token count")
        return bool(
            await self.redis.eval(
                CHANGE,
                len(lease["keys"]),
                *lease["keys"],
                lease["owner"],
                "finish",
                str(actual) if actual is not None else "unknown",
            )
        )
