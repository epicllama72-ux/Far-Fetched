-- Leaner version of the map crowd (replaces story_pins in the game): the server works out how many players this pin beat,
-- and sends at most 150 dots, so each request stays about 2-3 KB however many people play. Read-only; safe to run again.
create or replace function story_crowd(p_day text, p_kind text, p_i integer, p_player text, p_dist numeric) returns jsonb
language sql volatile security definer set search_path = public as $$
  with a as (
    select s.answers::jsonb -> p_i as v
      from scores s
     where s.day::text = p_day and s.kind::text = p_kind and s.answers is not null
       and jsonb_typeof(s.answers::jsonb) = 'array'
       and (p_player is null or s.player::text <> p_player)
  ), d as (
    select (v->>1)::numeric as km from a where jsonb_typeof(v) = 'array' and jsonb_typeof(v->1) = 'number'
  )
  select jsonb_build_object(
    'n',    (select count(*) from d),
    'beat', (select coalesce(sum(case when km > p_dist then 1 when km = p_dist then 0.5 else 0 end), 0) from d),
    'p',    coalesce((select jsonb_agg(jsonb_build_array((v->>2)::numeric, (v->>3)::numeric))
                       from (select v from a where jsonb_typeof(v) = 'array' and jsonb_typeof(v->2) = 'number' and jsonb_typeof(v->3) = 'number'
                             order by random() limit 150) q), '[]'::jsonb));
$$;
grant execute on function story_crowd(text,text,integer,text,numeric) to anon, authenticated;

-- the first version is no longer used by the game; remove it (an out-of-date copy of the game just shows no dots)
drop function if exists story_pins(text,text,integer,text);
