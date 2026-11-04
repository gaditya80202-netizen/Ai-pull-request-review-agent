-- Track GitHub publication attempts separately from the human decision.
-- This migration is additive and does not delete or rewrite existing reviews.
BEGIN;

ALTER TABLE public.pr_review_records
    ADD COLUMN IF NOT EXISTS publication_status TEXT NOT NULL DEFAULT 'not_attempted';

ALTER TABLE public.pr_review_records
    ADD COLUMN IF NOT EXISTS publication_event TEXT;

ALTER TABLE public.pr_review_records
    ADD COLUMN IF NOT EXISTS publication_attempted_at TIMESTAMPTZ;

ALTER TABLE public.pr_review_records
    ADD COLUMN IF NOT EXISTS publication_error TEXT;

CREATE INDEX IF NOT EXISTS pr_review_records_publication_status_idx
    ON public.pr_review_records (publication_status, updated_at);

COMMIT;
