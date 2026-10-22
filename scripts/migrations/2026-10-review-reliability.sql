-- Track when a review's state last changed, enabling stale-job recovery.
ALTER TABLE public.pr_review_records
ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS pr_review_records_status_updated_idx
ON public.pr_review_records (status, updated_at);

-- Do not silently discard existing pending human-review records.
-- Abort this migration if duplicate pending requests already exist.
DO $migration$
BEGIN
    IF EXISTS (
        SELECT review_id
        FROM public.hitl_reviews
        WHERE status = 'pending'
        GROUP BY review_id
        HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION
            'Multiple pending human-review requests exist for a review. '
            'Inspect them before applying the unique index.';
    END IF;
END
$migration$;

-- A review may have only one pending approval request at a time.
CREATE UNIQUE INDEX IF NOT EXISTS hitl_reviews_one_pending_per_review_idx
ON public.hitl_reviews (review_id)
WHERE status = 'pending';
