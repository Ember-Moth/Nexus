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
