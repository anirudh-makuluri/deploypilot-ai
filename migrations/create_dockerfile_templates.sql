CREATE TABLE IF NOT EXISTS dockerfile_templates (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    description TEXT DEFAULT '',
    match_stack_tokens TEXT[] DEFAULT '{}',
    match_signals JSONB DEFAULT '{}',
    priority INTEGER DEFAULT 0,
    template_content TEXT NOT NULL,
    variables JSONB DEFAULT '{}',
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_dockerfile_templates_active 
    ON dockerfile_templates (is_active, priority DESC);

CREATE OR REPLACE FUNCTION update_dockerfile_templates_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ language 'plpgsql';

DROP TRIGGER IF EXISTS trigger_update_dockerfile_templates_updated_at ON dockerfile_templates;
CREATE TRIGGER trigger_update_dockerfile_templates_updated_at
    BEFORE UPDATE ON dockerfile_templates
    FOR EACH ROW
    EXECUTE FUNCTION update_dockerfile_templates_updated_at();
