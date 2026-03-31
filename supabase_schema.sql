-- Create the analysis_cache table
create table public.analysis_cache (
  id uuid default gen_random_uuid() primary key,
  repo_url text not null,
  commit_sha text not null,
  package_path text not null default '.',
  service_name text,
  result jsonb not null,
  created_at timestamp with time zone default timezone('utc'::text, now()) not null,
  unique(repo_url, commit_sha, package_path, service_name)
);

-- Setup Row Level Security (RLS) if needed
alter table public.analysis_cache enable row level security;

-- Allow service role to do everything
create policy "Allow service role full access to analysis_cache"
  on public.analysis_cache
  as permissive
  for all
  to service_role
  using (true)
  with check (true);

-- Create example_bank table for grounded generation references
create table if not exists public.example_bank (
  id uuid default gen_random_uuid() primary key,
  source_repo text not null,
  source_path text not null,
  artifact_type text not null check (artifact_type in ('dockerfile', 'compose')),
  stack_tags text[] not null default '{}',
  license text,
  quality_score double precision not null default 0.5,
  snippet text not null,
  content text not null,
  is_active boolean not null default true,
  created_at timestamp with time zone default timezone('utc'::text, now()) not null,
  updated_at timestamp with time zone default timezone('utc'::text, now()) not null,
  unique(source_repo, source_path)
);

create index if not exists idx_example_bank_artifact_active
  on public.example_bank (artifact_type, is_active);

create index if not exists idx_example_bank_quality
  on public.example_bank (quality_score desc);

create index if not exists idx_example_bank_tags_gin
  on public.example_bank using gin (stack_tags);

alter table public.example_bank enable row level security;

create policy "Allow service role full access to example_bank"
  on public.example_bank
  as permissive
  for all
  to service_role
  using (true)
  with check (true);

-- Store benchmark artifacts (labels, quality reports, latest snapshots)
create table if not exists public.benchmark_artifacts (
  id uuid default gen_random_uuid() primary key,
  file_name text not null unique,
  artifact_type text not null,
  run_id text,
  generated_at timestamp with time zone,
  payload jsonb not null,
  created_at timestamp with time zone default timezone('utc'::text, now()) not null,
  updated_at timestamp with time zone default timezone('utc'::text, now()) not null
);

create index if not exists idx_benchmark_artifacts_type_generated
  on public.benchmark_artifacts (artifact_type, generated_at desc);

alter table public.benchmark_artifacts enable row level security;

create policy "Allow service role full access to benchmark_artifacts"
  on public.benchmark_artifacts
  as permissive
  for all
  to service_role
  using (true)
  with check (true);
