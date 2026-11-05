-- Player feedback from the "Letters to the Editor" box after a Daily Dig.
-- Run once in the Supabase SQL editor. The public (anon) key can only ADD feedback: it cannot read, change or delete any.
-- Read it in the dashboard: Table Editor > feedback.
create table if not exists feedback (
  id bigint generated always as identity primary key,
  created_at timestamptz not null default now(),
  player text not null check (char_length(player) between 8 and 64),
  day text not null check (day ~ '^\d{4}-\d{2}-\d{2}$'),
  kind text not null check (kind in ('call','blank','pol')),
  score int check (score between -2000 and 6000),
  tags text[] not null default '{}' check (tags <@ array['loved','too_easy','too_hard','again','okay','not_for_me']::text[] and cardinality(tags) <= 6),
  message text check (message is null or char_length(message) <= 1000),
  unique (player, day, kind)
);
alter table feedback enable row level security;
revoke all on feedback from anon, authenticated;
grant insert (player, day, kind, score, tags, message) on feedback to anon;
drop policy if exists "anyone can send feedback" on feedback;
create policy "anyone can send feedback" on feedback for insert to anon
  with check (cardinality(tags) > 0 or coalesce(char_length(trim(message)),0) > 0);
notify pgrst, 'reload schema';

-- A quick summary of the ticks (run on its own any time):
-- select t as tick, count(*) from feedback, unnest(tags) t group by t order by 2 desc;
-- The latest written comments:
-- select created_at, day, kind, score, tags, message from feedback where message is not null order by created_at desc limit 50;
