"""Redis Lua scripts for atomic, hierarchical budget reservations (LLD §12).

Each script runs atomically, so a reservation is checked against and applied to every bucket of its scope
chain, or to none. Amounts travel as decimal strings and counters change only through HINCRBY, so values
stay exact 64-bit integers (Lua numbers are used only for comparisons, exact below 2^53 micro-USD).
All keys of a tenant share one hash tag, so the scripts are also valid on Redis Cluster.

Bucket hash fields: `spent`, `reserved`, `seq` (bumped on every change), `hydrated_ms`, `prov:<label>`.
Reservation record fields: `reservation_id`, `state` (open|settled|released), `payload`, `summary`.
Budget-approval marker: the id of the one reservation a budget approval funded (ADR-0014).
"""

# KEYS[1] budget version, KEYS[2] reservation record, KEYS[3] open index, KEYS[4] budget-approval marker,
# KEYS[5..] buckets
# ARGV[1] expected budget version, ARGV[2] reservation id, ARGV[3] now (ms), ARGV[4] record payload,
# ARGV[5] verified budget-approval id ('' = none), ARGV[6] approval marker TTL (s),
# ARGV[7..] per bucket: amount, cap (-1 = uncapped), approved headroom over the cap ('0' = none)
# A verified approval is consumed here, in the same atomic step that reserves (ADR-0014): a second use sees
# the marker and is refused. Returns {'ok', i...} (buckets taken past their plain cap by the approval) |
# {'stale'} | {'existing', id, state, payload} | {'approval_used', reservation id} | {'missing', i...} |
# {'denied', i, spent, reserved, ...}
RESERVE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
  return {'stale'}
end
if redis.call('EXISTS', KEYS[2]) == 1 then
  local found = redis.call('HMGET', KEYS[2], 'reservation_id', 'state', 'payload')
  return {'existing', found[1] or '', found[2] or '', found[3] or ''}
end
if ARGV[5] ~= '' then
  local holder = redis.call('GET', KEYS[4])
  if holder then
    return {'approval_used', holder}
  end
