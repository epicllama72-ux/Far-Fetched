-- Private groups: a small leaderboard for friends, family or a team, joined with a link (?g=CODE).
-- The tables are closed to the public key; everything goes through the functions below. Safe to run more than once.

create table if not exists groups (
  code       text primary key,
  name       text not null check (char_length(name) between 1 and 40),
  owner      text not null,
  created_at timestamptz not null default now()
);
create table if not exists group_members (
  code      text not null references groups(code) on delete cascade,
  player    text not null,
  joined_at timestamptz not null default now(),
  primary key (code, player)
);
create index if not exists group_members_player on group_members(player);
alter table groups enable row level security;
alter table group_members enable row level security;

-- make a group and join it; a player may own at most 10
create or replace function group_create(p_player text, p_name text) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_code text; v_name text := left(btrim(regexp_replace(coalesce(p_name,''), '[<>]', '', 'g')), 40);
begin
  if coalesce(p_player,'') = '' or v_name = '' then return null; end if;
  if (select count(*) from groups where owner = p_player) >= 10 then return jsonb_build_object('error','too_many'); end if;
  loop
    v_code := upper(substr(md5(random()::text || clock_timestamp()::text), 1, 6));
    exit when not exists (select 1 from groups where code = v_code);
  end loop;
  insert into groups(code, name, owner) values (v_code, v_name, p_player);
  insert into group_members(code, player) values (v_code, p_player);
  return jsonb_build_object('code', v_code, 'name', v_name, 'members', 1);
end $$;

-- the group's name and size, for the "Join this group?" prompt
create or replace function group_info(p_code text) returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object('code', g.code, 'name', g.name, 'members', (select count(*) from group_members m where m.code = g.code))
  from groups g where g.code = upper(btrim(p_code));
$$;

-- join (at most 200 members a group)
create or replace function group_join(p_player text, p_code text) returns jsonb
language plpgsql security definer set search_path = public as $$
declare v_code text := upper(btrim(p_code));
begin
  if coalesce(p_player,'') = '' or not exists (select 1 from groups where code = v_code) then return null; end if;
  if not exists (select 1 from group_members where code = v_code and player = p_player)
     and (select count(*) from group_members where code = v_code) >= 200 then return jsonb_build_object('error','full'); end if;
  insert into group_members(code, player) values (v_code, p_player) on conflict do nothing;
  return group_info(v_code);
end $$;

create or replace function group_leave(p_player text, p_code text) returns void
language sql security definer set search_path = public as $$
  delete from group_members where code = upper(btrim(p_code)) and player = p_player;
$$;

-- the groups a player is in
create or replace function my_groups(p_player text) returns jsonb
language sql stable security definer set search_path = public as $$
  select coalesce(jsonb_agg(jsonb_build_object('code', g.code, 'name', g.name,
           'members', (select count(*) from group_members x where x.code = g.code)) order by m.joined_at), '[]'::jsonb)
  from group_members m join groups g on g.code = m.code where m.player = p_player;
$$;

-- today's board for one group: every member who has played this day and mode, deepest first
create or replace function group_board(p_code text, p_day text, p_kind text) returns jsonb
language sql stable security definer set search_path = public as $$
  select jsonb_build_object(
    'members', (select count(*) from group_members where code = upper(btrim(p_code))),
    'rows', coalesce((select jsonb_agg(jsonb_build_object('player', s.player::text, 'name', s.name, 'm', s.m, 'score', s.score) order by s.m desc, s.score desc)
             from scores s join group_members gm on gm.player = s.player::text and gm.code = upper(btrim(p_code))
             where s.day::text = p_day and s.kind::text = p_kind), '[]'::jsonb));
$$;

grant execute on function group_create(text,text), group_info(text), group_join(text,text), group_leave(text,text),
  my_groups(text), group_board(text,text,text) to anon, authenticated;

-- one-time tidy-up: give every player's older scores the name on their latest score
-- (renames made before the rename permission existed never reached old rows)
update scores s set name = l.name
  from (select distinct on (player) player, name from scores order by player, created_at desc) l
 where s.player = l.player and s.name is distinct from l.name;
