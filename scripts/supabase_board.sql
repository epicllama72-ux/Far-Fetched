-- Leaderboard summary in one small response, so the game no longer downloads every score row.
-- Run once in Supabase: SQL editor > New query > paste > Run. Safe to run again.
-- Until it is run, the game falls back to downloading the rows, exactly as before.
--
-- Returns, for one day and mode:
--   n      number of players
--   hist   [[m, count], ...]  how many players reached each depth (depths are stored to 0.1 m, so this is exact)
--   top    the five deepest players (player, name, m, score)
--   me     the asking player's own row and rank, if they have played
--   crowd  per headline: answers given (n), right calls (ok) and the median pin distance (med)

create index if not exists scores_day_kind on public.scores (day, kind);

create or replace function public.board(p_day text, p_kind text, p_player text default null)
returns jsonb
language sql
stable
as $$
with s as (
  select player, name, m, score, answers::jsonb as answers
  from public.scores
  where day = p_day and kind = p_kind
),
hist as (
  select coalesce(jsonb_agg(jsonb_build_array(m, c) order by m), '[]'::jsonb) as h
  from (select m, count(*) as c from s group by m) t
),
top5 as (
  select coalesce(jsonb_agg(jsonb_build_object('player', player, 'name', name, 'm', m, 'score', score)
                            order by m desc, score desc), '[]'::jsonb) as t
  from (select * from s order by m desc, score desc limit 5) x
),
me as (
  select jsonb_build_object('player', s.player, 'name', s.name, 'm', s.m, 'score', s.score,
           'rank', (select count(*) from s s2 where s2.m > s.m or (s2.m = s.m and s2.score > s.score)) + 1) as j
  from s where p_player is not null and s.player = p_player
  limit 1
),
ans as (
  select (a.idx - 1)::int as i,
         case when (a.v ->> 0) in ('1', 'true') then 1 else 0 end as ok,
         case when jsonb_typeof(a.v -> 1) = 'number' then (a.v ->> 1)::numeric end as dist
  from s,
       jsonb_array_elements(case when jsonb_typeof(s.answers) = 'array' then s.answers else '[]'::jsonb end)
         with ordinality as a(v, idx)
  where jsonb_typeof(a.v) = 'array'
),
crowd as (
  select coalesce(jsonb_agg(jsonb_build_object('i', i, 'n', n, 'ok', ok, 'med', med) order by i), '[]'::jsonb) as c
  from (
    select i, count(*) as n, sum(ok) as ok,
           (array_agg(dist order by dist) filter (where dist is not null))[(count(dist) / 2) + 1] as med
    from ans group by i
  ) q
)
select jsonb_build_object(
  'n', (select count(*) from s),
  'hist', (select h from hist),
  'top', (select t from top5),
  'me', (select j from me),
  'crowd', (select c from crowd)
);
$$;

grant execute on function public.board(text, text, text) to anon;
