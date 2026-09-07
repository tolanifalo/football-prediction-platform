CREATE TABLE "data_sources" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"slug" text NOT NULL,
	"display_name" text NOT NULL,
	"kinds" text[] DEFAULT '{}' NOT NULL,
	"base_url" text,
	"rate_limit_per_min" integer,
	"is_active" boolean DEFAULT true NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "data_sources_slug_unique" UNIQUE("slug")
);
--> statement-breakpoint
CREATE TABLE "job_runs" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "job_runs_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"job_name" text NOT NULL,
	"scope_key" text DEFAULT '' NOT NULL,
	"run_date" date NOT NULL,
	"attempt" integer DEFAULT 1 NOT NULL,
	"source_id" uuid,
	"adapter_version" text,
	"params" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"status" text NOT NULL,
	"started_at" timestamp with time zone DEFAULT now() NOT NULL,
	"finished_at" timestamp with time zone,
	"stats" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"error" text,
	CONSTRAINT "job_runs_identity_key" UNIQUE("job_name","scope_key","run_date","attempt"),
	CONSTRAINT "job_runs_status_check" CHECK ("job_runs"."status" IN ('running', 'ok', 'partial', 'failed')),
	CONSTRAINT "job_runs_attempt_check" CHECK ("job_runs"."attempt" >= 1),
	CONSTRAINT "job_runs_finished_after_started_check" CHECK ("job_runs"."finished_at" IS NULL OR "job_runs"."finished_at" >= "job_runs"."started_at")
);
--> statement-breakpoint
ALTER TABLE "job_runs" ADD CONSTRAINT "job_runs_source_id_data_sources_id_fk" FOREIGN KEY ("source_id") REFERENCES "public"."data_sources"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "job_runs_job_name_run_date_idx" ON "job_runs" USING btree ("job_name","run_date" DESC NULLS LAST);--> statement-breakpoint
CREATE INDEX "job_runs_running_idx" ON "job_runs" USING btree ("status") WHERE "job_runs"."status" = 'running';