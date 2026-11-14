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
