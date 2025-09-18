-- Single minimal table for Xero user connections
CREATE TABLE IF NOT EXISTS xero_users (
  id SERIAL PRIMARY KEY,
  tenant_id TEXT UNIQUE NOT NULL,
  tenant_name TEXT,
  access_token TEXT NOT NULL,
  refresh_token TEXT,
  expires_at TIMESTAMPTZ NOT NULL,
  connected_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Table for storing OAuth states
CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY,
  created_at TIMESTAMPTZ DEFAULT NOW()
);
