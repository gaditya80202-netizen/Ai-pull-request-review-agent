-- Ensure exactly one daily aggregate row per UTC date.
CREATE UNIQUE INDEX IF NOT EXISTS daily_budget_usage_budget_day_uidx
ON public.daily_budget_usage (budget_day);

-- One reservation record per review, so retries cannot reserve twice.
CREATE TABLE IF NOT EXISTS public.llm_budget_reservations (
    review_id UUID PRIMARY KEY
        REFERENCES public.pr_review_records(id) ON DELETE CASCADE,
    budget_day DATE NOT NULL,
    reserved_usd NUMERIC(14,8) NOT NULL
        CHECK (reserved_usd >= 0),
    actual_cost_usd NUMERIC(14,8) NOT NULL DEFAULT 0
        CHECK (actual_cost_usd >= 0),
    status TEXT NOT NULL
        CHECK (status IN ('reserved', 'settled', 'released')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS llm_budget_reservations_day_status_idx
ON public.llm_budget_reservations (budget_day, status);
