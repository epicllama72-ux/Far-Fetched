-- One-time conversion of every stored score to the new point scale (a perfect day = 1,000 points, 1 point = 10 m).
-- Old scale: a perfect Daily Dig / Politics day = 4,780 points, Fill the Blank = 5,000. Depth `m` becomes new score x 10 (metres).
-- Run it ONCE, right after the new game goes live. A marker table makes a second run do nothing.
create table if not exists scale_migrations (name text primary key, ran_at timestamptz default now());
do $$
begin
  if exists (select 1 from scale_migrations where name = 'points_1000') then
    raise notice 'already converted: nothing to do';
    return;
  end if;
  update scores
     set m     = round(round(score::numeric * case when kind::text = 'blank' then 0.2 else 1000.0/4780 end) * 10),
         score = round(score::numeric * case when kind::text = 'blank' then 0.2 else 1000.0/4780 end);
  insert into scale_migrations(name) values ('points_1000');
end $$;
