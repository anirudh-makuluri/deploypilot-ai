-- Create the analysis_cache table
create table public.analysis_cache (
  id uuid default gen_random_uuid() primary key,
  repo_url text not null,
  commit_sha text not null,
  result jsonb not null,
  created_at timestamp with time zone default timezone('utc'::text, now()) not null,
  unique(repo_url, commit_sha)
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
