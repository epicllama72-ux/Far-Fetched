-- Everyone's pins for one story on one day (the dots on the map after you pin), plus everyone's distances,
-- so the game can say "Closer than 81% of today's players". Pins are saved in scores.answers as [callOk, km, lat, lng]
-- (older rows have only [callOk, km], so they count for the distances but draw no dot). Read-only; safe to run again.
create or replace function story_pins(p_day text, p_kind text, p_i integer, p_player text default null) returns jsonb
language sql stable security definer set search_path = public as $$
  with a as (
    select s.answers::jsonb -> p_i as v
      from scores s
     where s.day::text = p_day and s.kind::text = p_kind and s.answers is not null
       and jsonb_typeof(s.answers::jsonb) = 'array'
       and (p_player is null or s.player::text <> p_player)
  )
  select jsonb_build_object(
    'd', coalesce((select jsonb_agg((v->>1)::numeric) from a where jsonb_typeof(v) = 'array' and jsonb_typeof(v->1) = 'number'), '[]'::jsonb),
    'p', coalesce((select jsonb_agg(jsonb_build_array((v->>2)::numeric, (v->>3)::numeric))
                     from (select v from a where jsonb_typeof(v) = 'array' and jsonb_typeof(v->2) = 'number' and jsonb_typeof(v->3) = 'number' limit 500) q), '[]'::jsonb));
$$;
grant execute on function story_pins(text,text,integer,text) to anon, authenticated;
