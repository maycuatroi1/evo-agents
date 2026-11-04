/**
 * The size of one part of an archive's multipart upload, as the hub cuts it (`evo_agents.hub.blobs.PART_SIZE`); never
 * larger, since the hub's largest archive (`MAX_KG_UPLOAD_MAX_BYTES`) is 10,000 parts of it. Each part goes through the
 * web's /v1 rewrite, whose body limit next.config.ts sets from this.
 */
export const UPLOAD_PART_BYTES = 64 * 1024 * 1024;
