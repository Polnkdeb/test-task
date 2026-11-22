
-- Индекс для поиска покупателей
CREATE INDEX IF NOT EXISTS ix_customers_gin
ON abstract_customer
USING GIN (amo_customers jsonb_path_ops);

-- Индекс для поиска сделок
CREATE INDEX IF NOT EXISTS ix_leads_gin
ON abstract_customer
USING GIN (amo_leads jsonb_path_ops);
