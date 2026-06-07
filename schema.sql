-- Uruchom to w Supabase SQL Editor

-- Tabela kont Steam (możliwe że już istnieje, sprawdź kolumny)
create table if not exists steam_accounts (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  login text not null,
  password text not null,
  price numeric(10,2) not null default 0,
  sold boolean not null default false,
  created_at timestamptz default now()
);

-- Tabela zamówień
create table if not exists orders (
  id uuid primary key default gen_random_uuid(),
  product_id uuid references steam_accounts(id),
  discord_id text not null,
  discord_username text,
  status text not null default 'pending',
  channel_id text,
  created_at timestamptz default now()
);

-- Przykładowe konto (usuń po testach)
-- insert into steam_accounts (name, login, password, price) values ('Konto Steam #1', 'login123', 'haslo123', 49.99);
