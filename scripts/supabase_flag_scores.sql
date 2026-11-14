-- Hide implausibly perfect Daily Dig scores from the leaderboard (they stay in the table, and the player still sees their own row).
-- Run once in Supabase: SQL editor > New query > paste > Run. Safe to run again.
--
-- How a score gets flagged: for every headline, each player's pin distance is ranked against everyone else's that day (cume_dist: the share of players
-- who did as well or better). Multiplying those shares across a player's pins gives how likely a result that good is by chance; the sum of their
-- natural logs is "lnp". A score is flagged when lnp < -30 (roughly 1 in 10 trillion) with at least 4 pins and at least 300 players that day. On
-- 2026-11-13 that caught exactly one of 2,819 players; the next-best real player scored -25, and ordinary experts sit around -15 to -20.
-- It cannot tell a script from someone replaying a paper with the answers memorised; both look the same, and both are hidden.

create table if not exists public.score_flags (
  day text not null, kind text not null, player text not null,
  lnp double precision, flagged_at timestamptz not null default now(),
  primary key (day, kind, player)
);
alter table public.score_flags enable row level security;      -- no policies: only the functions below can read it

-- recompute the flags for one day and mode (service use: run it from the SQL editor or a schedule; not granted to the game)
create or replace function public.flag_scores(p_day text, p_kind text default 'call') returns integer
language sql security definer set search_path = public as $$
  with s as (
    select player::text as player, answers::jsonb as answers from public.scores
    where day::text = p_day and kind::text = p_kind and jsonb_typeof(answers::jsonb) = 'array'
  ), pins as (
    select s.player, a.idx as i, (a.v ->> 1)::numeric as dist
    from s, jsonb_array_elements(s.answers) with ordinality as a(v, idx)
    where jsonb_typeof(a.v) = 'array' and jsonb_typeof(a.v -> 1) = 'number'
  ), r as (
    select player, i, cume_dist() over (partition by i order by dist) as p from pins
  ), per as (
    select player, count(*) as n, sum(ln(p)) as lnp from r group by player
  ), hit as (
    select player, lnp from per
    where n >= 4 and lnp < -30 and (select count(distinct player) from pins) >= 300
  ), ins as (
    insert into public.score_flags (day, kind, player, lnp)
    select p_day, p_kind, player, lnp from hit
    on conflict (day, kind, player) do update set lnp = excluded.lnp
    returning 1
  )
  select count(*)::int from ins;
$$;

-- the players hidden for one day and mode (used by the leaderboard)
create or replace function public.flagged_players(p_day text, p_kind text) returns setof text
language sql stable security definer set search_path = public as $$
  select player from public.score_flags where day = p_day and kind = p_kind;
$$;
grant execute on function public.flagged_players(text, text) to anon, authenticated;

-- the leaderboard summary, unchanged except that flagged players are left out for everyone but themselves
create or replace function public.board(p_day text, p_kind text, p_player text default null)
returns jsonb
language sql
stable
as $$
with s as (
  select t.player::text as player, t.name, t.m, t.score,
         case when jsonb_typeof(to_jsonb(t) -> 'answers') = 'string' then (to_jsonb(t) ->> 'answers')::jsonb
              else to_jsonb(t) -> 'answers' end as answers
  from public.scores t
  where t.day::text = p_day and t.kind::text = p_kind
    and (t.player::text = p_player or t.player::text not in (select public.flagged_players(p_day, p_kind)))
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
           (array_agg(dist order by dist) filter (where dist is not null))[(count(dist) / 2)::int + 1] as med
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

-- Optional, if you enable the pg_cron extension (Database > Extensions): check every five minutes, no manual runs needed.
-- select cron.schedule('flag-scores', '*/5 * * * *', $$ select public.flag_scores((now() at time zone 'America/New_York')::date::text, 'call') $$);
