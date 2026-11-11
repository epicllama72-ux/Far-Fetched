-- How many players have ever pinned a true story within 1 km (the OMNISCIENT popup).
-- Reads the per-story distances already saved in scores.answers ([callOk, km] pairs); the asking player is left out
-- so the game can add them back once. Read-only and safe to run more than once.
create or replace function omniscient_count(p_player text default null) returns integer
language sql stable security definer set search_path = public as $$
  select count(distinct s.player)::int
    from scores s, jsonb_array_elements(s.answers::jsonb) a
   where s.answers is not null
     and jsonb_typeof(a) = 'array' and jsonb_typeof(a->1) = 'number'
     and (a->>1)::numeric <= 1
     and (p_player is null or s.player::text <> p_player);
$$;
grant execute on function omniscient_count(text) to anon, authenticated;
