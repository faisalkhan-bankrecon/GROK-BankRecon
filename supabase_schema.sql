-- Concord multi-user schema (run in Supabase SQL Editor)

-- Workspace / period meta per user
CREATE TABLE IF NOT EXISTS workspaces (
  user_id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
  period JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Transactions scoped to user
CREATE TABLE IF NOT EXISTS transactions (
  id TEXT PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  side TEXT NOT NULL CHECK (side IN ('bank', 'book')),
  date DATE NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  amount_cents INTEGER NOT NULL,
  reference TEXT,
  balance_cents INTEGER,
  match_id TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'proposed', 'matched')),
  source_file TEXT,
  raw TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_user_side ON transactions(user_id, side);

-- Matches scoped to user
CREATE TABLE IF NOT EXISTS matches (
  id TEXT PRIMARY KEY,
  user_id UUID NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
  bank_ids TEXT[] NOT NULL DEFAULT '{}',
  book_ids TEXT[] NOT NULL DEFAULT '{}',
  status TEXT NOT NULL CHECK (status IN ('proposed', 'matched')),
  method TEXT NOT NULL DEFAULT 'auto',
  confidence INTEGER NOT NULL DEFAULT 0,
  reasons TEXT[] NOT NULL DEFAULT '{}',
  bank_total_cents INTEGER NOT NULL DEFAULT 0,
  book_total_cents INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_matches_user ON matches(user_id);

-- Row Level Security (extra safety; API still scopes by user_id)
ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE matches ENABLE ROW LEVEL SECURITY;

-- Service role / backend uses service key; policies for authenticated users via anon key if needed
CREATE POLICY "users manage own workspace"
  ON workspaces FOR ALL
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "users manage own transactions"
  ON transactions FOR ALL
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

CREATE POLICY "users manage own matches"
  ON matches FOR ALL
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);