end
local n = #KEYS - 4
local missing = {'missing'}
for i = 1, n do
  if redis.call('EXISTS', KEYS[4 + i]) == 0 then
    missing[#missing + 1] = tostring(i)
  end
end
if #missing > 1 then
  return missing
end
local denied = {'denied'}
local over = {'ok'}
for i = 1, n do
  local base = 4 + 3 * i
  local amount = tonumber(ARGV[base])
  local cap = tonumber(ARGV[base + 1])
  local headroom = tonumber(ARGV[base + 2])
  if cap >= 0 and amount > 0 then
    local held = redis.call('HMGET', KEYS[4 + i], 'spent', 'reserved')
    local spent = held[1] or '0'
    local reserved = held[2] or '0'
    local used = tonumber(spent) + tonumber(reserved) + amount
    if used > cap + headroom then
      denied[#denied + 1] = tostring(i)
      denied[#denied + 1] = spent
      denied[#denied + 1] = reserved
    elseif used > cap then
      over[#over + 1] = tostring(i)
    end
  end
end
if #denied > 1 then
  return denied
end
for i = 1, n do
  local base = 4 + 3 * i
  if ARGV[base] ~= '0' then
    redis.call('HINCRBY', KEYS[4 + i], 'reserved', ARGV[base])
  end
  redis.call('HINCRBY', KEYS[4 + i], 'seq', 1)
end
redis.call('HSET', KEYS[2], 'reservation_id', ARGV[2], 'state', 'open', 'payload', ARGV[4])
redis.call('ZADD', KEYS[3], ARGV[3], KEYS[2])
if ARGV[5] ~= '' then
  redis.call('SET', KEYS[4], ARGV[2], 'EX', ARGV[6])
end
return over
"""

# KEYS[1] budget-approval marker. ARGV[1] reservation id. Frees the approval only while that reservation
# holds it (its journal write failed, so it funded nothing). Returns 1 when freed.
FREE_APPROVAL = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

# KEYS[1] reservation record, KEYS[2] open index, KEYS[3..] buckets
# ARGV[1] reservation id, ARGV[2] final state (settled|released), ARGV[3] record TTL (s), ARGV[4] summary,
# ARGV[5..] per bucket: negated reserved amount, committed amount, provenance label ('' = none)
# Returns {'done', summary} when already settled, {'missing', i...}, or {'ok', spent before, spent after...}
SETTLE = """
local state = redis.call('HGET', KEYS[1], 'state')
if state == 'settled' or state == 'released' then
  return {'done', redis.call('HGET', KEYS[1], 'summary') or ''}
end
local n = #KEYS - 2
local missing = {'missing'}
for i = 1, n do
  if redis.call('EXISTS', KEYS[2 + i]) == 0 then
    missing[#missing + 1] = tostring(i)
  end
end
if #missing > 1 then
  return missing
end
local out = {'ok'}
for i = 1, n do
  local key = KEYS[2 + i]
  local base = 5 + 3 * (i - 1)
  local before = redis.call('HGET', key, 'spent') or '0'
  if redis.call('HINCRBY', key, 'reserved', ARGV[base]) < 0 then
    redis.call('HSET', key, 'reserved', '0')
  end
  if ARGV[base + 1] ~= '0' then
    redis.call('HINCRBY', key, 'spent', ARGV[base + 1])
  end
  if ARGV[base + 2] ~= '' then
    redis.call('HSET', key, 'prov:' .. ARGV[base + 2], '1')
  end
  redis.call('HINCRBY', key, 'seq', 1)
  out[#out + 1] = before
  out[#out + 1] = redis.call('HGET', key, 'spent')
end
redis.call('HSET', KEYS[1], 'reservation_id', ARGV[1], 'state', ARGV[2], 'summary', ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[3])
redis.call('ZREM', KEYS[2], KEYS[1])
return out
"""

# KEYS[1..] buckets. ARGV[1] now (ms); ARGV[2..] per bucket: spent, reserved, expire-at (unix s, '0' = none),
# comma-separated provenance labels. Only absent buckets are written. Returns the number created.
HYDRATE = """
local created = 0
for i = 1, #KEYS do
  local base = 2 + 4 * (i - 1)
  if redis.call('EXISTS', KEYS[i]) == 0 then
    redis.call('HSET', KEYS[i], 'spent', ARGV[base], 'reserved', ARGV[base + 1], 'seq', '0',
      'hydrated_ms', ARGV[1])
    for label in string.gmatch(ARGV[base + 3], '[^,]+') do
      redis.call('HSET', KEYS[i], 'prov:' .. label, '1')
    end
    if ARGV[base + 2] ~= '0' then
      redis.call('EXPIREAT', KEYS[i], ARGV[base + 2])
    end
    created = created + 1
  end
end
return created
"""

# KEYS[1..] buckets; ARGV[i] signed delta for KEYS[i]. Absent buckets are skipped: they rebuild from the
# ledger, which already holds the adjustment. Returns {spent before, spent after} per bucket ('' if absent).
ADJUST = """
local out = {}
for i = 1, #KEYS do
  if redis.call('EXISTS', KEYS[i]) == 1 then
    out[#out + 1] = redis.call('HGET', KEYS[i], 'spent') or '0'
    redis.call('HINCRBY', KEYS[i], 'spent', ARGV[i])
    redis.call('HINCRBY', KEYS[i], 'seq', 1)
    out[#out + 1] = redis.call('HGET', KEYS[i], 'spent')
  else
    out[#out + 1] = ''
    out[#out + 1] = ''
  end
end
return out
"""

# KEYS[1] bucket. ARGV[1] seq observed before the ledger was read, ARGV[2] spent per the ledger.
# Corrects drift only while the bucket is quiescent: unchanged since the snapshot and nothing reserved.
CORRECT = """
local state = redis.call('HMGET', KEYS[1], 'seq', 'reserved', 'spent')
if state[1] ~= ARGV[1] or (state[2] or '0') ~= '0' then
  return {'busy'}
end
if state[3] == ARGV[2] then
  return {'same'}
end
redis.call('HSET', KEYS[1], 'spent', ARGV[2])
redis.call('HINCRBY', KEYS[1], 'seq', 1)
return {'corrected', state[3] or '0'}
"""
