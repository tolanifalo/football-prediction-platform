CREATE TABLE "raw_payload_bodies" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "raw_payload_bodies_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"hash_algo" text DEFAULT 'sha256' NOT NULL,
	"body_hash" text NOT NULL,
	"body" "bytea",
	"byte_size" integer NOT NULL,
	"first_seen_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "raw_payload_bodies_content_key" UNIQUE("hash_algo","body_hash"),
	CONSTRAINT "raw_payload_bodies_hash_algo_check" CHECK ("raw_payload_bodies"."hash_algo" IN ('sha256')),
	CONSTRAINT "raw_payload_bodies_body_hash_check" CHECK ("raw_payload_bodies"."body_hash" ~ '^[0-9a-f]{64}$'),
	CONSTRAINT "raw_payload_bodies_byte_size_check" CHECK ("raw_payload_bodies"."byte_size" >= 0),
	CONSTRAINT "raw_payload_bodies_body_length_check" CHECK ("raw_payload_bodies"."body" IS NULL OR octet_length("raw_payload_bodies"."body") = "raw_payload_bodies"."byte_size")
);
